"""User-facing search-intent types.

Three frozen pydantic models, discriminated by `kind`, capture every Matrix
search variant we know. Adapters (in wire.py / links.py / fli_bridge.py)
match on the variant to produce backend-specific bytes.

Adding a new search mode = add a new variant + extend each adapter's match
block. Type checker (pyright/mypy) flags every adapter that forgot the
new case via `typing.assert_never`.
"""
from __future__ import annotations
from datetime import date as _date
from enum import Enum
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ──────────────────────────────── enums ────────────────────────────────────

class Cabin(str, Enum):
    COACH = "COACH"
    PREMIUM_COACH = "PREMIUM_COACH"
    BUSINESS = "BUSINESS"
    FIRST = "FIRST"


class TimeOfDay(str, Enum):
    """Preferred time-of-day filter. Captured from Matrix's wire format —
    do NOT change the (min, max) tuples without re-verifying via capture.

    Quirk: Early Morning uses zero-padded '00:00', everything else uses
    single-digit hour ('8:00' not '08:00')."""
    EARLY_MORNING = "early_morning"   # before 8:00
    MORNING = "morning"               # 8:00-11:00
    MIDDAY = "midday"                 # 11:00-14:00
    AFTERNOON = "afternoon"           # 14:00-17:00
    EVENING = "evening"               # 17:00-21:00
    NIGHT = "night"                   # after 21:00


_TIME_RANGE_FOR: dict[TimeOfDay, tuple[str, str]] = {
    TimeOfDay.EARLY_MORNING: ("00:00", "8:00"),
    TimeOfDay.MORNING:       ("8:00",  "11:00"),
    TimeOfDay.MIDDAY:        ("11:00", "14:00"),
    TimeOfDay.AFTERNOON:     ("14:00", "17:00"),
    TimeOfDay.EVENING:       ("17:00", "21:00"),
    TimeOfDay.NIGHT:         ("21:00", "23:59"),
}


def time_range_for(t: TimeOfDay) -> dict[str, str]:
    """Return the wire-format {min,max} dict for a TimeOfDay."""
    lo, hi = _TIME_RANGE_FOR[t]
    return {"min": lo, "max": hi}


# ──────────────────────────────── shared ───────────────────────────────────

class Pax(BaseModel):
    """Passenger counts. Adults default to 1."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    adults: int = Field(1, ge=0, le=9)
    children: int = Field(0, ge=0, le=9)
    seniors: int = Field(0, ge=0, le=9)
    youth: int = Field(0, ge=0, le=9)
    infants_in_seat: int = Field(0, ge=0, le=9)
    infants_in_lap: int = Field(0, ge=0, le=9)

    @property
    def total(self) -> int:
        return (self.adults + self.children + self.seniors + self.youth +
                self.infants_in_seat + self.infants_in_lap)


class SearchOptions(BaseModel):
    """Shared search constraints across all modes."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    cabin: Cabin = Cabin.COACH
    pax: Pax = Field(default_factory=Pax)
    # max connecting stops. None = no limit. 0 = nonstop only. N = at most N.
    max_stops: int | None = None
    allow_airport_changes: bool = True
    show_only_available: bool = True
    extra_stops: int | None = None     # SPA UI quirk; None = use default
    page_size: int = 25


def _iata(v: str) -> str:
    v = v.upper().strip()
    if not (len(v) == 3 and v.isalpha()):
        raise ValueError(f"Not a 3-letter IATA code: {v!r}")
    return v


class Leg(BaseModel):
    """One segment of intent. Origins/destinations support multi-airport.

    Calendar-mode legs leave `date` unset; the calendar window owns dates
    at the search level. Specific-date and followup legs require date."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    date: _date | None = None
    is_arrival_date: bool = False
    date_minus: int = Field(0, ge=0, le=3)
    date_plus: int = Field(0, ge=0, le=3)
    route_language: str | None = None      # 'LH+', 'BA AA', '[F* X F*]'
    extension: str | None = None           # 'MAXCONNECT 5:00', etc.
    time_ranges: tuple[TimeOfDay, ...] = ()   # empty = no preference

    @field_validator("origins", "destinations")
    @classmethod
    def _validate_airports(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_iata(c) for c in v)

    @classmethod
    def of(cls, origin: str | list[str] | tuple[str, ...],
           destination: str | list[str] | tuple[str, ...],
           dt: _date | None = None,
           *,
           route_language: str | None = None,
           extension: str | None = None,
           time_ranges: tuple[TimeOfDay, ...] = (),
           ) -> "Leg":
        """Convenience constructor — accepts a single IATA or list/tuple."""
        os = (origin,) if isinstance(origin, str) else tuple(origin)
        ds = (destination,) if isinstance(destination, str) else tuple(destination)
        return cls(origins=os, destinations=ds, date=dt,
                   route_language=route_language, extension=extension,
                   time_ranges=time_ranges)


# ───────────────────────────── search variants ─────────────────────────────

class _SearchBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    legs: tuple[Leg, ...]
    options: SearchOptions = Field(default_factory=SearchOptions)


class SpecificDateSearch(_SearchBase):
    """1 leg = one-way. 2 = round-trip. 3+ = multi-city. Each leg has date."""
    kind: Literal["specific"] = "specific"

    @field_validator("legs")
    @classmethod
    def _legs_have_dates(cls, legs: tuple[Leg, ...]) -> tuple[Leg, ...]:
        if not legs:
            raise ValueError("at least one leg required")
        for i, leg in enumerate(legs):
            if leg.date is None:
                raise ValueError(
                    f"SpecificDateSearch.legs[{i}] requires a date")
        return legs


class _CalendarWindow(BaseModel):
    """Shared between CalendarSearch and CalendarFollowup."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: _date
    end: _date
    duration_min: int = Field(ge=0)
    duration_max: int = Field(ge=0)

    @field_validator("duration_max")
    @classmethod
    def _validate_duration_range(cls, m, info):
        d_min = info.data.get("duration_min", 0)
        if m < d_min:
            raise ValueError(f"duration_max ({m}) < duration_min ({d_min})")
        return m


class CalendarSearch(_SearchBase):
    """Lowest-fare grid across a date window. 1 leg = one-way calendar,
    2 legs = round-trip calendar. Legs are templates — no per-leg date;
    the calendar window owns dates."""
    kind: Literal["calendar"] = "calendar"
    window: _CalendarWindow

    @field_validator("legs")
    @classmethod
    def _legs_no_dates(cls, legs: tuple[Leg, ...]) -> tuple[Leg, ...]:
        if not (1 <= len(legs) <= 2):
            raise ValueError("calendar search supports 1 or 2 legs")
        for i, leg in enumerate(legs):
            if leg.date is not None:
                raise ValueError(
                    f"CalendarSearch.legs[{i}].date must be None "
                    "(window owns the dates)")
        return legs


class CalendarFollowup(_SearchBase):
    """Phase-2: itineraries for a date picked from a calendar grid. Legs
    have dates; preserves window context for the API."""
    kind: Literal["followup"] = "followup"
    window: _CalendarWindow

    @field_validator("legs")
    @classmethod
    def _legs_have_dates(cls, legs: tuple[Leg, ...]) -> tuple[Leg, ...]:
        if not (1 <= len(legs) <= 2):
            raise ValueError("followup search supports 1 or 2 legs")
        for i, leg in enumerate(legs):
            if leg.date is None:
                raise ValueError(
                    f"CalendarFollowup.legs[{i}] requires a date")
        return legs


Search = Annotated[
    Union[SpecificDateSearch, CalendarSearch, CalendarFollowup],
    Field(discriminator="kind"),
]
"""Tagged union of every search variant. Adapters dispatch on `kind` (or via
`match` on the runtime type). Use `TypeAdapter(Search).validate_python(d)`
to round-trip serialized state."""
