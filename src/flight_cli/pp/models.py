"""Pydantic v2 shapes for PointsPath API responses.

Mirrors the `_Loose`-extra style from src/flight_cli/models.py — PointsPath
adds and removes fields without notice; we capture the parts we use and
let the rest pass through.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, field_validator


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


def _none_to_empty_list(v: Any) -> Any:
    """PointsPath returns null for some list fields (`bankPointsInfos`,
    `outboundFlights` for shoulder dates, etc). Coerce to [] so consumers
    don't crash."""
    return [] if v is None else v


# ─────────────────────────── /api/airline-search ───────────────────────────

class PerPassengerPricing(_Loose):
    perPassengerMilesAmount: int = 0
    perPassengerTaxAmountUsd: float = 0.0
    taxCurrencyCode: str = "USD"
    isBasicEconomyFare: Optional[bool] = None


class OneWayPricing(_Loose):
    perPassengerMilesAmount: int = 0
    perPassengerTaxAmountUsd: float = 0.0
    taxCurrencyCode: str = ""
    isBasicEconomyFare: Optional[bool] = None


class SelectedFlightState(_Loose):
    oneWayPricing: Optional[OneWayPricing] = None
    airlineState: Optional[dict] = None


class PerCabinMilesPricing(_Loose):
    cabinClass: str
    perPassengerPricing: Optional[PerPassengerPricing] = None
    selectedFlightState: Optional[SelectedFlightState] = None


class OutboundFlight(_Loose):
    origin: str
    destination: str
    localDepartureDateTime: str  # ISO local without TZ, e.g. "2026-06-09T22:00:00"
    localArrivalDateTime: str
    firstFlightNumber: str
    googleAirlineName: Optional[str] = None
    numConnections: int = 0
    externalId: Optional[str] = None
    matchedGoogleFlightId: Optional[str] = None
    matchedGoogleFlightCashPriceUsd: Optional[float] = None
    perCabinMilesPricing: list[PerCabinMilesPricing] = []

    _none_pricing = field_validator("perCabinMilesPricing", mode="before")(_none_to_empty_list)


class AirlineSearchResponse(_Loose):
    outboundFlights: list[OutboundFlight] = []
    inboundFlights: list[OutboundFlight] = []
    roundTripReturnState: Optional[dict] = None

    _none_out = field_validator("outboundFlights", mode="before")(_none_to_empty_list)
    _none_in = field_validator("inboundFlights", mode="before")(_none_to_empty_list)


# ─────────────────────────── /api/pricing-info ─────────────────────────────

class BankPointsInfo(_Loose):
    bank: str
    conversionValue: float = 1.0
    defaultConversionValue: float = 1.0
    isBonusActive: bool = False
    conversionExpiryDate: Optional[str] = None


class PricingInfo(_Loose):
    airline: str
    milesToCashRatio: float = 0.0  # PointsPath's valuation, e.g. 0.0125 = 1.25¢/mi
    bankPointsInfos: list[BankPointsInfo] = []

    _none_banks = field_validator("bankPointsInfos", mode="before")(_none_to_empty_list)


class PricingInfoResponse(_Loose):
    pricingInfos: list[PricingInfo] = []

    _none_pi = field_validator("pricingInfos", mode="before")(_none_to_empty_list)
