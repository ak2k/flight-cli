"""Pydantic response models. Request-side models live in domain.py.

Lenient (`extra='ignore'`) because the Matrix Alkali response is
undocumented and prone to occasional field additions.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Airport(_Loose):
    code: str
    name: str | None = None


class Carrier(_Loose):
    code: str
    short_name: str | None = Field(None, alias="shortName")


class BookingInfo(_Loose):
    cabin: str | None = None
    booking_code: str | None = Field(None, alias="bookingCode")


class Leg(_Loose):
    """One physical flight segment within a solution.itinerary.slices[].
    Distinct from the domain `Leg` (which represents user intent)."""
    origin: Airport
    destination: Airport
    departure: datetime
    arrival: datetime
    duration: int | None = None
    aircraft: str | None = None


class Segment(_Loose):
    origin: Airport
    destination: Airport
    departure: datetime
    arrival: datetime
    duration: int | None = None
    carrier: Carrier
    flight: dict[str, Any] | None = None
    booking_infos: list[BookingInfo] = Field(default_factory=list, alias="bookingInfos")
    legs: list[Leg] = Field(default_factory=list)

    @property
    def flight_number(self) -> str | None:
        return (self.flight or {}).get("number")


class Itinerary(_Loose):
    """One bookable itinerary returned by /v1/search."""
    display_total: str | None = Field(None, alias="displayTotal")
    ext: dict[str, Any] | None = None
    itinerary: dict[str, Any] | None = None

    @property
    def price(self) -> str | None:
        if self.ext and "price" in self.ext:
            return self.ext["price"]
        return self.display_total


class SearchResult(_Loose):
    """/v1/search (specific-date or followup) response."""
    solution_count: int = Field(0, alias="solutionCount")
    solutions: list[Itinerary] = Field(default_factory=list)
    carrier_stop_matrix: dict[str, Any] | None = Field(None, alias="carrierStopMatrix")
    currency_notice: dict[str, Any] = Field(default_factory=dict, alias="currencyNotice")
    session: str | None = None
    solution_set: str | None = Field(None, alias="solutionSet")
    raw: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> SearchResult:
        sol_list = (body.get("solutionList") or {}).get("solutions") or []
        return cls(
            solutionCount=body.get("solutionCount", len(sol_list)),
            solutions=sol_list,  # type: ignore[arg-type]
            carrierStopMatrix=body.get("carrierStopMatrix"),
            currencyNotice=body.get("currencyNotice", {}),
            session=body.get("session"),
            solutionSet=body.get("solutionSet"),
            raw=body,
        )

    @property
    def cheapest_price(self) -> str | None:
        return (self.currency_notice or {}).get("ext", {}).get("price")


class DurationOption(_Loose):
    trip_length: int = Field(alias="tripLength")
    min_price: str = Field(alias="minPrice")
    solution_count: int = Field(0, alias="solutionCount")
    min_price_in_summary: bool = Field(False, alias="minPriceInSummary")
    solution: dict[str, Any] | None = None

    @property
    def price_value(self) -> float:
        s = self.min_price
        i = next((j for j, c in enumerate(s) if c.isdigit() or c == '.'), len(s))
        return float(s[i:])


class CalendarDay(_Loose):
    date: int
    disabled: bool = False
    solution_count: int = Field(0, alias="solutionCount")
    min_price: str | None = Field(None, alias="minPrice")
    min_price_in_week: bool = Field(False, alias="minPriceInWeek")
    trip_duration: dict[str, Any] | None = Field(None, alias="tripDuration")

    @property
    def options(self) -> list[DurationOption]:
        td = self.trip_duration or {}
        return [DurationOption.model_validate(o) for o in (td.get("options") or [])]

    @property
    def price_value(self) -> float | None:
        if not self.min_price:
            return None
        s = self.min_price
        i = next((j for j, c in enumerate(s) if c.isdigit() or c == '.'), len(s))
        return float(s[i:])


class CalendarMonth(_Loose):
    month: int | None = None
    weeks: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def days(self) -> list[CalendarDay]:
        return [CalendarDay.model_validate(d)
                for w in self.weeks for d in (w.get("days") or [])]


class CalendarResult(_Loose):
    solution_count: int = Field(0, alias="solutionCount")
    currency_notice: dict[str, Any] = Field(default_factory=dict, alias="currencyNotice")
    months: list[CalendarMonth] = Field(default_factory=list)
    session: str | None = None
    solution_set: str | None = Field(None, alias="solutionSet")
    raw: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> CalendarResult:
        cal = body.get("calendar") or {}
        return cls(
            solutionCount=body.get("solutionCount", 0),
            currencyNotice=body.get("currencyNotice", {}),
            months=cal.get("months") or [],
            session=body.get("session"),
            solutionSet=body.get("solutionSet"),
            raw=body,
        )

    @property
    def cheapest_price(self) -> str | None:
        return (self.currency_notice.get("ext") or {}).get("price")

    @property
    def days(self) -> list[CalendarDay]:
        return [d for m in self.months for d in m.days]

    @property
    def priced_days(self) -> list[CalendarDay]:
        return [d for d in self.days if d.min_price and not d.disabled]


class Location(_Loose):
    """Result of an airport autocomplete or single-code lookup."""
    code: str
    display_name: str | None = Field(None, alias="displayName")
    city_code: str | None = Field(None, alias="cityCode")
    city_name: str | None = Field(None, alias="cityName")
    type: str | None = None
    timezone: str | None = None
