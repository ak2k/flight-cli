# pyright: reportCallIssue=false, reportPrivateUsage=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/test_enrich.py.
"""The Google-vs-Matrix cross-check: a delta for the same trip in one currency,
a reason the two answers decide on every other row, and where Matrix's page
ends — on the table and as the `--format json --enrich` document."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conftest import _ds1
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._cross_check import Answers, CrossCheck, RowCheck, cross_check, document
from flight_cli._enrich import MergedRow, merge_results
from flight_cli._gf_errors import GfThrottledError
from flight_cli.client import MatrixApiError
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
    SliceCarrier,
)
from flight_cli.pp.gflight_adapter import fli_results_to_search_result
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    from collections.abc import Callable

_BOARD = "ds1_jfk_lax_tfu.json"


def _board() -> SearchResult:
    """The tracked JFK-LAX board, 95 one-way rows on AA, AS, B6 and DL."""
    payload: list[Any] = json.loads(_ds1(_BOARD))
    return fli_results_to_search_result(
        [gfid._parse_flight_with_id(r) for r in gfid._rows_from_ds1(payload).rows]
    )


def _as_matrix(g: Itinerary, price: str, *, late: int = 0) -> Itinerary:
    """Google row `g` as Matrix states it: its own price, a UTC offset on each
    landing, no per-flight dates and no leg detail. `late` minutes on the
    first landing make it another trip."""
    assert g.itinerary is not None
    slices: list[Slice] = []
    for i, s in enumerate(g.itinerary.slices):
        assert s.arrival is not None
        lands = datetime.fromisoformat(s.arrival) + timedelta(minutes=late if i == 0 else 0)
        slices.append(
            s.model_copy(
                update={"arrival": f"{lands.isoformat()}+00:00", "segment_dates": [], "legs": []}
            )
        )
    return Itinerary(ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=slices))


def _its(*slices: Slice, price: str) -> Itinerary:
    return Itinerary(ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=list(slices)))


def _slice(flights: list[str], day: str = "2026-11-04", lands: str = "12:00") -> Slice:
    return Slice(flights=flights, departure=f"{day}T09:00:00", arrival=f"{day}T{lands}:00")


def _answer(*its: Itinerary, count: int | None = None) -> SearchResult:
    return SearchResult(solutionCount=len(its) if count is None else count, solutions=list(its))


def _flights(row: MergedRow) -> str:
    itn = row.itinerary.itinerary
    return " / ".join("+".join(s.flights) for s in itn.slices) if itn else ""


def _checked(
    google: SearchResult | None,
    matrix: SearchResult,
    *,
    filtered: bool = False,
    stop_limit: bool = False,
    round_trip: bool = False,
) -> tuple[CrossCheck, dict[str, tuple[MergedRow, RowCheck]]]:
    rows = merge_results(google or _answer(), matrix, currency="USD")
    xc = cross_check(
        rows,
        Answers(
            matrix=matrix,
            google=google,
            google_filtered=filtered,
            stop_limit=stop_limit,
            round_trip=round_trip,
            currency="USD",
        ),
    )
    return xc, {_flights(r): (r, c) for r, c in zip(rows, xc.rows, strict=True)}


def _matrix_answer(
    board: SearchResult, *extra: Itinerary, count: int | None = None
) -> SearchResult:
    """DL1788 and B61023 as Google's own trips at USD199, B6123 landing a
    minute after Google's, and `extra`."""
    sols = board.solutions
    return _answer(
        _as_matrix(sols[0], "USD199.00"),
        _as_matrix(sols[1], "USD199.00"),
        _as_matrix(sols[3], "USD199.00", late=1),
        *extra,
        count=count,
    )


# ───────────────────────────── a row both sides price ───────────────────────


def test_the_same_trip_in_one_currency_shows_google_minus_matrix() -> None:
    board = _board()
    _, rows = _checked(board, _matrix_answer(board))
    row, c = rows["DL1788"]
    assert (row.source, row.same_trip) == ("both", True)
    assert (c.delta, c.reasons, c.reason) == (5.0, (), None)


def test_two_trips_sharing_a_key_show_no_delta_and_name_both_landings() -> None:
    board = _board()
    _, rows = _checked(board, _matrix_answer(board))
    row, c = rows["B6123"]
    assert (row.source, row.same_trip, c.delta) == ("both", False, None)
    assert c.reasons == ("trip_unconfirmed",)
    assert c.reason == "trip unconfirmed: Google lands 11-04 08:58, Matrix 11-04 08:59"


def test_a_pair_in_two_currencies_shows_no_delta() -> None:
    board = _board()
    _, rows = _checked(board, _answer(_as_matrix(board.solutions[0], "GBP160.00")))
    _, c = rows["DL1788"]
    assert (c.delta, c.reasons, c.reason) == (
        None,
        ("other_currency",),
        "Matrix in GBP, Google in USD",
    )


def test_a_pair_with_no_google_price_shows_no_delta() -> None:
    board = _board()
    g = board.solutions[0].model_copy(update={"ext": ItineraryExt(price=None)})
    _, rows = _checked(_answer(g), _answer(_as_matrix(board.solutions[0], "USD199.00")))
    _, c = rows["DL1788"]
    assert (c.delta, c.reasons) == (None, ("unpriced",))


def test_a_row_built_without_the_merge_fields_is_never_a_confirmed_pair() -> None:
    class _Row:
        source = "both"
        gf_price = "USD204.00"
        matrix_price = "USD199.00"
        itinerary = Itinerary()

    xc = cross_check([_Row()], Answers(_answer(), _answer(), False, False, False, "USD"))
    assert (xc.rows[0].delta, xc.rows[0].reasons) == (None, ("trip_unconfirmed",))


# ───────────────────────────── a Google row alone ───────────────────────────


def test_a_google_row_on_a_carrier_a_complete_answer_lacks_says_so() -> None:
    board = _board()
    xc, rows = _checked(board, _matrix_answer(board))
    assert xc.boundary.complete
    _, c = rows["AS21+AS487"]
    assert c.reasons == ("carrier_absent",)
    assert c.reason == "no AS flight in Matrix's answer of 3"


def test_a_carrier_matrix_names_on_an_itinerary_is_not_absent() -> None:
    board = _board()
    matrix = _matrix_answer(board)
    first = matrix.solutions[0]
    assert first.itinerary is not None
    named = first.itinerary.model_copy(update={"carriers": [SliceCarrier(code="AS")]})
    matrix.solutions[0] = first.model_copy(update={"itinerary": named})
    _, rows = _checked(board, matrix)
    assert rows["AS21+AS487"][1].reasons == ("not_in_matrix",)


def test_an_incomplete_answer_shows_no_carrier_absent_and_says_where_its_page_ends() -> None:
    board = _board()
    xc, rows = _checked(board, _matrix_answer(board, count=40))
    assert not xc.boundary.complete
    _, c = rows["AS21+AS487"]
    assert c.reasons == ("past_page",)
    assert c.reason == "Matrix listed only 3 of 40, to USD199.00"


def test_a_google_row_on_carriers_matrix_lists_is_not_in_its_answer() -> None:
    board = _board()
    _, rows = _checked(board, _matrix_answer(board))
    _, c = rows["DL747"]
    assert (c.reasons, c.reason) == (("not_in_matrix",), "not in Matrix's answer of 3")


def _three_flights(board: SearchResult) -> SearchResult:
    """The board and one trip of three B6 flights, two stops."""
    trip = _its(_slice(["B61", "B62", "B63"], lands="20:00"), price="USD150.00")
    return _answer(*board.solutions, trip)


def test_a_google_row_past_the_extra_flight_matrix_was_asked_for_says_so() -> None:
    """Without a stop limit Matrix is asked for one flight beyond the fewest a
    slice needs; it listed nonstops, so three flights are past its window."""
    board = _three_flights(_board())
    _, rows = _checked(board, _matrix_answer(board))
    _, c = rows["B61+B62+B63"]
    assert c.reasons == ("stops_outside",)
    assert c.reason == "3 flights; Matrix searched up to 2"


def test_no_stops_reason_under_a_stop_limit() -> None:
    board = _three_flights(_board())
    _, rows = _checked(board, _matrix_answer(board), stop_limit=True)
    assert rows["B61+B62+B63"][1].reasons == ("not_in_matrix",)


def test_no_stops_reason_on_a_slice_matrix_listed_no_row_on() -> None:
    board = _three_flights(_board())
    _, rows = _checked(board, _answer())
    assert "stops_outside" not in rows["B61+B62+B63"][1].reasons


def test_every_reason_that_holds_is_listed_in_order() -> None:
    board = _answer(_its(_slice(["AS1", "AS2", "AS3"]), price="USD150.00"))
    _, rows = _checked(board, _answer(_its(_slice(["DL1"]), price="USD200.00"), count=9))
    _, c = rows["AS1+AS2+AS3"]
    assert c.reasons == ("stops_outside", "past_page")
    _, rows = _checked(board, _answer(_its(_slice(["DL1"]), price="USD200.00")))
    assert rows["AS1+AS2+AS3"][1].reasons == ("carrier_absent", "stops_outside")


def test_a_google_row_matrix_may_list_on_another_row_is_not_called_absent() -> None:
    """Two Google rows land when Matrix's does, with the middle flight on other
    days: the merge pairs Matrix with the first, and the second may be its trip."""
    a = Slice(
        flights=["AA1", "AA2", "AA3"],
        departure="2026-11-01T07:00:00",
        arrival="2026-11-03T09:00:00",
        segment_dates=["2026-11-01", "2026-11-01", "2026-11-03"],
    )
    b = a.model_copy(update={"segment_dates": ["2026-11-01", "2026-11-02", "2026-11-03"]})
    m = a.model_copy(update={"arrival": "2026-11-03T09:00+00:00", "segment_dates": []})
    rows = merge_results(
        _answer(_its(a, price="USD700.00"), _its(b, price="USD720.00")),
        _answer(_its(m, price="USD690.00")),
        currency="USD",
    )
    google = _answer(_its(a, price="USD700.00"), _its(b, price="USD720.00"))
    xc = cross_check(
        rows, Answers(_answer(_its(m, price="USD690.00")), google, False, False, False, "USD")
    )
    assert [(r.source, c.reasons) for r, c in zip(rows, xc.rows, strict=True)] == [
        ("both", ("trip_unconfirmed",)),
        ("gf", ("paired_elsewhere",)),
    ]


# ───────────────────────────── a Matrix row alone ───────────────────────────


def test_matrix_rows_say_google_gave_no_answer() -> None:
    board = _board()
    _, rows = _checked(None, _matrix_answer(board))
    assert {c.reasons for _, c in rows.values()} == {("no_google_answer",)}


def test_a_matrix_row_on_a_carrier_google_lists_no_flight_of_says_so() -> None:
    board = _board()
    _, rows = _checked(board, _matrix_answer(board, _its(_slice(["ZZ1"]), price="USD150.00")))
    _, c = rows["ZZ1"]
    assert (c.reasons, c.reason) == (("carrier_absent_google",), "no ZZ flight on Google's board")


def test_a_matrix_row_on_carriers_google_lists_is_not_on_its_board() -> None:
    board = _board()
    _, rows = _checked(board, _matrix_answer(board, _its(_slice(["DL9"]), price="USD150.00")))
    _, c = rows["DL9"]
    assert (c.reasons, c.reason) == (("not_on_google",), "not among Google's 95 rows")


def test_a_board_the_row_filter_cut_shows_no_carrier_absent() -> None:
    board = _board()
    _, rows = _checked(
        board, _matrix_answer(board, _its(_slice(["ZZ1"]), price="USD150.00")), filtered=True
    )
    assert rows["ZZ1"][1].reasons == ("not_on_google",)


def _round_trip(board: SearchResult) -> SearchResult:
    """Two pinned outbounds of the board, DL1788 and B61023, each with two
    returns, DL747 and DL742 on the 11th."""
    back = [
        s.model_copy(update={"departure": s.departure.replace("11-04", "11-11")})
        for s in (board.solutions[2].itinerary.slices[0], board.solutions[4].itinerary.slices[0])  # pyright: ignore[reportOptionalMemberAccess]
        if s.departure is not None
    ]
    out = [it.itinerary.slices[0] for it in board.solutions[:2] if it.itinerary is not None]
    return _answer(*(_its(o, r, price="USD420.00") for o in out for r in back))


def test_a_round_trip_matrix_row_whose_outbound_google_never_pinned_says_so() -> None:
    """Its carrier is on no Google flight either, and that is not said: Google
    searched no return for this outbound."""
    board = _board()
    google = _round_trip(board)
    zz = _its(_slice(["ZZ1"]), _slice(["ZZ2"], day="2026-11-11"), price="USD300.00")
    _, rows = _checked(google, _answer(zz), round_trip=True)
    _, c = rows["ZZ1 / ZZ2"]
    assert (c.reasons, c.reason) == (
        ("outbound_not_priced",),
        "Google priced no return for this outbound",
    )


def test_a_round_trip_matrix_row_on_a_pinned_outbound_names_the_carrier_google_lacks() -> None:
    board = _board()
    google = _round_trip(board)
    out = google.solutions[0].itinerary.slices[0]  # pyright: ignore[reportOptionalMemberAccess]
    m = _its(
        out.model_copy(update={"arrival": f"{out.arrival}+00:00", "legs": []}),
        _slice(["ZZ2"], day="2026-11-11"),
        price="USD300.00",
    )
    _, rows = _checked(google, _answer(m), round_trip=True)
    _, c = rows["DL1788 / ZZ2"]
    assert c.reasons == ("carrier_absent_google",)


def _bare(price: str, *more: Slice) -> Itinerary:
    """A trip whose first slice states no flight numbers."""
    first = Slice(departure="2026-11-04T09:00:00", arrival="2026-11-04T12:00:00")
    return _its(first, *more, price=price)


def test_a_row_stating_no_flights_is_never_called_absent_from_the_other_side() -> None:
    """Without its flight numbers neither answer decides whether the other side
    lists the trip, or whether Google priced a return for its outbound."""
    board = _board()
    _, google_row = _checked(_answer(*board.solutions, _bare("USD150.00")), _matrix_answer(board))
    _, matrix_row = _checked(board, _matrix_answer(board, _bare("USD150.00")))
    _, round_trip = _checked(
        _round_trip(board),
        _answer(_bare("USD300.00", _slice(["ZZ2"], day="2026-11-11"))),
        round_trip=True,
    )
    assert [rows[k][1].reasons for rows, k in ((google_row, ""), (matrix_row, ""))] == [
        ("unmatched",),
        ("unmatched",),
    ]
    assert round_trip[" / ZZ2"][1].reasons == ("unmatched",)
    assert google_row[""][1].reason == "cannot be matched: a flight, day or landing is unstated"


# ───────────────────────────── the boundary and the document ────────────────


def test_the_boundary_is_matrixs_page_and_googles_board() -> None:
    board = _board()
    xc, _ = _checked(board, _matrix_answer(board, count=40))
    b = xc.boundary
    assert (b.listed, b.solution_count, b.complete, b.last_price) == (3, 40, False, "USD199.00")
    assert (b.google_listed, b.google_answered) == (95, True)


def test_the_document_is_the_rows_in_order_with_numbers_for_deltas() -> None:
    board = _board()
    rows = merge_results(board, _matrix_answer(board), currency="USD")[:4]
    xc = cross_check(rows, Answers(_matrix_answer(board), board, False, False, False, "USD"))
    doc = document(rows, xc)
    assert json.loads(json.dumps(doc)) == doc
    assert doc["delta"] == "google_minus_matrix"
    assert doc["matrix"] == {
        "listed": 3,
        "solution_count": 3,
        "complete": True,
        "last_price": "USD199.00",
    }
    assert doc["google"] == {"listed": 95, "answered": True}
    first, *_, last = doc["rows"]
    assert first == {
        "source": "both",
        "google_price": "USD204.00",
        "matrix_price": "USD199.00",
        "delta": 5.0,
        "reasons": [],
        "reason": None,
        "slices": [
            {
                "flights": ["DL1788"],
                "departure": "2026-11-04T12:00:00",
                "arrival": "2026-11-04T15:05:00+00:00",
                "segment_dates": ["2026-11-04"],
            }
        ],
    }
    assert (last["source"], last["delta"], last["reasons"]) == ("google", None, ["not_in_matrix"])


# ───────────────────────────── the CLI: one weave ───────────────────────────

# fli's validator rejects a past travel date, so this is derived.
_DEP = date.today() + timedelta(days=45)
_SEARCH = ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cash-only"]
_LINKLESS = ["--no-matrix-url", "--no-google-url"]


def _weave(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    *,
    google_fails: bool = False,
    matrix_fails: bool = False,
    matrix: Callable[[SearchResult], SearchResult] | None = None,
) -> list[dict[str, Any]]:
    """Google answers with the tracked JFK-LAX board and Matrix, in price
    order, with ZZ1, two of Google's own trips at USD199 and one landing a
    minute off, or with `matrix` of the board; every Matrix body a run sends
    is recorded."""
    rows = gf_rows(_BOARD)
    board = fli_results_to_search_result(rows)
    answer = (
        matrix(board)
        if matrix is not None
        else _answer(_its(_slice(["ZZ1"]), price="USD150.00"), *_matrix_answer(board).solutions)
    )
    bodies: list[dict[str, Any]] = []

    def _gf(*_a: Any) -> list[Any]:
        if google_fails:
            raise GfThrottledError("rate-limited")
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
            if matrix_fails:
                raise MatrixApiError("boom", kind="internal")
            return answer

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    monkeypatch.setattr(cli, "MatrixClient", _Client)
    monkeypatch.setattr(cli, "console", Console(width=240))
    return bodies


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli.app, args)


def _merged_table(out: str) -> tuple[list[str], list[list[str]], str]:
    """The merged table's header cells, its numbered rows' cells, and the
    text under it."""
    _, after = out.split("Google Flights + Matrix", 1)
    lines = after.splitlines()
    header = next(ln for ln in lines if ln.strip().startswith("┃"))
    rows = [
        cells
        for ln in lines
        if ln.strip().startswith("│")
        and (cells := [c.strip() for c in ln.strip().strip("│").split("│")])[0].isdigit()
    ]
    end = next(i for i, ln in enumerate(lines) if ln.strip().startswith("└"))
    return (
        [c.strip() for c in header.strip().strip("┃").split("┃")],
        rows,
        " ".join(" ".join(lines[end + 1 :]).split()),
    )


def test_the_merged_table_shows_each_delta_or_why_and_where_matrixs_page_ends(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _weave(monkeypatch, gf_rows)
    result = _run([*_SEARCH, *_LINKLESS, "-n", "8"])
    assert result.exit_code == 0, result.output
    header, rows, under = _merged_table(result.stdout)
    assert header[:6] == ["#", "src", "Matrix", "Google", "delta", "why"]
    by_src: dict[str, list[list[str]]] = {}
    for r in rows:
        by_src.setdefault(r[1], []).append(r)
    priced = [r for r in by_src["GF+MX"] if r[4] != "—"]
    assert [(r[2], r[3], r[4]) for r in priced] == [("199.00", "204.00", "+5.00")] * 2
    assert [r[5] for r in by_src["GF+MX"] if r[4] == "—"] == [
        "trip unconfirmed: Google lands 11-04 08:58, Matrix 11-04 08:59"
    ]
    assert [r[5] for r in by_src["MX"]] == ["no ZZ flight on Google's board"]
    assert {r[5] for r in by_src["GF"]} == {"not in Matrix's answer of 4"}
    assert all(r[4] == "—" for r in by_src["GF"] + by_src["MX"])
    assert under.startswith(
        "Matrix listed 4 of 4 solutions (to USD199.00); Google listed 95 rows. "
        "delta = Google - Matrix."
    )


def _party_of_two(board: SearchResult) -> SearchResult:
    """DL1788, Google's own trip, as Matrix prices it for two: USD103 a
    passenger, its listed price rounded up, and USD203.60 for the party."""
    m = _as_matrix(board.solutions[0], "USD103.00")
    return _answer(m.model_copy(update={"display_total": "USD203.60"}))


def test_a_party_is_compared_on_matrixs_total(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Google prices the whole party (USD204 for two) and Matrix lists one
    passenger's price, so the Matrix column, the delta, the caption and the
    document all read Matrix's total for the party."""
    _weave(monkeypatch, gf_rows, matrix=_party_of_two)
    result = _run([*_SEARCH, *_LINKLESS, "--adults", "2", "-n", "3"])
    assert result.exit_code == 0, result.output
    _, rows, under = _merged_table(result.stdout)
    assert (rows[0][1], rows[0][2], rows[0][3], rows[0][4]) == (
        "GF+MX",
        "203.60",
        "204.00",
        "+0.40",
    )
    assert under.startswith("Matrix listed 1 of 1 solutions (to USD203.60);")
    doc = _document(_run([*_SEARCH, "--adults", "2", "-n", "3", "--enrich", "--format", "json"]))
    first = doc["cross_check"]["rows"][0]
    assert (first["matrix_price"], first["google_price"]) == ("USD203.60", "USD204.00")
    assert round(first["delta"], 2) == 0.40
    assert doc["cross_check"]["matrix"]["last_price"] == "USD203.60"


def _two_parties_of_two(board: SearchResult) -> SearchResult:
    """ZZ1 at USD70 a passenger and USD140 for two, then `_party_of_two`'s
    DL1788 at USD103 a passenger and USD203.60 for two."""
    zz1 = _its(_slice(["ZZ1"]), price="USD70.00").model_copy(update={"display_total": "USD140.00"})
    return _answer(zz1, *_party_of_two(board).solutions)


def test_a_party_cap_holds_matrix_to_the_total_the_table_prints(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """`--max-price` is compared with the printed price, which for a party is
    the total: DL1788 is USD103 a passenger but USD203.60 for two, over 150."""
    _weave(monkeypatch, gf_rows, matrix=_two_parties_of_two)
    capped = ["--adults", "2", "--max-price", "150", "-n", "3"]
    result = _run([*_SEARCH, *_LINKLESS, *capped])
    assert result.exit_code == 0, result.output
    _, rows, under = _merged_table(result.stdout)
    assert [r[2] for r in rows if r[2] != "—"] == ["140.00"]
    assert under.startswith("Matrix listed 1 of 1 solutions (to USD140.00);")
    xc = _document(_run([*_SEARCH, *capped, "--enrich", "--format", "json"]))["cross_check"]
    assert xc["matrix"]["listed"] == 1
    assert [r["matrix_price"] for r in xc["rows"] if r["matrix_price"]] == ["USD140.00"]


def _unread_by_the_cap(party: int) -> Callable[[SearchResult], SearchResult]:
    """DL1788 under the cap, then an AS fare the cap cannot read: in pounds for
    one passenger, with no total for a party. Matrix answered 40."""

    def answer(board: SearchResult) -> SearchResult:
        if party == 1:
            dl = _as_matrix(board.solutions[0], "USD199.00")
            unread = _its(_slice(["AS9"]), price="GBP90.00")
        else:
            dl = _party_of_two(board).solutions[0]
            unread = _its(_slice(["AS9"]), price="USD90.00")
        return _answer(dl, unread, count=40)

    return answer


@pytest.mark.parametrize("party", [1, 2])
def test_a_fare_the_cap_cannot_read_leaves_matrixs_answer_incomplete(
    party: int, monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Only a fare shown to be over the cap says the fares past the page are
    too; one the cap cannot read says nothing of them, nor of AS."""
    _weave(monkeypatch, gf_rows, matrix=_unread_by_the_cap(party))
    capped = ["--adults", str(party), "--max-price", "400", "-n", "30"]
    xc = _document(_run([*_SEARCH, *capped, "--enrich", "--format", "json"]))["cross_check"]
    assert (xc["matrix"]["listed"], xc["matrix"]["solution_count"]) == (1, 40)
    assert xc["matrix"]["complete"] is False
    by_flights = {"+".join(r["slices"][0]["flights"]): r for r in xc["rows"]}
    assert by_flights["AS21+AS487"]["reasons"] == ["past_page"]
    assert not [r for r in xc["rows"] if "carrier_absent" in r["reasons"]]


def _dearer_on_matrix(board: SearchResult) -> SearchResult:
    """DL2+DL3 at USD250, then DL1788, Google's own trip, at USD800."""
    return _answer(
        _its(_slice(["DL2", "DL3"]), price="USD250.00"),
        _as_matrix(board.solutions[0], "USD800.00"),
    )


def test_a_google_trip_the_cap_cut_from_matrix_names_matrixs_fare(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Google prices DL1788 at USD204 and Matrix at USD800, over a cap of
    400: the trip is in Matrix's answer, so its row says the cap cut Matrix's
    fare, not that Matrix lacks the trip."""
    _weave(monkeypatch, gf_rows, matrix=_dearer_on_matrix)
    capped = ["--max-price", "400", "-n", "30"]
    cut = "the price cap cut Matrix's fare for this trip, USD800.00"
    _, rows, _ = _merged_table(_run([*_SEARCH, *_LINKLESS, *capped]).stdout)
    assert [r[5] for r in rows if "DL1788" in r[6]] == [cut]
    xc = _document(_run([*_SEARCH, *capped, "--enrich", "--format", "json"]))["cross_check"]
    row = next(r for r in xc["rows"] if r["slices"][0]["flights"] == ["DL1788"])
    assert (row["source"], row["matrix_price"], row["reasons"], row["reason"]) == (
        "google",
        None,
        ["capped"],
        cut,
    )


def _three_flights_on_google(gf_rows: Callable[..., list[Any]]) -> Callable[..., list[Any]]:
    """`gf_rows` and AS21+AS600+AS9, a trip of three flights at USD214."""

    def build(name: str) -> list[Any]:
        rows = gf_rows(name)
        three = copy.deepcopy(rows[15])
        last = three.flight.legs[-1]
        three.flight.legs.append(
            last.model_copy(
                update={
                    "flight_number": "9",
                    "departure_datetime": last.arrival_datetime + timedelta(hours=1),
                    "arrival_datetime": last.arrival_datetime + timedelta(hours=2),
                }
            )
        )
        three.flight.stops = 2
        return [*rows, three]

    return build


def _nonstop_over_the_cap(board: SearchResult) -> SearchResult:
    """AS21+AS487, Google's own trip, at USD214, then DL1788, a nonstop, at
    USD800."""
    return _answer(
        _as_matrix(board.solutions[16], "USD214.00"),
        _as_matrix(board.solutions[0], "USD800.00"),
    )


def test_the_stop_window_is_measured_on_matrixs_page_before_the_cap(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Matrix listed a nonstop, so without a stop limit it searched up to two
    flights; a cap cutting that nonstop does not widen what Matrix searched."""
    _weave(monkeypatch, _three_flights_on_google(gf_rows), matrix=_nonstop_over_the_cap)
    capped = ["--max-price", "400", "-n", "30", "--enrich", "--format", "json"]
    xc = _document(_run([*_SEARCH, *capped]))["cross_check"]
    row = next(r for r in xc["rows"] if r["slices"][0]["flights"] == ["AS21", "AS600", "AS9"])
    assert (row["reasons"], row["reason"]) == (
        ["stops_outside"],
        "3 flights; Matrix searched up to 2",
    )


def test_a_party_matrix_states_no_total_for_shows_no_delta() -> None:
    """Matrix's price per passenger is not the party's price."""
    board = _board()
    matrix = _answer(_as_matrix(board.solutions[0], "USD103.00"))
    rows = [r for r in merge_results(board, matrix, currency="USD") if r.source == "both"]
    c = cross_check(rows, Answers(matrix, board, False, False, False, "USD", passengers=2)).rows[0]
    assert (rows[0].same_trip, c.delta, c.reasons, c.matrix_price) == (
        True,
        None,
        ("unpriced",),
        None,
    )


def _document(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


def test_enrich_writes_the_base_document_beside_the_cross_check(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    bodies = _weave(monkeypatch, gf_rows)
    base = _document(_run([*_SEARCH, "-n", "5", "--format", "json"]))
    assert bodies == []
    doc = _document(_run([*_SEARCH, "-n", "5", "--enrich", "--format", "json"]))
    assert [b["inputs"]["page"]["size"] for b in bodies] == [500]
    assert set(doc) == {"search", "cross_check"}
    assert doc["search"] == base
    xc = doc["cross_check"]
    assert xc["matrix"] == {
        "listed": 4,
        "solution_count": 4,
        "complete": True,
        "last_price": "USD199.00",
    }
    assert xc["google"] == {"listed": 95, "answered": True}
    assert [(r["source"], r["delta"], r["reasons"]) for r in xc["rows"]] == [
        ("matrix", None, ["carrier_absent_google"]),
        ("both", 5.0, []),
        ("both", 5.0, []),
        ("both", None, ["trip_unconfirmed"]),
        ("google", None, ["not_in_matrix"]),
    ]


@pytest.mark.parametrize("flag", [[], ["--fast"]])
def test_a_document_without_enrich_asks_matrix_nothing(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]], flag: list[str]
) -> None:
    bodies = _weave(monkeypatch, gf_rows)
    doc = _document(_run([*_SEARCH, "-n", "5", "--format", "json", *flag]))
    assert isinstance(doc, list)
    assert bodies == []


_ROOT = Path(__file__).resolve().parents[1]


def test_a_document_hands_a_failed_google_query_to_matrix_as_the_docs_say(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    """Plain `--format json` does not cross-check, but on auto a failed Google
    query still goes to Matrix; the skill and the memo, which tell an agent
    what a plain document asks Matrix, say so."""
    bodies = _weave(monkeypatch, gf_rows, google_fails=True)
    _document(_run([*_SEARCH, "-n", "5", "--format", "json"]))
    assert len(bodies) == 1
    for doc in (
        _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md",
        _ROOT / "docs" / "memories" / "gf_routing_and_carriers.md",
    ):
        text = " ".join(doc.read_text().split())
        assert "a failed Google query is still handed to Matrix" in text, doc


@pytest.mark.parametrize(
    ("args", "said"),
    [
        pytest.param(
            ["search", "JFK", "LAX", "--dep", _DEP.isoformat()],
            "cross-checks cash fares only; add --cash-only",
            id="awards",
        ),
        pytest.param([*_SEARCH, "--sellers"], "writes no booking options", id="sellers"),
    ],
)
def test_enrich_refuses_a_document_it_cannot_write(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    args: list[str],
    said: str,
) -> None:
    bodies = _weave(monkeypatch, gf_rows)

    def _awards(sel: Any) -> bool:
        return not sel.cash_only

    def _no_award_search(*_a: Any, **_kw: Any) -> None:
        pytest.fail("an award provider was searched")

    monkeypatch.setattr(cli, "_should_run_awards", _awards)
    # A refusal that stopped holding would otherwise search the real provider.
    monkeypatch.setattr(cli, "run_pp_for_search", _no_award_search)
    result = _run([*args, "--enrich", "--format", "json"])
    assert result.exit_code == 2, result.output
    assert said in " ".join(result.stderr.split())
    assert result.stdout == ""
    assert bodies == []


def test_a_failed_matrix_leaves_the_cross_check_null(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _weave(monkeypatch, gf_rows, matrix_fails=True)
    base = _document(_run([*_SEARCH, "-n", "5", "--format", "json"]))
    result = _run([*_SEARCH, "-n", "5", "--enrich", "--format", "json"])
    assert _document(result) == {"search": base, "cross_check": None}
    assert "boom" in result.stderr


def test_a_failed_google_leaves_the_search_empty_and_says_why_on_each_matrix_row(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _weave(monkeypatch, gf_rows, google_fails=True)
    result = _run([*_SEARCH, "-n", "5", "--enrich", "--format", "json"])
    doc = _document(result)
    assert doc["search"] == []
    assert doc["cross_check"]["google"] == {"listed": 0, "answered": False}
    assert {tuple(r["reasons"]) for r in doc["cross_check"]["rows"]} == {("no_google_answer",)}
    assert "Matrix's rows only" in result.stderr


def test_with_both_failed_stdout_is_empty_and_the_exit_is_1(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _weave(monkeypatch, gf_rows, google_fails=True, matrix_fails=True)
    result = _run([*_SEARCH, "-n", "5", "--enrich", "--format", "json"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "boom" in result.stderr


def test_bags_keep_the_document_off_matrix_and_say_so(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    bodies = _weave(monkeypatch, gf_rows)
    result = _run([*_SEARCH, "-n", "5", "--bags", "1", "--enrich", "--format", "json"])
    doc = _document(result)
    assert isinstance(doc, list)
    assert "No Matrix enrichment: Matrix prices no bags." in result.stderr
    assert bodies == []
