"""Tests for the seatmap URL helper.

`fetch_seatmap_url` makes a real HTTP call; we cover it only via the
deterministic builder (`seatmap_api_url`) here. Live integration is exercised
via the `flight seatmap` smoke run, not in CI."""

from __future__ import annotations

from datetime import date

from flight_cli.seatmap import seatmap_api_url


def test_basic_url_shape() -> None:
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="100",
        carrier="AA",
        date=date(2026, 8, 15),
    )
    # Match what the Legrooms+ extension actually emits — same host, same
    # param names, M/D/YYYY (no leading zeros) date format.
    assert url.startswith("https://www.travelarrow.io/api/s?")
    assert "from=JFK" in url
    assert "to=LHR" in url
    assert "flightno=100" in url
    assert "carrier=AA" in url
    assert "date=8%2F15%2F2026" in url  # url-encoded "/"


def test_strips_iata_prefix_from_flight_number() -> None:
    """User-supplied 'AA100' → wire form 'flightno=100' (extension drops prefix)."""
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="AA100",
        carrier="AA",
        date=date(2026, 8, 15),
    )
    assert "flightno=100" in url


def test_keeps_flight_number_when_prefix_does_not_match_carrier() -> None:
    """Codeshare case: marketing carrier ≠ first 2 chars of flight#."""
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="DL1",  # 'DL' prefix, but the carrier param is 'KL'
        carrier="KL",
        date=date(2026, 8, 15),
    )
    assert "flightno=DL1" in url


def test_aircraft_param_optional() -> None:
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="100",
        carrier="AA",
        date=date(2026, 8, 15),
    )
    assert "aircraft=" not in url


def test_aircraft_param_included_when_set() -> None:
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="100",
        carrier="AA",
        date=date(2026, 8, 15),
        aircraft="Airbus A330",
    )
    assert "aircraft=Airbus+A330" in url


def test_iso_string_date_accepted() -> None:
    url = seatmap_api_url(
        origin="JFK",
        dest="LHR",
        flight_number="100",
        carrier="AA",
        date="2026-08-15",
    )
    assert "date=8%2F15%2F2026" in url


def test_codes_are_uppercased() -> None:
    url = seatmap_api_url(
        origin="jfk",
        dest="lhr",
        flight_number="100",
        carrier="aa",
        date=date(2026, 8, 15),
    )
    assert "from=JFK" in url and "to=LHR" in url and "carrier=AA" in url
