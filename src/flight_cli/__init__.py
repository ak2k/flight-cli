"""flight-cli: ITA Matrix Alkali backend wrapper."""
from .client import MatrixClient, MatrixApiError, ApiKeyResolutionError, resolve_api_key
from .domain import (
    Cabin, Pax, TimeOfDay, Leg, SearchOptions,
    SpecificDateSearch, CalendarSearch, CalendarFollowup, Search,
)
from .models import (
    SearchResult, Itinerary, CalendarResult, CalendarDay, DurationOption,
    Location,
)
from .wire import to_wire, WireBody
from .links import matrix_deep_link, google_flights_url

__all__ = [
    "MatrixClient", "MatrixApiError", "ApiKeyResolutionError", "resolve_api_key",
    "Cabin", "Pax", "TimeOfDay", "Leg", "SearchOptions",
    "SpecificDateSearch", "CalendarSearch", "CalendarFollowup", "Search",
    "SearchResult", "Itinerary",
    "CalendarResult", "CalendarDay", "DurationOption",
    "Location",
    "to_wire", "WireBody",
    "matrix_deep_link", "google_flights_url",
]
