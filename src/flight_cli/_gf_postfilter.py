"""Post-filter Google Flights results against Tier-2 predicates the GF query
can't express natively.

Runs only on the gflight path (Matrix legs don't carry the per-leg carrier
identity these predicates need). `search_page_reasons` is the search page's
gate: a predicate rides there when the page's tfs= encodes it or this module
post-filters it on the full board. The date grid does not reach here at all —
`_gf_dategrid.grid_can_serve` admits Tier-1 only, because the grid returns
prices per date and there are no itineraries to post-filter. Anything this
module can't evaluate (min-layover, red-eyes, overnight stops) escalates the
whole query to Matrix rather than being silently dropped.

Supported Tier-2 predicates:
  - operating carrier include/exclude (`O:LH+`, `OPAIRLINES`, `-OPAIRLINES`)
  - marketing-carrier exclude (`~UA+`, `-AIRLINES`)
  - connection-airport exclude (`~DFW`, `-CITIES`)
  - no codeshare (`-CODESHARE`)
  - specific flight # / range (`UA882`, `UA1000-2000`)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .routing_predicates import (
    CarrierPred,
    ConnectionAirportPred,
    ExcludeCodesharePred,
    SpecificFlightPred,
    Tier,
    page_can_encode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .models import Itinerary, SearchResult, Slice
    from .routing_predicates import Predicate

# Tier-2 predicate types this module can evaluate. Other Tier-2 predicates
# (ConnectTimePred min, red-eyes, overnights) need per-segment times we don't
# yet thread through, so they escalate to Matrix at the gate.
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


def search_page_reasons(predicates: Iterable[Predicate]) -> list[str]:
    """Why the search page can't serve `predicates`: one reason per predicate
    that its tfs= cannot encode and this module does not post-filter. Empty
    when the page serves them all."""
    return [
        reason
        for p in predicates
        if not _served_by_postfilter(p)
        for reason in page_can_encode([p])[1]
    ]


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


def routing_keep(
    per_slice_predicates: Sequence[Sequence[Predicate]],
) -> Callable[[int, Any], bool] | None:
    """The per-leg filter `_gflight_ids.search_with_ids` applies to each board
    it is served: `keep(i, row)` is whether one Google Flights row passes slice
    `i`'s predicates. None when no slice carries any."""
    if not any(per_slice_predicates):
        return None
    # Deferred: the adapter pulls in the award client, which only a Google
    # Flights search with a routing filter has any use for.
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    def keep(leg: int, row: Any) -> bool:
        preds = per_slice_predicates[leg] if leg < len(per_slice_predicates) else ()
        if not preds:
            return True
        itn = fli_results_to_search_result([row]).solutions[0].itinerary
        return itn is None or _slice_passes(itn.slices[0], preds)

    return keep


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
