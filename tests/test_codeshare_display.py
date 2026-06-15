# pyright: reportPrivateUsage=false
"""Tests for codeshare-aware leg labels (_leg_display) and _match_carriers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from flight_cli.cli import _leg_display, _match_carriers
from flight_cli.domain import Leg


def _leg(code: str, number: str, marketing_flights: tuple[str, ...] = ()) -> tuple[Any, Any]:
    leg = SimpleNamespace(airline=SimpleNamespace(name=code), flight_number=number)
    amenity = SimpleNamespace(marketing_flights=marketing_flights)
    return leg, amenity


def test_relabels_codeshare_to_matched_identity() -> None:
    """UA58 sold as LH9407 under `--routing LH+` -> show the LH identity."""
    leg, amenity = _leg("UA", "58", marketing_flights=("LH9407",))
    assert _leg_display(leg, amenity, frozenset({"LH"})) == "LH9407 (op UA58)"


def test_passthrough_when_booking_carrier_already_matches() -> None:
    leg, amenity = _leg("LH", "455", marketing_flights=())
    assert _leg_display(leg, amenity, frozenset({"LH"})) == "LH 455"


def test_passthrough_when_no_carrier_filter() -> None:
    leg, amenity = _leg("UA", "58", marketing_flights=("LH9407",))
    assert _leg_display(leg, amenity, frozenset()) == "UA 58"


def test_passthrough_when_no_matching_codeshare() -> None:
    # Booking UA, filter BA, no BA codeshare -> leave it as the booking identity.
    leg, amenity = _leg("UA", "58", marketing_flights=("LH9407",))
    assert _leg_display(leg, amenity, frozenset({"BA"})) == "UA 58"


def test_handles_missing_amenity() -> None:
    leg, _ = _leg("UA", "58")
    assert _leg_display(leg, None, frozenset({"LH"})) == "UA 58"


# ─────────────────────────── _match_carriers ───────────────────────────


def test_match_carriers_from_marketing_include() -> None:
    legs = (Leg.of(["SFO"], ["FRA"], None, route_language="LH+"),)
    assert _match_carriers(legs) == frozenset({"LH"})


def test_match_carriers_empty_for_operating_filter() -> None:
    # O:LH is an operating filter — codeshare relabeling doesn't apply.
    legs = (Leg.of(["SFO"], ["FRA"], None, route_language="O:LH+"),)
    assert _match_carriers(legs) == frozenset()


def test_match_carriers_empty_without_routing() -> None:
    assert _match_carriers((Leg.of(["SFO"], ["FRA"], None),)) == frozenset()
