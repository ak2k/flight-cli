"""Pydantic shapes for the Seats.aero /partnerapi/search response.

Mirrors seats.aero's PascalCase JSON attrs verbatim (same DIVERGE Profile-B
treatment as pp/models.py and wire.py — these are reverse-engineered upstreams
where the attr names ARE the contract). N815 is suppressed per-file in
pyproject.toml because seats.aero ships PascalCase, not snake_case.

The top-level shape is paginated:
    {count, cursor, data, hasMore, moreURL}

Each `data` item is one (Route, Date, Source/program) tuple with per-cabin
(Y/W/J/F) availability columns and an optional AvailabilityTrips array
populated when the request includes `include_trips=true`.

The fields we don't use we let Pydantic ignore — `extra="ignore"` covers the
"Raw" variants (pre-filter availability), CreatedAt/UpdatedAt timestamps,
and ID fields that are useless to us.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class _Loose(BaseModel):
    # DIVERGE Profile-B edge: same justification as pp/models.py._Loose —
    # seats.aero is an undocumented reverse-engineered upstream; we capture
    # the fields we use and let the rest pass through.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def _none_to_empty_list(v: Any) -> Any:
    """seats.aero returns null for `AvailabilityTrips` when include_trips
    isn't requested, and for `Connections` on direct flights. Coerce to []
    so consumers don't crash."""
    return [] if v is None else v


class SeatsRoute(_Loose):
    """`Route` sub-object on each availability item."""

    OriginAirport: str
    DestinationAirport: str
    Source: str  # program slug: "american", "british", "united", etc.


class SeatsAvailabilityTrip(_Loose):
    """Trip-level detail inside `AvailabilityTrips` (only present when
    include_trips=true was sent).

    A single Trip = one journey (possibly multi-segment), one cabin, one
    program. `FlightNumbers` is the comma-joined list of flight numbers
    in segment order (e.g. "AA4671, BA216" for a JFK-IAD-LHR journey).
    """

    OriginAirport: str
    DestinationAirport: str
    # MISLABELLED UPSTREAM: carries a 'Z' suffix but the value is LOCAL time
    # at the airport, not UTC. Verified against this repo's own fixture —
    # honouring the Z gives ~12.1h JFK→LHR nonstops against a real ~7h.
    # `provider._local_naive` strips the suffix on the way into AwardFlight.
    DepartsAt: str  # naive local despite the 'Z', e.g. "2026-08-15T06:30:00Z"
    ArrivesAt: str
    Cabin: str  # lowercase: "economy", "premium", "business", "first"
    FlightNumbers: str  # comma list, in segment order
    Carriers: str  # comma list of marketing carrier IATA codes
    Connections: list[str] = []  # connection airport codes, empty for direct

    _connections_none_to_empty = field_validator("Connections", mode="before")(_none_to_empty_list)
    MileageCost: int
    TotalTaxes: int  # in cents
    RemainingSeats: int  # often 0; seats.aero data freshness is imperfect
    Stops: int
    Source: str  # program slug, redundant with parent availability item


class SeatsAvailabilityItem(_Loose):
    """One row in the `data` array.

    Per-cabin (Y/W/J/F) availability columns hold the aggregated cheapest
    price across all trips on that (route, date, program). We use the
    AvailabilityTrips array (when present) for the actual flight-level
    pricing; the wide columns are useful for "is this cabin available at
    all" gut-checks but lose flight detail.

    MileageCost columns are returned as strings (with thousands separators
    sometimes); the *Raw variants are integers. We pick the raw ones to
    avoid parsing the string form.
    """

    Date: str  # "YYYY-MM-DD"
    Route: SeatsRoute
    Source: str  # mirrors Route.Source, kept for forward-compat

    YAvailable: bool = False
    WAvailable: bool = False
    JAvailable: bool = False
    FAvailable: bool = False

    YMileageCostRaw: int = 0
    WMileageCostRaw: int = 0
    JMileageCostRaw: int = 0
    FMileageCostRaw: int = 0

    YTotalTaxesRaw: int = 0  # in cents
    WTotalTaxesRaw: int = 0
    JTotalTaxesRaw: int = 0
    FTotalTaxesRaw: int = 0

    TaxesCurrency: str = "USD"

    AvailabilityTrips: list[SeatsAvailabilityTrip] = []

    _trips_none_to_empty = field_validator("AvailabilityTrips", mode="before")(_none_to_empty_list)


class CachedSearchResponse(_Loose):
    """Top-level paginated response from GET /partnerapi/search."""

    count: int = 0
    data: list[SeatsAvailabilityItem] = []
    hasMore: bool = False
    cursor: int | None = None
    moreURL: str | None = None
