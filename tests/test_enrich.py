# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/pp/test_match.py.
"""Tests for reconciling GF + Matrix cash results (_enrich.merge_results)."""

from __future__ import annotations

from flight_cli._enrich import merge_results
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
    SliceEndpoint,
)


def _it(price: str, flights: list[str], dep: str = "2026-08-15T08:00") -> Itinerary:
    return Itinerary(
        ext=ItineraryExt(price=price),
        itinerary=ItineraryDetails(slices=[Slice(flights=flights, departure=dep)]),
    )


def _sr(*its: Itinerary) -> SearchResult:
    return SearchResult(solutionCount=len(its), solutions=list(its))


def _first_flight(it: Itinerary) -> str | None:
    itn = it.itinerary
    return itn.slices[0].flights[0] if itn and itn.slices else None


def test_matched_itinerary_carries_both_prices_matrix_authoritative() -> None:
    gf = _sr(_it("USD500.00", ["LH455"], dep="2026-08-15T08:00"))
    # Same flight + date, different time + price -> still a match (flight# + date).
    matrix = _sr(_it("USD505.00", ["LH455"], dep="2026-08-15T09:30"))
    (row,) = merge_results(gf, matrix, currency="USD")
    assert row.source == "both"
    assert row.gf_price == "USD500.00"
    assert row.matrix_price == "USD505.00"
    assert row.itinerary.price == "USD505.00"  # Matrix structure is authoritative


def test_matrix_only_row() -> None:
    (row,) = merge_results(_sr(), _sr(_it("USD600.00", ["AF83"])), currency="USD")
    assert row.source == "matrix"
    assert row.matrix_price == "USD600.00"
    assert row.gf_price is None


def test_gf_only_row() -> None:
    (row,) = merge_results(_sr(_it("USD380.00", ["UA58"])), _sr(), currency="USD")
    assert row.source == "gf"
    assert row.gf_price == "USD380.00"
    assert row.matrix_price is None


def test_merge_sorts_by_best_price_and_tags_sources() -> None:
    gf = _sr(_it("USD380.00", ["UA58"]), _it("USD500.00", ["LH455"]))
    matrix = _sr(_it("USD505.00", ["LH455"]), _it("USD900.00", ["AF83"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [(r.source, _first_flight(r.itinerary)) for r in rows] == [
        ("gf", "UA58"),  # 380 — GF-only (ULCC/codeshare)
        ("both", "LH455"),  # 500/505 — matched
        ("matrix", "AF83"),  # 900 — Matrix-only
    ]


def test_a_fare_in_another_currency_ranks_after_the_requested_ones() -> None:
    """Matrix priced in GBP and Google in USD. By bare numbers GBP1027 ranks
    above USD1043 (about GBP780); ranked in the requested USD, the Google fare
    leads, the matched row follows on its USD price, and the Matrix-only GBP
    fare comes after every USD one."""
    gf = _sr(_it("USD1330.00", ["VS45"]), _it("USD1043.00", ["TK1988", "TK1"]))
    matrix = _sr(_it("GBP1004.00", ["VS45"]), _it("GBP1027.00", ["AA101"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [(r.source, _first_flight(r.itinerary)) for r in rows] == [
        ("gf", "TK1988"),
        ("both", "VS45"),
        ("matrix", "AA101"),
    ]
    assert (rows[1].matrix_price, rows[1].gf_price) == ("GBP1004.00", "USD1330.00")


def test_a_one_currency_merge_orders_by_amount_whatever_was_asked_for() -> None:
    gf = _sr(_it("GBP380.00", ["UA58"]), _it("GBP500.00", ["LH455"]))
    matrix = _sr(_it("GBP505.00", ["LH455"]), _it("GBP90.00", ["AF83"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [_first_flight(r.itinerary) for r in rows] == ["AF83", "UA58", "LH455"]


def test_unkeyed_itineraries_stay_single_source() -> None:
    # No flights -> unmatchable -> kept as a single-source row, not merged.
    gf = _sr(_it("USD100.00", []))
    matrix = _sr(_it("USD100.00", []))
    rows = merge_results(gf, matrix, currency="USD")
    assert len(rows) == 2
    assert {r.source for r in rows} == {"gf", "matrix"}


def test_empty_inputs() -> None:
    assert merge_results(_sr(), _sr(), currency="USD") == []


# ───────── a matched row takes Google's per-flight dates ─────────

_D, _D1 = "2026-10-20", "2026-10-21"


def _nz(arrival: str = f"{_D}T10:00", segment_dates: list[str] | None = None) -> Slice:
    """NZ104 SYD-AKL, then NZ10 AKL-HNL, which leaves Auckland after midnight
    and lands in Honolulu on the day the trip began."""
    return Slice(
        flights=["NZ104", "NZ10"],
        departure=f"{_D}T18:00",
        arrival=arrival,
        origin=SliceEndpoint(code="SYD"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="AKL")],
        segment_dates=segment_dates or [],
    )


def _nz_row(price: str, s: Slice, sid: str | None = None) -> Itinerary:
    return Itinerary(id=sid, ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=[s]))


def test_a_matched_matrix_connection_takes_googles_flight_dates() -> None:
    gf = _sr(_nz_row("USD900.00", _nz(segment_dates=[_D, _D1])))
    matrix = _sr(_nz_row("USD880.00", _nz(), sid="sol-1"))
    (row,) = merge_results(gf, matrix, currency="USD")
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    assert row.itinerary.itinerary.slices[0].segment_dates == [_D, _D1]
    # Still Matrix's itinerary: its id pins the Matrix link, its price is Matrix's.
    assert row.itinerary.id == "sol-1"
    assert row.itinerary.price == "USD880.00"


def test_the_merge_leaves_both_results_as_they_were() -> None:
    gf = _sr(_nz_row("USD900.00", _nz(segment_dates=[_D, _D1])))
    matrix = _sr(_nz_row("USD880.00", _nz(), sid="sol-1"))
    before = (gf.model_dump(), matrix.model_dump())
    merge_results(gf, matrix, currency="USD")
    assert (gf.model_dump(), matrix.model_dump()) == before


def test_a_google_row_landing_on_another_day_lends_no_dates() -> None:
    """Same flights leaving the same day share the match key, but a Google row
    whose NZ10 leaves a day later does not date Matrix's NZ10."""
    late = _nz(arrival=f"{_D1}T10:00", segment_dates=[_D, "2026-10-22"])
    (row,) = merge_results(
        _sr(_nz_row("USD900.00", late)), _sr(_nz_row("USD880.00", _nz())), currency="USD"
    )
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    assert row.itinerary.itinerary.slices[0].segment_dates == []


def _ua(arrival: str, segment_dates: list[str] | None = None) -> Slice:
    """UA100 SFO-DEN, then UA200 DEN-EWR, landing on 2026-11-02 either way:
    the same day's red-eye or the next morning's flight."""
    return Slice(
        flights=["UA100", "UA200"],
        departure="2026-11-01T07:00",
        arrival=arrival,
        origin=SliceEndpoint(code="SFO"),
        destination=SliceEndpoint(code="EWR"),
        stops=[SliceEndpoint(code="DEN")],
        segment_dates=segment_dates or [],
    )


def _dates_lent(google_arrival: str, matrix_arrival: str) -> list[str]:
    google = _ua(google_arrival, segment_dates=["2026-11-01", "2026-11-01"])
    (row,) = merge_results(
        _sr(_nz_row("USD500.00", google)),
        _sr(_nz_row("USD480.00", _ua(matrix_arrival))),
        currency="USD",
    )
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    return row.itinerary.itinerary.slices[0].segment_dates


def test_a_google_row_landing_at_another_time_that_day_lends_no_dates() -> None:
    """Google's UA200 is the red-eye leaving on the 1st; Matrix's lands at
    13:30, so it is the next morning's, and the 1st would pin the red-eye."""
    assert _dates_lent("2026-11-02T02:45:00", "2026-11-02T13:30-05:00") == []


def test_a_google_row_landing_at_the_same_minute_lends_its_dates() -> None:
    """Google writes local time with no offset, Matrix with one."""
    assert _dates_lent("2026-11-02T13:30:00", "2026-11-02T13:30-05:00") == [
        "2026-11-01",
        "2026-11-01",
    ]


def test_a_one_minute_skew_between_the_sources_lends_no_dates() -> None:
    assert _dates_lent("2026-11-02T13:31:00", "2026-11-02T13:30-05:00") == []


def test_the_google_row_that_is_the_same_trip_lends_its_dates_whatever_its_place() -> None:
    """Two Google rows share the Matrix row's match key; only the second is its
    trip. It lends the dates and the Google price, in either order."""
    late = _nz_row("USD900.00", _nz(arrival=f"{_D1}T10:00:00", segment_dates=[_D, "2026-10-22"]))
    same = _nz_row("USD910.00", _nz(arrival=f"{_D}T10:00:00", segment_dates=[_D, _D1]))
    matrix = _sr(_nz_row("USD880.00", _nz(arrival=f"{_D}T10:00-10:00"), sid="sol-1"))
    for google in (_sr(late, same), _sr(same, late)):
        (row,) = merge_results(google, matrix, currency="USD")
        assert row.source == "both"
        assert row.itinerary.itinerary is not None
        assert row.itinerary.itinerary.slices[0].segment_dates == [_D, _D1]
        assert row.gf_price == "USD910.00"


def _three_flights(segment_dates: list[str] | None = None) -> Slice:
    return Slice(
        flights=["AA1", "AA2", "AA3"],
        departure="2026-11-01T07:00",
        arrival="2026-11-03T09:00",
        segment_dates=segment_dates or [],
    )


def test_two_google_rows_that_are_the_trip_on_other_days_lend_no_dates() -> None:
    """Both land at Matrix's minute, but AA2 leaves on another day in each, and
    Matrix states no day for it: neither row can say which one Matrix's is."""
    a = _nz_row("USD700.00", _three_flights(["2026-11-01", "2026-11-01", "2026-11-03"]))
    b = _nz_row("USD720.00", _three_flights(["2026-11-01", "2026-11-02", "2026-11-03"]))
    matrix = _sr(_nz_row("USD690.00", _three_flights()))
    for google in (_sr(a, b), _sr(b, a)):
        (row,) = merge_results(google, matrix, currency="USD")
        assert row.source == "both"
        assert row.itinerary.itinerary is not None
        assert row.itinerary.itinerary.slices[0].segment_dates == []
