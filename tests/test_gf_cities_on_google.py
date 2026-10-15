# pyright: reportCallIssue=false
# DIVERGE: the response models' aliased fields read as required kwargs; same as
# tests/test_gf_postfilter.py.
"""`-CITIES` is served on the Google search page, its board post-filtered.

Matrix, logged out, measured it as "never connect at these cities": BTR-SFO
with `-CITIES DFW` kept nine rows, all via IAH; BOS-SFO with `-CITIES ORD` kept
all ten nonstops. The routing-language form `F* ~DUB F*` is positional (one
connection, not at DUB; nonstops dropped), so it stays on Matrix.
"""

from __future__ import annotations

import pytest

from flight_cli._gf_postfilter import apply_postfilter, search_page_reasons
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    LegInfo,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.routing_predicates import classify


def _result(*slices: Slice) -> SearchResult:
    sols = [
        Itinerary(ext=ItineraryExt(price="USD100.00"), itinerary=ItineraryDetails(slices=[s]))
        for s in slices
    ]
    return SearchResult(solutionCount=len(sols), solutions=sols)


def _via(flight: str, *stops: str) -> Slice:
    return Slice(
        flights=[flight],
        stops=[SliceEndpoint(code=s) for s in stops],
        legs=[LegInfo(operating_carrier="AA", marketing_carriers=["AA"])],
    )


def test_a_cities_exclusion_is_served_on_the_google_page() -> None:
    assert search_page_reasons(classify(None, "-CITIES DFW").predicates) == []


def test_a_cities_exclusion_keeps_nonstops_and_drops_the_named_connection() -> None:
    result = _result(_via("AA1"), _via("AA2", "DFW"), _via("AA3", "IAH"))
    predicates = classify(None, "-CITIES DFW").predicates
    kept = apply_postfilter(result, [list(predicates)])
    flights = [
        it.itinerary.slices[0].flights[0] for it in kept.solutions if it.itinerary is not None
    ]
    assert flights == ["AA1", "AA3"]


@pytest.mark.parametrize(
    ("extension", "named"),
    [("-CITIES DUB,LHR", "DUB,LHR"), ("-CITIES DFW DUB,LHR", "DFW, DUB,LHR")],
)
def test_a_cities_code_no_row_can_carry_goes_to_matrix(extension: str, named: str) -> None:
    """A row's stop is one airport, so a comma list would drop nothing on the
    page; how Matrix reads it is not measured."""
    reasons = search_page_reasons(classify(None, extension).predicates)
    assert reasons == [f"a connecting-airport exclusion ({named})"]


@pytest.mark.parametrize(
    ("extension", "named"), [("-CITIES NYC", "NYC"), ("-CITIES DFW LON", "DFW, LON")]
)
def test_a_cities_metro_code_goes_to_matrix(extension: str, named: str) -> None:
    """A row names a metro's airport (JFK, not NYC), so the filter would keep
    every connection in it; how Matrix reads a metro code is not measured."""
    reasons = search_page_reasons(classify(None, extension).predicates)
    assert reasons == [f"a connecting-airport exclusion ({named})"]


def test_the_routing_form_of_a_connection_exclusion_still_goes_to_matrix() -> None:
    reasons = search_page_reasons(classify("F* ~DUB F*", None).predicates)
    assert reasons == ["a connecting-airport exclusion (DUB)"]
