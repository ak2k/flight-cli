# pyright: reportPrivateUsage=false
"""Tests for the GF native date-grid foundation (gate, parse, chunking)."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

from flight_cli import _gf_dategrid
from flight_cli._gf_dategrid import _parse_grid, date_grid, grid_can_serve
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg

if TYPE_CHECKING:
    import pytest


def _cal(
    *,
    legs: tuple[Leg, ...] | None = None,
    routing: str | None = None,
    ext: str | None = None,
    start: date = date(2026, 8, 10),
    end: date = date(2026, 8, 25),
) -> CalendarSearch:
    return CalendarSearch(
        legs=legs or (Leg.of(["SFO"], ["FRA"], route_language=routing, extension=ext),),
        window=CalendarWindow(start=start, end=end, duration_min=5, duration_max=7),
    )


# ─────────────────────────── gate ──────────────────────────────────────


def test_grid_serves_oneway_single_airport_tier1() -> None:
    assert grid_can_serve(_cal())  # no routing
    assert grid_can_serve(_cal(routing="LH+"))  # Tier-1 marketing carrier
    assert grid_can_serve(_cal(routing="F* X:FRA F*"))  # Tier-1 via-airport


def test_grid_declines_tier2_and_tier3() -> None:
    assert not grid_can_serve(_cal(routing="O:LH+"))  # operating -> Tier-2 (no itineraries)
    assert not grid_can_serve(_cal(ext="F bc=y"))  # fare basis -> Tier-3


def test_grid_declines_multi_airport_and_round_trip() -> None:
    round_trip = (Leg.of(["SFO"], ["FRA"]), Leg.of(["FRA"], ["SFO"]))
    assert not grid_can_serve(_cal(legs=(Leg.of(["SFO", "OAK"], ["FRA"]),)))  # multi-airport
    assert not grid_can_serve(_cal(legs=round_trip))


# ─────────────────────────── parse ─────────────────────────────────────


def test_parse_grid_extracts_date_price() -> None:
    payload = json.dumps(
        [None, [["2026-08-10", None, [["x", 524]]], ["2026-08-11", None, [["x", 530.0]]]]]
    )
    assert _parse_grid(payload) == {"2026-08-10": 524.0, "2026-08-11": 530.0}


def test_parse_grid_skips_malformed_items() -> None:
    payload = json.dumps([None, [["2026-08-10", None, [["x", 600]]], ["bad"], [None, None, None]]])
    assert _parse_grid(payload) == {"2026-08-10": 600.0}


# ─────────────────────────── chunking (work-bcdex) ─────────────────────


def test_date_grid_chunks_over_61_days_and_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """A >61-day window is split into ≤61-day chunks we drive ourselves (with the
    full filter set), not fli's filter-dropping chunker."""
    chunks: list[tuple[str, str]] = []

    def fake_filters(_search: Any, from_iso: str, to_iso: str, _preds: Any) -> Any:
        chunks.append((from_iso, to_iso))
        return (from_iso, to_iso)

    def fake_call(filters: Any) -> dict[str, float]:
        from_iso, _to = filters
        return {from_iso: 100.0}

    monkeypatch.setattr(_gf_dategrid, "_grid_filters", fake_filters)
    monkeypatch.setattr(_gf_dategrid, "_one_grid_call", fake_call)

    # 2026-08-10 .. 2026-10-20 = 72 days -> two chunks (61 + 11).
    out = date_grid(_cal(start=date(2026, 8, 10), end=date(2026, 10, 20)))

    assert len(chunks) == 2
    assert chunks[0] == ("2026-08-10", "2026-10-09")  # first 61 days
    assert chunks[1] == ("2026-10-10", "2026-10-20")  # remainder
    for from_iso, to_iso in chunks:
        span = date.fromisoformat(to_iso).toordinal() - date.fromisoformat(from_iso).toordinal() + 1
        assert span <= _gf_dategrid._MAX_GRID_DAYS
    assert out == {"2026-08-10": 100.0, "2026-10-10": 100.0}


def test_date_grid_single_chunk_under_61_days(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_call(_f: Any) -> dict[str, float]:
        calls["n"] += 1
        return {"d": 1.0}

    def fake_filters(*_a: object) -> object:
        return None

    monkeypatch.setattr(_gf_dategrid, "_grid_filters", fake_filters)
    monkeypatch.setattr(_gf_dategrid, "_one_grid_call", fake_call)
    date_grid(_cal())  # 16-day window -> single chunk
    assert calls["n"] == 1
