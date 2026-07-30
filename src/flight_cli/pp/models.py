"""Pydantic v2 shapes for PointsPath API responses.

Mirrors the `_Loose`-extra style from src/flight_cli/models.py — PointsPath
adds and removes fields without notice; we capture the parts we use and
let the rest pass through.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class _Loose(BaseModel):
    # DIVERGE Profile-B edge: PointsPath is reverse-engineered, adds fields
    # without notice. Same justification as flight_cli.models._Loose for
    # Matrix responses.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


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
    isBasicEconomyFare: bool | None = None


class OneWayPricing(_Loose):
    perPassengerMilesAmount: int = 0
    perPassengerTaxAmountUsd: float = 0.0
    taxCurrencyCode: str = ""
    isBasicEconomyFare: bool | None = None


class SelectedFlightState(_Loose):
    oneWayPricing: OneWayPricing | None = None
    airlineState: dict[str, Any] | None = None


class PerCabinMilesPricing(_Loose):
    cabinClass: str
    perPassengerPricing: PerPassengerPricing | None = None
    selectedFlightState: SelectedFlightState | None = None


class OutboundFlight(_Loose):
    origin: str
    destination: str
    localDepartureDateTime: str  # ISO local without TZ, e.g. "2026-06-09T22:00:00"
    localArrivalDateTime: str
    firstFlightNumber: str
    googleAirlineName: str | None = None
    numConnections: int = 0
    # Connection airport codes in order, e.g. ["DFW"]. Empty for a nonstop.
    # PP has always sent this; `extra="ignore"` was silently dropping it. It's
    # the only journey-shape signal PP gives beyond the first flight number,
    # and Matrix populates the comparable `Slice.stops`.
    stops: list[str] = []
    externalId: str | None = None
    matchedGoogleFlightId: str | None = None
    matchedGoogleFlightCashPriceUsd: float | None = None
    perCabinMilesPricing: list[PerCabinMilesPricing] = []

    _none_pricing = field_validator("perCabinMilesPricing", mode="before")(_none_to_empty_list)
    _none_stops = field_validator("stops", mode="before")(_none_to_empty_list)


class AirlineSearchResponse(_Loose):
    outboundFlights: list[OutboundFlight] = []
    inboundFlights: list[OutboundFlight] = []
    roundTripReturnState: dict[str, Any] | None = None

    _none_out = field_validator("outboundFlights", mode="before")(_none_to_empty_list)
    _none_in = field_validator("inboundFlights", mode="before")(_none_to_empty_list)


# ─────────────────────────── /api/pricing-info ─────────────────────────────


class BankPointsInfo(_Loose):
    bank: str
    conversionValue: float = 1.0
    defaultConversionValue: float = 1.0
    isBonusActive: bool = False
    conversionExpiryDate: str | None = None


class PricingInfo(_Loose):
    airline: str
    milesToCashRatio: float = 0.0  # PointsPath's valuation, e.g. 0.0125 = 1.25¢/mi
    bankPointsInfos: list[BankPointsInfo] = []

    _none_banks = field_validator("bankPointsInfos", mode="before")(_none_to_empty_list)


class PricingInfoResponse(_Loose):
    pricingInfos: list[PricingInfo] = []

    _none_pi = field_validator("pricingInfos", mode="before")(_none_to_empty_list)
