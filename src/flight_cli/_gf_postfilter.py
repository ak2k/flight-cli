"""Post-filter Google Flights results against the constraints a search asked
for.

Runs only on the gflight path (Matrix legs don't carry the per-leg carrier
identity these predicates need). `search_page_reasons` is the search page's
gate: a predicate rides there when the page's tfs= encodes it or this module
post-filters it on the full board. The date grids do not reach here at all:
they return prices per date and there are no itineraries to post-filter, so
they refuse what they cannot ask for (`_gf_dategrid.grid_can_serve`,
`_gf_calgraph.page_blocker`). Anything this module can't evaluate (red-eyes,
overnight stops) escalates the whole query to Matrix rather than being
silently dropped.

Google has ignored a field it was sent (a carrier exclude on JFK-LHR), so what
the page encodes is checked here too wherever the row shows it: the carrier
include, the maximum duration, the layover minutes and the departure time.
The stop ceiling is left to Google's own filter, and so is an alliance, for
want of a membership table.

Supported Tier-2 predicates:
  - operating carrier include/exclude (`O:LH+`, `OPAIRLINES`, `-OPAIRLINES`)
  - marketing-carrier exclude (`~UA+`, `-AIRLINES`)
  - connection-airport exclude (`~DFW`, `-CITIES`)
  - no codeshare (`-CODESHARE`)
  - specific flight # / range (`UA882`, `UA1000-2000`)
  - minimum layover (`MINCONNECT`), on the raw row and encoded as well
"""

from __future__ import annotations

import itertools
import re
from typing import TYPE_CHECKING, Any

from .domain import time_bounds
from .routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    ExcludeCodesharePred,
    MaxDurationPred,
    SpecificFlightPred,
    StopsPred,
    Tier,
    page_can_encode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .domain import TimeOfDay
    from .models import Itinerary, SearchResult, Slice
    from .routing_predicates import Predicate

# Tier-2 predicate types `_slice_passes` evaluates on a parsed slice. A minimum
# layover is checked on the raw row instead (`_row_passes`); red-eyes and
# overnights escalate to Matrix at the gate.
_SUPPORTED: tuple[type, ...] = (
    CarrierPred,
    ConnectionAirportPred,
    ExcludeCodesharePred,
    SpecificFlightPred,
)

_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})(\d+)$", re.IGNORECASE)


def can_postfilter(pred: Predicate) -> bool:
    """True if this predicate is either not our concern (Tier 1 native / Tier 3
    Matrix-only, handled elsewhere) or a Tier-2 predicate we can evaluate here."""
    if pred.tier is not Tier.GF_POSTFILTER:
        return True
    return isinstance(pred, _SUPPORTED)


def _served_by_postfilter(pred: Predicate) -> bool:
    """A Tier-2 predicate the search page can serve by post-filtering its board.

    Not flight numbers and not connection-airport excludes, although this module
    evaluates both: Matrix reads them positionally and the filter here does not.
    Bare `AS21` is one flight to Matrix ("No solutions" where the filter keeps
    AS21 connections), and `F* ~DUB F*` is one connection, not at DUB (Matrix
    drops the nonstops the filter keeps)."""
    return (
        pred.tier is Tier.GF_POSTFILTER
        and can_postfilter(pred)
        and not isinstance(pred, SpecificFlightPred | ConnectionAirportPred)
    )


def _served_by_page(pred: Predicate) -> bool:
    """A predicate the search page's tfs= encodes beyond the stop ceiling
    `page_can_encode` admits. That function also answers for the date grids,
    which have no rows to check these on."""
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


def search_page_reasons(predicates: Iterable[Predicate], stops: int | None = None) -> list[str]:
    """Why the search page can't serve `predicates` beside a `--stops` of
    `stops`: one reason per predicate that its tfs= cannot encode and this
    module does not post-filter. Empty when the page serves them all.

    The page is asked for the strictest stop limit alone
    (`fli_bridge.apply_gf_native_filters`), so only that one has to fit.
    3.6 is one include list, so an alliance written beside a carrier or another
    alliance asks Google for either, and no row check narrows an alliance back."""
    preds = list(predicates)
    limits = [p.max_stops for p in preds if isinstance(p, StopsPred)]
    if stops is not None:
        limits.append(stops)
    reasons = page_can_encode([StopsPred(min(limits))])[1] if limits else []
    reasons += [
        reason
        for p in preds
        if not (isinstance(p, StopsPred) or _served_by_postfilter(p) or _served_by_page(p))
        for reason in (
            ["a maximum layover of 0 min"]
            if isinstance(p, ConnectTimePred) and p.max_minutes == 0
            else page_can_encode([p])[1]
        )
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
        elif isinstance(p, SpecificFlightPred):
            flights = [f for fl in slc.flights if (f := _parse_flight(fl))]
            if not any(c == p.carrier and p.low <= n <= p.high for c, n in flights):
                return False
    return True


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


def _row_passes(row: Any, predicates: Iterable[Predicate], times: Sequence[TimeOfDay]) -> bool:
    """The checks read off the raw row, which `models.Slice` does not carry.

    The duration is Google's own total: leg datetimes are local to each leg's
    airport, so the last arrival minus the first departure is off by the zone
    difference. A departure time must fall inside one of `times`, bounds
    included, to the minute: Google's own window is in whole hours."""
    flight = row.flight
    legs: Sequence[Any] = flight.legs
    if times:
        if not legs:
            return False
        dep = legs[0].departure_datetime
        clock = dep.hour * 60 + dep.minute
        if not any(lo <= clock <= hi for lo, hi in map(time_bounds, times)):
            return False
    for p in predicates:
        if isinstance(p, MaxDurationPred):
            if flight.duration is None or flight.duration > p.minutes:
                return False
        elif isinstance(p, ConnectTimePred):
            for gap in _layovers(row):
                if p.min_minutes is not None and gap < p.min_minutes:
                    return False
                if p.max_minutes is not None and gap > p.max_minutes:
                    return False
    return True


def routing_keep(
    per_slice_predicates: Sequence[Sequence[Predicate]],
    per_slice_times: Sequence[Sequence[TimeOfDay]] = (),
) -> Callable[[int, Any], bool] | None:
    """The per-leg filter `_gflight_ids.search_with_ids` applies to each board
    it is served: `keep(i, row)` is whether one Google Flights row passes slice
    `i`'s predicates and departs inside its time window. None when no slice
    carries either."""
    if not any(per_slice_predicates) and not any(per_slice_times):
        return None

    def keep(leg: int, row: Any) -> bool:
        preds = per_slice_predicates[leg] if leg < len(per_slice_predicates) else ()
        times = per_slice_times[leg] if leg < len(per_slice_times) else ()
        if not _row_passes(row, preds, times):
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
        case AlliancePred() | StopsPred():
            return None  # Google's own filter; the rows are not checked
        case ConnectTimePred(min_minutes=int() as low, max_minutes=None):
            return f"a minimum layover ({low:d} min)"
        case ConnectTimePred(min_minutes=None, max_minutes=int() as high):
            return f"a maximum layover ({high:d} min)"
        case _:
            return next(iter(page_can_encode([pred])[1]), None)


def row_check_names(
    per_slice_predicates: Sequence[Sequence[Predicate]],
    per_slice_times: Sequence[Sequence[TimeOfDay]] = (),
) -> list[str]:
    """Every check `routing_keep` applies to a row, in the user's vocabulary,
    for the sentence that says what emptied a board."""
    names = [name for preds in per_slice_predicates for p in preds if (name := _row_check_name(p))]
    for label, times in zip(("departure", "return"), per_slice_times, strict=False):
        if times:
            names.append(f"a {label}-time window ({', '.join(t.value for t in times)})")
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
