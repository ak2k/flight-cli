# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Tests for mapping Tier-1 predicates onto fli's native FlightSearchFilters."""

from __future__ import annotations

from typing import Any

from fli.models.airport import Airport
from fli.models.google_flights.base import MaxStops, SeatType, TripType
from fli.models.google_flights.flights import (
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
)

from flight_cli.fli_bridge import apply_gf_native_filters
from flight_cli.routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
    classify,
)


def _filters() -> Any:
    return FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.JFK, 0]],
                arrival_airport=[[Airport.LHR, 0]],
                travel_date="2026-08-15",
            )
        ],
        stops=MaxStops.ANY,
        seat_type=SeatType.ECONOMY,
        trip_type=TripType.ONE_WAY,
    )


def _names(airlines: Any) -> list[str]:
    return sorted(a.name for a in airlines)


def test_marketing_carrier_include_maps_to_airlines() -> None:
    f = _filters()
    lh = CarrierPred(frozenset({"LH"}), exclude=False, operating=False)
    assert apply_gf_native_filters(f, [lh])
    assert _names(f.airlines) == ["LH"]
    assert f.encode()  # the mapped filter still serializes to a valid TFS request


def test_alliance_maps_to_airline_token() -> None:
    f = _filters()
    assert apply_gf_native_filters(f, [AlliancePred(frozenset({"star-alliance"}))])
    assert _names(f.airlines) == ["STAR_ALLIANCE"]
    assert f.encode()


def test_connect_airport_and_max_layover_map_to_layover_restrictions() -> None:
    f = _filters()
    preds = [
        ConnectionAirportPred(frozenset({"FRA"}), exclude=False),
        ConnectTimePred(min_minutes=None, max_minutes=120),
    ]
    assert apply_gf_native_filters(f, preds)
    assert [a.name for a in f.layover_restrictions.airports] == ["FRA"]
    assert f.layover_restrictions.max_duration == 120
    assert f.encode()


def test_nonstop_and_maxdur_map_to_stops_and_duration() -> None:
    f = _filters()
    assert apply_gf_native_filters(f, [StopsPred(max_stops=0), MaxDurationPred(minutes=600)])
    assert f.stops is MaxStops.NON_STOP
    assert f.max_duration == 600


def test_unknown_carrier_code_returns_false() -> None:
    f = _filters()
    # 'XX' isn't a real IATA carrier in fli's enum -> can't map -> escalate.
    xx = CarrierPred(frozenset({"XX"}), exclude=False, operating=False)
    assert not apply_gf_native_filters(f, [xx])


def test_unknown_airport_code_returns_false() -> None:
    f = _filters()
    zzz = ConnectionAirportPred(frozenset({"ZZZ"}), exclude=False)
    assert not apply_gf_native_filters(f, [zzz])


def test_tier2_predicates_are_ignored_by_native_mapper() -> None:
    f = _filters()
    preds = [
        CarrierPred(frozenset({"UA"}), exclude=True, operating=False),  # exclude -> Tier 2
        CarrierPred(frozenset({"LH"}), exclude=False, operating=True),  # operating -> Tier 2
        ConnectionAirportPred(frozenset({"DFW"}), exclude=True),  # exclude -> Tier 2
    ]
    assert apply_gf_native_filters(f, preds)
    assert f.airlines is None  # nothing native applied
    assert f.layover_restrictions is None


def test_integration_classify_then_apply() -> None:
    c = classify("LH+", "MAXSTOPS 1; MAXCONNECT 2:00")
    f = _filters()
    assert apply_gf_native_filters(f, c.predicates)
    assert _names(f.airlines) == ["LH"]
    assert f.stops is MaxStops.ONE_STOP_OR_FEWER
    assert f.layover_restrictions.max_duration == 120
    assert f.encode()
