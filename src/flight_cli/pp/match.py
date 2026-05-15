"""Join Matrix cash itineraries to PointsPath award flights.

Primary key: (normalized first-segment flight number, ISO departure date).
Same key can appear at most once per side per day, so a dict-lookup is enough.

Codeshare-fallback key: (origin, destination, departure datetime to minute).
Matrix returns codeshares under the *marketing* flight number (e.g. AA6939
JFK→LHR), while PointsPath returns the same physical aircraft under the
*operating* flight number (e.g. BA174 — surfaced inside the American PP
query, because PP attributes codeshares to the operator). Flight-number
keys can't bridge that, but route+time can: both sources read the same
airline-published schedule, so origin+dest+minute is a near-tight identity.

Outputs MatchedFare records, one per cash itinerary, with optional award
data attached. Caller renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Itinerary, SearchResult
    from .models import (
        AirlineSearchResponse,
        OutboundFlight,
        PerCabinMilesPricing,
        PricingInfo,
        PricingInfoResponse,
    )

MatchKey = tuple[str, str]  # (FLIGHT_NUMBER_UPPER_NOSPACE, "YYYY-MM-DD")
RouteTimeKey = tuple[str, str, str]  # (ORIGIN_UPPER, DEST_UPPER, "YYYY-MM-DDTHH:MM")

_ISO_MINUTE_LEN = 16  # "YYYY-MM-DDTHH:MM"


def _norm_fn(fn: str | None) -> str:
    return (fn or "").upper().replace(" ", "")


def _iso_date(s: str | None) -> str:
    """Best-effort isolate the YYYY-MM-DD prefix from various formats."""
    if not s:
        return ""
    # PointsPath: "2026-06-09T22:00:00"
    # Matrix: "2026-06-09T22:00" / "2026-06-09 22:00"
    s = s.replace(" ", "T")
    return s[:10]


def _iso_minute(s: str | None) -> str:
    """Trim a datetime string to YYYY-MM-DDTHH:MM (minute precision). Tolerant
    of the space-separator Matrix sometimes returns. '' on missing/short."""
    if not s:
        return ""
    s = s.replace(" ", "T")
    return s[:_ISO_MINUTE_LEN] if len(s) >= _ISO_MINUTE_LEN else ""


def cash_match_key(it: Itinerary, slice_index: int = 0) -> MatchKey | None:
    """Build the match key from a Matrix itinerary's slice's first flight.

    Default slice_index=0 = outbound leg. For round-trips pass 1 to match the
    return leg; for multi-city pass 2, 3, etc.
    """
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return None
    s = itn.slices[slice_index]
    flights = s.flights or []
    if not flights:
        return None
    fn = _norm_fn(flights[0])
    dep = _iso_date(s.departure)
    if not fn or not dep:
        return None
    return (fn, dep)


def award_match_key(of: OutboundFlight) -> MatchKey:
    return (_norm_fn(of.firstFlightNumber), _iso_date(of.localDepartureDateTime))


def cash_route_time_key(it: Itinerary, slice_index: int = 0) -> RouteTimeKey | None:
    """Codeshare-fallback key: origin, destination, minute-precision departure.

    Doesn't require `slice.flights` to be populated — origin/dest/departure
    are enough to anchor the same physical flight on the award side."""
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return None
    s = itn.slices[slice_index]
    o = ((s.origin.code if s.origin else None) or "").upper()
    d = ((s.destination.code if s.destination else None) or "").upper()
    t = _iso_minute(s.departure)
    if not (o and d and t):
        return None
    return (o, d, t)


def award_route_time_key(of: OutboundFlight) -> RouteTimeKey | None:
    o = (of.origin or "").upper()
    d = (of.destination or "").upper()
    t = _iso_minute(of.localDepartureDateTime)
    if not (o and d and t):
        return None
    return (o, d, t)


@dataclass
class CabinAward:
    """One cabin's award price for a single flight."""

    cabin: str  # "Economy" / "Business" / etc.
    miles: int
    tax_usd: float
    tax_currency: str
    is_basic_economy: bool | None = None


@dataclass
class AwardOption:
    """All cabin offerings for a single matched flight, plus transfer info."""

    airline: str  # PointsPath canonical name (e.g. "United")
    miles_to_cash_ratio: float  # PointsPath valuation (¢/mi)
    flight: OutboundFlight
    cabins: list[CabinAward] = field(default_factory=list)
    funding_banks: list[str] = field(default_factory=list)


@dataclass
class MatchedFare:
    """One cash itinerary with zero-or-more award options attached."""

    itinerary: Itinerary
    awards: list[AwardOption] = field(default_factory=list)


def _cabin_awards(pricing: list[PerCabinMilesPricing]) -> list[CabinAward]:
    out: list[CabinAward] = []
    for p in pricing:
        pp = p.perPassengerPricing
        if not pp or pp.perPassengerMilesAmount <= 0:
            continue
        out.append(
            CabinAward(
                cabin=p.cabinClass,
                miles=pp.perPassengerMilesAmount,
                tax_usd=pp.perPassengerTaxAmountUsd,
                tax_currency=pp.taxCurrencyCode or "USD",
                is_basic_economy=pp.isBasicEconomyFare,
            )
        )
    return out


def _index_pricing(pi: PricingInfoResponse) -> dict[str, PricingInfo]:
    return {p.airline: p for p in pi.pricingInfos}


def join(
    search: SearchResult,
    award_by_airline: dict[str, AirlineSearchResponse],
    pricing: PricingInfoResponse,
    *,
    slice_index: int = 0,
    use_inbound: bool = False,
) -> list[MatchedFare]:
    """Outer-join cash itineraries onto award flights.

    Match strategy: primary by (flight#, date), then a route+time fallback to
    catch codeshares (Matrix's marketing flight# won't equal PP's operating
    flight#, but origin+dest+minute identifies the same physical flight).
    Hits from both keys are unioned and deduped by OutboundFlight identity, so
    non-codeshare flights aren't double-attached.

    Cash itineraries with no award match keep an empty `awards` list — caller
    decides whether to render them or filter to inner-join.

    `slice_index` selects which leg of each Itinerary to match against (0 for
    outbound, 1 for return on a round-trip, etc).

    `use_inbound` reads from `inboundFlights` instead of `outboundFlights` —
    set when joining the return leg of a round-trip query whose response is
    a single bidirectional record.
    """
    pricing_idx = _index_pricing(pricing)

    # Build both award indexes in one pass over the flights. A single flight
    # may appear under both — that's expected; per-itinerary dedup in the
    # join loop keeps the output clean.
    fn_idx: dict[MatchKey, list[tuple[str, OutboundFlight]]] = {}
    rt_idx: dict[RouteTimeKey, list[tuple[str, OutboundFlight]]] = {}
    for airline, resp in award_by_airline.items():
        flights = resp.inboundFlights if use_inbound else resp.outboundFlights
        for of in flights:
            fn_k = award_match_key(of)
            if fn_k[0]:
                fn_idx.setdefault(fn_k, []).append((airline, of))
            rt_k = award_route_time_key(of)
            if rt_k:
                rt_idx.setdefault(rt_k, []).append((airline, of))

    out: list[MatchedFare] = []
    for it in search.solutions:
        awards: list[AwardOption] = []
        seen_ids: set[int] = set()

        # Collect (airline, OutboundFlight) hits from both keys, then convert
        # to AwardOption once per unique flight.
        hits: list[tuple[str, OutboundFlight]] = []
        fn_k = cash_match_key(it, slice_index=slice_index)
        if fn_k and fn_k in fn_idx:
            hits.extend(fn_idx[fn_k])
        rt_k = cash_route_time_key(it, slice_index=slice_index)
        if rt_k and rt_k in rt_idx:
            hits.extend(rt_idx[rt_k])

        for airline, of in hits:
            if id(of) in seen_ids:
                continue
            seen_ids.add(id(of))
            pi = pricing_idx.get(airline)
            awards.append(
                AwardOption(
                    airline=airline,
                    miles_to_cash_ratio=pi.milesToCashRatio if pi else 0.0,
                    flight=of,
                    cabins=_cabin_awards(of.perCabinMilesPricing),
                    funding_banks=[b.bank for b in (pi.bankPointsInfos if pi else [])],
                )
            )

        out.append(MatchedFare(itinerary=it, awards=awards))
    return out
