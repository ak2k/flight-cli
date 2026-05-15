"""Convert a domain Search to fli's FlightSearchFilters and run the
Google Flights query. Used for the `flight gflight` handoff.

fli has no notion of multi-airport per slice or calendar mode — so we
flatten to the first IATA per leg, and for calendar searches we use the
window start as departure + mean(duration) as return."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, assert_never

from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    Search,
    SpecificDateSearch,
)

_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way


def to_fli_filter(s: Search) -> Any:
    """Translate domain → fli FlightSearchFilters. Lazy imports so the
    rest of flight_cli doesn't pay the fli import cost when not used."""

    # selectolax, etc.); the rest of the CLI shouldn't pay that startup cost.
    from fli.models.airport import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airport as FliAirport,
    )
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
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
    p = s.options.pax
    return FlightSearchFilters(
        passenger_info=PassengerInfo(
            adults=(p.adults + p.seniors + p.youth) or 1,
            children=p.children,
        ),
        flight_segments=segs,
        seat_type=cab_map[s.options.cabin],
        trip_type=trip_map.get(len(segs), TripType.MULTI_CITY),
    )


def run_gflight_search(s: Search, *, top_n: int = 5) -> Any:
    """Build a fli filter from a Search and run the Google Flights query."""

    from fli.search.flights import (  # noqa: PLC0415
        SearchFlights,  # pyright: ignore[reportMissingTypeStubs]
    )

    return SearchFlights().search(to_fli_filter(s), top_n=top_n)
