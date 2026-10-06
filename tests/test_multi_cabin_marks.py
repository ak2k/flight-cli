# pyright: reportPrivateUsage=false
"""A Google Flights multi-cabin search reads each cabin's Cheapest tab and
marks its separate-ticket and self-transfer rows as the one-cabin table does.

Google is faked below `search_with_ids`, at `_one_call_laddered`, answering per
(cabin, Cheapest tab): the default board is the JFK-LAX capture, and the
Cheapest tab the same capture with row 5 sold as a self transfer at USD50, so
it is the cheapest row of every cabin. No test reaches Google, Matrix or
Chrome."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pytest
    from click.testing import Result

    from flight_cli.domain import Cabin, Leg, SearchOptions

_LAX = "ds1_jfk_lax_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_MARKED = 5


def _dep() -> date:
    return date.today() + timedelta(days=45)


def _board(*, marked: bool) -> gfid.Board[gfid.GFlightWithId]:
    """The LAX capture, with row `_MARKED` sold as a self transfer at USD50
    when `marked`."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    if marked:
        row = gfid._rows_from_ds1(payload).rows[_MARKED]
        row[7] = [1]
        row[1][0][1] = 50
    html = _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_dep().isoformat())
    )
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


class _Google:
    """`_one_call_laddered`, counting each GET by (cabin, Cheapest tab)."""

    def __init__(self, *, marked: bool = True) -> None:
        self.marked = marked
        self.gets: Counter[tuple[str, bool]] = Counter()
        self._lock = threading.Lock()

    def __call__(
        self, filters: Any, _transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        _ = currency
        with self._lock:
            self.gets[(filters.seat_type.name, cheapest)] += 1
        return _board(marked=cheapest and self.marked)


def _search(
    monkeypatch: pytest.MonkeyPatch, google: _Google, *extra: str, cash_only: bool = True
) -> Result:
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    args = [
        "search",
        *(["--cash-only"] if cash_only else []),
        "--no-google-url",
        "--no-matrix-url",
        "JFK",
        "LAX",
        "--dep",
        _dep().isoformat(),
        "--cabin",
        "economy,business",
        "--backend",
        "gflight",
        "-n",
        "5",
        *extra,
    ]
    return CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})


def _price_cells(stdout: str) -> list[list[str]]:
    """Each numbered row's two price cells, economy then business."""
    return [
        [c.strip() for c in ln.strip().strip("│").split("│")][-2:]
        for ln in stdout.splitlines()
        if ln.strip().startswith("│") and ln.strip().strip("│").split("│")[0].strip().isdigit()
    ]


_KEY = "‡ self transfer: separate tickets, and you collect and recheck bags between flights."


def test_each_cabin_reads_its_cheapest_tab_and_marks_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which asks each cabin's default board alone (2 GETs)
    and shows no marked row."""
    google = _Google()
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert google.gets == Counter(
        {("ECONOMY", False): 1, ("ECONOMY", True): 1, ("BUSINESS", False): 1, ("BUSINESS", True): 1}
    )
    cells = _price_cells(result.stdout)
    assert cells[0] == ["50.00 ‡", "50.00 ‡"]
    assert not any(c.endswith(("†", "‡")) for row in cells[1:] for c in row)
    assert " ".join(result.stdout.split()).count(_KEY) == 1


def test_opting_out_prints_the_unmarked_table_and_counts_what_each_cabin_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table is the one a Cheapest tab with no mark leaves, which is the
    base's; each cabin says what it hid. Red at the base, which says nothing."""
    plain = _search(monkeypatch, _Google(marked=False))
    hidden = _search(monkeypatch, _Google(), "--no-separate-tickets")
    assert hidden.exit_code == plain.exit_code == 0, hidden.output
    assert hidden.stdout == plain.stdout
    said = " ".join(hidden.stderr.split())
    for cab in ("COACH", "BUSINESS"):
        assert (
            f"Google Flights {cab}: 1 itinerary on separate tickets hidden (--no-separate-tickets)."
            in said
        )


def test_json_carries_each_cabins_marked_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base, whose boards hold no marked row."""
    result = _search(monkeypatch, _Google(), "--format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    for cab in ("COACH", "BUSINESS"):
        marked = [r for r in doc[cab] if r["separate_tickets"]]
        assert [r["price"] for r in marked] == [50]


def test_no_marked_row_reaches_the_award_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Awards are matched and valued against one-ticket rows; the marked row
    shown in the table is counted once. Red at the base, which shows no marked
    row and so says nothing."""
    matched: list[SearchResult] = []
    cash: list[dict[int, dict[str, float]]] = []

    def _awards(res: SearchResult, **kw: Any) -> None:
        matched.append(res)
        cash.append(kw["cash_per_cabin"])

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    result = _search(monkeypatch, _Google(), cash_only=False)
    assert result.exit_code == 0, result.output
    (res,) = matched
    assert res.solutions
    assert all(it.ticketing is None for it in res.solutions)
    assert set(cash[0]) <= {id(it) for it in res.solutions}
    said = " ".join(result.stderr.split())
    assert (
        said.count(
            "Awards are matched to one-ticket rows; 1 row on separate tickets is not in the "
            "award table."
        )
        == 1
    )
    assert "50.00 ‡" in result.stdout


def test_a_marked_row_takes_no_row_from_the_award_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """At `-n 1` the table's one row is the marked one; the award matcher still
    gets the cheapest one-ticket row, priced as it is with the marked rows
    hidden. Red at the base, which shows no marked row."""
    pools: list[list[tuple[str, dict[str, float]]]] = []

    def _awards(res: SearchResult, **kw: Any) -> None:
        cash: dict[int, dict[str, float]] = kw["cash_per_cabin"]
        pools.append([(it.price or "", cash.get(id(it), {})) for it in res.solutions])

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    shown = _search(monkeypatch, _Google(), "-n", "1", cash_only=False)
    hidden = _search(monkeypatch, _Google(), "-n", "1", "--no-separate-tickets", cash_only=False)
    assert shown.exit_code == hidden.exit_code == 0, shown.output
    assert "50.00 ‡" in shown.stdout
    with_marks, without = pools
    assert len(with_marks) == 1
    assert with_marks == without
    assert (
        " ".join(shown.stderr.split()).count(
            "Awards are matched to one-ticket rows; 1 row on separate tickets is not in the "
            "award table."
        )
        == 1
    )


def test_awards_only_reads_no_cheapest_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the base."""

    def _awards(*_a: object, **_kw: object) -> None:
        return None

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    google = _Google()
    result = _search(monkeypatch, google, "--awards-only", cash_only=False)
    assert result.exit_code == 0, result.output
    assert google.gets
    assert not any(cheapest for _, cheapest in google.gets)


def test_a_matrix_multi_cabin_table_is_unmarked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix sells one ticket, so its rows carry no mark. Green at the base."""
    dep = _dep()

    def _answer(price: str) -> SearchResult:
        solution = {
            "displayTotal": price,
            "itinerary": {
                "slices": [
                    {
                        "flights": ["AA100"],
                        "departure": f"{dep}T09:00",
                        "arrival": f"{dep}T12:00",
                        "origin": {"code": "JFK"},
                        "destination": {"code": "LAX"},
                    }
                ]
            },
        }
        return SearchResult.from_api({"solutionList": {"solutions": [solution]}})

    def _matrix(
        *, legs: tuple[Leg, ...], opts: SearchOptions, cabins: tuple[Cabin, ...], **_kw: object
    ) -> dict[Cabin, SearchResult]:
        _ = legs, opts
        return dict(zip(cabins, (_answer("USD600.00"), _answer("USD3000.00")), strict=True))

    google = _Google()
    monkeypatch.setattr(cli, "_run_matrix_multi", _matrix)
    result = _search(monkeypatch, google, "--backend", "matrix")
    assert result.exit_code == 0, result.output
    assert google.gets == Counter()
    assert _price_cells(result.stdout) == [["600.00", "3000.00"]]
    assert "separate tickets" not in result.stdout
