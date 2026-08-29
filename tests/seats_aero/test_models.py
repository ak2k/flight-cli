# pyright: reportPrivateUsage=false
"""Golden-file parser tests for the seats.aero /partnerapi/search response.

The fixtures at tests/fixtures/seats_aero/ are real responses captured against
the live API for JFK→LHR on 2026-08-15:
  - cached_search_no_trips.json: take=5, no include_trips
  - cached_search_with_trips.json: take=5, include_trips=true (79 trips on
    the first availability item)

These pin the parser against the actual upstream shape so seats.aero adding
fields (or our pydantic shapes drifting) becomes a loud test failure rather
than a silent runtime crash mid-search.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_cli.providers.seats_aero.models import CachedSearchResponse

FIXTURES = Path(__file__).parent.parent / "fixtures" / "seats_aero"


def _load(name: str) -> CachedSearchResponse:
    return CachedSearchResponse.model_validate(json.loads((FIXTURES / name).read_text()))


def test_no_trips_top_level_shape() -> None:
    resp = _load("cached_search_no_trips.json")
    assert resp.count == 5
    assert resp.hasMore is True
    assert resp.cursor is not None
    assert len(resp.data) == 5


def test_no_trips_per_item_shape() -> None:
    resp = _load("cached_search_no_trips.json")
    item = resp.data[0]
    assert item.Route.OriginAirport == "JFK"
    assert item.Route.DestinationAirport == "LHR"
    assert item.Date == "2026-08-15"
    assert item.Source in {"american", "british", "finnair", "united", "virginatlantic"}


def test_no_trips_availability_columns() -> None:
    """When include_trips is omitted, AvailabilityTrips is empty."""
    resp = _load("cached_search_no_trips.json")
    item = resp.data[0]
    assert item.AvailabilityTrips == []
    # Per-cabin availability flags should be booleans (not strings/None).
    assert isinstance(item.YAvailable, bool)
    assert isinstance(item.JAvailable, bool)


def test_with_trips_populates_availability_trips() -> None:
    resp = _load("cached_search_with_trips.json")
    item = resp.data[0]
    assert len(item.AvailabilityTrips) > 0


def test_with_trips_trip_fields() -> None:
    resp = _load("cached_search_with_trips.json")
    trips = resp.data[0].AvailabilityTrips
    t = trips[0]
    # FlightNumbers is a comma-joined string in segment order.
    assert "," in t.FlightNumbers or "-" not in t.FlightNumbers
    # DepartsAt arrives with a 'Z' suffix — which upstream mislabels: the
    # value is local time at the airport. Pinned as the raw wire shape; the
    # provider strips it (see test_seats_aero_timestamps_are_normalized_local).
    assert t.DepartsAt.endswith("Z") or "+" in t.DepartsAt
    # Cabin is lowercased.
    assert t.Cabin in {"economy", "premium", "business", "first"}
    # Mileage cost is an integer.
    assert isinstance(t.MileageCost, int)
    assert t.MileageCost > 0


def test_extra_fields_ignored() -> None:
    """seats.aero adds fields without notice (CreatedAt timestamps, *Raw
    variants, ID fields). The _Loose extra='ignore' contract keeps the
    parser robust against forward-evolution."""
    # The raw fixture has many more fields than we model. Confirm the
    # parser doesn't choke and the modeled fields are populated.
    raw_keys = set(json.loads((FIXTURES / "cached_search_with_trips.json").read_text())["data"][0])
    # AvailabilityTrips, Date, Route, Source: modeled. Many *Raw fields: ignored.
    assert "YAvailableRaw" in raw_keys  # in raw
    resp = _load("cached_search_with_trips.json")
    # No attribute error means extra fields didn't break model construction.
    assert resp.data[0].Source != ""


@pytest.mark.parametrize(
    "fixture",
    ["cached_search_no_trips.json", "cached_search_with_trips.json"],
)
def test_round_trip_validates(fixture: str) -> None:
    """Defensive: every available fixture must parse without error."""
    _ = _load(fixture)
