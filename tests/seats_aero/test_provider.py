# pyright: reportPrivateUsage=false
"""Tests for seats_aero/provider.py grouping + cabin-mapping logic.

The provider's job is to convert AvailabilityTrip lists (per-cabin, per-
program) into AwardFlight lists (per-flight-set, with multiple cabins
inside). These tests pin the grouping key and label mapping against the
captured live fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from flight_cli.providers.seats_aero.models import (
    CachedSearchResponse,
    SeatsAvailabilityTrip,
)
from flight_cli.providers.seats_aero.provider import (
    _cabin_label,
    _first_flight_number,
    _group_trips_to_awards,
    _program_label,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "seats_aero"


def _load_trips() -> list[SeatsAvailabilityTrip]:
    resp = CachedSearchResponse.model_validate(
        json.loads((FIXTURES / "cached_search_with_trips.json").read_text()),
    )
    out: list[SeatsAvailabilityTrip] = []
    for item in resp.data:
        out.extend(item.AvailabilityTrips)
    return out


# ─────────────────────────── label helpers ─────────────────────────────────


def test_program_label_known() -> None:
    assert _program_label("american") == "American Airlines"
    assert _program_label("aeroplan") == "Air Canada Aeroplan"
    assert _program_label("virginatlantic") == "Virgin Atlantic Flying Club"


def test_program_label_unknown_falls_back_to_title() -> None:
    assert _program_label("mysteryairline") == "Mysteryairline"


def test_cabin_label_normalises_case() -> None:
    assert _cabin_label("economy") == "Economy"
    assert _cabin_label("BUSINESS") == "Business"
    assert _cabin_label("First") == "First"


def test_cabin_label_unknown_falls_back_to_title() -> None:
    assert _cabin_label("ultraplus") == "Ultraplus"


# ─────────────────────────── first flight number ───────────────────────────


def test_first_flight_number_single() -> None:
    assert _first_flight_number("AA100") == "AA100"


def test_first_flight_number_multi_segment() -> None:
    assert _first_flight_number("AA4671, BA216") == "AA4671"


def test_first_flight_number_handles_whitespace() -> None:
    assert _first_flight_number(" AA100  ,  BA216 ") == "AA100"


# ─────────────────────────── grouping ──────────────────────────────────────


def test_group_collapses_same_flight_multiple_cabins() -> None:
    """Same (FlightNumbers, DepartsAt, Source) with different cabins → one
    AwardFlight with multiple CabinAward entries."""
    trips = _load_trips()
    awards = _group_trips_to_awards(trips, tax_currency="USD")
    # Find any award with >1 cabin (the fixture has plenty).
    multi = [a for a in awards if len(a.cabins) > 1]
    assert len(multi) > 0, "expected at least one multi-cabin grouping"
    a = multi[0]
    cabins = [c.cabin for c in a.cabins]
    # No duplicates within a single award
    assert len(cabins) == len(set(cabins))


def test_group_distinguishes_different_flight_sets() -> None:
    """Same first flight number on the same date but different connecting
    segments → distinct AwardFlights. Pin this against the captured fixture
    which has AA2643/AA730 and AA2643/AA732 at the same DepartsAt."""
    trips = _load_trips()
    awards = _group_trips_to_awards(trips, tax_currency="USD")
    # Two awards with flight_number=AA2643 should exist (different second-leg
    # flight numbers in their flight_set keys).
    aa2643 = [a for a in awards if a.flight_number == "AA2643"]
    assert len(aa2643) >= 2


def test_group_assigns_correct_provider_and_program_labels() -> None:
    trips = _load_trips()
    awards = _group_trips_to_awards(trips, tax_currency="USD")
    # All awards from this provider tag as Seats.aero
    assert all(a.provider == "Seats.aero" for a in awards)
    # Programs cover at least the known set seen in the fixture
    programs = {a.program for a in awards}
    assert "American Airlines" in programs


def test_group_converts_tax_cents_to_usd() -> None:
    """seats.aero returns TotalTaxes in cents; CabinAward.tax_usd should be
    the dollar value."""
    trips = _load_trips()
    awards = _group_trips_to_awards(trips, tax_currency="USD")
    # Find an award with a non-zero tax; verify it's a sensible $ amount
    nonzero = [a for a in awards if any(c.tax_usd > 0 for c in a.cabins)]
    assert nonzero, "expected at least one award with tax > 0"
    sample = next(c.tax_usd for c in nonzero[0].cabins if c.tax_usd > 0)
    # Sanity: post-conversion taxes should be O($1)-O($1000), not O($10k+)
    # (which would indicate we forgot the /100 and are treating cents as dollars).
    assert 0 < sample < 5000


def test_group_empty_input_returns_empty() -> None:
    assert _group_trips_to_awards([], tax_currency="USD") == []
