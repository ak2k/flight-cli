# pyright: reportPrivateUsage=false
"""Tests for the GF native date-grid foundation (gate, parse, chunking, RPC gate)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from flight_cli import _gf_dategrid
from flight_cli._gf_dategrid import (
    GfGridUnavailableError,
    _one_grid_call,
    _parse_grid,
    date_grid,
    grid_can_serve,
    grid_routing_blocker,
)
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg


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

    monkeypatch.setattr(_gf_dategrid, "_GRID_RPC_GATED", False)  # the chunker is below it
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

    monkeypatch.setattr(_gf_dategrid, "_GRID_RPC_GATED", False)  # the chunker is below it
    monkeypatch.setattr(_gf_dategrid, "_grid_filters", fake_filters)
    monkeypatch.setattr(_gf_dategrid, "_one_grid_call", fake_call)
    date_grid(_cal())  # 16-day window -> single chunk
    assert calls["n"] == 1


# ─────────────── RPC gate: no network, no retry sleeps (work-h70kv.5) ──


class _ExplodingClient:
    def post(self, *_a: object, **_k: object) -> None:
        raise AssertionError("the gated date-grid must not reach the network")


def _no_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count `get_client()` calls and make any request through one fail loudly."""
    clients = {"n": 0}

    def _fake_get_client() -> _ExplodingClient:
        clients["n"] += 1
        return _ExplodingClient()

    monkeypatch.setattr(_gf_dategrid, "get_client", _fake_get_client)
    return clients


def test_date_grid_raises_without_touching_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is checked before `get_client()`, so a servable window costs zero
    POSTs and zero cold-session backoff — `retry_throttled` catches only
    GfThrottledError, so GfGridUnavailableError propagates on the first call."""
    clients = _no_client(monkeypatch)
    start = date.today() + timedelta(days=30)
    cal = _cal(start=start, end=start + timedelta(days=15))
    assert grid_can_serve(cal)  # the gate only matters on a window GF would serve
    with pytest.raises(GfGridUnavailableError):
        date_grid(cal)
    assert clients["n"] == 0  # no client was even constructed


def test_date_grid_refuses_city_codes_before_building_fli_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A city code reaches the gate, not `getattr(Airport, ...)`.

    fli's `Airport` enum holds airports only — NYC/LON/PAR/CHI are not members —
    so building filters for one raises AttributeError, which the callers' broad
    `except` reports as "date-grid failed: type object 'Airport' has no attribute
    'NYC'". That names a transport fault for a request the gate was never going to
    send. SFO/FRA everywhere else in this file is why that went unnoticed."""
    clients = _no_client(monkeypatch)
    start = date.today() + timedelta(days=30)
    cal = _cal(legs=(Leg.of(["NYC"], ["LHR"]),), start=start, end=start + timedelta(days=15))
    assert grid_can_serve(cal)  # one-way, single-airport, no routing: GF would serve it
    with pytest.raises(GfGridUnavailableError):
        date_grid(cal)
    assert clients["n"] == 0


def test_date_grid_refuses_a_past_window_before_building_fli_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape, other trigger: `FlightSegment` rejects a past travel_date, and
    that pydantic error would be reported as a date-grid transport failure too."""
    clients = _no_client(monkeypatch)
    start = date.today() - timedelta(days=60)
    with pytest.raises(GfGridUnavailableError):
        date_grid(_cal(start=start, end=start + timedelta(days=15)))
    assert clients["n"] == 0


def test_one_grid_call_still_refuses_while_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`date_grid`'s raise makes this one unreachable through it; it stays as the
    guard for any other caller, and it is what keeps the gate off the network."""
    clients = _no_client(monkeypatch)
    with pytest.raises(GfGridUnavailableError):
        _one_grid_call(object())
    assert clients["n"] == 0


# ─────────────── which tier declined, for the --fast refusal ───────────────


def test_grid_routing_blocker_separates_tier2_from_matrix_only() -> None:
    """Both tiers send a calendar to Matrix; only one of them is Tier-2, and the
    `--fast` refusal quotes this phrase."""
    assert grid_routing_blocker(_cal()) is None  # no routing: not the reason
    assert grid_routing_blocker(_cal(routing="LH+")) is None  # Tier-1: not the reason
    assert grid_routing_blocker(_cal(routing="O:LH+")) == "Tier-2 routing"

    booking_class = grid_routing_blocker(_cal(ext="F bc=y"))
    assert booking_class is not None
    assert "Tier-2" not in booking_class  # fare construction is Tier-3, not a post-filter
    assert booking_class.startswith("Matrix-only routing")
    assert "F bc=y" in booking_class  # and it says which constraint

    ordered = grid_routing_blocker(_cal(routing="BA AA"))
    assert ordered is not None and "Tier-2" not in ordered
