"""Pydantic shape tests. Real captured-JSON snippets in fixtures/."""
from __future__ import annotations
import json
import pathlib

from flight_cli.pp.models import (
    AirlineSearchResponse, OutboundFlight, PricingInfoResponse,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_airline_search_parses_real_capture():
    r = AirlineSearchResponse.model_validate(_load("airline_search_united.json"))
    assert len(r.outboundFlights) == 1
    f = r.outboundFlights[0]
    assert f.firstFlightNumber == "UA146"
    assert f.origin == "EWR"
    assert f.destination == "LHR"
    assert f.localDepartureDateTime.startswith("2026-06-09T22:00")
    # First cabin has full pricing
    economy = f.perCabinMilesPricing[0]
    assert economy.cabinClass == "Economy"
    assert economy.perPassengerPricing is not None
    assert economy.perPassengerPricing.perPassengerMilesAmount == 47000
    # Second cabin has perPassengerPricing=null (no availability)
    business = f.perCabinMilesPricing[1]
    assert business.cabinClass == "Business"
    assert business.perPassengerPricing is None


def test_airline_search_inbound_null_coerced():
    """PointsPath returns inboundFlights=null on one-way responses; we coerce
    to [] so callers can iterate without None-checks."""
    r = AirlineSearchResponse.model_validate(_load("airline_search_united.json"))
    assert r.inboundFlights == []


def test_outbound_flight_pricing_null_coerced():
    """`perCabinMilesPricing` arrives as null for some routes; coerce to []."""
    f = OutboundFlight.model_validate({
        "origin": "JFK", "destination": "LHR",
        "localDepartureDateTime": "2026-06-09T22:00:00",
        "localArrivalDateTime": "2026-06-10T10:25:00",
        "firstFlightNumber": "UA146",
        "perCabinMilesPricing": None,
    })
    assert f.perCabinMilesPricing == []


def test_pricing_info_parses_real_capture():
    p = PricingInfoResponse.model_validate(_load("pricing_info.json"))
    by_airline = {pi.airline: pi for pi in p.pricingInfos}
    assert "United" in by_airline
    united = by_airline["United"]
    assert united.milesToCashRatio == 0.0125
    banks = {b.bank for b in united.bankPointsInfos}
    assert banks == {"Chase", "Bilt"}


def test_pricing_info_null_bankPointsInfos_coerced():
    """American's `bankPointsInfos` is null in our fixture (we observed this
    in real responses). Should coerce to [] so consumers don't crash."""
    p = PricingInfoResponse.model_validate(_load("pricing_info.json"))
    american = next(pi for pi in p.pricingInfos if pi.airline == "American")
    assert american.bankPointsInfos == []


def test_pricing_info_active_bonus_preserved():
    """Active transfer bonuses round-trip through parsing — caller relies on
    `isBonusActive` and `conversionExpiryDate` to surface deal urgency."""
    p = PricingInfoResponse.model_validate(_load("pricing_info.json"))
    af = next(pi for pi in p.pricingInfos if pi.airline == "AirFrance")
    chase = next(b for b in af.bankPointsInfos if b.bank == "Chase")
    assert chase.isBonusActive is True
    assert chase.conversionExpiryDate == "2026-05-28T00:00:00Z"
    assert chase.conversionValue == 0.8333


def test_pricing_info_response_handles_null_top_level_list():
    """Defensive: even pricingInfos itself can come back null on errors."""
    p = PricingInfoResponse.model_validate({"pricingInfos": None})
    assert p.pricingInfos == []
