"""Regression tests for `matrix_deep_link()` — the SPA URL-state encoder.

Round-trip ground truth was captured 2026-05-19 by driving Matrix's SPA via
research/record_user_session.py (see fixture headers for the exact command).
If the SPA changes its URL state schema again, the byte-exact test fails
and we know to recapture.

The schema drift this set of tests locks down:
  Round-trip = ONE slice with both `departureDate` and `returnDate`
               (NOT two slices each carrying one direction's date — that
               legacy form silently misrenders today's SPA's return field)."""

from __future__ import annotations

import base64
import json
import pathlib
import urllib.parse
from datetime import date
from typing import Any, cast

from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
    SpecificDateSearch,
)
from flight_cli.links import matrix_deep_link, matrix_itinerary_url

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "matrix_url"


def _read_fixture_url(name: str) -> str:
    text = (FIXTURE_DIR / name).read_text()
    for line in text.splitlines():
        if line.startswith("https://"):
            return line.strip()
    raise AssertionError(f"no https URL line in fixture {name}")


def _decode_search(url: str) -> dict[str, Any]:
    qs = urllib.parse.urlparse(url).query
    b64 = urllib.parse.parse_qs(qs)["search"][0]
    return cast("dict[str, Any]", json.loads(base64.b64decode(b64)))


def test_round_trip_byte_exact_matches_spa_capture() -> None:
    """Generated round-trip URL is byte-identical to the SPA's own emission.

    Captured by driving the SPA with HNL→MIA, dep 2026-10-14, ret 2026-10-24,
    economy, 1 adult, no other knobs."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("HNL",), destinations=("MIA",), date=date(2026, 10, 14)),
            Leg(origins=("MIA",), destinations=("HNL",), date=date(2026, 10, 24)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    expected = _read_fixture_url("spa_round_trip_hnl_mia.txt")
    assert matrix_deep_link(s) == expected


def test_round_trip_emits_single_slice_with_return_date() -> None:
    """Structural check: round-trip emits ONE slice carrying both dates.
    Pre-2026-05 SPA accepted two slices; today's SPA renders that as a
    multi-city and corrupts the return date field."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 8, 15)),
            Leg(origins=("LHR",), destinations=("JFK",), date=date(2026, 8, 22)),
        ),
        options=SearchOptions(cabin=Cabin.BUSINESS, pax=Pax(adults=2)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "round-trip"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 1
    only = slices[0]
    assert only["origin"] == ["JFK"]
    assert only["dest"] == ["LHR"]
    dates: dict[str, Any] = only["dates"]
    assert dates["departureDate"] == "2026-08-15"
    assert dates["returnDate"] == "2026-08-22"


def test_one_way_emits_single_slice_with_empty_return_date() -> None:
    s = SpecificDateSearch(
        legs=(Leg(origins=("SFO",), destinations=("NRT",), date=date(2026, 11, 3)),),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "one-way"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 1
    only = slices[0]
    dates: dict[str, Any] = only["dates"]
    assert dates["departureDate"] == "2026-11-03"
    assert dates["returnDate"] == ""


def test_matrix_itinerary_url_byte_exact_matches_spa_capture() -> None:
    """Pinned `/itinerary` URL is byte-identical to the SPA's emission.

    Captured by driving the SPA: fill HNL->MIA round-trip 10/14-10/24,
    click Search, click the cheapest `$680` result row -> SPA navigates
    to `/itinerary?search=...` with a `solution` block carrying the
    server-generated identifiers."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("HNL",), destinations=("MIA",), date=date(2026, 10, 14)),
            Leg(origins=("MIA",), destinations=("HNL",), date=date(2026, 10, 24)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    url = matrix_itinerary_url(
        s,
        solution_id="LW2fnBXaw5TMrLyYxZfSj4001",
        session="v1K0kQfukbERvMxYF6uY3vqFf",
        solution_set="0BjvHzTSqMX6RLaj0Mc4UJ7",
    )
    expected = _read_fixture_url("spa_pinned_hnl_mia.txt")
    assert url == expected


def test_matrix_itinerary_url_rejects_calendar_search() -> None:
    """Calendar searches don't produce itinerary rows, so they can't be
    pinned; calling the builder is a programming error rather than a
    silent fallback."""
    cal = CalendarSearch(
        legs=(Leg(origins=("JFK",), destinations=("LHR",)),),
        window=CalendarWindow(
            start=date(2026, 9, 1),
            end=date(2026, 9, 15),
            duration_min=5,
            duration_max=7,
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    try:
        matrix_itinerary_url(cal, solution_id="x", session="y", solution_set="z")
    except TypeError as e:
        assert "SpecificDateSearch" in str(e)
    else:
        msg = "expected TypeError for calendar search"
        raise AssertionError(msg)


def test_multi_city_keeps_one_slice_per_leg() -> None:
    """3-leg multi-city = 3 slices each with its own departureDate.
    Captured SPA fixture confirms the SPA emits this same shape today."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("SFO",), destinations=("JFK",), date=date(2026, 9, 1)),
            Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 9, 5)),
            Leg(origins=("LHR",), destinations=("SFO",), date=date(2026, 9, 12)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "multi-city"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 3
    for slc, (o, d, iso) in zip(
        slices,
        [("SFO", "JFK", "2026-09-01"), ("JFK", "LHR", "2026-09-05"), ("LHR", "SFO", "2026-09-12")],
        strict=True,
    ):
        assert slc["origin"] == [o]
        assert slc["dest"] == [d]
        dates: dict[str, Any] = slc["dates"]
        assert dates["departureDate"] == iso
        # Multi-city slices keep returnDate empty — our benign superset.
        assert dates["returnDate"] == ""
