"""flight-cli: ITA Matrix Alkali backend wrapper."""

from .client import ApiKeyResolutionError, MatrixApiError, MatrixClient, resolve_api_key
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
)
from .links import google_flights_url, matrix_deep_link
from .models import (
    CalendarDay,
    CalendarResult,
    DurationOption,
    Itinerary,
    Location,
    SearchResult,
)
from .wire import WireBody, to_wire

__all__ = [
    "ApiKeyResolutionError",
    "Cabin",
    "CalendarDay",
    "CalendarFollowup",
    "CalendarResult",
    "CalendarSearch",
    "CalendarWindow",
    "DurationOption",
    "Itinerary",
    "Leg",
    "Location",
    "MatrixApiError",
    "MatrixClient",
    "Pax",
    "Search",
    "SearchOptions",
    "SearchResult",
    "SpecificDateSearch",
    "TimeOfDay",
    "WireBody",
    "google_flights_url",
    "matrix_deep_link",
    "resolve_api_key",
    "to_wire",
]
