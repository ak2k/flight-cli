"""Convert a domain Search to fli's FlightSearchFilters and run the
Google Flights query. Used for the `flight gflight` handoff.

fli has no notion of multi-airport per slice or calendar mode — so we
flatten to the first IATA per leg, and for calendar searches we use the
window start as departure + mean(duration) as return."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, assert_never

from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    Search,
    SpecificDateSearch,
)
from .routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .routing_predicates import Predicate

_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way


def to_fli_filter(s: Search) -> Any:
    """Translate domain → fli FlightSearchFilters. Lazy imports so the
    rest of flight_cli doesn't pay the fli import cost when not used."""

    # selectolax, etc.); the rest of the CLI shouldn't pay that startup cost.
    from fli.models.airport import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airport as FliAirport,
    )
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        MaxStops,
        SeatType,
        TripType,
    )
    from fli.models.google_flights.flights import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
        FlightSegment,
        PassengerInfo,
    )

    cab_map = {
        Cabin.COACH: SeatType.ECONOMY,
        Cabin.PREMIUM_COACH: SeatType.PREMIUM_ECONOMY,
        Cabin.BUSINESS: SeatType.BUSINESS,
        Cabin.FIRST: SeatType.FIRST,
    }

    def _seg(origin: str, dest: str, dt: str) -> FlightSegment:
        o = getattr(FliAirport, origin)
        d = getattr(FliAirport, dest)
        return FlightSegment(
            departure_airport=[[o, 0]],
            arrival_airport=[[d, 0]],
            travel_date=dt,
        )

    segs: list[Any] = []
    match s:
        case SpecificDateSearch() | CalendarFollowup():
            for leg in s.legs:
                # SpecificDate/Followup validators guarantee leg.date is set;
                # surface a clear error if invariants were bypassed.
                if leg.date is None:
                    raise AssertionError(
                        f"{type(s).__name__}.leg.date should be set after validation",
                    )
                segs.append(_seg(leg.origins[0], leg.destinations[0], leg.date.isoformat()))
        case CalendarSearch():
            mean_dur = (s.window.duration_min + s.window.duration_max) // 2
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == _ROUND_TRIP_LEGS else None
            segs.append(_seg(out.origins[0], out.destinations[0], s.window.start.isoformat()))
            if ret:
                segs.append(
                    _seg(
                        ret.origins[0],
                        ret.destinations[0],
                        (s.window.start + timedelta(days=mean_dur)).isoformat(),
                    )
                )
        case _:
            assert_never(s)

    trip_map = {1: TripType.ONE_WAY, 2: TripType.ROUND_TRIP}

    # Honor --stops on the gflight backend. `max_extra_stops` is "extra legs
    # beyond nonstop" == stop count: 0 nonstop, 1 one-stop, ... fli's enum tops
    # out at "2 or fewer", so 3+ (and None = no limit) fall through to ANY.
    stops_map = {
        0: MaxStops.NON_STOP,
        1: MaxStops.ONE_STOP_OR_FEWER,
        2: MaxStops.TWO_OR_FEWER_STOPS,
    }
    mx = s.options.max_extra_stops
    stops = stops_map.get(mx, MaxStops.ANY) if mx is not None else MaxStops.ANY

    p = s.options.pax
    return FlightSearchFilters(
        passenger_info=PassengerInfo(
            adults=(p.adults + p.seniors + p.youth) or 1,
            children=p.children,
        ),
        flight_segments=segs,
        stops=stops,
        seat_type=cab_map[s.options.cabin],
        trip_type=trip_map.get(len(segs), TripType.MULTI_CITY),
    )


def run_gflight_search(s: Search, *, top_n: int = 5) -> Any:
    """Build a fli filter from a Search and run the Google Flights query."""

    from fli.search.flights import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        SearchFlights,
    )

    return SearchFlights().search(to_fli_filter(s), top_n=top_n)


def _fli_max_stops(max_stops: int) -> Any:
    """Map a stop count to fli's MaxStops enum (it tops out at 'two or fewer')."""
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        MaxStops,
    )

    return {
        0: MaxStops.NON_STOP,
        1: MaxStops.ONE_STOP_OR_FEWER,
        2: MaxStops.TWO_OR_FEWER_STOPS,
    }.get(max_stops, MaxStops.ANY)


def apply_gf_native_filters(filters: Any, predicates: Iterable[Predicate]) -> bool:  # noqa: PLR0912 - flat predicate dispatch
    """Apply Tier-1 (GF-native) predicates onto an fli FlightSearchFilters in
    place: marketing-carrier include -> airlines; alliance -> airlines; connect-
    at airport -> layover_restrictions.airports; max layover -> layover max;
    MAXDUR -> max_duration; nonstop / MAXSTOPS -> stops.

    Returns False if a predicate names a carrier or airport code fli can't map —
    the caller falls back to Matrix rather than silently dropping the constraint.
    Tier-2 / Tier-3 predicates are ignored here (the post-filter and gate own
    those)."""
    from fli.models.airline import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airline,
    )
    from fli.models.airport import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airport,
    )
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        LayoverRestrictions,
    )
    from fli.search.flights import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        SearchFlights,
    )

    airlines: list[Any] = []
    layover_airports: list[Any] = []
    layover_max: int | None = None

    for p in predicates:
        if isinstance(p, CarrierPred):
            if p.operating or p.exclude:
                continue  # Tier-2 — honored by the result post-filter
            for code in sorted(p.codes):
                try:
                    airlines.append(SearchFlights._parse_airline(code))  # pyright: ignore[reportPrivateUsage]
                except AttributeError:
                    return False  # code unknown to fli -> fall back to Matrix
        elif isinstance(p, AlliancePred):
            for token in sorted(p.codes):
                try:
                    airlines.append(Airline[token.upper().replace("-", "_")])
                except KeyError:
                    return False
        elif isinstance(p, ConnectionAirportPred):
            if p.exclude:
                continue  # Tier-2
            for code in sorted(p.codes):
                try:
                    layover_airports.append(getattr(Airport, code))
                except AttributeError:
                    return False
        elif isinstance(p, StopsPred):
            filters.stops = _fli_max_stops(p.max_stops)
        elif isinstance(p, MaxDurationPred):
            filters.max_duration = p.minutes
        elif isinstance(p, ConnectTimePred) and p.max_minutes is not None:
            layover_max = p.max_minutes

    if airlines:
        filters.airlines = airlines
    if layover_airports or layover_max is not None:
        filters.layover_restrictions = LayoverRestrictions(
            airports=layover_airports or None, max_duration=layover_max
        )
    return True
