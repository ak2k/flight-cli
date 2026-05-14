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


class Flight(_Loose):
    """Flight-number metadata attached to a Segment."""
    number: str | None = None


class Segment(_Loose):
    origin: Airport
    destination: Airport
    departure: datetime
    arrival: datetime
    duration: int | None = None
    carrier: Carrier
    flight: Flight | None = None
    booking_infos: list[BookingInfo] = Field(default_factory=list[BookingInfo], alias="bookingInfos")
    legs: list[Leg] = Field(default_factory=list[Leg])

    @property
    def flight_number(self) -> str | None:
        return self.flight.number if self.flight else None


class ItineraryExt(_Loose):
    """`ext` blob shared by Itinerary and CurrencyNotice. Holds the
    user-facing price string."""
    price: str | None = None


class SliceEndpoint(_Loose):
    code: str | None = None


class Slice(_Loose):
    flights: list[str] = Field(default_factory=list[str])
    departure: str | None = None
    arrival: str | None = None
    duration: int | None = None
    origin: SliceEndpoint | None = None
    destination: SliceEndpoint | None = None


class SliceCarrier(_Loose):
    code: str | None = None


class ItineraryDetails(_Loose):
    """The deep itinerary payload — slices + carriers per solution."""
    slices: list[Slice] = Field(default_factory=list[Slice])
    carriers: list[SliceCarrier] = Field(default_factory=list[SliceCarrier])


class Itinerary(_Loose):
    """One bookable itinerary returned by /v1/search."""
    display_total: str | None = Field(None, alias="displayTotal")
    ext: ItineraryExt | None = None
    itinerary: ItineraryDetails | None = None

    @property
    def price(self) -> str | None:
        if self.ext and self.ext.price:
            return self.ext.price
        return self.display_total


class CSMLabel(_Loose):
    code: str | None = None
    short_name: str | None = Field(None, alias="shortName")


class CSMColumn(_Loose):
    label: CSMLabel | None = None


class CSMCell(_Loose):
    min_price: str | None = Field(None, alias="minPrice")
    min_price_in_grid: bool | None = Field(None, alias="minPriceInGrid")
    min_price_in_row: bool | None = Field(None, alias="minPriceInRow")


class CSMRow(_Loose):
    # Matrix returns `label` heterogeneously across row types; `Any` defers
    # parsing to the consumer, which str()'s it for display.
    label: Any = None
    cells: list[CSMCell] = Field(default_factory=list[CSMCell])


class CarrierStopMatrix(_Loose):
    columns: list[CSMColumn] = Field(default_factory=list[CSMColumn])
    rows: list[CSMRow] = Field(default_factory=list[CSMRow])


class CurrencyNotice(_Loose):
    ext: ItineraryExt | None = None


class SearchResult(_Loose):
    """/v1/search (specific-date or followup) response."""
    solution_count: int = Field(0, alias="solutionCount")
    solutions: list[Itinerary] = Field(default_factory=list[Itinerary])
    carrier_stop_matrix: CarrierStopMatrix | None = Field(None, alias="carrierStopMatrix")
    currency_notice: CurrencyNotice = Field(default_factory=CurrencyNotice, alias="currencyNotice")
    session: str | None = None
    solution_set: str | None = Field(None, alias="solutionSet")
    raw: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> SearchResult:
        sol_container: dict[str, Any] = body.get("solutionList") or {}
        sol_list: list[Any] = sol_container.get("solutions") or []
        return cls(
            solutionCount=body.get("solutionCount", len(sol_list)),
            solutions=sol_list,
            carrierStopMatrix=body.get("carrierStopMatrix"),
            currencyNotice=body.get("currencyNotice", {}),
            session=body.get("session"),
            solutionSet=body.get("solutionSet"),
            raw=body,
        )

    @property
    def cheapest_price(self) -> str | None:
        return self.currency_notice.ext.price if self.currency_notice.ext else None


class DurationOption(_Loose):
    trip_length: int = Field(alias="tripLength")
    min_price: str = Field(alias="minPrice")
    solution_count: int = Field(0, alias="solutionCount")
    min_price_in_summary: bool = Field(False, alias="minPriceInSummary")

    @property
    def price_value(self) -> float:
        s = self.min_price
        i = next((j for j, c in enumerate(s) if c.isdigit() or c == '.'), len(s))
        return float(s[i:])


class TripDuration(_Loose):
    options: list[DurationOption] = Field(default_factory=list[DurationOption])


class CalendarDay(_Loose):
    date: int
    disabled: bool = False
    solution_count: int = Field(0, alias="solutionCount")
    min_price: str | None = Field(None, alias="minPrice")
    min_price_in_week: bool = Field(False, alias="minPriceInWeek")
    trip_duration: TripDuration | None = Field(None, alias="tripDuration")

    @property
    def options(self) -> list[DurationOption]:
        return self.trip_duration.options if self.trip_duration else []

    @property
    def price_value(self) -> float | None:
        if not self.min_price:
            return None
        s = self.min_price
        i = next((j for j, c in enumerate(s) if c.isdigit() or c == '.'), len(s))
        return float(s[i:])


class Week(_Loose):
    days: list[CalendarDay] = Field(default_factory=list[CalendarDay])


class CalendarMonth(_Loose):
    month: int | None = None
    weeks: list[Week] = Field(default_factory=list[Week])

    @property
    def days(self) -> list[CalendarDay]:
        return [d for w in self.weeks for d in w.days]


class CalendarResult(_Loose):
    solution_count: int = Field(0, alias="solutionCount")
    currency_notice: CurrencyNotice = Field(default_factory=CurrencyNotice, alias="currencyNotice")
    months: list[CalendarMonth] = Field(default_factory=list[CalendarMonth])
    session: str | None = None
    solution_set: str | None = Field(None, alias="solutionSet")
    raw: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> CalendarResult:
        cal: dict[str, Any] = body.get("calendar") or {}
        months: list[Any] = cal.get("months") or []
        return cls(
            solutionCount=body.get("solutionCount", 0),
            currencyNotice=body.get("currencyNotice", {}),
            months=months,
            session=body.get("session"),
            solutionSet=body.get("solutionSet"),
            raw=body,
        )

    @property
    def cheapest_price(self) -> str | None:
        return self.currency_notice.ext.price if self.currency_notice.ext else None

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
