"""Google Flights native date-grid (SearchDates / GetCalendarGraph) for fast,
Tier-1 calendars.

Returns cheapest-price-per-date for a whole window in ONE call — far faster than
Matrix's calendar, and it sidesteps Matrix's compute-budget under-reporting
(MEMORY quirk #7). `DateSearchFilters` carries the full Tier-1 filter set, so
airlines/stops/layover/max_duration/cabin/times/price are honored server-side
(reusing `apply_gf_native_filters`). It returns `{date: price}` only — **no
itineraries** — so Tier-2 constraints (`O:`/`-CODESHARE`/`~UA`/flight#) can't be
honored on a grid; those calendars go to Matrix.

We chunk windows to <=61 days OURSELVES with the full filter set, dodging the fli
`SearchDates` >61-day bug that drops filters on later chunks (bd work-bcdex).
Throttle-hardened via the shared `retry_throttled` (same code-13 detection +
backoff as the search path). fli is heavy, so `cli` imports this module lazily —
only when a GF calendar is actually run.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]
from fli.models.google_flights.base import (  # pyright: ignore[reportMissingTypeStubs]
    FlightSegment,
    SeatType,
    TripType,
)
from fli.models.google_flights.dates import (  # pyright: ignore[reportMissingTypeStubs]
    DateSearchFilters,
)
from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
    PassengerInfo,
)
from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]
from fli.search.dates import SearchDates  # pyright: ignore[reportMissingTypeStubs]

from ._gflight_ids import (  # shared GF-internal helpers (sibling module)
    GfThrottledError,
    _is_throttle_block,  # pyright: ignore[reportPrivateUsage]
    _persist_cookies,  # pyright: ignore[reportPrivateUsage]
    _seed_cookies_once,  # pyright: ignore[reportPrivateUsage]
    retry_throttled,
)
from .domain import Cabin
from .fli_bridge import (
    _fli_max_stops,  # pyright: ignore[reportPrivateUsage]
    apply_gf_native_filters,
)
from .routing_predicates import Tier, classify

if TYPE_CHECKING:
    from .domain import CalendarSearch
    from .routing_predicates import Predicate

_MAX_GRID_DAYS = 61  # GetCalendarGraph's per-request span limit

_CABIN_TO_SEAT = {
    Cabin.COACH: SeatType.ECONOMY,
    Cabin.PREMIUM_COACH: SeatType.PREMIUM_ECONOMY,
    Cabin.BUSINESS: SeatType.BUSINESS,
    Cabin.FIRST: SeatType.FIRST,
}


def grid_can_serve(search: CalendarSearch) -> bool:
    """Whether the GF date-grid can fully serve this calendar: one-way,
    single-airport per leg, and only Tier-1 constraints (the grid has no
    itineraries, so even Tier-2 can't be post-filtered — those go to Matrix).
    Round-trip is excluded for now: a duration *range* doesn't map to the grid's
    single-duration parameter."""
    if len(search.legs) != 1:
        return False
    leg = search.legs[0]
    if len(leg.origins) != 1 or len(leg.destinations) != 1:
        return False
    constraints = classify(leg.route_language, leg.extension)
    return all(p.tier is Tier.GF_NATIVE for p in constraints.predicates)


def _grid_filters(
    search: CalendarSearch, from_iso: str, to_iso: str, predicates: list[Predicate]
) -> Any:
    """Build a one-way DateSearchFilters for a <=61-day sub-window, with the
    search's cabin/stops/pax plus the Tier-1 routing predicates applied."""
    leg = search.legs[0]
    p = search.options.pax
    extra_stops = search.options.max_extra_stops
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(
            adults=(p.adults + p.seniors + p.youth) or 1, children=p.children
        ),
        flight_segments=[
            FlightSegment(
                departure_airport=[[getattr(Airport, leg.origins[0]), 0]],
                arrival_airport=[[getattr(Airport, leg.destinations[0]), 0]],
                travel_date=from_iso,
            )
        ],
        stops=_fli_max_stops(extra_stops if extra_stops is not None else 99),
        seat_type=_CABIN_TO_SEAT[search.options.cabin],
        trip_type=TripType.ONE_WAY,
        from_date=from_iso,
        to_date=to_iso,
    )
    if predicates:
        apply_gf_native_filters(filters, predicates)
    return filters


def _parse_grid(parsed: str) -> dict[str, float]:
    """{date: price} from the inner GetCalendarGraph payload. Each item in the
    last array is `[date, _, [[_, price], ...], ...]`; bad-shaped items skipped."""
    out: dict[str, float] = {}
    for item in json.loads(parsed)[-1]:
        try:
            day = item[0]
            price = item[2][0][1]
        except (IndexError, TypeError):
            continue
        if isinstance(day, str) and price is not None:
            out[day] = float(price)
    return out


def _one_grid_call(filters: Any) -> dict[str, float]:
    """One GetCalendarGraph round-trip -> {date: price}. Raises GfThrottledError
    on a genuine code-13 block; returns {} on a cold-session empty."""
    client = get_client()
    _seed_cookies_once(client)
    resp = client.post(
        url=SearchDates.BASE_URL,
        data=f"f.req={filters.encode()}",
        impersonate="chrome",
        allow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.text
    parsed = json.loads(body.lstrip(")]}'"))[0][2]
    if not parsed:
        if _is_throttle_block(body):
            raise GfThrottledError("Google Flights rate-limited the date-grid request")
        return {}
    _persist_cookies(client)
    return _parse_grid(parsed)


def date_grid(search: CalendarSearch) -> dict[str, float]:
    """Cheapest price per departure date across the window (caller ensures
    `grid_can_serve`). Chunks to <=61 days with the FULL filter set, throttle-
    retries each, and merges. Raises GfThrottledError if the throttle persists."""
    leg = search.legs[0]
    predicates = list(classify(leg.route_language, leg.extension).predicates)
    out: dict[str, float] = {}
    cursor = search.window.start
    while cursor <= search.window.end:
        chunk_end = min(cursor + timedelta(days=_MAX_GRID_DAYS - 1), search.window.end)
        filters = _grid_filters(search, cursor.isoformat(), chunk_end.isoformat(), predicates)
        out.update(retry_throttled(lambda f=filters: _one_grid_call(f)))
        cursor = chunk_end + timedelta(days=1)
    return out
