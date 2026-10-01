"""Convert a domain Search to fli's FlightSearchFilters and run the
Google Flights query. Used for the `flight gflight` handoff.

Each leg carries every airport of its origin and destination sets, metro codes
expanded (`_metro`). fli has no calendar mode, so for calendar searches we use
the window start as departure + mean(duration) as return."""

from __future__ import annotations

import functools
from datetime import timedelta
from typing import TYPE_CHECKING, Any, assert_never

from ._metro import expand_airports
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    Search,
    SpecificDateSearch,
    time_bounds,
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
    from collections.abc import Iterable, Sequence
    from enum import Enum

    from .domain import TimeOfDay
    from .routing_predicates import Predicate

_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way

# Codes fli aliases that name the same airport as the code they alias. Google
# serves EuroAirport only as BSL: JFK-MLH asked for MLH came back an empty
# board, asked for BSL eight rows (2026-10-01).
_SERVED_AS_CANONICAL = frozenset({"MLH"})


@functools.cache
def fli_airports() -> dict[str, Any]:
    """Every code in fli's airport table, to the member a request for it carries.

    fli builds `Airport` as an enum over a code -> display-name table, and an
    enum makes each code whose display name repeats an earlier one an alias of
    the earlier member: `Airport.OKA` is `Airport.NAH`, Naha in Indonesia
    rather than Okinawa. Every request and every decoded row reads a member's
    name, so each aliased code gets a member of its own, named that code and
    carrying the same display name, except MLH (`_SERVED_AS_CANONICAL`).

    Those members are outside the enum's own tables: one survives `deepcopy`,
    which returns an enum member itself, but would unpickle as the member it
    aliases."""
    from fli.models.airport import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airport,
    )

    def own(code: str, canonical: Enum) -> Any:
        member = object.__new__(Airport)
        vars(member).update(vars(canonical))
        member._name_ = code
        return member

    return {
        code: (member if member.name == code or code in _SERVED_AS_CANONICAL else own(code, member))
        for code, member in Airport.__members__.items()
    }


def fli_airport(code: str) -> Any:
    """The fli airport member for `code`. AttributeError for a code fli has no
    entry for, as `getattr(Airport, code)` raises."""
    try:
        return fli_airports()[code]
    except KeyError:
        raise AttributeError(code) from None


def to_fli_filter(s: Search) -> Any:
    """Translate domain → fli FlightSearchFilters. Lazy imports so the
    rest of flight_cli doesn't pay the fli import cost when not used."""

    # selectolax, etc.); the rest of the CLI shouldn't pay that startup cost.
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        BagsFilter,
        MaxStops,
        PriceLimit,
        SeatType,
        TimeRestrictions,
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

    def _window(buckets: Sequence[TimeOfDay]) -> TimeRestrictions | None:
        # Google takes whole hours and a latest hour includes its every minute
        # (11 answers up to 11:59), so this window can only be wider than the
        # buckets; the row filter holds the rows to the buckets' own minutes.
        if not buckets:
            return None
        spans = [time_bounds(b) for b in buckets]
        return TimeRestrictions(
            earliest_departure=min(lo for lo, _ in spans) // 60,
            latest_departure=max(hi for _, hi in spans) // 60,
        )

    def _seg(
        origins: Sequence[str], dests: Sequence[str], dt: str, buckets: Sequence[TimeOfDay]
    ) -> FlightSegment:
        return FlightSegment(
            departure_airport=[[fli_airport(a), 0] for a in expand_airports(origins)],
            arrival_airport=[[fli_airport(a), 0] for a in expand_airports(dests)],
            travel_date=dt,
            time_restrictions=_window(buckets),
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
                segs.append(
                    _seg(leg.origins, leg.destinations, leg.date.isoformat(), leg.time_ranges)
                )
        case CalendarSearch():
            mean_dur = (s.window.duration_min + s.window.duration_max) // 2
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == _ROUND_TRIP_LEGS else None
            segs.append(
                _seg(out.origins, out.destinations, s.window.start.isoformat(), out.time_ranges)
            )
            if ret:
                segs.append(
                    _seg(
                        ret.origins,
                        ret.destinations,
                        (s.window.start + timedelta(days=mean_dur)).isoformat(),
                        ret.time_ranges,
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
    cap, bags = s.options.max_price, s.options.bags
    return FlightSearchFilters(
        # fli's PassengerInfo takes all four types and permits adults=0, so
        # pass the party through as asked. The old `or 1` SYNTHESIZED an adult
        # for a child-only search — pricing a 2-passenger trip nobody
        # requested — and infants were dropped entirely, so an infant-in-seat
        # search silently priced one fewer seat than the Matrix side.
        passenger_info=PassengerInfo(
            adults=p.adults + p.seniors + p.youth,
            children=p.children,
            infants_in_seat=p.infants_in_seat,
            infants_on_lap=p.infants_in_lap,
        ),
        flight_segments=segs,
        stops=stops,
        seat_type=cab_map[s.options.cabin],
        trip_type=trip_map.get(len(segs), TripType.MULTI_CITY),
        # The page prices the cap in its own `curr=`, so fli's currency would
        # only restate it, or contradict it.
        price_limit=PriceLimit(max_price=cap, currency=None) if cap is not None else None,
        bags=(
            BagsFilter(checked_bags=bags.checked, carry_on=bool(bags.carry_on))
            if bags is not None
            else None
        ),
    )


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


def _include_list(predicates: Iterable[Predicate]) -> tuple[list[Any], list[str]]:
    """fli's airline include list for the marketing-carrier and alliance
    includes among `predicates`, and the codes it has no member for."""
    from fli.models.airline import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airline,
    )

    # DIVERGE: fli's airline row-decoder moved to a private module in 0.9.0;
    # same AttributeError-on-unknown contract the except below relies on.
    from fli.search._decoders import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        _parse_airline,  # pyright: ignore[reportPrivateUsage]
    )

    airlines: list[Any] = []
    unmapped: list[str] = []
    for p in predicates:
        if isinstance(p, CarrierPred) and not (p.operating or p.exclude):
            for code in sorted(p.codes):
                try:
                    airlines.append(_parse_airline(code))
                except AttributeError:
                    unmapped.append(code)
        elif isinstance(p, AlliancePred):
            for token in sorted(p.codes):
                try:
                    airlines.append(Airline[token.upper().replace("-", "_")])
                except KeyError:
                    unmapped.append(token)
    return airlines, unmapped


def unmappable_codes(predicates: Iterable[Predicate]) -> list[str]:
    """The carrier codes and alliance names among `predicates`' includes that
    fli has no member for, so Google Flights cannot be asked for them."""
    return _include_list(predicates)[1]


def apply_gf_native_filters(filters: Any, predicates: Iterable[Predicate]) -> bool:  # noqa: PLR0912 - flat predicate dispatch
    """Apply the predicates Google Flights can be asked for onto an fli
    FlightSearchFilters in place: marketing-carrier include -> airlines;
    alliance -> airlines; connect-at airport -> layover_restrictions.airports;
    layover bounds -> layover min/max; MAXDUR -> max_duration; nonstop /
    MAXSTOPS -> stops, the strictest of them and `--stops` winning.

    Returns False if a predicate names a carrier or airport code fli can't map;
    that list is then left out whole, so the filters ask for more than the
    predicates allow. The search picker sends such requests to Matrix before
    this runs (`unmappable_codes`); the date grids do not check the return.
    Other predicates are ignored here (the post-filter and gate own those). The
    RPC grid refuses a minimum layover before calling this: it has no rows to
    check it on. The price graph takes one, which Google was measured applying
    from the URL, and refuses a minimum above the maximum, which this drops,
    and a second maximum, which replaces the first."""
    from fli.models.google_flights.base import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        LayoverRestrictions,
    )

    predicates = list(predicates)
    airlines, unmapped = _include_list(predicates)
    layover_airports: list[Any] = []
    layover_min: int | None = None
    layover_max: int | None = None
    stop_limits: list[int] = []
    airports_ok = True

    for p in predicates:
        if isinstance(p, ConnectionAirportPred):
            if p.exclude:
                continue  # Tier-2
            for code in sorted(p.codes):
                try:
                    layover_airports.append(fli_airport(code))
                except AttributeError:
                    airports_ok = False
        elif isinstance(p, StopsPred):
            stop_limits.append(p.max_stops)
        elif isinstance(p, MaxDurationPred):
            filters.max_duration = p.minutes
        elif isinstance(p, ConnectTimePred):
            if p.max_minutes is not None:
                layover_max = p.max_minutes
            # Zero constrains nothing, and fli's field refuses it.
            if p.min_minutes:
                layover_min = max(layover_min or 0, p.min_minutes)

    # Stop counts, not MaxStops values: fli's enum is one-based with ANY = 0.
    if stop_limits:
        if filters.stops.value:
            stop_limits.append(filters.stops.value - 1)
        filters.stops = _fli_max_stops(min(stop_limits))
    # A minimum above the maximum leaves only nonstops, which the maximum alone
    # still returns; what Google does with the pair is unknown.
    if layover_min is not None and layover_max is not None and layover_min > layover_max:
        layover_min = None
    # Apply a code-list dimension only if EVERY code mapped — a partial list
    # would narrow the query and drop the unmapped carrier's/airport's flights
    # (under-return).
    if airlines and not unmapped:
        filters.airlines = airlines
    use_airports = bool(layover_airports) and airports_ok
    if use_airports or layover_min is not None or layover_max is not None:
        filters.layover_restrictions = LayoverRestrictions(
            airports=layover_airports if use_airports else None,
            min_duration=layover_min,
            max_duration=layover_max,
        )
    return not unmapped and airports_ok
