# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
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
) -> SimpleNamespace:
    """Mimic fli FlightLeg with duck-typed attributes (airline/airport are
    Enum-like; we just need .name with optional leading underscore stripping)."""
    return SimpleNamespace(
        airline=SimpleNamespace(name="AA"),
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
        _leg("AA100", "JFK", "LHR", datetime(2026, 8, 15, 19, 0), datetime(2026, 8, 16, 7, 0)),
    )
    sr = fli_results_to_search_result([fr])
    assert sr.solution_count == 1
    it = sr.solutions[0]
    assert it.price == "USD877.00"
    assert it.itinerary is not None
    slices = it.itinerary.slices
    assert len(slices) == 1
    s = slices[0]
    assert s.flights == ["AA100"]
    assert s.departure == "2026-08-15T19:00:00"
    assert s.origin is not None and s.origin.code == "JFK"
    assert s.destination is not None and s.destination.code == "LHR"


def test_round_trip_tuple_maps_to_two_slices() -> None:
    out = _result(
        1078.0,
        _leg("AA100", "JFK", "LHR", datetime(2026, 8, 15, 19, 0), datetime(2026, 8, 16, 7, 0)),
    )
    ret = _result(
        1078.0,
        _leg("AA101", "LHR", "JFK", datetime(2026, 8, 22, 11, 0), datetime(2026, 8, 22, 14, 0)),
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
        _leg("B6100", "JFK", "BOS", datetime(2026, 8, 15, 9, 0), datetime(2026, 8, 15, 10, 30)),
        _leg("B6200", "BOS", "LHR", datetime(2026, 8, 15, 17, 0), datetime(2026, 8, 16, 5, 0)),
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
