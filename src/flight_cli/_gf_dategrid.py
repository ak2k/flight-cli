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
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
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
from .routing_predicates import (
    MAX_ENCODABLE_STOPS,
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
    Tier,
    classify,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .domain import CalendarSearch, Leg
    from .routing_predicates import Predicate

_MAX_GRID_DAYS = 61  # GetCalendarGraph's per-request span limit

# Flipping this to False is necessary but NOT sufficient. No test executes the
# transport below the gate: there is no captured GetCalendarGraph envelope to build
# one on, and inventing the shape is forbidden (AGENTS.md rule 5), so type-checking
# is all that guards it. Re-enable procedure: (1) capture a real envelope into
# tests/fixtures/, (2) add an ungated contract test over it — request URL, encoded
# body, and the success / empty / throttle branches of `_one_grid_call`, (3) keep
# `_grid_filters` behind the two refusals of an airport set or metro code:
# `_grid_branch_blocker` without `--fast` and `cli._http_date_grid` with it. It
# writes `origins[0]` and `destinations[0]`, which prices a set for one airport and
# raises AttributeError on NYC/LON/PAR/CHI (not in fli's `Airport` enum), (4) run a
# live smoke, (5) then flip. Tracked on work-h70kv.5.
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


def grid_can_serve(
    search: CalendarSearch, *, round_trip: bool = False, airport_sets: bool = False
) -> bool:
    """Whether the GF date-grid can fully serve this calendar: only Tier-1
    constraints on every leg (the grid has no itineraries, so even Tier-2 can't
    be post-filtered — those go to Matrix), each of which the request carries
    in full (`unwritten_constraint`), and so does the `--stops` ceiling.

    One-way, unless the caller can serve a round trip (`round_trip`: the page's
    price graph can, `date_grid` below cannot) AND the window names one trip
    length. The graph prices a single trip length, so a duration range has no
    one question to ask it.

    One token per side, unless the caller asks for every airport of a set
    (`airport_sets`: the page's URL carries them all, `date_grid` writes the
    first). The caller checks the tokens themselves: a metro code is one token."""
    window = search.window
    if len(search.legs) > 1 and not (round_trip and window.duration_min == window.duration_max):
        return False
    if unwritten_constraint(_stops_option(search)) is not None:
        return False
    for leg in search.legs:
        if not airport_sets and (len(leg.origins) != 1 or len(leg.destinations) != 1):
            return False
        predicates = classify(leg.route_language, leg.extension).predicates
        if any(p.tier is not Tier.GF_NATIVE for p in predicates):
            return False
        if unwritten_constraint(predicates) is not None:
            return False
    return True


_CODE_NOUN: Final = {
    CarrierPred: "a carrier",
    AlliancePred: "an alliance",
    ConnectionAirportPred: "a connecting airport",
}


def unwritten_constraint(predicates: Iterable[Predicate]) -> str | None:
    """The first Tier-1 constraint `apply_gf_native_filters` would not write in
    full, as a noun phrase naming its code or bound, or None.

    Both grids build their request with that function and have no rows to check
    afterwards, so what it leaves out is priced as though never asked: a code
    list with one unmappable code is left out whole, fli's encoder omits a
    maximum duration that is zero, a zero MAXCONNECT raises inside fli, and
    fli's stop enum ends at "two or fewer", so a higher ceiling is written as
    no ceiling."""
    for p in predicates:
        match p:
            case StopsPred(max_stops=ceiling) if ceiling > MAX_ENCODABLE_STOPS:
                return f"a stop ceiling above {MAX_ENCODABLE_STOPS} ({ceiling})"
            case MaxDurationPred(minutes=0):
                return "a maximum trip duration of 0 minutes"
            case ConnectTimePred(max_minutes=0):
                return "a maximum layover of 0 minutes"
            case CarrierPred() | AlliancePred() | ConnectionAirportPred():
                if bad := _unmapped_codes(p):
                    noun = _CODE_NOUN[type(p)]
                    return f"{noun} Google Flights has no code for ({', '.join(bad)})"
            case _:
                pass
    return None


def _unmapped_codes(p: CarrierPred | AlliancePred | ConnectionAirportPred) -> list[str]:
    """`p`'s codes the bridge cannot write, asked of the bridge one code at a time.

    Its own lookups decide, so this cannot drift from what the request carries;
    it reports only that some code failed, hence one call per code. It writes
    onto whatever it is handed, so a namespace takes the throwaway filters."""
    return [
        code
        for code in sorted(p.codes)
        if not apply_gf_native_filters(SimpleNamespace(), [replace(p, codes=frozenset({code}))])
    ]


def _decliner_phrase(tier: str, *, routing: bool, extension_count: int) -> str:
    """The phrase naming whichever of `--routing` / `--extension` declined, for a
    sentence that continues "this is …".

    `--routing` takes one string and is a mass noun; `--extension` takes a
    `;`-separated list, so its half is counted and carries an article only when a
    single directive declined. At least one side must have declined — the caller
    checks that — so a bare extension phrase is the remaining case, not a default.
    """
    if not extension_count:
        return f"{tier} routing"
    codes = f"a {tier} extension code" if extension_count == 1 else f"{tier} extension codes"
    return f"both {tier} routing and {codes}" if routing else codes


def grid_routing_blocker(search: CalendarSearch) -> str | None:
    """Name the constraint keeping the date-grid off this calendar, or None when
    every predicate is Tier-1 and written in full (so no constraint is the reason).

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
    `MINCONNECT` are Tier-2 extension codes, not routing; a booking class is a
    Matrix-only extension code, not Matrix-only routing. Both tiers name the
    source by the same rule, so the reader learns which flag to edit whichever
    tier stopped the query, and `--extension` takes a `;`-separated list, so the
    phrase agrees in number with how many of its directives declined.

    Every leg is read, outbound first. The return leg inherits `--routing` and
    `--extension` unless `--routing-ret` / `--ext-ret` replace them, so a return
    phrase is only ever about those two flags, and it says so. `--stops` is read
    last, in the same words as a MAXSTOPS the request cannot carry.
    """
    for i, leg in enumerate(search.legs):
        phrase = _leg_blocker(leg)
        if phrase is not None:
            return f"{phrase} on the return leg" if i else phrase
    return unwritten_constraint(_stops_option(search))


def _stops_option(search: CalendarSearch) -> list[Predicate]:
    """`--stops` as the predicate its ceiling amounts to: both grids write it
    through the same fli stop enum as a MAXSTOPS."""
    ceiling = search.options.max_extra_stops
    return [] if ceiling is None else [StopsPred(max_stops=ceiling)]


def _leg_blocker(leg: Leg) -> str | None:
    """`grid_routing_blocker` for one leg."""
    routing_c = classify(leg.route_language, None)
    ext_c = classify(None, leg.extension)
    # Keyed on the TIER, not on `UnsupportedPred`: `grid_can_serve` decides by
    # tier, and a Tier-3 predicate of some other class would otherwise fall
    # through to a Tier-2 phrase. `matrix_reasons` supplies the text where it can
    # (only `UnsupportedPred` carries one), never the branch.
    if routing_c.requires_matrix or ext_c.requires_matrix:
        head = _decliner_phrase(
            "Matrix-only",
            routing=routing_c.requires_matrix,
            extension_count=len(ext_c.matrix_only),
        )
        # Every reason, not just the first: a query can be Matrix-only several
        # times over, and fixing one would leave the refusal unchanged and
        # unexplained. This is also the count the phrase agrees in number with.
        reasons = routing_c.matrix_reasons + ext_c.matrix_reasons
        return f"{head} ({'; '.join(reasons)})" if reasons else head
    if routing_c.tier2 or ext_c.tier2:
        return _decliner_phrase(
            "Tier-2", routing=bool(routing_c.tier2), extension_count=len(ext_c.tier2)
        )
    return unwritten_constraint((*routing_c.predicates, *ext_c.predicates))


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
    # broad `except` reports the standing gate as "date grid failed: type object
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
