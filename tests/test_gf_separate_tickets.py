# pyright: reportPrivateUsage=false, reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/test_enrich.py.
"""Itineraries Google sells as separate tickets, read off its Cheapest tab.

The two FLL-LGA captures are the same round-trip query's outbound boards:
`ds1_fll_lga_rt_best.json` is the default board (58 rows, none marked) and
`ds1_fll_lga_rt_cheapest.json` the Cheapest tab (86 rows: 28 self transfers,
5 separate tickets booked together, 53 one ticket).
"""

from __future__ import annotations

import contextlib
import json
import re
import urllib.parse
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gf_booking, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._cross_check import Answers, RowCheck, cross_check
from flight_cli._enrich import MergedRow, merge_results
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin, Leg, SearchOptions
from flight_cli.links import build_search_tfs, google_flights_search_page_url
from flight_cli.models import Itinerary, ItineraryDetails, ItineraryExt, SearchResult, Slice
from flight_cli.pp.gflight_adapter import fli_results_to_search_result

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_BEST = "ds1_fll_lga_rt_best.json"
_CHEAPEST = "ds1_fll_lga_rt_cheapest.json"
_LAX = "ds1_jfk_lax_tfu.json"
_LHR = "ds1_jfk_lhr_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_CHEAPEST_TFU = "tfu=EggIABABIAIoASIA"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_FLL_LGA = [
    *("FLL", "LGA", "--dep", _DEP.isoformat(), "--return", _RET.isoformat()),
    *("--backend", "gflight", "--fast", "-n", "1000"),
]
_THROTTLE_PAGE = "<html>Our systems have detected unusual traffic</html>"
_KEY = (
    "† separate tickets: Google sells this trip as more than one booking. "
    "‡ self transfer: separate tickets, and you collect and recheck bags between flights."
)
_OUTBOUND_ONLY = (
    "A round trip on separate tickets lists its outbound only: Google prices the whole "
    "trip but serves no return for it."
)


def _served(name: str, *, origin: str | None = None, destination: str | None = None) -> str:
    day = _RET if origin == "LGA" else _DEP
    return _page(
        _answering(_ds1(name), origin=origin, destination=destination, date=day.isoformat())
    )


def _parsed(name: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(_ds1(name)), _URL, 200))


def _fll_lga_pages() -> list[str]:
    """The default board, a return board for each of the ten pins, then the
    Cheapest tab."""
    ret = _served("ds1_return_leg_pinned.json", origin="LGA", destination="FLL")
    return [_served(_BEST), *[ret] * gfid.pinned_fanout(1000), _served(_CHEAPEST)]


def _round_trip() -> tuple[Leg, ...]:
    return (Leg.of("FLL", "LGA", _DEP), Leg.of("LGA", "FLL", _RET))


def _lax_with_marked_twin(index: int, *, price: int | None = None) -> str:
    """The LAX capture with row `index` sold as a self transfer, at `price`
    when given: the Cheapest twin of the unmarked board."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    row = gfid._rows_from_ds1(payload).rows[index]
    row[7] = [1]
    if price is not None:
        row[1][0][1] = price
    return _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )


def _document(capsys: pytest.CaptureFixture[str], **kw: Any) -> list[Any]:
    cli._run_gflight_path(opts=SearchOptions(cabin=Cabin.COACH), top_n=1000, json_out=True, **kw)
    return json.loads(capsys.readouterr().out)


def _members(doc: list[Any]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = [r if isinstance(r, list) else [r] for r in doc]
    return [m for r in rows for m in r]


# ──────────────────────────────── the request ─────────────────────────────


def test_the_cheapest_tab_url_differs_from_the_board_url_only_in_tfu() -> None:
    from flight_cli.fli_bridge import to_fli_filter

    filters = to_fli_filter(
        cli.SpecificDateSearch(legs=_round_trip(), options=SearchOptions(cabin=Cabin.COACH))
    )
    tfs = build_search_tfs(filters)
    base = urllib.parse.urlsplit(google_flights_search_page_url(tfs))
    cheapest = urllib.parse.urlsplit(google_flights_search_page_url(tfs, cheapest=True))
    base_q = urllib.parse.parse_qs(base.query)
    cheap_q = urllib.parse.parse_qs(cheapest.query)
    assert base_q.pop("tfu") == ["EgQIABABIgA"]
    assert cheap_q.pop("tfu") == ["EggIABABIAIoASIA"]
    assert base_q == cheap_q
    assert base._replace(query="") == cheapest._replace(query="")
    assert google_flights_search_page_url(tfs, cheapest=False) == google_flights_search_page_url(
        tfs
    )
    assert gfid.search_page_url(filters, cheapest=True).endswith(f"&{_CHEAPEST_TFU}")


# ──────────────────────────────── the decode ──────────────────────────────


@pytest.mark.parametrize(
    ("name", "self_transfers", "separate", "one_ticket"),
    [(_CHEAPEST, 28, 5, 53), (_BEST, 0, 0, 58)],
)
def test_row_seven_says_how_google_sells_each_row(
    name: str, self_transfers: int, separate: int, one_ticket: int
) -> None:
    board = _parsed(name)
    kinds = [r.ticketing for r in board]
    assert kinds.count("self_transfer") == self_transfers
    assert kinds.count("separate_tickets") == separate
    assert kinds.count(None) == one_ticket
    assert all(
        r.flight.self_transfer is (r.ticketing == "self_transfer") for r in board
    )  # never None: every row of both captures states its ticketing


@pytest.mark.parametrize(
    ("slot", "ticketing", "self_transfer"),
    [
        ([1], "self_transfer", True),
        ([2], "separate_tickets", False),
        ([2, 1], "self_transfer", True),
        ([], None, False),
        ([5], None, False),
        (None, None, None),
        (7, None, None),
    ],
)
def test_a_row_seven_slot_decodes_or_says_nothing(
    slot: Any, ticketing: str | None, self_transfer: bool | None
) -> None:
    raw: list[Any] = json.loads(json.dumps(gfid._rows_from_ds1(json.loads(_ds1(_BEST))).rows[0]))
    raw[7] = slot
    row = gfid._parse_flight_with_id(raw)
    assert row.ticketing == ticketing
    assert row.flight.self_transfer is self_transfer
    del raw[7:]
    short = gfid._parse_flight_with_id(raw)
    assert (short.ticketing, short.flight.self_transfer) == (None, None)


# ───────────────────────────── fetch and merge ────────────────────────────


def test_a_round_trip_adds_each_separate_ticket_outbound_as_a_row_of_its_own(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The base combinations as the base builds them, then 33 one-member rows
    at Google's round-trip total, for one more GET of the Cheapest tab."""
    base_fake = gf_session(*_fll_lga_pages())
    base = _document(capsys, legs=_round_trip())
    fake = gf_session(*_fll_lga_pages())
    doc = _document(capsys, legs=_round_trip(), separate_tickets="show")
    assert len(fake.gets) == len(base_fake.gets) + 1 == 12
    assert fake.gets[:-1] == base_fake.gets
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert [r for r in doc if len(r) == 2] == base
    alone = [r for r in doc if len(r) == 1]
    assert len(alone) == 33
    assert all(m["separate_tickets"] is True for (m,) in alone)
    totals = {r.flight_id: r.flight.price for r in _parsed(_CHEAPEST) if r.ticketing}
    assert sorted((m["flight_id"], m["price"]) for (m,) in alone) == sorted(totals.items())


def test_a_one_way_adds_the_marked_row_of_the_cheapest_tab(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Its flights are on the base board too, one ticket at the same fare; it
    is still added, as a different booking."""
    legs = (Leg.of("JFK", "LAX", _DEP),)
    gf_session(_served(_LAX))
    base = _document(capsys, legs=legs)
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    doc = _document(capsys, legs=legs, separate_tickets="show")
    assert len(fake.gets) == 2
    marked = [r for r in doc if r["separate_tickets"]]
    assert [(r["flight_id"], r["self_transfer"]) for r in marked] == [("aR5Sef", True)]
    assert [r for r in doc if not r["separate_tickets"]] == base
    assert len(doc) == len(base) + 1


def test_a_refused_cheapest_tab_leaves_the_base_answer_and_says_why(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    legs = (Leg.of("JFK", "LAX", _DEP),)
    gf_session(_served(_LAX))
    base = _document(capsys, legs=legs)
    gf_session(_served(_LAX), _THROTTLE_PAGE)
    cli._run_gflight_path(
        legs=legs,
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=True,
        separate_tickets="show",
    )
    out, err = capsys.readouterr()
    assert json.loads(out) == base
    said = " ".join(err.split())
    assert said.count("Itineraries on separate tickets not read") == 1
    assert "Itineraries on separate tickets not read: Google Flights rate-limited." in said


def test_pinning_that_stopped_on_a_wall_skips_the_cheapest_tab(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The second pin meets the throttle; the Cheapest tab would meet it too."""
    pages = _fll_lga_pages()
    fake = gf_session(pages[0], pages[1], _THROTTLE_PAGE)
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=True,
        separate_tickets="show",
    )
    out, err = capsys.readouterr()
    assert all(len(r) == 2 for r in json.loads(out))
    assert not any(_CHEAPEST_TFU in url for url in fake.gets)
    assert "Itineraries on separate tickets not read: Google Flights rate-limited." in " ".join(
        err.split()
    )


@pytest.mark.parametrize("flag", ["run_pp", "sellers", "verify"])
def test_a_path_that_acts_on_a_row_still_reads_the_cheapest_tab(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Awards, `--sellers` and `--verify` each skip a separate-ticket row with
    a reason, so the table beside them still shows it."""
    asked: list[Any] = []

    def _gf(*_a: Any, separate_tickets: str = "off", **_kw: Any) -> gfid.Board[Any]:
        asked.append(separate_tickets)
        return gfid.Board()

    monkeypatch.setattr(cli, "_gflight_results", _gf)

    def _no_awards(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    # An empty board leaves `--sellers` and `--verify` no row to open: they exit.
    with contextlib.suppress(typer.Exit):
        cli._run_gflight_path(
            legs=(Leg.of("JFK", "LAX", _DEP),),
            opts=SearchOptions(cabin=Cabin.COACH),
            top_n=10,
            json_out=False,
            separate_tickets="show",
            run_pp=flag == "run_pp",
            sellers=flag == "sellers",
            verify=flag == "verify",
        )
    assert asked == ["show"]


def test_the_enriched_table_reads_the_cheapest_tab_and_takes_the_opt_out(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default search makes one GET more than it did, and
    `--no-separate-tickets` counts the row it hid there, once."""

    async def _no_matrix(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = None

    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--no-separate-tickets",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 2
    assert _CHEAPEST_TFU in fake.gets[-1]
    said = " ".join(result.stderr.split())
    assert said.count("on separate tickets hidden") == 1
    assert "Google Flights: 1 itinerary on separate tickets hidden (--no-separate-tickets)." in said
    assert "‡" not in result.stdout


def test_a_matrix_search_takes_the_opt_out_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "matrix",
            "--no-separate-tickets",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]


def test_the_deprecated_command_reads_no_cheapest_tab(gf_session: Callable[..., Any]) -> None:
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    result = CliRunner().invoke(cli.app, ["gflight", "JFK", "LAX", "--dep", _DEP.isoformat()])
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 1


# ──────────────────────────────── the table ───────────────────────────────

_ROW = re.compile(r"^│\s*(\d+[ab]?)\s*│([^│]*)│")


def _price_cells(stdout: str) -> dict[str, str]:
    return {m[1]: m[2].strip() for line in stdout.splitlines() if (m := _ROW.match(line))}


def test_the_table_marks_exactly_the_separate_ticket_rows_and_keys_them_once(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    cells = _price_cells(result.stdout)
    marked = {label: cell for label, cell in cells.items() if cell.endswith(("†", "‡"))}
    assert sum(cell.endswith(" ‡") for cell in marked.values()) == 28
    assert sum(cell.endswith(" †") for cell in marked.values()) == 5
    assert all(label.isdigit() for label in marked)
    assert len(cells) - len(marked) == 60  # 30 pairs, an `a` and a `b` row each
    said = " ".join(result.stdout.split())
    assert said.count(_KEY) == 1
    assert said.count(_OUTBOUND_ONLY) == 1


def test_a_board_with_no_separate_ticket_row_prints_what_the_base_prints(
    gf_session: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cheapest tab is read and holds no marked row, so nothing it carries
    reaches the table."""
    monkeypatch.setenv("COLUMNS", "200")
    fake = gf_session(_served(_LHR))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=10,
        json_out=False,
        separate_tickets="show",
    )
    shown = capsys.readouterr().out
    assert len(fake.gets) == 2
    gf_session(_served(_LHR))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=10,
        json_out=False,
    )
    assert shown == capsys.readouterr().out
    assert "†" not in shown
    assert "‡" not in shown


def test_a_link_never_pins_a_separate_ticket_row(gf_session: Callable[..., Any]) -> None:
    """Row 1 is the marked twin, priced below the board: a link pinned to it
    would open a one-ticket page, so the link pre-fills the search instead."""
    args = [
        *("search", "--cash-only", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()),
        *("--backend", "gflight", "--fast", "-n", "5"),
    ]
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    marked = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})
    assert marked.exit_code == 0, marked.output
    assert _price_cells(marked.stdout)["1"].endswith(" ‡")
    assert "Google Flights (tfs= structured):" in marked.stdout
    assert "pinned" not in marked.stdout
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    hidden = CliRunner().invoke(cli.app, [*args, "--no-separate-tickets"], env={"COLUMNS": "200"})
    assert hidden.exit_code == 0, hidden.output
    assert "Google Flights (itinerary #1 pinned):" in hidden.stdout


# ──────────────────────────────── the JSON ────────────────────────────────


def test_every_member_states_how_it_is_sold(gf_session: Callable[..., Any]) -> None:
    gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json"])
    assert result.exit_code == 0, result.output
    doc: list[Any] = json.loads(result.stdout)
    members = _members(doc)
    assert all(type(m["separate_tickets"]) is bool for m in members)
    assert sum(m["separate_tickets"] for m in members) == 33
    assert sum(m["self_transfer"] is True for m in members) == 28
    assert all(m["separate_tickets"] for m in members if m["self_transfer"])
    assert all(len(r) == 1 for r in doc if any(m["separate_tickets"] for m in r))


def test_a_row_that_states_nothing_is_null_in_the_document() -> None:
    raw: list[Any] = json.loads(json.dumps(gfid._rows_from_ds1(json.loads(_ds1(_BEST))).rows[0]))
    raw[7] = None
    (row,) = cli._gflight_json_document([gfid._parse_flight_with_id(raw)])
    assert row["separate_tickets"] is None
    assert row["self_transfer"] is None


# ─────────────────────────────── the opt-out ──────────────────────────────


def test_the_opt_out_prints_the_base_rows_and_counts_what_it_hid(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    gf_session(*_fll_lga_pages())
    base = _document(capsys, legs=_round_trip())
    fake = gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json", "--no-separate-tickets"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == base
    assert len(fake.gets) == 12
    said = " ".join(result.stderr.split())
    assert (
        "Google Flights: 33 itineraries on separate tickets hidden (--no-separate-tickets)." in said
    )


def test_the_opt_out_says_nothing_when_it_hid_nothing(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
            "--no-separate-tickets",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "separate tickets" not in result.stderr


# ─────────────────────────── the default search ───────────────────────────
# Google's table is painted first, then the merged table once Matrix answers.

_DEFAULT = [
    *_SEARCH,
    *("FLL", "LGA", "--dep", _DEP.isoformat(), "--return", _RET.isoformat(), "-n", "1000"),
]
_SELF_TRANSFER_REASON = (
    "Google sells this trip as a self transfer on separate tickets; Matrix prices one ticket"
)
_SEPARATE_REASON = "Google sells this trip as separate tickets; Matrix prices one ticket"


def _as_matrix(it: Itinerary, price: str) -> Itinerary:
    """Google row `it` as Matrix states it: its own price, a UTC offset on each
    landing, no per-flight dates and no leg detail."""
    assert it.itinerary is not None
    slices = [
        s.model_copy(update={"arrival": f"{s.arrival}+00:00", "segment_dates": [], "legs": []})
        for s in it.itinerary.slices
    ]
    return Itinerary(ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=slices))


def _its(*flights: str, day: date = _DEP, lands: str = "12:00", price: str) -> Itinerary:
    """One itinerary of one-flight slices, the first leaving on `day`."""
    slices = [
        Slice(
            flights=[f],
            departure=f"{day + timedelta(days=7 * i)}T09:00:00",
            arrival=f"{day + timedelta(days=7 * i)}T{lands}:00",
        )
        for i, f in enumerate(flights)
    ]
    return Itinerary(ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=slices))


def _marked(it: Itinerary, ticketing: str = "self_transfer") -> Itinerary:
    return it.model_copy(update={"ticketing": ticketing})


def _answer(*its: Itinerary) -> SearchResult:
    return SearchResult(solutionCount=len(its), solutions=list(its))


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, answer: SearchResult) -> list[Any]:
    """Matrix answers every search with `answer`; the searches are recorded."""
    asked: list[Any] = []

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, search: Any, *, cache: bool) -> SearchResult:
            _ = cache
            asked.append(search)
            return answer

    monkeypatch.setattr(cli, "MatrixClient", _Client)
    return asked


def _fll_lga_matrix(gf_session: Callable[..., Any]) -> tuple[SearchResult, list[str]]:
    """Matrix's answer to the FLL-LGA round trip, ZZ1/ZZ2 at USD150 and two of
    Google's own trips at USD199, and the GETs the default search made before
    it read the Cheapest tab."""
    base = gf_session(*_fll_lga_pages()[:-1])
    board = fli_results_to_search_result(
        cli._gflight_results(_round_trip(), SearchOptions(cabin=Cabin.COACH), 1000)
    )
    answer = _answer(
        _its("ZZ1", "ZZ2", price="USD150.00"),
        _as_matrix(board.solutions[0], "USD199.00"),
        _as_matrix(board.solutions[1], "USD199.00"),
    )
    return answer, base.gets


def _invoke(args: list[str]) -> Any:
    return CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})


def _as_the_base(gf_session: Callable[..., Any], args: list[str], *pages: str) -> Any:
    """`args` run as the default search ran before it read the Cheapest tab."""
    real = cli._gflight_results

    def _off(*a: Any, **kw: Any) -> Any:
        return real(*a, **{**kw, "separate_tickets": "off"})

    with pytest.MonkeyPatch.context() as m:
        m.setattr(cli, "_gflight_results", _off)
        gf_session(*pages)
        return _invoke(args)


def _merged_rows(stdout: str) -> list[list[str]]:
    """The merged table's numbered rows, cells stripped, each `why` cell
    joined across the lines it wraps onto."""
    _, after = stdout.split("Google Flights + Matrix", 1)
    rows: list[list[str]] = []
    for line in after.splitlines():
        if not line.startswith("│"):
            continue
        cells = [c.strip() for c in line.strip().strip("│").split("│")]
        if cells[0].isdigit():
            rows.append(cells)
        elif rows and not cells[0]:
            rows[-1][5] = f"{rows[-1][5]} {cells[5]}".strip()
    return rows


def _under_merged(stdout: str) -> str:
    _, after = stdout.split("Google Flights + Matrix", 1)
    return " ".join(after.split())


def test_the_adapted_board_carries_how_google_sells_each_row() -> None:
    for name, kinds in ((_CHEAPEST, (28, 5, 53)), (_BEST, (0, 0, 58))):
        sold = [it.ticketing for it in fli_results_to_search_result(_parsed(name)).solutions]
        assert (
            sold.count("self_transfer"),
            sold.count("separate_tickets"),
            sold.count(None),
        ) == kinds, name


def test_the_default_search_marks_the_google_table_for_one_get_more(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, base_gets = _fll_lga_matrix(gf_session)
    asked = _matrix_answers(monkeypatch, answer)
    fake = gf_session(*_fll_lga_pages())
    result = _invoke(_DEFAULT)
    assert result.exit_code == 0, result.output
    assert fake.gets[:-1] == base_gets
    assert len(fake.gets) == len(base_gets) + 1 == 12
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert len(asked) == 1
    google, _ = result.stdout.split("Google Flights + Matrix", 1)
    cells = _price_cells(google)
    assert sum(cell.endswith(" ‡") for cell in cells.values()) == 28
    assert sum(cell.endswith(" †") for cell in cells.values()) == 5
    assert " ".join(google.split()).count(_KEY) == 1


def test_the_default_search_opted_out_counts_what_it_hid_once_and_marks_nothing(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    base = _as_the_base(gf_session, _DEFAULT, *_fll_lga_pages()[:-1])
    fake = gf_session(*_fll_lga_pages())
    result = _invoke([*_DEFAULT, "--no-separate-tickets"])
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 12
    assert result.stdout == base.stdout
    said = " ".join(result.stderr.split())
    assert said.count("on separate tickets hidden") == 1
    assert (
        "Google Flights: 33 itineraries on separate tickets hidden (--no-separate-tickets)." in said
    )


def test_a_throttled_cheapest_tab_leaves_the_default_search_as_it_was_and_says_so_once(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    base = _as_the_base(gf_session, _DEFAULT, *_fll_lga_pages()[:-1])
    gf_session(*_fll_lga_pages()[:-1], _THROTTLE_PAGE)
    result = _invoke(_DEFAULT)
    assert result.exit_code == 0, result.output
    assert result.stdout == base.stdout
    said = " ".join(result.stderr.split())
    assert said.count("Itineraries on separate tickets not read") == 1
    assert "Itineraries on separate tickets not read: Google Flights rate-limited." in said


def test_a_default_search_whose_cheapest_tab_marks_nothing_prints_what_the_base_prints(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base board served as the Cheapest tab: it is fetched, and adds
    nothing to either table or to stderr."""
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    base = _as_the_base(gf_session, _DEFAULT, *_fll_lga_pages()[:-1])
    fake = gf_session(*_fll_lga_pages()[:-1], _served(_BEST))
    result = _invoke(_DEFAULT)
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 12
    assert (result.stdout, result.stderr) == (base.stdout, base.stderr)
    assert "Google Flights + Matrix" in result.stdout


# ──────────────────────────── the merged table ────────────────────────────


def test_a_one_way_separate_ticket_row_never_pairs_with_matrix_on_its_flights() -> None:
    """The board's row 5 sold as a self transfer, and Matrix pricing the same
    flights as one ticket: two rows, neither priced against the other."""
    board = fli_results_to_search_result(_parsed(_LAX))
    twin = _marked(board.solutions[5])
    google = _answer(*board.solutions[:5], twin, *board.solutions[6:])
    one_ticket = _as_matrix(board.solutions[5], "USD199.00")
    rows = merge_results(google, _answer(one_ticket), currency="USD")
    assert len(rows) == len(google.solutions) + 1
    assert [r for r in rows if r.google is twin] == [
        MergedRow(itinerary=twin, gf_price=twin.price, matrix_price=None, source="gf", google=twin)
    ]
    assert [r for r in rows if r.itinerary is one_ticket] == [
        MergedRow(itinerary=one_ticket, gf_price=None, matrix_price="USD199.00", source="matrix")
    ]


def test_the_merged_table_marks_exactly_the_separate_ticket_rows_and_keys_them_once(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    gf_session(*_fll_lga_pages())
    result = _invoke(_DEFAULT)
    assert result.exit_code == 0, result.output
    rows = _merged_rows(result.stdout)
    marked = [r for r in rows if r[3].endswith(("†", "‡"))]
    assert sum(r[3].endswith(" ‡") for r in marked) == 28
    assert sum(r[3].endswith(" †") for r in marked) == 5
    assert all((r[1], r[2], r[4]) == ("GF", "—", "—") for r in marked)
    assert {r[5] for r in marked if r[3].endswith("‡")} == {_SELF_TRANSFER_REASON}
    assert {r[5] for r in marked if r[3].endswith("†")} == {_SEPARATE_REASON}
    assert all(r[7] == "—" for r in marked)  # the outbound alone
    assert not any("separate tickets" in r[5] for r in rows if r not in marked)
    assert sorted(r[1] for r in rows if r not in marked).count("GF+MX") == 2
    under = _under_merged(result.stdout)
    assert under.count(_KEY) == 1
    assert under.count(_OUTBOUND_ONLY) == 1
    assert "Google listed 30 rows and 33 on separate tickets." in under


def test_a_one_way_merged_table_marks_the_row_and_keys_it_without_the_round_trip_sentence(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 5's flights sold as a self transfer beside the same flights as one
    ticket, which Matrix prices too."""
    served = gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))
    board = fli_results_to_search_result(served)
    _matrix_answers(monkeypatch, _answer(_as_matrix(board.solutions[5], "USD199.00")))
    gf_session(_served(_LAX), _lax_with_marked_twin(5))
    result = _invoke([*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), "-n", "200"])
    assert result.exit_code == 0, result.output
    rows = _merged_rows(result.stdout)
    (marked,) = [r for r in rows if r[3].endswith(("†", "‡"))]
    assert (marked[1], marked[2], marked[4], marked[5]) == ("GF", "—", "—", _SELF_TRANSFER_REASON)
    flights = marked[6]
    (paired,) = [r for r in rows if r[1] == "GF+MX"]
    assert paired[6] == flights
    under = _under_merged(result.stdout)
    assert under.count(_KEY) == 1
    assert _OUTBOUND_ONLY not in under


# ──────────────────────────── the cross-check ─────────────────────────────


def _checks(
    google: SearchResult, matrix: SearchResult, *, round_trip: bool = False
) -> dict[str, RowCheck]:
    """Each merged row's check, by the first flight of its first slice."""
    rows = merge_results(google, matrix, currency="USD")
    xc = cross_check(rows, Answers(matrix, google, False, False, round_trip, "USD"))
    out: dict[str, RowCheck] = {}
    for r, c in zip(rows, xc.rows, strict=True):
        itn = r.itinerary.itinerary
        assert itn is not None
        out[itn.slices[0].flights[0]] = c
    return out


def test_a_separate_ticket_row_is_explained_as_one_and_nothing_else() -> None:
    google = _answer(
        _marked(_its("AA1", price="USD90.00")),
        _marked(_its("AA2", price="USD95.00"), "separate_tickets"),
    )
    by_flight = _checks(google, _answer(_its("ZZ1", price="USD300.00")))
    assert (by_flight["AA1"].reasons, by_flight["AA1"].reason) == (
        ("separate_tickets",),
        _SELF_TRANSFER_REASON,
    )
    assert (by_flight["AA2"].reasons, by_flight["AA2"].reason) == (
        ("separate_tickets",),
        _SEPARATE_REASON,
    )
    assert by_flight["AA1"].delta is None


def test_an_outbound_google_prices_only_on_separate_tickets_is_not_priced() -> None:
    """Google's AA1 outbound is a self transfer alone; its one-ticket round
    trip is AA2/AA3."""
    google = _answer(_marked(_its("AA1", price="USD90.00")), _its("AA2", "AA3", price="USD200.00"))
    matrix = _answer(_its("AA1", "AA4", price="USD300.00"))
    c = _checks(google, matrix, round_trip=True)["AA1"]
    assert c.reasons == ("outbound_not_priced",)


def test_a_matrix_trip_google_sells_only_on_separate_tickets_is_not_paired_elsewhere() -> None:
    google = _answer(_marked(_its("AA1", price="USD90.00")), _its("AA2", price="USD95.00"))
    matrix = _answer(_its("AA1", price="USD300.00"))
    rows = merge_results(google, matrix, currency="USD")
    xc = cross_check(rows, Answers(matrix, google, False, False, False, "USD"))
    (c,) = [c for r, c in zip(rows, xc.rows, strict=True) if r.source == "matrix"]
    assert (c.reasons, c.reason) == (("not_on_google",), "not among Google's 1 rows")


def test_a_carrier_google_sells_only_on_separate_tickets_is_absent_from_its_board() -> None:
    google = _answer(_marked(_its("ZZ5", price="USD90.00")), _its("AA2", price="USD95.00"))
    c = _checks(google, _answer(_its("ZZ6", price="USD300.00")))["ZZ6"]
    assert (c.reasons, c.reason) == (("carrier_absent_google",), "no ZZ flight on Google's board")


def _enriched_document(monkeypatch: pytest.MonkeyPatch, rows: list[Any], *args: str) -> Any:
    """`--enrich --format json` on JFK-LAX, Google answering `rows` and Matrix
    DL1 at USD300."""

    def _gf(*_a: Any, **_kw: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    _matrix_answers(monkeypatch, _answer(_its("DL1", price="USD300.00")))
    result = _invoke(
        [*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), "--enrich", "--format", "json", *args]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_a_hand_marked_row_beside_a_matrix_row_reads_separate_tickets(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """AS21+AS600 marked by hand; every other row reads as it did unmarked."""
    plain = gf_rows(_LAX)
    rows = gf_rows(_LAX)
    rows[15].ticketing = "separate_tickets"
    base = _enriched_document(monkeypatch, plain, "-n", "100")
    doc = _enriched_document(monkeypatch, rows, "-n", "100")

    def by_trip(d: Any) -> dict[str, Any]:
        return {
            " / ".join("+".join(s["flights"]) for s in r["slices"]): r
            for r in d["cross_check"]["rows"]
        }

    marked, unmarked = by_trip(doc), by_trip(base)
    assert (marked["AS21+AS600"]["reasons"], marked["AS21+AS600"]["reason"]) == (
        ["separate_tickets"],
        _SEPARATE_REASON,
    )
    assert marked["AS21+AS600"]["source"] == "google"
    assert unmarked["AS21+AS600"]["reasons"] != ["separate_tickets"]
    assert set(marked) == set(unmarked)
    assert {k: r["reasons"] for k, r in marked.items() if k != "AS21+AS600"} == {
        k: r["reasons"] for k, r in unmarked.items() if k != "AS21+AS600"
    }
    sold = [m["separate_tickets"] for m in doc["search"]]
    assert sold.count(True) == 1
    assert base["cross_check"]["google"] == {"listed": 95, "answered": True}
    assert doc["cross_check"]["google"] == {"listed": 94, "answered": True, "separate": 1}


def test_the_fll_lga_document_states_each_separate_ticket_row_and_its_reason(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    gf_session(*_fll_lga_pages())
    result = _invoke([*_DEFAULT, "--enrich", "--format", "json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    members = _members(doc["search"])
    assert sum(m["separate_tickets"] is True for m in members) == 33
    rows = doc["cross_check"]["rows"]
    tagged = [r for r in rows if r["reasons"] == ["separate_tickets"]]
    assert len(tagged) == 33
    assert all(r["source"] == "google" and r["matrix_price"] is None for r in tagged)
    assert all(len(r["slices"]) == 1 for r in tagged)
    assert not [r for r in rows if "separate_tickets" in r["reasons"] and r not in tagged]
    assert doc["cross_check"]["google"] == {"listed": 30, "answered": True, "separate": 33}


# ─────────────────────── the surfaces that act on a row ───────────────────

_FAST = [*_SEARCH, *_FLL_LGA]


def _first_marked(labels: dict[str, str]) -> int:
    return next(int(k) for k, cell in labels.items() if cell.endswith(("†", "‡")))


def test_awards_are_matched_to_one_ticket_rows_and_say_how_many_were_left_out(
    gf_session: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched: list[Any] = []

    def _awards(sr: Any, **_kw: object) -> None:
        matched.append(sr)

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setenv("COLUMNS", "200")
    gf_session(*_fll_lga_pages())
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=False,
        run_pp=True,
        separate_tickets="show",
    )
    out, err = capsys.readouterr()
    said = " ".join(err.split())
    assert said.count("Awards are matched to one-ticket rows") == 1
    assert (
        "Awards are matched to one-ticket rows; 33 rows on separate tickets are not in "
        "the award table." in said
    )
    assert len(_price_cells(out)) == 60 + 33
    (sr,) = matched
    assert len(sr.solutions) == 30
    assert all(it.ticketing is None for it in sr.solutions)


# Every one-ticket row of the JFK-LAX board is over the cap; its self-transfer
# twin at USD100 is under it.
_CAPPED = ["search", "--no-google-url", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()]


@pytest.mark.parametrize("fmt", [["--fast"], ["--format", "json"]], ids=["fast", "json"])
def test_an_award_search_left_only_separate_ticket_rows_by_its_filter_is_handed_to_matrix(
    fmt: list[str], gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The award table matches one-ticket rows, and the filter left none, so
    Matrix answers as when it leaves no row at all."""
    asked = _matrix_answers(monkeypatch, _answer(_its("ZZ1", price="USD90.00")))
    matched: list[Any] = []

    def _awards(sr: Any, **_kw: object) -> None:
        matched.append(sr)

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    result = _invoke([*_CAPPED, *fmt, "--max-price", "120"])
    assert result.exit_code == 0, result.output
    assert len(asked) == 1
    assert [s.flights for sr in matched for it in sr.solutions for s in _slices_of(it)] == [["ZZ1"]]
    assert "‡" not in result.stdout
    said = " ".join(result.stderr.split())
    assert "not in the award table" not in said
    assert said.count("Using Matrix:") == 1
    assert (
        "Using Matrix: no one-ticket Google Flights itinerary matched a price cap of USD 120 "
        "(95 rows filtered out). Awards are matched to one-ticket rows; 1 itinerary on "
        "separate tickets did match, and --cash-only lists it." in said
    )


def test_a_cash_search_left_only_separate_ticket_rows_by_its_filter_shows_them(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _matrix_answers(monkeypatch, _answer(_its("ZZ1", price="USD90.00")))
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    result = _invoke([*_CAPPED, "--cash-only", "--fast", "--max-price", "120"])
    assert result.exit_code == 0, result.output
    assert asked == []
    assert _price_cells(result.stdout) == {"1": "USD100.00 ‡"}
    assert "Using Matrix" not in result.stderr


def _slices_of(it: Itinerary) -> list[Slice]:
    return it.itinerary.slices if it.itinerary is not None else []


@pytest.mark.parametrize("path", [["--backend", "gflight", "--fast"], []], ids=["fast", "default"])
def test_sellers_refuses_a_separate_ticket_row_before_chrome(
    path: list[str], gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, _ = _fll_lga_matrix(gf_session)
    _matrix_answers(monkeypatch, answer)
    opened: list[str] = []

    def _booking(url: str, **_kw: object) -> Any:
        opened.append(url)
        raise AssertionError("a separate-ticket row reached Chrome")

    monkeypatch.setattr(_gf_booking, "booking_options", _booking)
    gf_session(*_fll_lga_pages())
    args = [*_DEFAULT, *path]
    shown = _invoke(args)
    assert shown.exit_code == 0, shown.output
    if path:
        n = _first_marked(_price_cells(shown.stdout))
    else:
        n = next(int(r[0]) for r in _merged_rows(shown.stdout) if r[3].endswith(("†", "‡")))
    gf_session(*_fll_lga_pages())
    result = _invoke([*args, "--sellers", "--pick", str(n)])
    assert result.exit_code == 1, result.output
    assert opened == []
    assert " ".join(result.stderr.split()).endswith(
        f"No booking options for #{n}: Google sells #{n} as separate tickets; --sellers "
        "reads one-ticket booking pages only."
    )


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_verify_answers_a_separate_ticket_row_without_asking_matrix(
    fmt: str, gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_matrix(*_a: object, **_kw: object) -> Any:
        raise AssertionError("a separate-ticket row was asked of Matrix")

    monkeypatch.setattr(cli, "_run", _no_matrix)
    monkeypatch.setattr(cli, "MatrixClient", _no_matrix)
    gf_session(*_fll_lga_pages())
    shown = _invoke(_FAST)
    labels = _price_cells(shown.stdout)
    n = _first_marked(labels)
    reason = _SELF_TRANSFER_REASON if labels[str(n)].endswith("‡") else _SEPARATE_REASON
    gf_session(*_fll_lga_pages())
    result = _invoke([*_FAST, "--verify", "--pick", str(n), "--format", fmt])
    assert result.exit_code == 0, result.output
    if fmt == "table":
        said = " ".join(result.stdout.split())
        assert f"Not verified on Matrix · itinerary #{n}: {reason}" in said
        return
    verify = json.loads(result.stdout)["verify"]
    assert (verify["row"], verify["outcome"], verify["reason"]) == (n, "separate-tickets", reason)
    assert (verify["matrix"], verify["delta"], verify["fares"]) == (None, None, [])


@pytest.mark.parametrize("path", [["--backend", "gflight", "--fast"], []], ids=["fast", "default"])
def test_no_link_pins_a_separate_ticket_row_on_either_path(
    path: list[str], gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 1 is the marked twin, priced below everything else; a pick past the
    table falls back to it and so pins nothing."""
    _matrix_answers(monkeypatch, _answer(_its("ZZ1", price="USD300.00")))
    args = [
        *("search", "--cash-only", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()),
        *("-n", "5", *path),
    ]
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    picked = _invoke([*args, "--pick", "1"])
    assert picked.exit_code == 0, picked.output
    assert "Google Flights (tfs= structured):" in picked.stdout
    assert "pinned" not in picked.stdout
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    past = _invoke([*args, "--pick", "6"])
    assert past.exit_code == 0, past.output
    assert "Google Flights (tfs= structured):" in past.stdout
    assert "--pick 6 is out of range (1-5)." in " ".join(past.stderr.split())
    assert "pinning" not in past.stderr


@pytest.mark.parametrize("path", [["--backend", "gflight", "--fast"], []], ids=["fast", "default"])
def test_a_link_a_separate_ticket_row_leaves_unpinned_says_why(
    path: list[str], gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 1 is the marked twin, picked or pinned by default; row 2 is sold as
    one ticket."""
    _matrix_answers(monkeypatch, _answer(_its("ZZ1", price="USD300.00")))
    args = [
        *("search", "--cash-only", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()),
        *("-n", "5", *path),
    ]
    note = (
        "note: Google sells #1 as separate tickets, so this link opens the search, not that trip."
    )
    for pick in (["--pick", "1"], []):
        gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
        result = _invoke([*args, *pick])
        assert result.exit_code == 0, result.output
        assert " ".join(result.stdout.split()).count(note) == 1
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    one_ticket = _invoke([*args, "--pick", "2"])
    assert one_ticket.exit_code == 0, one_ticket.output
    assert "Google Flights (itinerary #2 pinned):" in one_ticket.stdout
    assert "separate tickets, so this link" not in one_ticket.stdout


def test_an_awards_only_search_reads_no_cheapest_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """It prints no Google row, and its award table takes one-ticket rows."""
    modes: list[str] = []

    def _path(**kw: Any) -> None:
        modes.append(kw.get("separate_tickets", "off"))

    monkeypatch.setattr(cli, "_run_enriched_path", _path)
    monkeypatch.setattr(cli, "_run_gflight_path", _path)
    base = ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight"]
    for extra in ([], ["--fast"]):
        for awards in (["--awards-only"], ["--cash-only"]):
            result = _invoke([*base, *extra, *awards])
            assert result.exit_code == 0, result.output
    assert modes == ["off", "show", "off", "show"]
