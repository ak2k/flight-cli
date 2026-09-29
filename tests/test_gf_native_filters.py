# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Tests for mapping Tier-1 predicates onto fli's native FlightSearchFilters."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fli.models.airport import Airport
from fli.models.google_flights.base import MaxStops, SeatType, TripType
from fli.models.google_flights.flights import (
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
)

from flight_cli.domain import Leg, SpecificDateSearch, TimeOfDay
from flight_cli.fli_bridge import apply_gf_native_filters, to_fli_filter, unmappable_codes
from flight_cli.routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
    classify,
)

# fli's FlightSegment validator rejects a past travel date, so the fixture date
# is derived from today rather than pinned — a literal rots the suite the day
# it passes.
_TRAVEL_DATE = (date.today() + timedelta(days=45)).isoformat()


def _filters(stops: Any = MaxStops.ANY) -> Any:
    return FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport["JFK"], 0]],
                arrival_airport=[[Airport["LHR"], 0]],
                travel_date=_TRAVEL_DATE,
            )
        ],
        stops=stops,
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


# ─────────────────────────── strictest stop limit ──────────────────────────


@pytest.mark.parametrize(
    "stops,limits,expected",
    [
        # `--stops 0 --ext "MAXSTOPS 2"` searched two-or-fewer: the last writer won.
        (MaxStops.NON_STOP, [2], MaxStops.NON_STOP),
        (MaxStops.ANY, [1], MaxStops.ONE_STOP_OR_FEWER),
        (MaxStops.ONE_STOP_OR_FEWER, [0], MaxStops.NON_STOP),
        (MaxStops.ANY, [2, 0], MaxStops.NON_STOP),
        (MaxStops.ANY, [0, 2], MaxStops.NON_STOP),
        (MaxStops.ANY, [3], MaxStops.ANY),
        (MaxStops.TWO_OR_FEWER_STOPS, [3], MaxStops.TWO_OR_FEWER_STOPS),
    ],
)
def test_the_strictest_stop_limit_wins(stops: Any, limits: list[int], expected: Any) -> None:
    f = _filters(stops)
    assert apply_gf_native_filters(f, [StopsPred(max_stops=n) for n in limits])
    assert f.stops is expected


def test_no_stop_predicate_leaves_the_stops_flag_alone() -> None:
    f = _filters(MaxStops.NON_STOP)
    assert apply_gf_native_filters(f, [MaxDurationPred(minutes=600)])
    assert f.stops is MaxStops.NON_STOP


# ─────────────────────────── layover bounds ────────────────────────────────


def test_a_minimum_layover_maps_to_the_layover_minimum() -> None:
    f = _filters()
    assert apply_gf_native_filters(f, classify(None, "MINCONNECT 2:00").predicates)
    assert f.layover_restrictions.min_duration == 120
    assert f.layover_restrictions.max_duration is None


def test_both_layover_bounds_map_and_the_higher_minimum_wins() -> None:
    f = _filters()
    preds = classify(None, "MINCONNECT 1:00; MINCONNECT 1:30; MAXCONNECT 4:00").predicates
    assert apply_gf_native_filters(f, preds)
    assert (f.layover_restrictions.min_duration, f.layover_restrictions.max_duration) == (90, 240)


def test_a_zero_minimum_layover_writes_nothing() -> None:
    """It constrains nothing, and fli's field takes only a positive number."""
    f = _filters()
    assert apply_gf_native_filters(f, classify(None, "MINCONNECT 0:00").predicates)
    assert f.layover_restrictions is None


def test_a_minimum_above_the_maximum_is_not_written() -> None:
    f = _filters()
    preds = classify(None, "MINCONNECT 3:00; MAXCONNECT 2:00").predicates
    assert apply_gf_native_filters(f, preds)
    assert (f.layover_restrictions.min_duration, f.layover_restrictions.max_duration) == (
        None,
        120,
    )


# ─────────────────────────── unmappable codes ──────────────────────────────


def test_unmappable_codes_names_what_fli_has_no_member_for() -> None:
    preds = classify("XX+", "ALLIANCE oneworld; AIRLINES AA").predicates
    assert unmappable_codes(preds) == ["XX"]
    assert unmappable_codes(classify("AA+", "ALLIANCE skyteam|star-alliance").predicates) == []
    # Excludes and operating carriers are not asked of Google.
    assert unmappable_codes(classify("~XX+", "OPAIRLINES XX").predicates) == []


# ─────────────────────────── time windows ──────────────────────────────────


@pytest.mark.parametrize(
    "buckets,hours",
    [
        ((TimeOfDay.MORNING,), (8, 11)),
        ((TimeOfDay.EARLY_MORNING,), (0, 8)),
        ((TimeOfDay.NIGHT,), (21, 23)),
        ((TimeOfDay.MIDDAY, TimeOfDay.MORNING), (8, 14)),
    ],
)
def test_a_time_window_becomes_the_segments_departure_hours(
    buckets: tuple[TimeOfDay, ...], hours: tuple[int, int]
) -> None:
    """A latest hour is the last hour included, so 11:00 writes 11."""
    day = date.fromisoformat(_TRAVEL_DATE)
    f = to_fli_filter(
        SpecificDateSearch(
            legs=(
                Leg.of("JFK", "LAX", day, time_ranges=buckets),
                Leg.of("LAX", "JFK", day + timedelta(days=7)),
            )
        )
    )
    out, back = f.flight_segments
    window = out.time_restrictions
    assert (window.earliest_departure, window.latest_departure) == hours
    assert (window.earliest_arrival, window.latest_arrival) == (None, None)
    assert back.time_restrictions is None
