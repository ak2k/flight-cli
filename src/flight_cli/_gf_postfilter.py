"""Post-filter Google Flights results against Tier-2 predicates the GF query
can't express natively.

Runs only on the gflight path (Matrix legs don't carry the per-leg carrier
identity these predicates need). The gate (`gf_can_serve`) only routes a query
to GF when every Tier-2 predicate here is *supported* — anything this module
can't evaluate (min-layover, red-eyes, overnight stops) escalates the whole
query to Matrix rather than being silently dropped.

Supported Tier-2 predicates:
  - operating carrier include/exclude (`O:LH+`, `OPAIRLINES`, `-OPAIRLINES`)
  - marketing-carrier exclude (`~UA+`, `-AIRLINES`)
  - connection-airport exclude (`~DFW`, `-CITIES`)
  - no codeshare (`-CODESHARE`)
  - specific flight # / range (`UA882`, `UA1000-2000`)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .routing_predicates import (
    CarrierPred,
    ConnectionAirportPred,
    ExcludeCodesharePred,
    SpecificFlightPred,
    Tier,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import Itinerary, SearchResult, Slice
    from .routing_predicates import ClassifiedConstraints, Predicate

# Tier-2 predicate types this module can evaluate. Other Tier-2 predicates
# (ConnectTimePred min, red-eyes, overnights) need per-segment times we don't
# yet thread through, so they escalate to Matrix via `gf_can_serve`.
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


def gf_can_serve(constraints: ClassifiedConstraints) -> bool:
    """Whether Google Flights alone can honor every predicate: no Tier-3, and
    every Tier-2 predicate is post-filterable here."""
    if constraints.requires_matrix:
        return False
    return all(can_postfilter(p) for p in constraints.predicates)


def _parse_flight(flight: str) -> tuple[str, int] | None:
    m = _FLIGHT_RE.match(flight)
    return (m.group(1).upper(), int(m.group(2))) if m else None


def _leg_carriers(slc: Slice) -> list[tuple[set[str], str | None]]:
    """Per leg: (marketing carrier set, operating carrier). The marketing set is
    the booking carrier (from `flights[i]`) plus the codeshare sellers
    (`legs[i].marketing_carriers`)."""
    out: list[tuple[set[str], str | None]] = []
    n = max(len(slc.flights), len(slc.legs))
    for i in range(n):
        flight = slc.flights[i] if i < len(slc.flights) else ""
        leg = slc.legs[i] if i < len(slc.legs) else None
        marketing: set[str] = {c.upper() for c in leg.marketing_carriers} if leg else set()
        if parsed := _parse_flight(flight):
            marketing.add(parsed[0])
        operating = leg.operating_carrier.upper() if leg and leg.operating_carrier else None
        out.append((marketing, operating))
    return out


def _carrier_pred_passes(slc: Slice, pred: CarrierPred) -> bool:
    legs = _leg_carriers(slc)
    if pred.operating and not pred.exclude:  # O:/OPAIRLINES — all legs operated by one of these
        return all(op in pred.codes for _, op in legs)
    if pred.operating and pred.exclude:  # -OPAIRLINES — no leg operated by these
        return not any(op in pred.codes for _, op in legs)
    # marketing exclude (~UA / -AIRLINES) — no leg sold by an excluded carrier
    return not any(marketing & pred.codes for marketing, _ in legs)


def _slice_passes(slc: Slice, predicates: Iterable[Predicate]) -> bool:
    for p in predicates:
        if isinstance(p, CarrierPred):
            if p.tier is Tier.GF_POSTFILTER and not _carrier_pred_passes(slc, p):
                return False
        elif isinstance(p, ConnectionAirportPred) and p.exclude:
            stop_codes = {s.code.upper() for s in slc.stops if s.code}
            if stop_codes & p.codes:
                return False
        elif isinstance(p, ExcludeCodesharePred):
            for marketing, op in _leg_carriers(slc):
                # codeshare = booked carrier(s) differ from the operating metal
                if op is not None and marketing and op not in marketing:
                    return False
        elif isinstance(p, SpecificFlightPred):
            flights = [f for fl in slc.flights if (f := _parse_flight(fl))]
            if not any(c == p.carrier and p.low <= n <= p.high for c, n in flights):
                return False
    return True


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
