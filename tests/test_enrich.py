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
    (row,) = merge_results(gf, matrix)
    assert row.source == "both"
    assert row.gf_price == "USD500.00"
    assert row.matrix_price == "USD505.00"
    assert row.itinerary.price == "USD505.00"  # Matrix structure is authoritative


def test_matrix_only_row() -> None:
    (row,) = merge_results(_sr(), _sr(_it("USD600.00", ["AF83"])))
    assert row.source == "matrix"
    assert row.matrix_price == "USD600.00"
    assert row.gf_price is None


def test_gf_only_row() -> None:
    (row,) = merge_results(_sr(_it("USD380.00", ["UA58"])), _sr())
    assert row.source == "gf"
    assert row.gf_price == "USD380.00"
    assert row.matrix_price is None


def test_merge_sorts_by_best_price_and_tags_sources() -> None:
    gf = _sr(_it("USD380.00", ["UA58"]), _it("USD500.00", ["LH455"]))
    matrix = _sr(_it("USD505.00", ["LH455"]), _it("USD900.00", ["AF83"]))
    rows = merge_results(gf, matrix)
    assert [(r.source, _first_flight(r.itinerary)) for r in rows] == [
        ("gf", "UA58"),  # 380 — GF-only (ULCC/codeshare)
        ("both", "LH455"),  # 500/505 — matched
        ("matrix", "AF83"),  # 900 — Matrix-only
    ]


def test_unkeyed_itineraries_stay_single_source() -> None:
    # No flights -> unmatchable -> kept as a single-source row, not merged.
    gf = _sr(_it("USD100.00", []))
    matrix = _sr(_it("USD100.00", []))
    rows = merge_results(gf, matrix)
    assert len(rows) == 2
    assert {r.source for r in rows} == {"gf", "matrix"}


def test_empty_inputs() -> None:
    assert merge_results(_sr(), _sr()) == []
