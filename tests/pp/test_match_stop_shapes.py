# pyright: reportPrivateUsage=false, reportCallIssue=false
# DIVERGE: `_flight_to_award` is the provider's PointsPath-to-AwardFlight
# conversion, the only path a stop shape reaches the matcher through; the
# cash-side models trip reportCallIssue as in test_match.py.
"""Connection airports reach the matcher unchanged whichever stop shape
PointsPath sent. Starts from PointsPath JSON, not a hand-built AwardFlight, so
the model's stop handling is on the path under test."""

from __future__ import annotations

import copy
import json
import pathlib
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli.models import Itinerary, ItineraryDetails, SearchResult, Slice, SliceEndpoint
from flight_cli.pp.match import join
from flight_cli.pp.models import AirlineSearchResponse
from flight_cli.providers.pointspath.provider import _flight_to_award

if TYPE_CHECKING:
    from flight_cli.providers.base import AwardFlight

FIX = pathlib.Path(__file__).parent / "fixtures"


def _capture() -> dict[str, Any]:
    return json.loads((FIX / "airline_search_alaska_stop_objects.json").read_text())


def _as_codes(body: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(body)
    for f in out["outboundFlights"]:
        f["stops"] = [s["airport"] for s in f["stops"]]
    return out


def _awards(body: dict[str, Any]) -> list[AwardFlight]:
    r = AirlineSearchResponse.model_validate(body)
    return [
        _flight_to_award(of, program="Alaska", miles_to_cash_ratio=0.013, funding_banks=["Bilt"])
        for of in r.outboundFlights
    ]


def _with_wrong_hub(body: dict[str, Any]) -> dict[str, Any]:
    """Add a cheaper AS1399 that connects in SEA: same first flight, departure
    and connection count as the PDX journey, so only the hub tells them apart."""
    out = copy.deepcopy(body)
    decoy = copy.deepcopy(out["outboundFlights"][0])
    stop = decoy["stops"][0]
    decoy["stops"][0] = (
        "SEA" if isinstance(stop, str) else {"airport": "SEA", "layoverDurationMinutes": 58}
    )
    decoy["perCabinMilesPricing"][0]["perPassengerPricing"]["perPassengerMilesAmount"] = 9000
    out["outboundFlights"].append(decoy)
    return out


def _cash_via_pdx() -> SearchResult:
    return SearchResult(
        solutions=[
            Itinerary(
                displayTotal="USD320.00",
                itinerary=ItineraryDetails(
                    slices=[
                        Slice(
                            flights=["AS1399", "AS3421"],
                            departure="2026-10-20T06:30:00",
                            arrival="2026-10-20T13:24:00",
                            origin=SliceEndpoint(code="EWR"),
                            destination=SliceEndpoint(code="LAX"),
                            stops=[SliceEndpoint(code="PDX")],
                        ),
                    ],
                    carriers=[],
                ),
            ),
        ],
    )


def test_both_shapes_convert_to_identical_awards():
    assert _awards(_capture()) == _awards(_as_codes(_capture()))


@pytest.mark.parametrize("shape", ["objects", "codes"])
def test_connection_airport_drops_wrong_hub_in_either_shape(shape: str):
    body = _capture() if shape == "objects" else _as_codes(_capture())
    matches = join(_cash_via_pdx(), _awards(_with_wrong_hub(body)))
    assert [(a.flight_number, a.stop_airports, a.cabins[0].miles) for a in matches[0].awards] == [
        ("AS1399", ["PDX"], 15000),
    ]
