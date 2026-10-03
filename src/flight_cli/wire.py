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

import re
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
    time_range_for,
)
from .routing_predicates import StopsPred, parse_extension

_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way (calendar variants)

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
    routeLanguage: str | None = None  # routing language ('LH+', 'BA AA')
    commandLine: str | None = None  # extension codes ('MAXCONNECT 2:00')
    dateModifier: WireDateModifier | None = None
    isArrivalDate: bool | None = None
    timeRanges: list[WireTimeRange] | None = None
    filter: WireSliceFilter = Field(default_factory=WireSliceFilter)
    selected: bool = False  # always emitted (matches SPA)


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
    # Extra legs beyond the route's own minimum, not stops: on a route with no
    # nonstop, 0 still answers one-stop trips. The SPA's "No limit" UI default
    # is 1 (see CLAUDE.md quirk #3). The wire adapter (`_base_inputs`) always
    # sets this explicitly, so the value here is only used if someone
    # constructs WireInputs directly.
    maxLegsRelativeToMin: int = 1
    slices: list[WireSlice]
    # Calendar / followup add these:
    startDate: str | None = None
    endDate: str | None = None
    layover: WireLayover | None = None
    # Specific-date keeps an empty `filter`; followup omits it. We default
    # to None and set explicitly per variant in to_wire().
    filter: dict[str, Any] | None = None
    # Omitted unless asked for, so a body without it is the SPA's byte for byte.
    currency: str | None = None


class WireBody(_Wire):
    summarizers: list[str]
    summarizerSet: str
    name: Literal["specificDatesSlice", "calendar", "calendarFollowup"]
    inputs: WireInputs

    def as_json(self) -> dict[str, Any]:
        """Serialize to JSON dict (camelCase keys, drop None fields)."""
        return self.model_dump(by_alias=True, exclude_none=True)


class WireSummarizeInputs(_Wire):
    solution: str  # "<solutionSet>/<solution id>"
    fareKeys: str | None = None  # one key, "0/1", despite the plural


class WireSummarizeBody(_Wire):
    """A `/v1/summarize` request: a follow-up question about one solution of
    a search, answered from that search's live session."""

    summarizerSet: Literal["viewDetails", "viewRules"]
    summarizers: list[str]
    solutionSet: str
    session: str
    inputs: WireSummarizeInputs

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


def booking_details_body(*, session: str, solution_set: str, solution_id: str) -> WireSummarizeBody:
    """Fare basis, booking codes, fare-calculation line and fare keys for one
    solution."""
    return WireSummarizeBody(
        summarizerSet="viewDetails",
        summarizers=["bookingDetails"],
        solutionSet=solution_set,
        session=session,
        inputs=WireSummarizeInputs(solution=f"{solution_set}/{solution_id}"),
    )


def fare_rules_body(
    *, session: str, solution_set: str, solution_id: str, fare_key: str
) -> WireSummarizeBody:
    """The rule categories of one fare, named by a key `booking_details_body`
    returned."""
    return WireSummarizeBody(
        summarizerSet="viewRules",
        summarizers=["fareRules"],
        solutionSet=solution_set,
        session=session,
        inputs=WireSummarizeInputs(solution=f"{solution_set}/{solution_id}", fareKeys=fare_key),
    )


# ──────────────────────────────── adapter ──────────────────────────────────

# Summarizer sets per mode — order matters for golden-file regression
# tests; ordering verified from real SPA captures.
_SUMMARIZERS_SPECIFIC = [
    "carrierStopMatrix",
    "currencyNotice",
    "solutionList",
    "itineraryPriceSlider",
    "itineraryCarrierList",
    "itineraryDepartureTimeRanges",
    "itineraryArrivalTimeRanges",
    "durationSliderItinerary",
    "itineraryOrigins",
    "itineraryDestinations",
    "itineraryStopCountList",
    "warningsItinerary",
]
_SUMMARIZERS_CALENDAR = [
    "calendar",
    "overnightFlightsCalendar",
    "itineraryStopCountList",
    "itineraryCarrierList",
    "currencyNotice",
]
_SUMMARIZERS_FOLLOWUP = _SUMMARIZERS_SPECIFIC


def _pax_dict(p: Pax) -> dict[str, int]:
    d = {"adults": p.adults}
    for k, v in (
        ("children", p.children),
        ("seniors", p.seniors),
        ("youth", p.youth),
        ("infantsInSeat", p.infants_in_seat),
        ("infantsInLap", p.infants_in_lap),
    ):
        if v:
            d[k] = v
    return d


_TRAILING_SEPARATORS = re.compile(r"[\s;]+\Z")


def _command_line(extension: str | None, max_stops: int | None) -> str | None:
    """The slice's extension codes, held to at most `max_stops` stops.

    `MAXSTOPS N` goes after the user's own codes, because a later, stricter
    MAXSTOPS was measured to hold beside an earlier, looser one; the reverse
    order was not measured. So the codes are sent as typed only when every
    MAXSTOPS in them is N or fewer."""
    if max_stops is None or max_stops < 0:
        return extension
    if extension is not None:
        limits = [p.max_stops for p in parse_extension(extension) if isinstance(p, StopsPred)]
        if limits and max(limits) <= max_stops:
            return extension
    typed = _TRAILING_SEPARATORS.sub("", extension or "")
    return f"{typed}; MAXSTOPS {max_stops:d}" if typed else f"MAXSTOPS {max_stops:d}"


def _leg_to_wire(
    leg: Leg, *, mode: Literal["specific", "calendar", "followup"], max_stops: int | None
) -> WireSlice:
    """Convert domain Leg to wire slice. Captured behaviour per mode:
    specific:  date + dateModifier + isArrivalDate always present
    calendar:  no date, no dateModifier, no isArrivalDate
    followup:  date present; dateModifier / isArrivalDate omitted

    Matrix's `timeRanges` bound the departure only, so a leg with an arrival
    window raises rather than reach Matrix without it.
    """
    if leg.arrival_ranges:
        raise ValueError("Matrix has no input for an arrival-time window")
    include_date = mode in ("specific", "followup")
    include_modifier_fields = mode == "specific"
    return WireSlice(
        origins=list(leg.origins),
        destinations=list(leg.destinations),
        date=leg.date.isoformat() if (include_date and leg.date) else None,
        routeLanguage=leg.route_language,
        commandLine=_command_line(leg.extension, max_stops),
        dateModifier=(
            WireDateModifier(minus=leg.date_minus, plus=leg.date_plus)
            if include_modifier_fields
            else None
        ),
        isArrivalDate=(leg.is_arrival_date if include_modifier_fields else None),
        timeRanges=(
            [WireTimeRange(**time_range_for(t)) for t in leg.time_ranges]
            if leg.time_ranges
            else None
        ),
    )


def _base_inputs(opts: SearchOptions, slices: list[WireSlice]) -> WireInputs:
    return WireInputs(
        pax=_pax_dict(opts.pax),
        cabin=opts.cabin.value,
        page=WirePage(size=opts.page_size),
        changeOfAirport=opts.allow_airport_changes,
        checkAvailability=opts.show_only_available,
        # The SPA's "No limit" default of 1 extra leg, or the stop limit itself:
        # a slice held to N stops has at most N legs beyond the minimum, so N
        # never cuts below the MAXSTOPS each slice carries.
        maxLegsRelativeToMin=(
            1 if opts.max_extra_stops is None or opts.max_extra_stops < 0 else opts.max_extra_stops
        ),
        slices=slices,
        currency=opts.currency,
    )


def _set_trip_length(inputs: WireInputs, window: CalendarWindow) -> None:
    """Attach the trip-LENGTH range (nights between the outbound and the return).

    Round-trip only, keyed off the leg count rather than the summarizer string:
    with one slice there is no return to measure against, and Matrix answers a
    one-way `calendar` / `calendarFollowup` that carries it with HTTP 200 +
    "Internal server error". Verified 2026-09-02 against live Matrix — the same
    bodies without `layover` return a grid (10 solutions on the followup);
    changing only the summarizer does not help (work-h70kv.7)."""
    inputs.layover = WireLayover(min=window.duration_min, max=window.duration_max)


def to_wire(s: Search) -> WireBody:
    """Map a domain search to its Matrix wire body. The match is exhaustive;
    adding a new Search variant breaks type-check until handled here."""
    stops = s.options.max_extra_stops
    match s:
        case SpecificDateSearch():
            slices = [_leg_to_wire(leg, mode="specific", max_stops=stops) for leg in s.legs]
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
            slices = [_leg_to_wire(leg, mode="calendar", max_stops=stops) for leg in s.legs]
            inputs = _base_inputs(s.options, slices)
            inputs.filter = {}
            inputs.startDate = s.window.start.isoformat()
            inputs.endDate = s.window.end.isoformat()
            rt = len(s.legs) == _ROUND_TRIP_LEGS
            if rt:
                _set_trip_length(inputs, s.window)
            return WireBody(
                summarizers=_SUMMARIZERS_CALENDAR,
                summarizerSet="calendarRoundTrip" if rt else "calendarOneWay",
                name="calendar",
                inputs=inputs,
            )

        case CalendarFollowup():
            slices = [_leg_to_wire(leg, mode="followup", max_stops=stops) for leg in s.legs]
            inputs = _base_inputs(s.options, slices)
            # Followup omits inputs.filter but DOES include page.current=1
            # (per SPA capture).
            inputs.filter = None
            inputs.page = WirePage(current=1, size=s.options.page_size)
            inputs.startDate = s.window.start.isoformat()
            inputs.endDate = s.window.end.isoformat()
            if len(s.legs) == _ROUND_TRIP_LEGS:
                _set_trip_length(inputs, s.window)
            return WireBody(
                summarizers=_SUMMARIZERS_FOLLOWUP,
                summarizerSet="wholeTrip",
                name="calendarFollowup",
                inputs=inputs,
            )

        case _:
            assert_never(s)
