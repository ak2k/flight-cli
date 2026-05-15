# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportCallIssue=false
# DIVERGE: pydantic alias fields (e.g. displayTotal, solutionCount) trip
# basedpyright into thinking aliases are required kwargs even with
# populate_by_name=True. Same posture as tests/pp/test_match.py.
"""Tests for the fli → SearchResult adapter.

The adapter is the bridge that lets match.py join PP awards against
Google-Flights cash itineraries. The matcher only reads structural fields
(slices[i].flights[0], .departure, .origin.code, .destination.code) plus
the price string — so this test pins those exact fields end-to-end."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from flight_cli.pp.gflight_adapter import fli_results_to_search_result


def _leg(
    flight_number: str,
    dep_iata: str,
    arr_iata: str,
    dep: datetime,
    arr: datetime,
    airline_iata: str = "AA",
) -> SimpleNamespace:
    """Mimic fli FlightLeg with duck-typed attributes (airline/airport are
    Enum-like; we just need .name with optional leading underscore stripping).

    The adapter concatenates the airline IATA with `flight_number` to produce
    Matrix's-format slice.flights (e.g. 'AA100'), so tests pass bare numbers
    here as `flight_number` and the IATA via `airline_iata`."""
    return SimpleNamespace(
        airline=SimpleNamespace(name=airline_iata),
        flight_number=flight_number,
        departure_airport=SimpleNamespace(name=dep_iata),
        arrival_airport=SimpleNamespace(name=arr_iata),
        departure_datetime=dep,
        arrival_datetime=arr,
        duration=(arr - dep).seconds // 60,
    )


def _result(price: float, *legs: Any, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        legs=list(legs),
        price=price,
        currency=currency,
        duration=sum(leg.duration for leg in legs),
        stops=len(legs) - 1,
    )


def test_one_way_single_leg_maps_to_one_slice() -> None:
    fr = _result(
        877.0,
        _leg(
            "100",
            "JFK",
            "LHR",
            datetime(2026, 8, 15, 19, 0),
            datetime(2026, 8, 16, 7, 0),
            airline_iata="AA",
        ),
    )
    sr = fli_results_to_search_result([fr])
    assert sr.solution_count == 1
    it = sr.solutions[0]
    assert it.price == "USD877.00"
    assert it.itinerary is not None
    slices = it.itinerary.slices
    assert len(slices) == 1
    s = slices[0]
    # slice.flights[0] is IATA-prefixed: matches Matrix's format so the
    # (flight#, date) matcher key joins across backends.
    assert s.flights == ["AA100"]
    assert s.departure == "2026-08-15T19:00:00"
    assert s.origin is not None and s.origin.code == "JFK"
    assert s.destination is not None and s.destination.code == "LHR"


def test_round_trip_tuple_maps_to_two_slices() -> None:
    out = _result(
        1078.0,
        _leg(
            "100",
            "JFK",
            "LHR",
            datetime(2026, 8, 15, 19, 0),
            datetime(2026, 8, 16, 7, 0),
            airline_iata="AA",
        ),
    )
    ret = _result(
        1078.0,
        _leg(
            "101",
            "LHR",
            "JFK",
            datetime(2026, 8, 22, 11, 0),
            datetime(2026, 8, 22, 14, 0),
            airline_iata="AA",
        ),
    )
    sr = fli_results_to_search_result([(out, ret)])
    assert sr.solution_count == 1
    it = sr.solutions[0]
    assert it.itinerary is not None
    slices = it.itinerary.slices
    assert len(slices) == 2
    assert slices[0].flights == ["AA100"]
    assert slices[1].flights == ["AA101"]
    assert slices[1].origin is not None and slices[1].origin.code == "LHR"
    assert slices[1].destination is not None and slices[1].destination.code == "JFK"


def test_connection_slice_flattens_all_flight_numbers_first_origin_last_dest() -> None:
    """Connecting itinerary: slice.flights lists every leg; origin/destination
    are first/last leg's airports (so the (route, time) fallback key works)."""
    fr = _result(
        450.0,
        _leg(
            "100",
            "JFK",
            "BOS",
            datetime(2026, 8, 15, 9, 0),
            datetime(2026, 8, 15, 10, 30),
            airline_iata="B6",
        ),
        _leg(
            "200",
            "BOS",
            "LHR",
            datetime(2026, 8, 15, 17, 0),
            datetime(2026, 8, 16, 5, 0),
            airline_iata="B6",
        ),
    )
    sr = fli_results_to_search_result([fr])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.flights == ["B6100", "B6200"]
    assert s.origin is not None and s.origin.code == "JFK"
    assert s.destination is not None and s.destination.code == "LHR"
    assert s.departure == "2026-08-15T09:00:00"


def test_underscore_prefixed_airport_codes_stripped() -> None:
    """fli prefixes numeric airport codes with `_` (since Python enum names
    can't start with a digit). Adapter strips that so codes match Matrix."""
    fr = _result(
        300.0,
        _leg(
            "WN100",
            "_4U",  # imagined numeric-leading IATA
            "JFK",
            datetime(2026, 8, 15, 7, 0),
            datetime(2026, 8, 15, 11, 0),
        ),
    )
    sr = fli_results_to_search_result([fr])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.origin is not None and s.origin.code == "4U"


def test_cheapest_price_tracks_min_across_results() -> None:
    a = _result(
        500.0,
        _leg("AA1", "JFK", "LAX", datetime(2026, 8, 15, 8, 0), datetime(2026, 8, 15, 11, 0)),
    )
    b = _result(
        320.0,
        _leg("DL1", "JFK", "LAX", datetime(2026, 8, 15, 9, 0), datetime(2026, 8, 15, 12, 0)),
    )
    sr = fli_results_to_search_result([a, b])
    assert sr.cheapest_price == "USD320.00"


def test_empty_results_yield_empty_search_result() -> None:
    sr = fli_results_to_search_result([])
    assert sr.solution_count == 0
    assert sr.solutions == []
    assert sr.cheapest_price is None


def test_gflightwithid_populates_slice_flight_id() -> None:
    """When given our `GFlightWithId` wrapper, the adapter pulls the opaque
    flight_id through onto each Slice — the data the PP provider then turns
    into a CashFlightHint."""
    g = SimpleNamespace(
        flight=_result(
            877.0,
            _leg(
                "1",
                "JFK",
                "LHR",
                datetime(2026, 8, 15, 19, 0),
                datetime(2026, 8, 16, 7, 0),
                airline_iata="DL",
            ),
        ),
        flight_id="MWRvrf",
    )
    sr = fli_results_to_search_result([g])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.flight_id == "MWRvrf"
    assert s.flights == ["DL1"]


def test_cash_hints_from_search_result_shape() -> None:
    """The hints generator outputs the exact shape PP's airline-search wants:
    IATA-prefixed firstFlightNumber, human-readable airline, space-separated
    times, opaque flight_id passed through verbatim. Verified empirically
    against the live extension capture."""
    from flight_cli.pp.gflight_adapter import cash_hints_from_search_result

    g = SimpleNamespace(
        flight=_result(
            802.0,
            _leg(
                "6",
                "MIA",
                "LHR",
                datetime(2026, 6, 30, 18, 5),
                datetime(2026, 7, 1, 8, 5),
                airline_iata="VS",
            ),
        ),
        flight_id="NbXSYb",
    )
    sr = fli_results_to_search_result([g])
    hints = cash_hints_from_search_result(sr)
    assert len(hints) == 1
    h = hints[0]
    # Exact PP-expected format (per research/capture/pp_extension_capture.json)
    assert h.flight_id == "NbXSYb"
    assert h.first_flight_number == "VS6"  # IATA-prefixed
    assert h.airline == "Virgin Atlantic"  # human-readable
    assert h.google_airlines == ["Virgin Atlantic"]
    assert h.origin == "MIA"
    assert h.dest == "LHR"
    assert h.start_dt == "2026-06-30 18:05"  # space-separated
    assert h.end_dt == "2026-07-01 08:05"
    assert h.cash_price_usd == 802


def test_cash_hints_skip_slices_without_flight_id() -> None:
    """Matrix-backend SearchResults have Slice.flight_id=None; the hint
    builder skips those (nothing to match on)."""
    from flight_cli.models import (
        Itinerary,
        ItineraryDetails,
        ItineraryExt,
        SearchResult,
        Slice,
        SliceEndpoint,
    )
    from flight_cli.pp.gflight_adapter import cash_hints_from_search_result

    sr = SearchResult(
        solutions=[
            Itinerary(
                ext=ItineraryExt(price="USD500.00"),
                itinerary=ItineraryDetails(
                    slices=[
                        Slice(
                            flights=["UA146"],
                            departure="2026-06-09T22:00:00",
                            origin=SliceEndpoint(code="JFK"),
                            destination=SliceEndpoint(code="LHR"),
                            # flight_id intentionally None — Matrix cash
                        ),
                    ],
                ),
            ),
        ],
    )
    hints = cash_hints_from_search_result(sr)
    assert hints == []
