"""Post-filter Google Flights results against the constraints a search asked
for.

Runs only on the gflight path (Matrix legs don't carry the per-leg carrier
identity these predicates need). `search_page_reasons` is the search page's
gate: a predicate rides there when the page's tfs= encodes it or this module
post-filters it on the full board. The date grids do not reach here at all:
they return prices per date and there are no itineraries to post-filter, so
they refuse what they cannot ask for (`_gf_dategrid.grid_can_serve` and
`_gf_calgraph.page_blocker` for the RPC grid, `_gf_calgraph.graph_blocker` for
the Chrome price graph). Anything this module can't evaluate escalates the
whole query to Matrix rather than being silently dropped.

Google has ignored a field it was sent (a carrier exclude on JFK-LHR), so what
the page encodes is checked here too wherever the row shows it: the stop
ceiling, the carrier include, the maximum duration, the layover minutes, the
departure time and a price cap. An alliance is left to Google's own filter, for
want of a membership table.

Supported Tier-2 predicates:
  - operating carrier include/exclude (`O:LH+`, `OPAIRLINES`, `-OPAIRLINES`)
  - marketing-carrier exclude (`~UA+`, `-AIRLINES`)
  - connection-airport exclude (`~DFW`, `-CITIES`)
  - no codeshare (`-CODESHARE`)
  - one flight by number or range (`UA882`, `UA882+`, `UA1000-2000`), every
    leg of the slice that flight, as Matrix reads a lone flight-number token
  - minimum layover (`MINCONNECT`), on the raw row and encoded as well
  - no red-eye flight (`-REDEYES`) and no overnight stop (`-OVERNIGHTS`), on
    the raw row's local clocks
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .domain import Cabin, time_bounds, window_label, within_price_cap
from .routing_predicates import (
    AlliancePred,
    CabinPred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    ExcludeCodesharePred,
    ExcludeOvernightsPred,
    ExcludeRedeyesPred,
    MaxDurationPred,
    SpecificFlightPred,
    StopsPred,
    Tier,
    page_can_encode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .domain import TimeWindow
    from .models import Itinerary, SearchResult, Slice
    from .routing_predicates import Predicate

# Tier-2 predicate types this module evaluates: on a parsed slice
# (`_slice_passes`), or on the raw row (`_row_passes`) for red-eyes, overnight
# stops and cabins, which need each leg's own clocks and cabin. A minimum
# layover is checked on the raw row too.
_SUPPORTED: tuple[type, ...] = (
    CabinPred,
    CarrierPred,
    ConnectionAirportPred,
    ExcludeCodesharePred,
    ExcludeOvernightsPred,
    ExcludeRedeyesPred,
    SpecificFlightPred,
)

_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})(\d+)$", re.IGNORECASE)
_MAX_FLIGHT_NUMBER = 9999


def can_postfilter(pred: Predicate) -> bool:
    """True if this predicate is either not our concern (Tier 1 native / Tier 3
    Matrix-only, handled elsewhere) or a Tier-2 predicate we can evaluate here."""
    if pred.tier is not Tier.GF_POSTFILTER:
        return True
    return isinstance(pred, _SUPPORTED)


def _served_by_postfilter(pred: Predicate) -> bool:
    """A Tier-2 predicate the search page can serve by post-filtering its board.

    Not connection-airport excludes, although this module evaluates them:
    `F* ~DUB F*` is one connection, not at DUB, to Matrix, which drops the
    nonstops the filter keeps. Nor a flight-number range with `+` or `*`,
    which Matrix may read as several flights in the range: `AA1-3000+`
    answered JFK-LAX with the same ten nonstops as bare `AA1-3000`, so no
    answer has shown which connections it admits. Nor a number Matrix rejects
    as a bad route specification (`AA3000-1`, `AA0`, `AA10000`): Google's empty
    board would stand in for that error. Matrix bounds the number, not its
    digits: `AA00001` is AA1. Nor a cabin requirement, which
    `search_page_reasons` serves beside the one cabin the page is asked for."""
    match pred:
        case ConnectionAirportPred() | CabinPred():
            return False
        case SpecificFlightPred() if pred.several and pred.low != pred.high:
            return False
        case SpecificFlightPred() if not 1 <= pred.low <= pred.high <= _MAX_FLIGHT_NUMBER:
            return False
        case _:
            return pred.tier is Tier.GF_POSTFILTER and can_postfilter(pred)


def _served_by_page(pred: Predicate) -> bool:
    """A predicate the search page's tfs= encodes beyond the stop ceiling
    `page_can_encode` admits. The `--fast --gf-transport http` gate asks that
    function alone. The price graph, which has no rows to check these on
    either, admits one of each a leg (`_gf_calgraph.graph_blocker`) because
    Google was measured applying them from the URL."""
    match pred:
        case CarrierPred(exclude=False, operating=False) | AlliancePred():
            return True
        # fli's maximums take a positive number only; assigning the duration
        # skips that check, so a zero would reach the page as 3.12=0.
        case MaxDurationPred():
            return pred.minutes != 0
        case ConnectTimePred():
            return pred.max_minutes != 0
        case _:
            return False


_CABIN_FLAG = {
    Cabin.COACH: "economy",
    Cabin.PREMIUM_COACH: "premium-coach",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}


def _page_refusal(pred: Predicate, cabin: Cabin | None) -> list[str]:
    reasons = page_can_encode([pred])[1]
    match pred:
        case ConnectTimePred(max_minutes=0):
            return ["a maximum layover of 0 min"]
        case CabinPred():
            beside = (
                "beside more than one --cabin"
                if cabin is None
                else f"other than --cabin {_CABIN_FLAG[cabin]}"
            )
            return [f"{reason} {beside}" for reason in reasons]
        case _:
            return reasons


def search_page_reasons(
    predicates: Iterable[Predicate], stops: int | None = None, cabin: Cabin | None = None
) -> list[str]:
    """Why the search page can't serve `predicates` beside a `--stops` of
    `stops` and the one `--cabin` asked, `cabin` (None for several): one
    reason per predicate that its tfs= cannot encode and this module does not
    post-filter. Empty when the page serves them all.

    The page is asked for the strictest stop limit alone
    (`fli_bridge.apply_gf_native_filters`), so only that one has to fit.
    3.6 is one include list, so an alliance written beside a carrier or another
    alliance asks Google for either, and no row check narrows an alliance back.
    A `+CABIN` naming exactly `cabin` is served, its rows held to it: the page
    is asked for that cabin, and another set would need rows it never asks
    for."""
    preds = list(predicates)
    limits = [p.max_stops for p in preds if isinstance(p, StopsPred)]
    if stops is not None:
        limits.append(stops)
    reasons = page_can_encode([StopsPred(min(limits))])[1] if limits else []
    reasons += [
        reason
        for p in preds
        if not (
            isinstance(p, StopsPred)
            or _served_by_postfilter(p)
            or _served_by_page(p)
            or (isinstance(p, CabinPred) and p.cabins == frozenset({cabin}))
        )
        for reason in _page_refusal(p, cabin)
    ]
    alliances = sum(isinstance(p, AlliancePred) for p in preds)
    includes = any(isinstance(p, CarrierPred) and not (p.exclude or p.operating) for p in preds)
    if alliances > 1 or (alliances and includes):
        reasons.append("an alliance filter combined with another carrier or alliance filter")
    return reasons


def _parse_flight(flight: str) -> tuple[str, int] | None:
    m = _FLIGHT_RE.match(flight)
    return (m.group(1).upper(), int(m.group(2))) if m else None


def _leg_carriers(slc: Slice) -> list[tuple[str | None, set[str], str | None]]:
    """Per leg: (booking carrier, marketing carrier set, operating carrier). The
    booking carrier is the one the row is sold under (from `flights[i]`); the
    marketing set is that plus the codeshare sellers (`legs[i].marketing_carriers`)."""
    out: list[tuple[str | None, set[str], str | None]] = []
    n = max(len(slc.flights), len(slc.legs))
    for i in range(n):
        flight = slc.flights[i] if i < len(slc.flights) else ""
        leg = slc.legs[i] if i < len(slc.legs) else None
        marketing: set[str] = {c.upper() for c in leg.marketing_carriers} if leg else set()
        booking = parsed[0] if (parsed := _parse_flight(flight)) else None
        if booking:
            marketing.add(booking)
        operating = leg.operating_carrier.upper() if leg and leg.operating_carrier else None
        out.append((booking, marketing, operating))
    return out


def _carrier_pred_passes(slc: Slice, pred: CarrierPred) -> bool:
    legs = _leg_carriers(slc)
    if pred.operating:
        if pred.exclude:  # -OPAIRLINES — no leg operated by these
            # A leg with no operating carrier fails, as it fails an include:
            # the filter cannot tell, and must not keep a row Matrix drops.
            return not any(op is None or op in pred.codes for _, _, op in legs)
        return all(op in pred.codes for _, _, op in legs)  # O:/OPAIRLINES — all operated by these
    if pred.exclude:
        # ~UA+ / -AIRLINES: no leg booked under an excluded carrier, which is
        # Matrix's reading for the fare shown. The other sellers of a leg are
        # not the row's fare: AA100 sold also by BA stays under ~BA+, and so
        # does BA178 booked as AA6939. A leg with no booking carrier fails, as
        # an unknown operator fails the operating exclude.
        return not any(booking is None or booking in pred.codes for booking, _, _ in legs)
    # marketing include (LH+ / AIRLINES) — every leg sold by an allowed carrier.
    # Applied natively too; this is the correctness backstop if an fli code didn't map.
    return all(marketing & pred.codes for _, marketing, _ in legs)


def _slice_passes(slc: Slice, predicates: Iterable[Predicate]) -> bool:
    for p in predicates:
        if isinstance(p, CarrierPred):
            if not _carrier_pred_passes(slc, p):
                return False
        elif isinstance(p, ConnectionAirportPred):
            stop_codes = {s.code.upper() for s in slc.stops if s.code}
            if p.exclude:
                if stop_codes & p.codes:  # ~DFW / -CITIES — no connection at an excluded airport
                    return False
            elif stop_codes and not (stop_codes <= p.codes):
                # connect-at include: every connection must be an allowed airport
                return False
        elif isinstance(p, ExcludeCodesharePred):
            for _, marketing, op in _leg_carriers(slc):
                # codeshare = booked carrier(s) differ from the operating metal;
                # with no operating carrier it cannot be ruled out, so the leg fails
                if op is None or (marketing and op not in marketing):
                    return False
        elif isinstance(p, SpecificFlightPred) and not _flight_pred_passes(slc, p):
            return False
    return True


def _flight_pred_passes(slc: Slice, pred: SpecificFlightPred) -> bool:
    """Matrix's reading of a lone flight-number token: the slice is that flight
    and nothing else. Every leg is booked under the carrier and numbered in
    range and, for one flight, under one number (bare `AS21` is "No solutions"
    on JFK-LAX, where AS21 connects to other AS flights). A leg whose flight
    number the row does not state fails: the filter cannot tell."""
    flights = [_parse_flight(f) for f in slc.flights]
    if not flights or len(flights) < len(slc.legs):
        return False
    numbers: set[int] = set()
    for flight in flights:
        if flight is None or flight[0] != pred.carrier or not pred.low <= flight[1] <= pred.high:
            return False
        numbers.add(flight[1])
    return pred.several or len(numbers) == 1


def _layovers(row: Any) -> list[int]:
    """The minutes of each connection the row can measure: the page's own
    figure, else the clock difference at the connecting airport. That
    difference is an hour out across a daylight-saving change, and a negative
    one is such a change, which leaves the gap unmeasured."""
    stated: Sequence[int | None] = row.layovers
    gaps: list[int] = []
    for i, (a, b) in enumerate(itertools.pairwise(row.flight.legs)):
        gap = stated[i] if i < len(stated) else None
        if gap is None:
            gap = int((b.departure_datetime - a.arrival_datetime).total_seconds() // 60)
        if gap >= 0:
            gaps.append(gap)
    return gaps


def _stop_ceiling(predicates: Iterable[Predicate], max_stops: int | None) -> int | None:
    """The strictest of `max_stops` and every stop ceiling in `predicates`, or
    None when there is none. A negative `max_stops` is no limit."""
    limits = [p.max_stops for p in predicates if isinstance(p, StopsPred)]
    if max_stops is not None and max_stops >= 0:
        limits.append(max_stops)
    return min(limits, default=None)


def _within(stamp: Any, windows: Sequence[TimeWindow]) -> bool:
    clock = stamp.hour * 60 + stamp.minute
    return any(lo <= clock <= hi for lo, hi in map(time_bounds, windows))


def _row_passes(
    row: Any,
    predicates: Iterable[Predicate],
    times: Sequence[TimeWindow],
    arrivals: Sequence[TimeWindow] = (),
) -> bool:
    """The checks read off the raw row, which `models.Slice` does not carry.

    The duration is Google's own total: leg datetimes are local to each leg's
    airport, so the last arrival minus the first departure is off by the zone
    difference. The first departure must fall inside one of `times` and the
    last arrival inside one of `arrivals`, bounds included, to the minute:
    Google's own windows are in whole hours."""
    legs: Sequence[Any] = row.flight.legs
    if (times or arrivals) and not (
        legs
        and (not times or _within(legs[0].departure_datetime, times))
        and (not arrivals or _within(legs[-1].arrival_datetime, arrivals))
    ):
        return False
    return not any(_row_fails(row, p) for p in predicates)


def _row_fails(row: Any, pred: Predicate) -> bool:
    flight = row.flight
    match pred:
        case MaxDurationPred():
            return flight.duration is None or flight.duration > pred.minutes
        case ConnectTimePred():
            return any(
                (pred.min_minutes is not None and gap < pred.min_minutes)
                or (pred.max_minutes is not None and gap > pred.max_minutes)
                for gap in _layovers(row)
            )
        case ExcludeRedeyesPred():
            return any(map(_red_eye, flight.legs))
        case ExcludeOvernightsPred():
            return any(itertools.starmap(_overnight_stop, itertools.pairwise(flight.legs)))
        case CabinPred():
            # A leg Google states no cabin for cannot be shown to meet it.
            wanted = {_GOOGLE_CABIN[c] for c in pred.cabins}
            booked = [a.cabin for a in row.amenities][: len(flight.legs)]
            return len(booked) < len(flight.legs) or any(c not in wanted for c in booked)
        case _:
            return False


# Each `Cabin` as `_gflight_ids._CABIN` decodes a leg's cabin.
_GOOGLE_CABIN = {
    Cabin.COACH: "ECONOMY",
    Cabin.PREMIUM_COACH: "PREMIUM",
    Cabin.BUSINESS: "BUSINESS",
    Cabin.FIRST: "FIRST",
}


# A flight in the air between midnight and 05:00 local flew the night.
_NIGHT_ENDS_HOUR = 5
# The most a leg's local clocks can gain or lose on its own duration without
# crossing the date line.
_DATE_LINE_SHIFT = 12 * 60


def _red_eye(leg: Any) -> bool:
    """A red-eye leg, on its own local clocks: it lands on a later local date
    than it took off, or takes off 00:00-04:59, or its clocks and its duration
    differ by twelve hours or more. That last is a leg across the date line,
    where a night flight can land on its takeoff date (Tokyo 17:00 -> Los
    Angeles 10:00). The date rule matched Matrix's own flag on 73 of 74
    nonstops measured, none across the date line; the 74th (16:30 -> 00:52)
    Matrix keeps and this drops."""
    off, on = leg.departure_datetime, leg.arrival_datetime
    shift = (on - off).total_seconds() // 60 - leg.duration
    return on.date() > off.date() or off.hour < _NIGHT_ENDS_HOUR or abs(shift) >= _DATE_LINE_SHIFT


def _overnight_stop(arrived: Any, leaves: Any) -> bool:
    """A connection spent overnight, on the connecting airport's own clock:
    the next leg leaves on a later date than the arrival, or the arrival is
    00:00-04:59."""
    lands = arrived.arrival_datetime
    return leaves.departure_datetime.date() > lands.date() or lands.hour < _NIGHT_ENDS_HOUR


@dataclass
class StopDrops:
    """The rows `routing_keep` dropped for making more stops than the ceiling
    the page was asked for, and that ceiling. One per query: the page is asked
    for one ceiling, and every slice holds the same codes."""

    rows: int = 0
    ceiling: int | None = None


def routing_keep(
    per_slice_predicates: Sequence[Sequence[Predicate]],
    per_slice_times: Sequence[Sequence[TimeWindow]] = (),
    *,
    per_slice_arrivals: Sequence[Sequence[TimeWindow]] = (),
    max_price: int | None = None,
    currency: str = "USD",
    max_stops: int | None = None,
    stop_drops: StopDrops | None = None,
) -> Callable[[int, Any], bool] | None:
    """The per-leg filter `_gflight_ids.search_with_ids` applies to each board
    it is served: `keep(i, row)` is whether one Google Flights row passes slice
    `i`'s predicates, makes no more stops than `max_stops` or any stop ceiling
    among them, departs and lands inside its time windows and is priced in
    `currency` at or under `max_price`. None when nothing is asked of a row; a
    negative `max_stops` asks nothing.

    The cap and the stop ceiling are checked on every board, a round trip's
    outbound as well as each return: whether or not the page was asked for
    them, every row is held to them. Each row over the ceiling is counted in
    `stop_drops`, before any other check, so the count is every such row
    Google served."""
    if max_stops is not None and max_stops < 0:
        max_stops = None
    if (
        not any(per_slice_predicates)
        and not any(per_slice_times)
        and not any(per_slice_arrivals)
        and max_price is None
        and max_stops is None
    ):
        return None

    def keep(leg: int, row: Any) -> bool:
        preds = per_slice_predicates[leg] if leg < len(per_slice_predicates) else ()
        ceiling = _stop_ceiling(preds, max_stops)
        if ceiling is not None and len(row.flight.legs) - 1 > ceiling:
            if stop_drops is not None:
                stop_drops.rows += 1
                stop_drops.ceiling = ceiling
            return False
        if max_price is not None and not within_price_cap(
            row.flight.price, row.flight.currency, cap=max_price, cap_currency=currency
        ):
            return False
        times = per_slice_times[leg] if leg < len(per_slice_times) else ()
        arrivals = per_slice_arrivals[leg] if leg < len(per_slice_arrivals) else ()
        if not _row_passes(row, preds, times, arrivals):
            return False
        if not preds:
            return True
        # Deferred: the adapter pulls in the award client, which only a Google
        # Flights search with a routing filter has any use for.
        from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

        itn = fli_results_to_search_result([row]).solutions[0].itinerary
        return itn is None or _slice_passes(itn.slices[0], preds)

    return keep


def _row_check_name(pred: Predicate) -> str | None:
    match pred:
        case AlliancePred():
            return None  # Google's own filter; the rows are not checked
        case StopsPred():
            return None  # `row_check_names` names the strictest ceiling once
        case ConnectTimePred(min_minutes=int() as low, max_minutes=None):
            return f"a minimum layover ({low:d} min)"
        case ConnectTimePred(min_minutes=None, max_minutes=int() as high):
            return f"a maximum layover ({high:d} min)"
        case _:
            return next(iter(page_can_encode([pred])[1]), None)


def row_check_names(
    per_slice_predicates: Sequence[Sequence[Predicate]],
    per_slice_times: Sequence[Sequence[TimeWindow]] = (),
    *,
    per_slice_arrivals: Sequence[Sequence[TimeWindow]] = (),
    max_price: int | None = None,
    currency: str = "USD",
    max_stops: int | None = None,
) -> list[str]:
    """Every check `routing_keep` applies to a row, in the user's vocabulary,
    for the sentence that says what emptied a board."""
    names = [name for preds in per_slice_predicates for p in preds if (name := _row_check_name(p))]
    ceiling = _stop_ceiling(itertools.chain.from_iterable(per_slice_predicates), max_stops)
    if ceiling is not None:
        names.append(f"a stop ceiling of {ceiling:d}")
    for label, times in zip(("departure", "return"), per_slice_times, strict=False):
        if times:
            names.append(f"a {label}-time window ({', '.join(map(window_label, times))})")
    for label, times in zip(("an arrival", "a return arrival"), per_slice_arrivals, strict=False):
        if times:
            names.append(f"{label}-time window ({', '.join(map(window_label, times))})")
    if max_price is not None:
        names.append(f"a price cap of {currency} {max_price:d}")
    return list(dict.fromkeys(names))


def _itinerary_passes(it: Itinerary, per_slice_predicates: Sequence[Sequence[Predicate]]) -> bool:
    itn = it.itinerary
    if itn is None:
        return True
    for i, slc in enumerate(itn.slices):
        preds = per_slice_predicates[i] if i < len(per_slice_predicates) else ()
        if not _slice_passes(slc, preds):
            return False
    return True


def apply_postfilter(
    result: SearchResult, per_slice_predicates: Sequence[Sequence[Predicate]]
) -> SearchResult:
    """Drop solutions whose slice `i` violates `per_slice_predicates[i]`. Mutates
    and returns `result` (solutions + solutionCount)."""
    if not any(per_slice_predicates):
        return result
    kept = [it for it in result.solutions if _itinerary_passes(it, per_slice_predicates)]
    result.solutions = kept
    result.solution_count = len(kept)
    return result
