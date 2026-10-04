# pyright: reportPrivateUsage=false
"""`--currency X`: every money figure the CLI prints is in X or carries its own
label, and a figure that would mix currencies is not printed at all.

Matrix takes the code as `inputs.currency` (the wire bodies are pinned in
`test_wire_round_trip.py`); Google Flights takes it as the page's `curr=`. The
one Google surface that cannot price in it is the date grid, which a non-USD
calendar never reaches."""

from __future__ import annotations

import json
import pathlib
import urllib.parse
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pydantic
import pytest
import typer
from typer.testing import CliRunner

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    SearchOptions,
    SpecificDateSearch,
)
from flight_cli.fli_bridge import to_fli_filter
from flight_cli.links import google_flights_pinned_url, google_flights_url
from flight_cli.models import CalendarResult, Itinerary, SearchResult
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# fli's validator rejects a past travel date, so this is derived.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli.app, args)


# ───────────────────────────── the CLI seam ─────────────────────────────────


def test_a_currency_is_upper_cased() -> None:
    assert cli._resolve_currency("eur") == "EUR"
    assert cli._resolve_currency(None) is None


@pytest.mark.parametrize("bad", ["EURO", "E1R", "", "€"])
def test_a_currency_that_is_not_three_letters_is_a_usage_error(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exc:
        cli._resolve_currency(bad)
    assert exc.value.exit_code == 2
    assert "3-letter ISO 4217" in capsys.readouterr().err


def test_the_domain_refuses_a_code_the_cli_did_not_normalize() -> None:
    with pytest.raises(pydantic.ValidationError):
        SearchOptions(currency="eur")


# ──────────────────────────── Google Flights ────────────────────────────────


def _search(currency: str | None) -> SpecificDateSearch:
    return SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET)),
        options=SearchOptions(currency=currency),
    )


def test_the_google_links_carry_the_searchs_currency() -> None:
    seg = {"origin": "JFK", "date": _DEP.isoformat(), "destination": "LAX", "carrier": "AA"}
    seg["flight"] = "1"
    back = {**seg, "origin": "LAX", "destination": "JFK", "date": _RET.isoformat()}
    assert "&curr=EUR" in google_flights_url(_search("EUR"))
    assert "&curr=EUR" in google_flights_pinned_url(
        _search("EUR"), outbound_segments=[seg], return_segments=[back]
    )
    # Unset is USD, as before; an explicit argument still wins.
    assert "&curr=USD" in google_flights_url(_search(None))
    assert "&curr=GBP" in google_flights_url(_search("EUR"), currency="GBP")


def test_the_page_is_asked_for_the_currency(
    gf_session: Callable[..., Any], gf_capture: Callable[[str], str]
) -> None:
    fake = gf_session(gf_capture("ds1_jfk_lax_3rows.json"))
    filters = to_fli_filter(SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _DEP),)))
    assert gfid.search_with_ids(filters, currency="EUR")
    assert len(fake.gets) == 1
    assert _curr(fake.gets[0]) == ["EUR"]
    assert _curr(gfid.search_page_url(filters)) == ["USD"]


def _curr(url: str) -> list[str]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["curr"]


def test_every_board_of_a_round_trip_is_asked_for_the_currency(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """The return boards are fetched by the recursion, one per pinned outbound."""
    asked: list[str] = []

    def _board(_f: Any, _t: Any, *, currency: str) -> gfid.Board[Any]:
        asked.append(currency)
        return gfid.Board(gf_rows("ds1_jfk_lax_3rows.json")[:1] if len(asked) == 1 else [])

    monkeypatch.setattr(gfid, "_one_call_laddered", _board)
    filters = to_fli_filter(_search(None))
    gfid.search_with_ids(filters, top_n=1, currency="EUR")
    assert asked == ["EUR", "EUR"]


def _rows(gf_rows: Callable[..., list[Any]], *currencies: str | None) -> list[Any]:
    rows = gf_rows("ds1_jfk_lax_3rows.json")[: len(currencies)]
    return [
        gfid.GFlightWithId(
            flight=r.flight.model_copy(update={"currency": c}),
            flight_id=r.flight_id,
            amenities=r.amenities,
        )
        for r, c in zip(rows, currencies, strict=True)
    ]


def test_a_row_with_no_decoded_currency_takes_its_boards(
    gf_rows: Callable[..., list[Any]],
) -> None:
    """fli returns None when a price token does not decode; the page's other
    rows say what currency it was priced in, and the requested code is only
    the fallback."""
    board = gfid._with_board_currency(gfid.Board(_rows(gf_rows, "EUR", None, "EUR")), "GBP")
    assert [r.flight.currency for r in board] == ["EUR", "EUR", "EUR"]
    board = gfid._with_board_currency(gfid.Board(_rows(gf_rows, None, None)), "GBP")
    assert [r.flight.currency for r in board] == ["GBP", "GBP"]


def test_a_dearer_listing_with_no_decoded_currency_takes_its_boards(
    gf_rows: Callable[..., list[Any]],
) -> None:
    """A `+CABIN` search can be offered one of a row's `others` in place of
    the row, so each of them is filled from the board as the row is."""
    best, side = _rows(gf_rows, "EUR", "EUR")
    [other] = _rows(gf_rows, None)
    board = gfid._with_board_currency(gfid.Board([replace(best, others=(other,)), side]), "GBP")
    assert [o.flight.currency for o in board[0].others] == ["EUR"]


def test_a_row_in_another_currency_keeps_it_and_the_run_says_so(
    gf_rows: Callable[..., list[Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    board = gfid._with_board_currency(gfid.Board(_rows(gf_rows, "USD", "EUR")), "EUR")
    assert [r.flight.currency for r in board] == ["USD", "EUR"]
    cli._note_other_currencies(board, "EUR")
    err = capsys.readouterr().err
    assert "USD" in err
    assert "EUR" in err
    cli._note_other_currencies(_rows(gf_rows, "EUR", "EUR"), "EUR")
    assert capsys.readouterr().err == ""


def test_the_search_asks_google_for_the_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _search_with_ids(
        _f: Any, *, top_n: int, transport: Any, currency: str, keep: Any, checks: str
    ) -> list[Any]:
        _ = top_n, transport, keep, checks
        seen.append(currency)
        return []

    monkeypatch.setattr(gfid, "search_with_ids", _search_with_ids)
    legs = (Leg.of("JFK", "LAX", _DEP),)
    cli._gflight_results(legs, SearchOptions(currency="EUR"), 5)
    cli._gflight_results(legs, SearchOptions(), 5)
    assert seen == ["EUR", "USD"]


# ───────────────────────────── the date grid ────────────────────────────────


def _calendar(currency: str | None) -> CalendarSearch:
    return CalendarSearch(
        legs=(Leg.of("JFK", "LAX"),),
        options=SearchOptions(currency=currency),
        window=CalendarWindow(
            start=_DEP, end=_DEP + timedelta(days=6), duration_min=5, duration_max=7
        ),
    )


def test_the_grid_blocker_refuses_a_non_usd_currency_first() -> None:
    """First, so an admission branch added later cannot let a non-USD calendar
    through to a grid that prices in USD."""
    kw: dict[str, Any] = {"json_out": True, "one_way": False, "origins": ("JFK", "EWR")}
    assert cli._grid_branch_blocker(_calendar("EUR"), dests=("LAX",), **kw) == "a non-USD currency"
    assert cli._grid_branch_blocker(_calendar("USD"), dests=("LAX",), **kw) == "JSON output"


def _calendar_args(*extra: str) -> list[str]:
    start = _DEP.isoformat()
    end = (_DEP + timedelta(days=6)).isoformat()
    return ["calendar", "JFK", "LAX", "--start", start, "--end", end, "--one-way", *extra]


def test_a_fast_calendar_refuses_a_non_usd_currency() -> None:
    result = _run(_calendar_args("--currency", "eur", "--fast"))
    assert result.exit_code == 1
    # rich wraps the refusal at ~80 columns, so a phrase can straddle two lines.
    assert "non-USD currency" in " ".join(result.stderr.split())
    assert result.stdout == ""


def test_a_non_usd_calendar_is_priced_by_matrix_in_that_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[CalendarSearch] = []

    def _matrix(search: CalendarSearch, **_kw: Any) -> tuple[CalendarResult, int, bool]:
        asked.append(search)
        return CalendarResult.from_api({"solutionCount": 0}), 0, False

    def _no_grid(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("a non-USD calendar reached the USD date grid")

    monkeypatch.setattr(cli, "_run_calendar", _matrix)
    monkeypatch.setattr(cli, "_run_calendar_enriched", _no_grid)
    monkeypatch.setattr(cli, "_run_fast_calendar_grid", _no_grid)
    result = _run([*_calendar_args("--currency", "GBP"), "--no-matrix-url"])
    assert result.exit_code == 0, result.output
    assert [s.options.currency for s in asked] == ["GBP"]


# ───────────────────────────── Matrix commands ──────────────────────────────


def _gbp_result() -> SearchResult:
    body = json.loads(
        (FIXTURES / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json").read_text()
    )
    return SearchResult.from_api(body)


def test_detail_prices_in_the_currency_and_titles_it(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Any] = []

    def _matrix(search: Any, *_a: Any) -> SearchResult:
        asked.append(search)
        return _gbp_result()

    monkeypatch.setattr(cli, "_run", _matrix)
    result = _run(
        [
            "detail",
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "--currency",
            "GBP",
            "--no-matrix-url",
            "--no-google-url",
        ]
    )
    assert result.exit_code == 0, result.output
    assert [s.options.currency for s in asked] == ["GBP"]
    assert "Itineraries (GBP)" in result.stdout
    assert "USD" not in result.stdout


def test_a_matrix_search_sends_the_currency_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Any] = []

    def _matrix(search: Any, *_a: Any) -> SearchResult:
        asked.append(search)
        return _gbp_result()

    monkeypatch.setattr(cli, "_run", _matrix)
    result = _run(
        [
            "search",
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "--backend",
            "matrix",
            "--currency",
            "gbp",
            "--cash-only",
            "--format",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output
    assert [s.options.currency for s in asked] == ["GBP"]
    assert json.loads(result.stdout) == _gbp_result().raw


def test_a_matrix_price_in_another_currency_keeps_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tables are titled with the cheapest fare's currency; a row or a grid
    cell Matrix priced in another one keeps its own code."""
    body = json.loads(
        (FIXTURES / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json").read_text()
    )
    body["solutionList"]["solutions"][1]["ext"]["price"] = "USD900.00"
    body["carrierStopMatrix"]["rows"][0]["cells"][2]["minPrice"] = "USD729.00"

    def _matrix(*_a: Any) -> SearchResult:
        return SearchResult.from_api(body)

    monkeypatch.setattr(cli, "_run", _matrix)
    result = _run(
        [
            "search",
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "--backend",
            "matrix",
            "--currency",
            "GBP",
            "-n",
            "2",
            "--cash-only",
            "--no-matrix-url",
            "--no-google-url",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Itineraries (GBP)" in result.stdout
    assert "USD900.00" in result.stdout
    assert "USD729.00" in result.stdout
    assert "618.00" in result.stdout
    assert "GBP618.00" not in result.stdout


def test_a_calendar_price_in_another_currency_keeps_its_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = CalendarResult.from_api(
        {
            "solutionCount": 5,
            "currencyNotice": {"ext": {"price": "GBP300.00"}},
            "calendar": {
                "months": [
                    {
                        "weeks": [
                            {
                                "days": [
                                    {"date": 20, "solutionCount": 3, "minPrice": "GBP300.00"},
                                    {
                                        "date": 21,
                                        "solutionCount": 2,
                                        "minPrice": "USD400.00",
                                        "tripDuration": {
                                            "options": [{"tripLength": 7, "minPrice": "USD410.00"}]
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        }
    )
    cli._render_calendar(
        res,
        dmin=7,
        dmax=7,
        origin=("JFK",),
        destination=("LHR",),
        sd=_DEP,
        ed=_DEP + timedelta(days=6),
        round_trip=True,
    )
    out = capsys.readouterr().out
    assert "cheapest: 300.00 (GBP)" in out
    assert "USD400.00" in out
    assert "USD410.00" in out
    assert "GBP300.00" not in out


# ────────────────────────── the enriched Google path ────────────────────────


def test_the_enriched_table_asks_both_backends_and_titles_the_currency(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """The default search: Google and Matrix both asked for EUR, and the merged
    table says EUR where it used to say nothing."""
    gf_asked: list[str | None] = []
    matrix_asked: list[str | None] = []
    rows = _rows(gf_rows, "EUR")

    def _gf(_legs: Any, opts: SearchOptions, *_a: Any, **_kw: Any) -> list[Any]:
        gf_asked.append(opts.currency)
        return rows

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, search: Any, *, cache: bool) -> SearchResult:
            _ = cache
            matrix_asked.append(search.options.currency)
            return SearchResult.model_validate({"solutions": [{"ext": {"price": "EUR321.00"}}]})

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    monkeypatch.setattr(cli, "MatrixClient", _Client)
    result = _run(
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--currency",
            "EUR",
            "--cash-only",
            "--no-matrix-url",
            "--no-google-url",
        ]
    )
    assert result.exit_code == 0, result.output
    assert gf_asked == ["EUR"]
    # The merged search, then the chain search on Google's row under Matrix's
    # EUR321, in the row's currency.
    assert matrix_asked == ["EUR", "EUR"]
    assert "(EUR)" in result.stdout
    assert "USD" not in result.stdout


_MERGED_SEARCH = [
    "search",
    "JFK",
    "LAX",
    "--dep",
    _DEP.isoformat(),
    "--cash-only",
    "--no-matrix-url",
    "--no-google-url",
]


def _matrix_bodies(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    *,
    matrix_price: str = "GBP321.00",
    fares: int = 1,
) -> list[dict[str, Any]]:
    """Google answers with two USD rows and Matrix with `fares` fares of
    `matrix_price`; every Matrix request body a run sends is recorded."""
    bodies: list[dict[str, Any]] = []
    rows = _rows(gf_rows, "USD", "USD")

    def _gf(*_a: Any, **_kw: Any) -> list[Any]:
        return rows

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
            return SearchResult.model_validate(
                {"solutions": [{"ext": {"price": matrix_price}}] * fares}
            )

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    monkeypatch.setattr(cli, "MatrixClient", _Client)
    return bodies


@pytest.mark.parametrize(
    ("args", "currency"),
    [
        pytest.param([], "USD", id="merged-unset"),
        pytest.param(["--currency", "EUR"], "EUR", id="merged-eur"),
        pytest.param(["--backend", "matrix"], None, id="matrix-only-unset"),
    ],
)
def test_the_merged_table_asks_matrix_in_the_currency_google_is_asked_in(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    args: list[str],
    currency: str | None,
) -> None:
    """Unset, Matrix prices in its own default (GBP from LHR) while Google is
    asked for USD, and the merged table would rank one against the other. The
    merged run asks Matrix in Google's currency; a Matrix-only run merges
    nothing and sends the body it always has."""
    bodies = _matrix_bodies(monkeypatch, gf_rows)
    result = _run([*_MERGED_SEARCH, *args])
    assert result.exit_code == 0, result.output
    assert [b["inputs"].get("currency") for b in bodies] == [currency]


def test_the_merged_tables_matrix_body_differs_from_a_matrix_only_one_by_the_currency_and_page(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    bodies = _matrix_bodies(monkeypatch, gf_rows)
    for args in ([], ["--backend", "matrix"]):
        result = _run([*_MERGED_SEARCH, *args])
        assert result.exit_code == 0, result.output
    merged, matrix_only = bodies
    assert merged["inputs"].pop("currency") == "USD"
    assert merged["inputs"].pop("page") == {"current": 1, "size": 500}
    assert matrix_only["inputs"].pop("page") == {"current": 1, "size": 10}
    assert merged == matrix_only


@pytest.mark.parametrize(("n", "page"), [(10, 500), (600, 600)])
def test_the_merged_table_asks_matrix_for_a_page_that_holds_its_whole_answer(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]], n: int, page: int
) -> None:
    """Matrix answers in price order, so a page of `-n` compares Google's whole
    board with Matrix's first `-n` fares. Still one request, of max(-n, 500)."""
    bodies = _matrix_bodies(monkeypatch, gf_rows)
    result = _run([*_MERGED_SEARCH, "-n", str(n)])
    assert result.exit_code == 0, result.output
    assert [b["inputs"]["page"]["size"] for b in bodies] == [page]


def test_a_matrix_only_search_still_asks_for_n_rows(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    bodies = _matrix_bodies(monkeypatch, gf_rows)
    result = _run([*_MERGED_SEARCH, "--backend", "matrix", "-n", "7"])
    assert result.exit_code == 0, result.output
    assert [b["inputs"]["page"]["size"] for b in bodies] == [7]


def test_the_awards_are_fanned_out_over_matrixs_first_n_fares(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """The deeper page explains the table; the award fan-out stays at `-n`."""
    _matrix_bodies(monkeypatch, gf_rows, matrix_price="USD321.00", fares=30)
    seen: list[int] = []

    def _overlay(res: SearchResult, **_kw: Any) -> None:
        seen.append(len(res.solutions))

    def _awards(_sel: Any) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _awards)
    monkeypatch.setattr(cli, "_overlay_awards", _overlay)
    args = ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "-n", "5"]
    result = _run([*args, "--no-matrix-url", "--no-google-url"])
    assert result.exit_code == 0, result.output
    assert seen == [5]


def test_a_matrix_fare_in_another_currency_ranks_after_googles_on_the_merged_table(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Matrix answering in GBP although asked for USD: its fare is the smaller
    number, and a trim by bare numbers keeps it over a USD fare. Ranked in the
    requested USD, the two Google fares fill `-n 2`."""
    _matrix_bodies(monkeypatch, gf_rows, matrix_price="GBP1.00")
    result = _run([*_MERGED_SEARCH, "-n", "2"])
    assert result.exit_code == 0, result.output
    merged = result.stdout.split("Google Flights + Matrix", 1)[1]
    assert "(USD)" in merged.splitlines()[0]
    # The caption under the rows names the last fare of Matrix's page, GBP1.00.
    rows, caption = merged.split("Matrix listed", 1)
    assert "GBP" not in rows
    assert "(to GBP1.00)" in caption


# ──────────────────────────── the rendered tables ───────────────────────────


def _itin(price: str) -> Itinerary:
    return Itinerary.model_validate({"ext": {"price": price}})


def test_the_multi_cabin_header_names_the_currency(capsys: pytest.CaptureFixture[str]) -> None:
    row = MultiCabinRow(itinerary=_itin("EUR100.00"))
    row.prices[Cabin.COACH] = "EUR100.00"
    row.prices[Cabin.BUSINESS] = "USD900.00"
    cli._render_multi_cabin_search([row], cabins=(Cabin.COACH, Cabin.BUSINESS), sort_by=Cabin.COACH)
    out = capsys.readouterr().out
    assert "Y (EUR)" in out
    assert "$" not in out
    # The one price in another currency keeps its own label under the EUR title.
    assert "USD900.00" in out
    assert "EUR100.00" not in out


def test_the_merged_title_names_the_currency(capsys: pytest.CaptureFixture[str]) -> None:
    class _Row:
        def __init__(self, gf: str, mx: str) -> None:
            self.itinerary = _itin(gf)
            self.source = "both"
            self.gf_price = gf
            self.matrix_price = mx

    cli._render_merged(
        [_Row("EUR100.00", "EUR101.00"), _Row("USD99.00", "EUR98.00")],
        legs=(Leg.of("JFK", "LAX", _DEP),),
        top_n=5,
    )
    out = capsys.readouterr().out
    assert "(EUR)" in out
    assert "USD99.00" in out
    assert "EUR101.00" not in out


# ─────────────────────── derived figures: cents per mile ────────────────────


def test_no_cents_per_mile_is_computed_from_a_non_usd_fare() -> None:
    """The award table divides this cash by miles and prints cents. A GBP fare
    read as dollars is a wrong valuation printed as a right one."""
    usd, dollar, gbp = _itin("USD600.00"), _itin("$1,200"), _itin("GBP617.39")
    res = SearchResult.model_validate({"solutions": [usd, dollar, gbp]})
    single = cli._cash_per_cabin_single(res, Cabin.COACH)
    assert single == {id(usd): {"Economy": 600.0}, id(dollar): {"Economy": 1200.0}}

    row = MultiCabinRow(itinerary=gbp)
    row.prices[Cabin.COACH] = "GBP617.39"
    row.prices[Cabin.BUSINESS] = "USD3000.00"
    assert cli._cash_per_cabin_multi([row]) == {id(gbp): {"Business": 3000.0}}
    assert cast("dict[int, Any]", cli._cash_per_cabin_multi([MultiCabinRow(itinerary=usd)])) == {}
