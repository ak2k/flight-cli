"""Wrapper around fli's Google Flights search that ALSO captures the opaque
`flightId` Google emits at index [17] of each flight row.

fli's `SearchFlights._parse_flights_data` parses legs, price, duration, stops —
but drops `data[0][17]`. PointsPath's `enableGoogleFlightMatching` mode joins
its award catalog against exactly that opaque ID (see PP browser extension
chunk-5KW5VSHS.js: `flightId: a` where `a = n[17]`). Without it, PP returns
an empty result for hint-based queries; with it, `matchedGoogleFlightId`
echoes back populated.

We re-use fli's `FlightSearchFilters.encode()` + curl_cffi client so the
request shape stays in lockstep with upstream; only the response parser
diverges (extends fli's by one field).
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
    FlightLeg,
    FlightResult,
)
from fli.models.google_flights.base import TripType  # pyright: ignore[reportMissingTypeStubs]
from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]
from fli.search.flights import SearchFlights  # pyright: ignore[reportMissingTypeStubs]

if TYPE_CHECKING:
    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
    )

log = logging.getLogger(__name__)

_BASE_URL = SearchFlights.BASE_URL

# Position of the opaque per-flight ID in Google Flights' API row array.
# Mirrors the PP browser extension's parser (chunk-5KW5VSHS.js: `a = n[17]`).
_FLIGHT_ID_IDX = 17


@dataclass
class GFlightWithId:
    """fli's FlightResult plus Google's opaque flight_id for PP matching."""

    flight: FlightResult
    flight_id: str


def _parse_flight_with_id(data: list[Any]) -> GFlightWithId:
    """Mirror of fli's `_parse_flights_data` but also reads `data[0][17]`.

    Indices match the PP extension's parser (chunks/chunk-5KW5VSHS.js): n[17]
    is the per-flight opaque ID; n[2] legs; n[9] duration; t[0][-1] price."""
    price, currency = SearchFlights._parse_price_info(data)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
    flight_id = data[0][_FLIGHT_ID_IDX] if len(data[0]) > _FLIGHT_ID_IDX else ""
    flight = FlightResult(
        price=price,
        currency=currency,
        duration=data[0][9],
        stops=len(data[0][2]) - 1,
        legs=[
            FlightLeg(
                airline=SearchFlights._parse_airline(fl[22][0]),  # pyright: ignore[reportPrivateUsage]
                flight_number=fl[22][1],
                departure_airport=SearchFlights._parse_airport(fl[3]),  # pyright: ignore[reportPrivateUsage]
                arrival_airport=SearchFlights._parse_airport(fl[6]),  # pyright: ignore[reportPrivateUsage]
                departure_datetime=SearchFlights._parse_datetime(fl[20], fl[8]),  # pyright: ignore[reportPrivateUsage]
                arrival_datetime=SearchFlights._parse_datetime(fl[21], fl[10]),  # pyright: ignore[reportPrivateUsage]
                duration=fl[11],
            )
            for fl in data[0][2]
        ],
    )
    return GFlightWithId(flight=flight, flight_id=flight_id)


def _one_call(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """Single HTTP round-trip to Google's endpoint; flat list of one leg's flights."""
    client = get_client()
    encoded = filters.encode()
    resp = client.post(
        url=_BASE_URL,
        data=f"f.req={encoded}",
        impersonate="chrome",
        allow_redirects=True,
    )
    resp.raise_for_status()
    parsed = json.loads(resp.text.lstrip(")]}'"))[0][2]
    if not parsed:
        return []
    inner = json.loads(parsed)
    flights_data: list[Any] = [
        item for i in (2, 3) if isinstance(inner[i], list) for item in inner[i][0]
    ]
    out: list[GFlightWithId] = []
    for fd in flights_data:
        try:
            out.append(_parse_flight_with_id(fd))
        except (AttributeError, KeyError, ValueError, IndexError) as e:
            log.debug("skipping flight with unparseable data: %s", e)
            continue
    return out


def search_with_ids(
    filters: FlightSearchFilters,
    *,
    top_n: int = 5,
) -> list[GFlightWithId | tuple[GFlightWithId, ...]] | None:
    """Drop-in for fli's `SearchFlights().search()` but each result carries
    its Google Flights opaque flight_id.

    Round-trip / multi-city follow the same iterative leg-selection pattern
    as fli: query first leg, pick top_n, drive each through the rest. Each
    `GFlightWithId` in a returned tuple has its own per-leg flight_id."""
    first = _one_call(filters)
    if not first:
        return None

    if filters.trip_type == TripType.ONE_WAY:
        return list(first)

    num_segments = len(filters.flight_segments)
    selected_count = sum(1 for s in filters.flight_segments if s.selected_flight is not None)
    # Last leg already — no further iteration.
    if selected_count >= num_segments - 1:
        return list(first)

    combos: list[GFlightWithId | tuple[GFlightWithId, ...]] = []
    for picked in first[:top_n]:
        next_filters = deepcopy(filters)
        next_filters.flight_segments[selected_count].selected_flight = picked.flight
        nxt = search_with_ids(next_filters, top_n=top_n)
        if nxt is None:
            continue
        for nx in nxt:
            if isinstance(nx, tuple):
                combos.append((picked, *nx))
            else:
                combos.append((picked, nx))
    return combos or None
