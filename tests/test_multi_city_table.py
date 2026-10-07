"""A table of a three-slice (multi-city) search shows every slice."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin
from flight_cli.models import Itinerary, SearchResult


def _slice(origin: str, destination: str, flight: str) -> dict[str, Any]:
    return {
        "flights": [flight],
        "origin": {"code": origin},
        "destination": {"code": destination},
        "departure": "2026-11-01T09:00",
        "arrival": "2026-11-01T12:00",
        "duration": 180,
    }


def _itinerary(*flights: str) -> Itinerary:
    """A priced itinerary of one slice per flight number, each its own city pair."""
    pairs = [("JFK", "LAX"), ("LAX", "BOS"), ("BOS", "SFO")]
    return Itinerary.model_validate(
        {
            "ext": {"price": "USD500.00"},
            "itinerary": {
                "slices": [_slice(o, d, f) for (o, d), f in zip(pairs, flights, strict=False)],
                "carriers": [{"code": "UA"}],
            },
        }
    )


def _result(*itineraries: Itinerary) -> SearchResult:
    return SearchResult.model_validate(
        {"solutionCount": len(itineraries), "solutions": list(itineraries)}
    )


@pytest.fixture
def out(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """The table's console, wide enough that no cell wraps."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=300, color_system=None))
    return buf


def test_a_three_slice_table_shows_the_third_slice(out: io.StringIO) -> None:
    res = _result(_itinerary("UA1", "UA2", "UA1023"), _itinerary("UA1", "UA2", "UA582"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    text = out.getvalue()
    assert "UA1023" in text
    assert "UA582" in text
    assert "slice 3" in text


def test_a_three_slice_multi_cabin_table_shows_the_third_slice(out: io.StringIO) -> None:
    rows: list[MultiCabinRow] = []
    for third in ("UA1023", "UA582"):
        row = MultiCabinRow(itinerary=_itinerary("UA1", "UA2", third))
        row.prices[Cabin.COACH] = "USD500.00"
        rows.append(row)
    cli._render_multi_cabin_search(  # pyright: ignore[reportPrivateUsage]
        rows, cabins=(Cabin.COACH,), sort_by=Cabin.COACH
    )
    text = out.getvalue()
    assert "UA1023" in text
    assert "UA582" in text
    assert "slice 3" in text


def test_a_round_trip_table_keeps_outbound_and_return(out: io.StringIO) -> None:
    res = _result(_itinerary("UA1", "UA2"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage]
    header = out.getvalue()
    assert "outbound" in header
    assert "return" in header
    assert "slice" not in header


def test_a_short_row_on_a_three_slice_table_dashes_its_missing_slices(
    out: io.StringIO,
) -> None:
    res = _result(_itinerary("UA1", "UA2", "UA1023"), _itinerary("UA77"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage]
    text = out.getvalue()
    assert "slice 3" in text
    (short_line,) = [line for line in text.splitlines() if "UA77" in line]
    assert short_line.count("—") == 2
