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
