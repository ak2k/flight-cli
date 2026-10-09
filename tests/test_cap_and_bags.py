# pyright: reportPrivateUsage=false
"""`search --max-price N` and `--bags CHECKED[,CARRY]`.

A cap is asked of Google Flights (tfs field 12) and checked on every row it
serves; a Matrix answer, which has no such input, is cut to the fares under it.
Bags are asked of Google Flights (field 13), each row says what its price
covers, and a search that only Matrix could answer is refused rather than
answered without them.
"""

from __future__ import annotations

import copy
import io
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_errors import GfBackendError, GfTfsUnsupportedError
from flight_cli.domain import Bags, Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter
from flight_cli.links import build_search_tfs, matrix_deep_link
from flight_cli.models import SearchResult
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    from click.testing import Result

# fli's validator rejects a past travel date, so the dates are derived.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_PAGES = Path(__file__).parent / "fixtures" / "gflight_page"
_ABSENT = object()


def _page_rows(name: str) -> list[Any]:
    return gfid._rows_from_ds1(json.loads((_PAGES / f"{name}.json").read_text())).rows


def _raw_row(slot: object = _ABSENT) -> list[Any]:
    """A JFK-LAX row off a committed page, with `row[4][6]` replaced by
    `slot`, or cut off before it."""
    row = copy.deepcopy(_page_rows("ds1_jfk_lax_3rows")[0])
    if slot is _ABSENT:
        row[4] = row[4][:6]
    else:
        row[4][6] = slot
    return row


def _search(**options: Any) -> SpecificDateSearch:
    return SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET)),
        options=SearchOptions(**options),
    )


# ───────────────────────────── domain and bridge ────────────────────────────


def test_the_fli_filter_carries_the_cap_and_the_bags() -> None:
    f = to_fli_filter(_search(max_price=250, bags=Bags(checked=1, carry_on=1)))
    assert f.price_limit.max_price == 250
    assert (f.bags.checked_bags, f.bags.carry_on) == (1, True)


def test_the_fli_filter_carries_neither_unless_asked() -> None:
    f = to_fli_filter(_search())
    assert f.price_limit is None
    assert f.bags is None


def test_matrix_is_sent_the_same_request_with_or_without_them() -> None:
    """Matrix has no input for either, so its body and its link stay the
    unconstrained search's, byte for byte."""
    plain, asked = _search(), _search(max_price=250, bags=Bags(checked=1))
    assert to_wire(asked).as_json() == to_wire(plain).as_json()
    assert matrix_deep_link(asked) == matrix_deep_link(plain)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"checked": 0, "carry_on": 0}, {"carry_on": 2}, {"checked": -1}],
    ids=["nothing", "zeros", "two-carry-ons", "negative"],
)
def test_bags_refuse_a_count_google_cannot_be_asked_for(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Bags(**kwargs)


def test_a_cap_below_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        SearchOptions(max_price=0)


# ─────────────────────────── the row's bag statement ─────────────────────────


def test_each_row_carries_the_bags_google_says_its_price_covers() -> None:
    def stated(name: str) -> Counter[tuple[int | None, int | None]]:
        return Counter(gfid._parse_flight_with_id(r).bags_included for r in _page_rows(name))

    assert stated("ds1_jfk_lax_tfu") == {(0, 1): 95}
    # JFK-LHR states no checked allowance at all, and four rows state nothing.
    assert stated("ds1_jfk_lhr_tfu") == {(None, 1): 97, (None, None): 4}


@pytest.mark.parametrize(
    "slot,stated",
    [
        ([1, 1], (1, 1)),
        ([None, 1], (None, 1)),
        (_ABSENT, (None, None)),
        ([1], (1, None)),
        (None, (None, None)),
        ("[1, 1]", (None, None)),
        ([True, -1], (None, None)),
    ],
    ids=["both", "checked-unstated", "no-slot", "short", "null", "string", "bool-and-negative"],
)
def test_a_missing_short_or_malformed_statement_says_nothing(
    slot: object, stated: tuple[int | None, int | None]
) -> None:
    row = gfid._parse_flight_with_id(_raw_row(slot))
    assert row.bags_included == stated
    assert row.flight.price is not None  # the row itself still parses whole


# ─────────────────────────────── the flags ───────────────────────────────────


def _flat(output: str) -> str:
    """`output` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def _search_cli(*args: str) -> Result:
    return CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--cash-only",
            "--no-matrix-url",
            "--no-google-url",
            *args,
        ],
    )


_PATHS = (
    "_run_gflight_path",
    "_run_enriched_path",
    "_run_matrix_path",
    "_run_gflight_path_multi",
    "_run_matrix_path_multi",
)


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every backend path stubbed, recording which ran and with what."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def stub(name: str) -> Any:
        def _path(**kw: Any) -> None:
            calls.append((name, kw))

        return _path

    for name in _PATHS:
        monkeypatch.setattr(cli, name, stub(name))
    return calls


def test_bags_on_auto_are_served_by_google_with_no_matrix_fallback(
    ran: list[tuple[str, dict[str, Any]]],
) -> None:
    result = _search_cli("--bags", "1", "--fast")
    assert result.exit_code == 0, result.output
    [(name, kw)] = ran
    assert name == "_run_gflight_path"
    assert kw["opts"].bags == Bags(checked=1, carry_on=0)
    assert kw["matrix_fallback"] is False
    assert "Using Matrix" not in result.output


def test_bags_skip_the_matrix_enrichment_and_say_why(
    ran: list[tuple[str, dict[str, Any]]],
) -> None:
    result = _search_cli("--bags", "0,1")
    assert result.exit_code == 0, result.output
    assert [name for name, _ in ran] == ["_run_gflight_path"]
    assert ran[0][1]["opts"].bags == Bags(checked=0, carry_on=1)
    assert "No Matrix enrichment: Matrix prices no bags." in result.stderr


@pytest.mark.parametrize(
    "args,path",
    [
        ([], "_run_enriched_path"),
        (["--fast"], "_run_gflight_path"),
        (["--backend", "matrix"], "_run_matrix_path"),
        (["--seniors", "1"], "_run_matrix_path"),
    ],
    ids=["enriched", "fast", "matrix", "matrix-only-passenger"],
)
def test_a_price_cap_alone_never_changes_the_backend(
    ran: list[tuple[str, dict[str, Any]]], args: list[str], path: str
) -> None:
    for cap in ([], ["--max-price", "250"]):
        ran.clear()
        result = _search_cli(*args, *cap)
        assert result.exit_code == 0, result.output
        [(name, kw)] = ran
        assert name == path
        assert kw["opts"].max_price == (250 if cap else None)
        if name == "_run_gflight_path":
            assert kw["matrix_fallback"] is True


@pytest.mark.parametrize(
    "args,reason",
    [
        (["--backend", "matrix"], "Matrix prices no bags"),
        # An infant is served on Google, but as a second traveler.
        (["--inf-lap", "1"], "--bags takes one traveler"),
        (["--seniors", "1"], "a senior or youth passenger"),
        (["--no-airport-changes"], "a ban on changing airports"),
        (["--backend", "gflight", "--inf-seat", "1"], "--bags takes one traveler"),
    ],
    ids=["backend-matrix", "infant", "senior", "airport-changes", "gflight-infant"],
)
def test_bags_are_refused_where_only_matrix_could_answer(
    ran: list[tuple[str, dict[str, Any]]], args: list[str], reason: str
) -> None:
    result = _search_cli("--bags", "1", *args)
    assert result.exit_code == 2, result.output
    assert not ran
    flat = _flat(result.output)
    assert reason in flat
    assert "--bags" in flat
    assert "Using Matrix" not in flat
    assert "use --backend matrix" not in flat


@pytest.mark.parametrize(
    "args,says",
    [
        # Google counts the bags for the party and then states no allowance.
        (["--bags", "1", "--adults", "2"], "--bags takes one traveler"),
        (["--bags", "1", "--children", "1"], "--bags takes one traveler"),
        (["--bags", "1", "--sellers", "--fast"], "--sellers lists fares from a booking page"),
    ],
    ids=["bags-two-adults", "bags-child", "bags-sellers"],
)
def test_what_the_flags_cannot_be_combined_with_exits_2_before_any_backend(
    ran: list[tuple[str, dict[str, Any]]], args: list[str], says: str
) -> None:
    result = _search_cli(*args)
    assert result.exit_code == 2, result.output
    assert not ran
    assert says in _flat(result.output)
    assert "Using Matrix" not in result.output


@pytest.mark.parametrize(
    ("args", "asked"),
    [
        (["--max-price", "250", "--cabin", "economy,business"], {"max_price": 250}),
        (["--bags", "1", "--cabin", "y,j"], {"bags": Bags(checked=1, carry_on=0)}),
    ],
    ids=["cap-cabins", "bags-cabins"],
)
def test_several_cabins_search_with_the_cap_or_the_bags(
    ran: list[tuple[str, dict[str, Any]]], args: list[str], asked: dict[str, Any]
) -> None:
    """Each cabin's search is the one-cabin search with the flag. Red at the
    base, which refused both (exit 2)."""
    result = _search_cli(*args)
    assert result.exit_code == 0, result.output
    [(name, kw)] = ran
    assert name == "_run_gflight_path_multi"
    for field, value in asked.items():
        assert getattr(kw["opts"], field) == value


def test_a_price_cap_with_sellers_is_allowed(ran: list[tuple[str, dict[str, Any]]]) -> None:
    result = _search_cli("--max-price", "250", "--sellers", "--fast")
    assert result.exit_code == 0, result.output
    assert [name for name, _ in ran] == ["_run_gflight_path"]


@pytest.mark.parametrize("bad", ["x", "1,2", "0", "0,0", ",1", "-1", "1,1,1", "100"])
def test_a_malformed_bag_count_exits_2(ran: list[tuple[str, dict[str, Any]]], bad: str) -> None:
    result = _search_cli("--bags", bad)
    assert result.exit_code == 2, result.output
    assert not ran


def test_a_price_cap_below_one_exits_2(ran: list[tuple[str, dict[str, Any]]]) -> None:
    result = _search_cli("--max-price", "0")
    assert result.exit_code == 2, result.output
    assert not ran


# ───────────────────────── Google rows under a cap ───────────────────────────


def _gf_row(price: float | None, currency: str | None = "USD", slot: object = _ABSENT) -> Any:
    row = gfid._parse_flight_with_id(_raw_row(slot))
    return replace(row, flight=row.flight.model_copy(update={"price": price, "currency": currency}))


class _Page:
    """Google's page, answered in process: `board` is what it serves, held to
    the search's own row check, and `asked` the filters each search sent."""

    def __init__(self) -> None:
        self.board: list[Any] = []
        self.asked: list[Any] = []

    def search_with_ids(self, filters: Any, *, keep: Any = None, **_kw: Any) -> Any:
        self.asked.append(filters)
        kept = [r for r in self.board if keep is None or keep(0, r)]
        return gfid.Board(kept, dropped=len(self.board) - len(kept))


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> _Page:
    page = _Page()
    monkeypatch.setattr(gfid, "search_with_ids", page.search_with_ids)
    return page


def test_a_capped_google_search_asks_the_page_and_prints_no_row_over_it(
    served: _Page,
) -> None:
    served.board.extend(
        [_gf_row(204.0), _gf_row(250.0), _gf_row(250.5), _gf_row(None), _gf_row(200.0, "EUR")]
    )
    result = _search_cli("--max-price", "250", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [(r["price"], r["currency"]) for r in json.loads(result.stdout)] == [
        (204.0, "USD"),
        (250.0, "USD"),
    ]
    assert [f.price_limit.max_price for f in served.asked] == [250]


def _one_way_filter(**options: Any) -> Any:
    return to_fli_filter(
        SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _DEP),), options=SearchOptions(**options))
    )


def test_only_a_usd_page_is_asked_for_the_cap() -> None:
    """A EUR page asked for a cap served fewer of the fares under it than the
    uncapped page (JFK-LAX at EUR 240: 34 of 45), so off USD the page is asked
    for the whole board and the row check alone applies the cap."""
    capped, plain = _one_way_filter(max_price=240), _one_way_filter()
    assert gfid.search_page_url(capped, currency="EUR") == gfid.search_page_url(
        plain, currency="EUR"
    )
    assert gfid.search_page_url(capped, currency="USD") != gfid.search_page_url(
        plain, currency="USD"
    )


def test_a_cap_wider_than_an_int32_is_left_to_the_row_check() -> None:
    """2**64 would be an 11-byte varint, which no protobuf reader accepts, and
    how the page reads a cap past an int32 was never measured. No USD fare
    comes near one, so the page is fetched uncapped instead."""
    plain = gfid.search_page_url(_one_way_filter(), currency="USD")
    for cap in (2**31, 2**64):
        assert gfid.search_page_url(_one_way_filter(max_price=cap), currency="USD") == plain
    widest = build_search_tfs(_one_way_filter(max_price=2**31 - 1))
    assert b"\x60\xff\xff\xff\xff\x07" in widest  # field 12, five bytes


def test_a_eur_cap_still_holds_every_row(served: _Page) -> None:
    served.board.extend([_gf_row(196.0, "EUR"), _gf_row(241.0, "EUR"), _gf_row(200.0, "USD")])
    result = _search_cli("--max-price", "240", "--currency", "EUR", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [(r["price"], r["currency"]) for r in json.loads(result.stdout)] == [(196.0, "EUR")]


def test_a_board_the_cap_empties_goes_to_matrix_naming_the_cap(
    served: _Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix: list[SearchOptions] = []

    def _run_matrix_path(*, opts: SearchOptions, **_kw: Any) -> None:
        matrix.append(opts)

    monkeypatch.setattr(cli, "_run_matrix_path", _run_matrix_path)
    served.board.extend([_gf_row(300.0), _gf_row(410.0)])
    result = _search_cli("--max-price", "250", "--fast")
    assert result.exit_code == 0, result.output
    assert "no Google Flights itinerary matched a price cap of USD 250 (2 rows filtered out)" in (
        _flat(result.stderr)
    )
    assert [o.max_price for o in matrix] == [250]


def test_under_bags_a_board_the_cap_empties_is_answered_empty(
    served: _Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_matrix(**_kw: Any) -> None:
        raise AssertionError("--bags reached Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    served.board.extend([_gf_row(300.0)])
    result = _search_cli("--bags", "1", "--max-price", "250", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "no itinerary matched a price cap of USD 250 (1 rows filtered out)" in _flat(
        result.stderr
    )


def test_google_s_own_empty_board_under_a_cap_says_no_fare_is_under_it(
    served: _Page,
) -> None:
    result = _search_cli("--max-price", "250", "--fast")
    assert result.exit_code == 0, result.output
    assert "Google Flights: no fare at or under USD 250." in result.stdout


# Off USD, or past an int32, the page is fetched uncapped, so an empty board is
# Google having no rows at all.
_UNASKED_CAPS: list[tuple[list[str], int]] = [(["--currency", "EUR"], 250), ([], 2**31)]


@pytest.mark.parametrize(("args", "cap"), _UNASKED_CAPS, ids=["eur", "past-int32"])
def test_an_empty_board_the_page_was_not_asked_to_cap_is_not_blamed_on_it(
    served: _Page, args: list[str], cap: int
) -> None:
    result = _search_cli("--max-price", str(cap), *args, "--fast")
    assert result.exit_code == 0, result.output
    assert "Google Flights: no results." in result.stdout
    assert "at or under" not in result.output


def test_the_enriched_paint_names_the_cap(capsys: pytest.CaptureFixture[str]) -> None:
    legs = (Leg.of("JFK", "LAX", _DEP),)
    opts = SearchOptions(max_price=250)
    cli._paint_first_gf_table({}, gfid.Board([]), legs=legs, top_n=5, awards_only=False, opts=opts)
    assert "no fare at or under USD 250; awaiting Matrix" in capsys.readouterr().err
    for args, cap in _UNASKED_CAPS:
        unasked = SearchOptions(max_price=cap, currency=args[1] if args else None)
        cli._paint_first_gf_table(
            {}, gfid.Board([]), legs=legs, top_n=5, awards_only=False, opts=unasked
        )
        assert "Google Flights: no results; awaiting Matrix" in capsys.readouterr().err
    cli._paint_first_gf_table(
        {}, gfid.Board([], dropped=3), legs=legs, top_n=5, awards_only=False, opts=opts
    )
    assert "no itinerary matched a price cap of USD 250; awaiting Matrix" in _flat(
        capsys.readouterr().err
    )


# ─────────────────────────── Matrix under a cap ─────────────────────────────

# The fixture's 25 solutions ascend GBP 618 to 736; 14 are at or under 690.
_GBP_CAP = "690"
_GBP_KEPT = 14
_GBP_OVER = ("691.00", "718.00", "729.00", "736.00")


def _gbp_body() -> dict[str, Any]:
    return json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "matrix_currency"
            / "specific_jfk_lhr_rt_gbp_resp.json"
        ).read_text()
    )


@pytest.fixture
def matrix_gbp(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return SearchResult.from_api(_gbp_body())

    monkeypatch.setattr(cli, "_run", _run)


def _matrix_cli(*args: str) -> Result:
    return _search_cli("--backend", "matrix", "--currency", "GBP", "-n", "25", *args)


@pytest.mark.usefixtures("matrix_gbp")
def test_a_capped_matrix_table_prints_no_fare_over_the_cap_no_grid_and_the_kept_count() -> None:
    plain = _matrix_cli()
    assert "Carrier x stops grid" in plain.stdout
    result = _matrix_cli("--max-price", _GBP_CAP)
    assert result.exit_code == 0, result.output
    assert f"{_GBP_KEPT} solutions" in result.stdout
    assert "Carrier x stops grid" not in result.stdout
    assert "684.00" in result.stdout
    assert not [p for p in _GBP_OVER if p in result.stdout]


@pytest.mark.usefixtures("matrix_gbp")
def test_a_capped_matrix_document_lists_and_counts_only_the_kept_fares() -> None:
    result = _matrix_cli("--max-price", _GBP_CAP, "--format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    solutions = doc["solutionList"]["solutions"]
    assert len(solutions) == doc["solutionCount"] == doc["solutionList"]["solutionCount"]
    assert len(solutions) == _GBP_KEPT
    assert all(float(s["ext"]["price"].removeprefix("GBP")) <= int(_GBP_CAP) for s in solutions)
    # The blocks describing Matrix's whole answer stay as it served them.
    served = _gbp_body()
    assert doc["carrierStopMatrix"] == served["carrierStopMatrix"]
    assert doc["solutionList"]["minPrice"] == served["solutionList"]["minPrice"]


@pytest.mark.usefixtures("matrix_gbp")
@pytest.mark.parametrize("cap", ["736", "10000"])
def test_a_cap_over_the_whole_page_leaves_matrix_s_own_count(cap: str) -> None:
    """Every fare of the page is under the cap, so the fares past it went
    unchecked, and the count stays Matrix's total (88 beside 25 rows)."""
    uncapped = json.loads(_matrix_cli("--format", "json").stdout)
    doc = json.loads(_matrix_cli("--max-price", cap, "--format", "json").stdout)
    assert len(doc["solutionList"]["solutions"]) == len(uncapped["solutionList"]["solutions"])
    assert doc["solutionCount"] == uncapped["solutionCount"] > len(doc["solutionList"]["solutions"])
    assert doc["solutionList"]["solutionCount"] == uncapped["solutionList"]["solutionCount"]
    total = uncapped["solutionCount"]
    assert f"{total} solutions" in _matrix_cli("--max-price", cap).stdout
    capped = cli._price_capped(
        SearchResult.from_api(_gbp_body()), SearchOptions(currency="GBP", max_price=int(cap))
    )
    assert capped.solution_count == total


@pytest.mark.usefixtures("matrix_gbp")
def test_a_matrix_cap_in_another_currency_keeps_nothing() -> None:
    result = _search_cli("--backend", "matrix", "--max-price", "5000", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["solutionList"]["solutions"] == []


@pytest.mark.usefixtures("matrix_gbp")
def test_an_empty_capped_matrix_answer_names_the_cap() -> None:
    result = _matrix_cli("--max-price", "100")
    assert result.exit_code == 0, result.output
    assert "No solutions at or under GBP 100." in result.stdout


def _matrix_bodies(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every Matrix search body a run sends, each answered with the GBP page."""
    bodies: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, search: Any, *, cache: bool) -> SearchResult:
            _ = cache
            bodies.append(to_wire(search).as_json())
            return SearchResult.from_api(_gbp_body())

    monkeypatch.setattr(cli, "MatrixClient", _Client)
    return bodies


@pytest.mark.parametrize(
    ("args", "currency"),
    [pytest.param([], "USD", id="unset"), pytest.param(["--currency", "GBP"], "GBP", id="gbp")],
)
def test_a_capped_matrix_search_asks_matrix_in_the_cap_s_currency(
    monkeypatch: pytest.MonkeyPatch, args: list[str], currency: str
) -> None:
    """Unset, Matrix prices in its own default (GBP from LHR), and a USD cap
    would drop every one of its fares as another currency's."""
    bodies = _matrix_bodies(monkeypatch)
    result = _search_cli("--backend", "matrix", "--max-price", "2000", "--format", "json", *args)
    assert result.exit_code == 0, result.output
    assert [b["inputs"].get("currency") for b in bodies] == [currency]


def test_a_capped_matrix_body_is_the_uncapped_one_plus_the_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _matrix_bodies(monkeypatch)
    for args in (["--max-price", "2000"], []):
        result = _search_cli("--backend", "matrix", "--format", "json", *args)
        assert result.exit_code == 0, result.output
    capped, uncapped = bodies
    assert capped["inputs"].pop("currency") == "USD"
    assert capped == uncapped


def test_an_uncapped_matrix_search_leaves_the_currency_to_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = _matrix_bodies(monkeypatch)
    result = _search_cli("--backend", "matrix", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [b["inputs"].get("currency", "unset") for b in bodies] == ["unset"]
    assert json.loads(result.stdout) == _gbp_body()


def test_a_search_the_cap_hands_to_matrix_asks_it_in_the_cap_s_currency(
    served: _Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = _matrix_bodies(monkeypatch)
    served.board.extend([_gf_row(300.0), _gf_row(410.0)])
    result = _search_cli("--max-price", "250", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [b["inputs"].get("currency") for b in bodies] == ["USD"]


def test_the_enriched_path_merges_only_the_matrix_fares_under_the_cap(
    served: _Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _matrix_into(state: dict[str, Any], *_a: Any) -> None:
        state["matrix"] = SearchResult.from_api(_gbp_body())

    monkeypatch.setattr(cli, "_matrix_into", _matrix_into)
    result = _search_cli("--currency", "GBP", "--max-price", _GBP_CAP, "-n", "25")
    assert result.exit_code == 0, result.output
    assert "Google Flights + Matrix" in result.stdout
    assert "684.00" in result.stdout
    assert not [p for p in _GBP_OVER if p in result.stdout]


# ────────────────────────── what a row's price covers ────────────────────────


@pytest.mark.parametrize(
    "stated,asked,cell",
    [
        ((1, 1), Bags(checked=1), "incl."),
        ((0, 1), Bags(checked=1), "not incl."),
        ((None, 1), Bags(checked=1), "unknown"),
        ((None, None), Bags(checked=1), "unknown"),
        ((1, 1), Bags(carry_on=1), "incl."),
        ((0, 1), Bags(carry_on=1), "incl."),
        ((None, 1), Bags(carry_on=1), "incl."),
        ((None, None), Bags(carry_on=1), "unknown"),
        ((1, 0), Bags(checked=1, carry_on=1), "not incl."),
        ((1, None), Bags(checked=1, carry_on=1), "unknown"),
        ((2, 1), Bags(checked=2, carry_on=1), "incl."),
    ],
)
def test_a_row_is_incl_only_when_it_says_it_covers_what_was_asked(
    stated: tuple[int | None, int | None], asked: Bags, cell: str
) -> None:
    assert cli._bag_cell(stated, asked) == cell


_STATEMENTS: list[object] = [[1, 1], [0, 1], [None, 1], _ABSENT]


def test_each_json_row_says_what_its_price_covers_under_bags(served: _Page) -> None:
    served.board.extend(_gf_row(204.0, slot=slot) for slot in _STATEMENTS)
    result = _search_cli("--bags", "1", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [r["bags_included"] for r in json.loads(result.stdout)] == [
        {"checked": 1, "carry_on": 1},
        {"checked": 0, "carry_on": 1},
        {"checked": None, "carry_on": 1},
        {"checked": None, "carry_on": None},
    ]


def test_without_bags_the_json_rows_carry_no_statement(served: _Page) -> None:
    served.board.extend(_gf_row(204.0, slot=slot) for slot in _STATEMENTS)
    result = _search_cli("--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert all("bags_included" not in r for r in json.loads(result.stdout))


def test_each_member_of_a_round_trip_pair_says_what_its_price_covers() -> None:
    pair = (_gf_row(398.0, slot=[1, 1]), _gf_row(398.0, slot=_ABSENT))
    [(out, back)] = cli._gflight_json_document([pair], Bags(checked=1))
    assert out["bags_included"] == {"checked": 1, "carry_on": 1}
    assert back["bags_included"] == {"checked": None, "carry_on": None}


@pytest.fixture
def awards_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An award provider configured and matching nothing, so every cash row
    reaches the award document on its own."""
    from flight_cli.pp import cli as pp_cli

    async def _gather(*, legs: list[Any], **_kw: Any) -> tuple[list[list[Any]], list[Any]]:
        return ([[] for _ in legs], [])

    def _configured(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _configured)
    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)


def _award_document(*args: str) -> list[dict[str, Any]]:
    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--fast", "--format", "json", *args],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.usefixtures("awards_on")
def test_each_cash_row_beside_the_awards_says_what_its_price_covers(served: _Page) -> None:
    served.board.extend(_gf_row(204.0, slot=slot) for slot in _STATEMENTS)
    [leg] = _award_document("--bags", "1")
    assert [m["bags_included"] for m in leg["matches"]] == [
        {"checked": 1, "carry_on": 1},
        {"checked": 0, "carry_on": 1},
        {"checked": None, "carry_on": 1},
        {"checked": None, "carry_on": None},
    ]


@pytest.mark.usefixtures("awards_on")
def test_each_leg_beside_the_awards_says_what_its_own_member_covers(served: _Page) -> None:
    served.board.append((_gf_row(398.0, slot=[1, 1]), _gf_row(398.0, slot=_ABSENT)))
    out, back = _award_document("--return", _RET.isoformat(), "--bags", "1")
    assert [m["bags_included"] for m in out["matches"]] == [{"checked": 1, "carry_on": 1}]
    assert [m["bags_included"] for m in back["matches"]] == [{"checked": None, "carry_on": None}]


@pytest.mark.usefixtures("awards_on")
def test_without_bags_the_cash_rows_beside_the_awards_carry_no_statement(
    served: _Page,
) -> None:
    served.board.extend(_gf_row(204.0, slot=slot) for slot in _STATEMENTS)
    [leg] = _award_document()
    assert len(leg["matches"]) == len(_STATEMENTS)
    assert all("bags_included" not in m for m in leg["matches"])


def _table(monkeypatch: pytest.MonkeyPatch, bags: Bags | None) -> str:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=250, no_color=True))
    cli._render_gflight_table(
        [_gf_row(204.0, slot=slot) for slot in _STATEMENTS],
        legs=(Leg.of("JFK", "LAX", _DEP),),
        top_n=10,
        bags=bags,
        currency="USD",
    )
    return buffer.getvalue()


def test_the_table_labels_each_row_with_what_its_price_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _table(monkeypatch, Bags(checked=1)).splitlines()
    header = next(ln for ln in lines if "price" in ln)
    assert header.rstrip(" │┃").endswith("bags")
    cells = [ln.rstrip(" │").rsplit("│", 1)[-1].strip() for ln in lines if re.match(r"^│\s+\d", ln)]
    assert cells == ["incl.", "not incl.", "unknown", "unknown"]


def test_without_bags_the_table_has_no_bag_column(monkeypatch: pytest.MonkeyPatch) -> None:
    table = _table(monkeypatch, None)
    assert "bags" not in table
    assert "incl." not in table


def test_under_bags_every_google_link_says_its_prices_leave_them_out() -> None:
    asked = SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP),), options=SearchOptions(bags=Bags(checked=1))
    )
    plain = SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _DEP),))
    note = "the linked page's prices do not include the bags --bags asked for"
    assert note in cli._gflight_url_caveats(asked)
    assert note in cli._pinned_gflight_url_caveats(asked)
    assert cli._gflight_url_caveats(plain) == []
    assert cli._pinned_gflight_url_caveats(plain) == []


@pytest.mark.parametrize("bags", [["--bags", "1"], []], ids=["bags", "plain"])
def test_under_bags_the_matrix_link_says_its_prices_leave_them_out(
    served: _Page, bags: list[str]
) -> None:
    served.board.append(_gf_row(249.0, slot=[1, 1]))
    args = ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cash-only", "--no-google-url"]
    result = CliRunner().invoke(cli.app, [*args, "--fast", *bags])
    assert result.exit_code == 0, result.output
    matrix = _flat(result.stdout.partition("Matrix deep-link:")[2])
    assert matrix, result.stdout
    assert ("note: Matrix prices no bags" in matrix) == bool(bags), matrix


def _published_refusals() -> list[GfBackendError]:
    def every(root: type[GfBackendError]) -> list[type[GfBackendError]]:
        return [c for child in root.__subclasses__() for c in (child, *every(child))]

    return [
        GfTfsUnsupportedError("stops", "a ceiling") if cls is GfTfsUnsupportedError else cls("x")
        for cls in (GfBackendError, *every(GfBackendError))
        if cls.__module__ == GfBackendError.__module__
    ]


@pytest.mark.parametrize("transport", ["http", "browser"])
def test_under_bags_no_refusal_points_at_matrix(transport: Any) -> None:
    """Matrix prices no bags, so under `--bags` the way out a refusal names is
    dropping them."""
    for error in _published_refusals():
        plain = cli._gf_refusal(error, transport=transport).message
        bagged = cli._gf_refusal(error, transport=transport, bags=True).message
        assert "--backend matrix" not in bagged
        assert ("--backend matrix" in plain) == ("--bags" in bagged), type(error).__name__
