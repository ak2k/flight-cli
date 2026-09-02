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

    # Opening the gate is what lets these two reach the chunk loop, and it is also
    # the one thing standing between this suite and a live batchexecute POST. The
    # transport is stubbed below, so this only has to fail loudly if that changes.
    clients = _no_client(monkeypatch)
    monkeypatch.setattr(_gf_dategrid, "_GRID_RPC_GATED", False)
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
    assert clients["n"] == 0  # an open gate still never dialed Google


def test_date_grid_single_chunk_under_61_days(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_call(_f: Any) -> dict[str, float]:
        calls["n"] += 1
        return {"d": 1.0}

    def fake_filters(*_a: object) -> object:
        return None

    # Opening the gate is what lets these two reach the chunk loop, and it is also
    # the one thing standing between this suite and a live batchexecute POST. The
    # transport is stubbed below, so this only has to fail loudly if that changes.
    clients = _no_client(monkeypatch)
    monkeypatch.setattr(_gf_dategrid, "_GRID_RPC_GATED", False)
    monkeypatch.setattr(_gf_dategrid, "_grid_filters", fake_filters)
    monkeypatch.setattr(_gf_dategrid, "_one_grid_call", fake_call)
    date_grid(_cal())  # 16-day window -> single chunk
    assert calls["n"] == 1
    assert clients["n"] == 0  # an open gate still never dialed Google


# ─────────────── RPC gate: no network, no retry sleeps (work-h70kv.5) ──


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
    so building filters for one raises AttributeError, and the callers' broad
    `except` renders that as "date-grid failed: type object 'Airport' has no
    attribute 'NYC'": a transport fault named for a request the gate never sends.
    Every other case in this file uses SFO/FRA, which the enum does hold."""
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
    assert grid_routing_blocker(_cal()) is None  # nothing set: not the reason
    assert grid_routing_blocker(_cal(routing="LH+")) is None  # Tier-1: not the reason
    assert grid_routing_blocker(_cal(ext="MAXCONNECT 2:00")) is None  # Tier-1 extension
    assert grid_routing_blocker(_cal(routing="O:LH+")) == "Tier-2 routing"

    booking_class = grid_routing_blocker(_cal(ext="F bc=y"))
    assert booking_class is not None
    assert "Tier-2" not in booking_class  # fare construction is Tier-3, not a post-filter
    assert booking_class.startswith("a Matrix-only extension code")
    assert "routing" not in booking_class  # a booking class is not routing, at either tier
    assert "F bc=y" in booking_class  # and it says which constraint

    ordered = grid_routing_blocker(_cal(routing="BA AA"))
    assert ordered is not None and "Tier-2" not in ordered
    assert ordered.startswith("Matrix-only routing")


@pytest.mark.parametrize(
    ("routing", "ext", "expected"),
    [
        # Tier-2 and Tier-3 name their source the same way, so the reader learns
        # which flag to edit from the phrase alone, at either tier.
        ("O:LH+", None, "Tier-2 routing"),
        (None, "-CODESHARE", "a Tier-2 extension code"),
        (None, "MINCONNECT 1:00", "a Tier-2 extension code"),
        (None, "-REDEYES", "a Tier-2 extension code"),
        ("O:LH+", "-CODESHARE", "both Tier-2 routing and a Tier-2 extension code"),
        # `--extension` takes a `;`-separated list, so its half is counted.
        (None, "-CODESHARE;MINCONNECT 1:00", "Tier-2 extension codes"),
        ("O:LH+", "-CODESHARE;-REDEYES", "both Tier-2 routing and Tier-2 extension codes"),
        ("BA AA", None, "Matrix-only routing"),
        (None, "F bc=y", "a Matrix-only extension code"),
        (None, "F bc=y;Q zz=1", "Matrix-only extension codes"),
        ("BA AA", "F bc=y", "both Matrix-only routing and a Matrix-only extension code"),
        ("BA AA", "F bc=y;Q zz=1", "both Matrix-only routing and Matrix-only extension codes"),
    ],
)
def test_grid_routing_blocker_names_the_flag_that_declined(
    routing: str | None, ext: str | None, expected: str
) -> None:
    """`classify` flattens `--routing` and `--extension` into one predicate set
    that no longer remembers which carried what, so the two are classified
    separately and the phrase names every side that declined — in the number the
    declining directives actually have, and reading as a sentence after
    "this is …"."""
    blocker = grid_routing_blocker(_cal(routing=routing, ext=ext))
    assert blocker is not None
    assert blocker.startswith(expected)
    if ext is not None and routing is None:
        assert "routing" not in blocker  # nothing on this command line is routing


def test_grid_routing_blocker_reports_every_matrix_only_reason() -> None:
    """A query can be Matrix-only twice over. Fixing one flag would leave the
    refusal unchanged, so it lists both rather than the first it found."""
    both = grid_routing_blocker(_cal(routing="BA AA", ext="F bc=y"))
    assert both is not None
    assert "BA AA" in both and "F bc=y" in both


def test_grid_routing_blocker_matrix_only_outranks_tier2_on_the_other_flag() -> None:
    """Tier-3 outranks Tier-2 whichever flag carries it: the grid cannot serve the
    query at all, so the post-filterable half is not the news — and the phrase
    names the Tier-3 side, which is the one that has to change."""
    mixed = grid_routing_blocker(_cal(routing="O:LH+", ext="F bc=y"))
    assert mixed is not None
    assert mixed.startswith("a Matrix-only extension code")
    assert "Tier-2" not in mixed
    flipped = grid_routing_blocker(_cal(routing="BA AA", ext="-CODESHARE"))
    assert flipped is not None
    assert flipped.startswith("Matrix-only routing")
    assert "Tier-2" not in flipped
