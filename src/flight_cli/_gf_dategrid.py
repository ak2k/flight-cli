"""Google Flights native date-grid (SearchDates / GetCalendarGraph) for fast,
Tier-1 calendars.

**Gated off since 2026-08 (upstream report fli#223), verified here 2026-09-02
(work-h70kv.5).** GetCalendarGraph answers HTTP 200 with an empty payload to any
client that can't sign `x-goog-batchexecute-bgr` (BotGuard). An empty payload is
not a throttle, so the shared retry reads it as a cold session and burns 4 POSTs
+ ~6s of backoff per chunk to learn nothing — hence the `GfGridUnavailableError`
behind `_GRID_RPC_GATED`, raised at two sites: the top of `date_grid` (the primary
one, ahead of the chunk loop so no fli model is built for a window that cannot be
priced) and the top of `_one_grid_call` (defense in depth, and what keeps a gated
grid off the network). Flipping that flag back to False re-enables the transport
below them, as does an attested transport landing (work-udpp1).

When the RPC answers, it returns cheapest-price-per-date for a whole window in
ONE call — far faster than Matrix's calendar, and it sidesteps Matrix's
compute-budget under-reporting (MEMORY quirk #7). `DateSearchFilters` carries the
full Tier-1 filter set, so airlines/stops/layover/max_duration/cabin/times/price
are honored server-side (reusing `apply_gf_native_filters`). It returns
`{date: price}` only — **no itineraries** — so Tier-2 constraints
(`O:`/`-CODESHARE`/`~UA`/flight#) can't be honored on a grid; those calendars go
to Matrix.

We chunk windows to <=61 days OURSELVES with the full filter set, dodging the fli
`SearchDates` >61-day bug that drops filters on later chunks (bd work-bcdex).
Throttle-hardened via the shared `retry_throttled` (same code-13 detection +
backoff as the search path). fli is heavy, so `cli` imports this module lazily —
only when a GF calendar is actually run.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

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

# Flipping this to False is necessary but NOT sufficient. No test executes the
# transport below the gate: there is no captured GetCalendarGraph envelope to build
# one on, and inventing the shape is forbidden (AGENTS.md rule 5), so type-checking
# is all that guards it. Re-enable procedure: (1) capture a real envelope into
# tests/fixtures/, (2) add an ungated contract test over it — request URL, encoded
# body, and the success / empty / throttle branches of `_one_grid_call`, (3) teach
# `_grid_filters` to map or refuse city codes — NYC/LON/PAR/CHI are not in fli's
# `Airport` enum, and while the gate stands it is the only thing between them and an
# AttributeError, (4) run a live smoke, (5) then flip. Tracked on work-h70kv.5.
#
# A flag rather than an unconditional raise precisely so basedpyright keeps checking
# that code: `SearchDates.BASE_URL` and `DateSearchFilters.encode()` have no other
# caller here, and `flights` is pinned with an open floor, so an fli bump could
# otherwise rot the flip-back with green CI. Not `Final[bool]` — that narrows to the
# literal and the body goes unchecked (measured). Bare `Final` keeping it checked is
# basedpyright 1.39.4's inference for an un-subscripted Final, so re-measure on a
# basedpyright bump — same open-floor caveat as `flights`.
_GRID_RPC_GATED: Final = True

_GRID_GATED_MSG = "Google Flights' calendar RPC (GetCalendarGraph) returns no data to this client"

_CABIN_TO_SEAT = {
    Cabin.COACH: SeatType.ECONOMY,
    Cabin.PREMIUM_COACH: SeatType.PREMIUM_ECONOMY,
    Cabin.BUSINESS: SeatType.BUSINESS,
    Cabin.FIRST: SeatType.FIRST,
}


class GfGridUnavailableError(Exception):
    """Google Flights' date-grid RPC won't answer a plain HTTP client.

    A standing gate, not a transient: unlike `GfThrottledError` (backoff helps)
    or a cold-session empty (a retry on the same client helps), nothing this
    process can do makes the next call succeed — so callers degrade to Matrix at
    once instead of retrying, and say which backend priced the grid."""


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


def grid_routing_blocker(search: CalendarSearch) -> str | None:
    """Name the constraint keeping the date-grid off this calendar, or None when
    every predicate is Tier-1 (so no constraint is the reason).

    A diagnostic only — `grid_can_serve` owns the decision, and also rejects
    shapes no constraint speaks to (round trip, multi-airport). Both tiers above
    Tier-1 send a calendar to Matrix, but for different reasons: Tier-2 is
    post-filterable and merely needs the itineraries the grid does not return,
    while Tier-3 is fare construction Google can neither request nor reconstruct.
    Calling a booking class "Tier-2" sends the reader looking for a post-filter
    that was never the problem.

    `--routing` and `--extension` are classified separately because the phrase
    names the flag to go edit, and `classify` flattens both into one predicate
    set that no longer remembers which one carried what. `-CODESHARE` and
    `MINCONNECT` are Tier-2 extension codes, not routing.
    """
    leg = search.legs[0]
    routing_c = classify(leg.route_language, None)
    ext_c = classify(None, leg.extension)
    # Keyed on the TIER, not on `UnsupportedPred`: `grid_can_serve` decides by
    # tier, and a Tier-3 predicate of some other class would otherwise fall
    # through to a Tier-2 phrase. `matrix_reasons` supplies the text where it can
    # (only `UnsupportedPred` carries one), never the branch.
    if routing_c.requires_matrix or ext_c.requires_matrix:
        reasons = routing_c.matrix_reasons + ext_c.matrix_reasons
        return f"Matrix-only routing ({'; '.join(reasons)})" if reasons else "Matrix-only routing"
    if routing_c.tier2:
        return "Tier-2 routing"
    if ext_c.tier2:
        return "a Tier-2 extension code"
    return None


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
    # Defense in depth: `date_grid` already refuses while gated, so this is
    # unreachable through it. It guards any future caller that builds filters
    # itself, and it is what keeps a gated grid off the network — first statement,
    # ahead of `get_client()`, and `retry_throttled` catches only GfThrottledError,
    # so it propagates with zero POSTs and zero backoff sleeps.
    if _GRID_RPC_GATED:
        raise GfGridUnavailableError(_GRID_GATED_MSG)
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
    retries each, and merges. Raises GfThrottledError if the throttle persists —
    and, while `_GRID_RPC_GATED`, GfGridUnavailableError before anything else."""
    # Ahead of the chunk loop, so no fli model is built for a grid that cannot be
    # priced. `_grid_filters` resolves airports through fli's `Airport` enum and
    # dates through `FlightSegment`, both of which reject inputs this command
    # accepts: a city code (NYC/LON/PAR) is not in that enum, and a window opening
    # in the past fails travel-date validation. Either one raises, and the callers'
    # broad `except` reports the standing gate as "date-grid failed: type object
    # 'Airport' has no attribute 'NYC'" — a transport-shaped error for a request no
    # transport was going to carry.
    if _GRID_RPC_GATED:
        raise GfGridUnavailableError(_GRID_GATED_MSG)
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
