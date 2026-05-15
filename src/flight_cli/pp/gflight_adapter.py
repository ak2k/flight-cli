# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs even though populate_by_name=True is
# set. Same posture as tests/pp/test_match.py.
"""Adapt fli's Google Flights output into the SearchResult shape match.py expects.

The matcher reads structural fields only — slices[i].flights[0], .departure,
.origin.code, .destination.code, Itinerary.price — so a thin wrap-and-translate
layer is enough; no matcher changes needed. PP runs with enable_matching=False
on this path; the same (flight#, date) + (route, time) keys that work for the
Matrix backend bridge the cash↔award join here too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
    SliceEndpoint,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _airport_code(a: Any) -> str:
    name: str = getattr(a, "name", "") or ""
    return name.removeprefix("_")


def _slice_from_flight_result(fr: Any) -> Slice:
    legs: list[Any] = list(fr.legs)
    first, last = legs[0], legs[-1]
    return Slice(
        flights=[leg.flight_number for leg in legs],
        departure=first.departure_datetime.isoformat(),
        arrival=last.arrival_datetime.isoformat(),
        duration=fr.duration,
        origin=SliceEndpoint(code=_airport_code(first.departure_airport)),
        destination=SliceEndpoint(code=_airport_code(last.arrival_airport)),
    )


def _price_string(fr: Any) -> str:
    """Match Matrix's price format ('USD877.00') so match._parse_cash works."""
    currency = fr.currency or "USD"
    return f"{currency}{fr.price:.2f}"


def fli_results_to_search_result(results: Sequence[Any]) -> SearchResult:
    """Wrap fli's heterogeneous return into a SearchResult.

    fli returns ``list[FlightResult]`` for one-way and ``list[tuple[FlightResult, ...]]``
    for round-trip/multi-city. Each top-level entry maps to one Itinerary; for
    tuples, each FlightResult becomes one Slice in slice-index order. The
    cheapest-cash price for the itinerary uses the outbound leg's price
    (round-trip prices in fli are attached per-result; the outbound carries
    the combined fare on round-trip queries).
    """
    solutions: list[Itinerary] = []
    cheapest_price: float | None = None
    cheapest_currency: str = "USD"
    for r in results:
        items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
        if not items:
            continue
        slices = [_slice_from_flight_result(fr) for fr in items]
        price_str = _price_string(items[0])
        solutions.append(
            Itinerary(
                ext=ItineraryExt(price=price_str),
                itinerary=ItineraryDetails(slices=slices, carriers=[]),
            ),
        )
        p: float = items[0].price
        if cheapest_price is None or p < cheapest_price:
            cheapest_price = p
            cheapest_currency = items[0].currency or "USD"

    sr = SearchResult(
        solutionCount=len(solutions),
        solutions=solutions,
    )
    if cheapest_price is not None:
        sr.currency_notice.ext = ItineraryExt(
            price=f"{cheapest_currency}{cheapest_price:.2f}",
        )
    return sr
