"""Join Matrix cash itineraries to PointsPath award flights.

Match key: (normalized first-segment flight number, ISO departure date).
Same key can appear at most once per side per day, so a dict-lookup is enough.

Outputs MatchedFare records, one per cash itinerary, with optional award
data attached. Caller renders.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from ..models import Itinerary, SearchResult
from .models import (
    AirlineSearchResponse, OutboundFlight, PerCabinMilesPricing,
    PricingInfo, PricingInfoResponse,
)

MatchKey = tuple[str, str]   # (FLIGHT_NUMBER_UPPER_NOSPACE, "YYYY-MM-DD")


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


def cash_match_key(it: Itinerary, slice_index: int = 0) -> Optional[MatchKey]:
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


@dataclass
class CabinAward:
    """One cabin's award price for a single flight."""
    cabin: str                    # "Economy" / "Business" / etc.
    miles: int
    tax_usd: float
    tax_currency: str
    is_basic_economy: Optional[bool] = None


@dataclass
class AwardOption:
    """All cabin offerings for a single matched flight, plus transfer info."""
    airline: str                  # PointsPath canonical name (e.g. "United")
    miles_to_cash_ratio: float    # PointsPath valuation (¢/mi)
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
        out.append(CabinAward(
            cabin=p.cabinClass,
            miles=pp.perPassengerMilesAmount,
            tax_usd=pp.perPassengerTaxAmountUsd,
            tax_currency=pp.taxCurrencyCode or "USD",
            is_basic_economy=pp.isBasicEconomyFare,
        ))
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
    """Outer-join cash itineraries onto award flights by (flight#, date).

    Cash itineraries with no award match keep an empty `awards` list — caller
    decides whether to render them or filter to inner-join.

    `slice_index` selects which leg of each Itinerary to match against (0 for
    outbound, 1 for return on a round-trip, etc).

    `use_inbound` reads from `inboundFlights` instead of `outboundFlights` —
    set when joining the return leg of a round-trip query whose response is
    a single bidirectional record.
    """
    pricing_idx = _index_pricing(pricing)

    # Build award index. One key may surface from multiple airlines (codeshares),
    # so we keep a list per key.
    award_idx: dict[MatchKey, list[tuple[str, OutboundFlight]]] = {}
    for airline, resp in award_by_airline.items():
        flights = resp.inboundFlights if use_inbound else resp.outboundFlights
        for of in flights:
            k = award_match_key(of)
            if not k[0]:
                continue
            award_idx.setdefault(k, []).append((airline, of))

    out: list[MatchedFare] = []
    for it in search.solutions:
        k = cash_match_key(it, slice_index=slice_index)
        awards: list[AwardOption] = []
        if k and k in award_idx:
            for airline, of in award_idx[k]:
                pi = pricing_idx.get(airline)
                awards.append(AwardOption(
                    airline=airline,
                    miles_to_cash_ratio=pi.milesToCashRatio if pi else 0.0,
                    flight=of,
                    cabins=_cabin_awards(of.perCabinMilesPricing),
                    funding_banks=[
                        b.bank for b in (pi.bankPointsInfos if pi else [])
                    ],
                ))
        out.append(MatchedFare(itinerary=it, awards=awards))
    return out


