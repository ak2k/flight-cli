"""Pydantic shape tests. Real captured-JSON snippets in fixtures/."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
from pydantic import ValidationError

from flight_cli.pp.models import (
    AirlineSearchResponse,
    OutboundFlight,
    PricingInfoResponse,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
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
    f = OutboundFlight.model_validate(
        {
            "origin": "JFK",
            "destination": "LHR",
            "localDepartureDateTime": "2026-06-09T22:00:00",
            "localArrivalDateTime": "2026-06-10T10:25:00",
            "firstFlightNumber": "UA146",
            "perCabinMilesPricing": None,
        }
    )
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


# ─────────────────────── stops: bare codes or stop objects ───────────────────────


def _flight(stops: object) -> dict[str, Any]:
    return {
        "origin": "EWR",
        "destination": "LAX",
        "localDepartureDateTime": "2026-10-20T06:30:00",
        "localArrivalDateTime": "2026-10-20T13:24:00",
        "firstFlightNumber": "AS1399",
        "stops": stops,
    }


def test_airline_search_stop_objects_real_capture():
    """PointsPath sends each stop as `{airport, layoverDurationMinutes}`; the
    whole airline's answer must validate and keep its connection codes."""
    r = AirlineSearchResponse.model_validate(_load("airline_search_alaska_stop_objects.json"))
    assert [(f.firstFlightNumber, f.stops) for f in r.outboundFlights] == [
        ("AS1399", ["PDX"]),
        ("AS21", ["SEA"]),
        ("AS287", []),
    ]


def test_stops_mixed_codes_and_objects_keep_order():
    f = OutboundFlight.model_validate(
        _flight(["SEA", {"airport": "PDX", "layoverDurationMinutes": 76}]),
    )
    assert f.stops == ["SEA", "PDX"]


# Guards: inputs the codes-only model already handled must keep their meaning,
# and a stop neither shape describes must still fail validation rather than
# reach the matcher as "no connection evidence".


@pytest.mark.parametrize(
    ("stops", "want"),
    [(None, []), ([], []), (["DFW"], ["DFW"]), (["dfw", "LHR"], ["dfw", "LHR"])],
)
def test_stops_code_shapes_unchanged(stops: object, want: list[str]):
    assert OutboundFlight.model_validate(_flight(stops)).stops == want


@pytest.mark.parametrize(
    "element",
    [{"code": "PDX"}, {"airport": None}, {"airport": 7}, 7, None, ["PDX"]],
)
def test_stops_unknown_element_raises(element: object):
    with pytest.raises(ValidationError) as ei:
        OutboundFlight.model_validate(_flight([element]))
    assert ei.value.errors()[0]["loc"] == ("stops", 0)


def test_stops_bare_string_raises():
    """A lone string is not a list of codes; iterating it would yield one
    bogus code per letter."""
    with pytest.raises(ValidationError) as ei:
        OutboundFlight.model_validate(_flight("PDX"))
    assert ei.value.errors()[0]["type"] == "list_type"


_SECRET_MARKERS = re.compile(r"eyJ|Bearer|access_token|refresh_token|[\w.+-]+@[\w-]+\.\w")


@pytest.mark.parametrize("path", sorted(FIX.glob("*.json")), ids=lambda p: p.name)
def test_fixture_carries_no_credentials(path: pathlib.Path):
    assert _SECRET_MARKERS.search(path.read_text()) is None
