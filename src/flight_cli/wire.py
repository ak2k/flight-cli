"""Wire-format types + adapter (domain → Matrix Alkali request body).

Pydantic models mirror Matrix's actual JSON shape exactly. Field names are
the wire-side names (camelCase) — when this file says `routeLanguage`,
Matrix says `routeLanguage`. Mistakes that used to land at runtime as
"Illegal COMMAND-LINE prefix" now land at type-check time.

The adapter `to_wire(search)` matches on the discriminated union and
produces a typed body. Exhaustiveness is enforced by `typing.assert_never`
— add a new variant, every adapter that doesn't handle it lights red.
"""
from __future__ import annotations
from typing import Any, Literal, assert_never
from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    Leg, Pax, SearchOptions, Search, SpecificDateSearch,
    CalendarSearch, CalendarFollowup, time_range_for,
)


# ─────────────────────────────── wire shapes ───────────────────────────────
# Pydantic config: camelCase field names match Matrix's JSON exactly.
# `extra="ignore"` so we tolerate fields we don't know about (forward-compat).
# `exclude_none=True` at dump time omits unset optional fields.

class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class WireDateModifier(_Wire):
    minus: int = 0
    plus: int = 0


class WireTimeRange(_Wire):
    min: str
    max: str


class WireSliceFilter(_Wire):
    warnings: dict[str, Any] = Field(default_factory=lambda: {"values": []})


class WireSlice(_Wire):
    """One slice in the API request. Field order matches captured SPA
    bodies. Optional fields use `None` default + `exclude_none=True` at
    dump time so unset fields disappear (matching SPA omission)."""
    origins: list[str]
    destinations: list[str]
    date: str | None = None
    routeLanguage: str | None = None       # routing language ('LH+', 'BA AA')
    commandLine: str | None = None         # extension codes ('MAXCONNECT 2:00')
    dateModifier: WireDateModifier | None = None
    isArrivalDate: bool | None = None
    timeRanges: list[WireTimeRange] | None = None
    filter: WireSliceFilter = Field(default_factory=WireSliceFilter)
    selected: bool = False                 # always emitted (matches SPA)


class WirePage(_Wire):
    current: int | None = None
    size: int = 25


class WireLayover(_Wire):
    min: int
    max: int


class WireInputs(_Wire):
    pax: dict[str, int]
    cabin: str
    page: WirePage = Field(default_factory=WirePage)
    sliceIndex: int = 0
    sorts: str = "default"
    firstDayOfWeek: str = "SUNDAY"
    internalUser: bool = False
    changeOfAirport: bool = True
    checkAvailability: bool = True
    # SPA's "No limit" UI default = 1 (see CLAUDE.md quirk #3). The wire
    # adapter (`_base_inputs`) always sets this explicitly, so the value
    # here is only used if someone constructs WireInputs directly.
    maxLegsRelativeToMin: int = 1
    slices: list[WireSlice]
    # Calendar / followup add these:
    startDate: str | None = None
    endDate: str | None = None
    layover: WireLayover | None = None
    # Specific-date keeps an empty `filter`; followup omits it. We default
    # to None and set explicitly per variant in to_wire().
    filter: dict[str, Any] | None = None


class WireBody(_Wire):
    summarizers: list[str]
    summarizerSet: str
    name: Literal["specificDatesSlice", "calendar", "calendarFollowup"]
    inputs: WireInputs

    def as_json(self) -> dict[str, Any]:
        """Serialize to JSON dict (camelCase keys, drop None fields)."""
        return self.model_dump(by_alias=True, exclude_none=True)


# ──────────────────────────────── adapter ──────────────────────────────────

# Summarizer sets per mode — order matters for golden-file regression
# tests; ordering verified from real SPA captures.
_SUMMARIZERS_SPECIFIC = [
    "carrierStopMatrix", "currencyNotice", "solutionList",
    "itineraryPriceSlider", "itineraryCarrierList",
    "itineraryDepartureTimeRanges", "itineraryArrivalTimeRanges",
    "durationSliderItinerary", "itineraryOrigins",
    "itineraryDestinations", "itineraryStopCountList",
    "warningsItinerary",
]
_SUMMARIZERS_CALENDAR = [
    "calendar", "overnightFlightsCalendar",
    "itineraryStopCountList", "itineraryCarrierList", "currencyNotice",
]
_SUMMARIZERS_FOLLOWUP = _SUMMARIZERS_SPECIFIC


def _pax_dict(p: Pax) -> dict[str, int]:
    d = {"adults": p.adults}
    for k, v in (("children", p.children), ("seniors", p.seniors),
                  ("youth", p.youth), ("infantsInSeat", p.infants_in_seat),
                  ("infantsInLap", p.infants_in_lap)):
        if v:
            d[k] = v
    return d


def _leg_to_wire(leg: Leg, *, mode: Literal["specific", "calendar", "followup"]) -> WireSlice:
    """Convert domain Leg to wire slice. Captured behaviour per mode:
        specific:  date + dateModifier + isArrivalDate always present
        calendar:  no date, no dateModifier, no isArrivalDate
        followup:  date present; dateModifier / isArrivalDate omitted
    """
    include_date = mode in ("specific", "followup")
    include_modifier_fields = (mode == "specific")
    return WireSlice(
        origins=list(leg.origins),
        destinations=list(leg.destinations),
        date=leg.date.isoformat() if (include_date and leg.date) else None,
        routeLanguage=leg.route_language,
        commandLine=leg.extension,
        dateModifier=(WireDateModifier(minus=leg.date_minus, plus=leg.date_plus)
                      if include_modifier_fields else None),
        isArrivalDate=(leg.is_arrival_date if include_modifier_fields else None),
        timeRanges=([WireTimeRange(**time_range_for(t)) for t in leg.time_ranges]
                    if leg.time_ranges else None),
    )


def _base_inputs(opts: SearchOptions, slices: list[WireSlice]) -> WireInputs:
    return WireInputs(
        pax=_pax_dict(opts.pax),
        cabin=opts.cabin.value,
        page=WirePage(size=opts.page_size),
        changeOfAirport=opts.allow_airport_changes,
        checkAvailability=opts.show_only_available,
        # Matches the SPA's "No limit" / "Up to 1 extra stop" default = 1.
        # User can override by passing options.max_extra_stops explicitly.
        maxLegsRelativeToMin=(1 if opts.max_extra_stops is None or opts.max_extra_stops < 0
                               else opts.max_extra_stops),
        slices=slices,
    )


def to_wire(s: Search) -> WireBody:
    """Map a domain search to its Matrix wire body. The match is exhaustive;
    adding a new Search variant breaks type-check until handled here."""
    match s:
        case SpecificDateSearch():
            slices = [_leg_to_wire(l, mode="specific") for l in s.legs]
            inputs = _base_inputs(s.options, slices)
            inputs.filter = {}
            inputs.page = WirePage(current=1, size=s.options.page_size)
            return WireBody(
                summarizers=_SUMMARIZERS_SPECIFIC,
                summarizerSet="wholeTrip",
                name="specificDatesSlice",
                inputs=inputs,
            )

        case CalendarSearch():
            slices = [_leg_to_wire(l, mode="calendar") for l in s.legs]
            inputs = _base_inputs(s.options, slices)
            inputs.filter = {}
            inputs.startDate = s.window.start.isoformat()
            inputs.endDate = s.window.end.isoformat()
            inputs.layover = WireLayover(min=s.window.duration_min,
                                          max=s.window.duration_max)
            rt = len(s.legs) == 2
            return WireBody(
                summarizers=_SUMMARIZERS_CALENDAR,
                summarizerSet="calendarRoundTrip" if rt else "calendarOneWay",
                name="calendar",
                inputs=inputs,
            )

        case CalendarFollowup():
            slices = [_leg_to_wire(l, mode="followup") for l in s.legs]
            inputs = _base_inputs(s.options, slices)
            # Followup omits inputs.filter but DOES include page.current=1
            # (per SPA capture).
            inputs.filter = None
            inputs.page = WirePage(current=1, size=s.options.page_size)
            inputs.startDate = s.window.start.isoformat()
            inputs.endDate = s.window.end.isoformat()
            inputs.layover = WireLayover(min=s.window.duration_min,
                                          max=s.window.duration_max)
            return WireBody(
                summarizers=_SUMMARIZERS_FOLLOWUP,
                summarizerSet="wholeTrip",
                name="calendarFollowup",
                inputs=inputs,
            )

        case _:
            assert_never(s)
