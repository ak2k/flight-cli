# pyright: reportPrivateUsage=false
"""Tests for the itinerary-cell formatters added for work-72syf.

The cash itinerary cell must (a) show connection cities so a 1-stop slice is
not indistinguishable from a nonstop, and (b) render times so an overnight
arrival can never read as landing before it departs.
"""

from __future__ import annotations

from flight_cli.cli import _fmt_slice_cell, _fmt_slice_route, _fmt_slice_times
from flight_cli.models import Slice, SliceEndpoint


def _slice(**kw: object) -> Slice:
    base: dict[str, object] = {
        "flights": ["B6 100"],
        "departure": "2026-08-15T13:53:00",
        "arrival": "2026-08-15T17:10:00",
        "duration": 197,
        "origin": SliceEndpoint(code="MSY"),
        "destination": SliceEndpoint(code="MCO"),
    }
    base.update(kw)
    return Slice.model_validate(base)


# ───────────────────────────── _fmt_slice_times ────────────────────────────


def test_same_day_times_have_no_day_marker() -> None:
    assert _fmt_slice_times("2026-08-15T13:53:00", "2026-08-15T17:10:00") == "Aug15 13:53→17:10"


def test_overnight_arrival_gets_plus_one_day() -> None:
    # 23:30 dep, 05:15 arrival next day — must not read as arriving before
    # departing.
    out = _fmt_slice_times("2026-08-15T23:30:00", "2026-08-16T05:15:00")
    assert out == "Aug15 23:30→05:15 +1d"
    assert "+1d" in out


def test_unparseable_falls_back_to_raw_iso() -> None:
    # Garbage in → still carries both date strings (never silently blank).
    assert _fmt_slice_times("not-a-date", "also-bad") == "not-a-date→also-bad"


# ───────────────────────────── _fmt_slice_route ────────────────────────────


def test_route_nonstop_is_origin_dest() -> None:
    assert _fmt_slice_route(_slice(stops=[])) == "MSY→MCO"


def test_route_threads_connection_cities() -> None:
    s = _slice(flights=["B6 100", "B6 200"], stops=[SliceEndpoint(code="DEN")])
    assert _fmt_slice_route(s) == "MSY→DEN→MCO"


def test_route_multi_connection() -> None:
    s = _slice(stops=[SliceEndpoint(code="DEN"), SliceEndpoint(code="PHX")])
    assert _fmt_slice_route(s) == "MSY→DEN→PHX→MCO"


# ───────────────────────────── _fmt_slice_cell ─────────────────────────────


def test_cell_includes_route_flights_times_and_duration() -> None:
    cell = _fmt_slice_cell(_slice())
    assert "MSY→MCO" in cell
    assert "B6 100" in cell
    assert "Aug15 13:53→17:10" in cell
    assert "3h17m" in cell


def test_cell_connection_shows_via_city() -> None:
    s = _slice(flights=["B6 100", "B6 200"], stops=[SliceEndpoint(code="DEN")])
    assert "MSY→DEN→MCO" in _fmt_slice_cell(s)
