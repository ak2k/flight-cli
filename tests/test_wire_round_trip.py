"""Golden-file regression net.

For each captured SPA request body, build the equivalent domain Search,
serialize via `to_wire()`, and diff against the captured JSON. If Matrix
changes the wire shape, this test fails immediately and tells us which
field drifted.

To capture more fixtures: drive a search in the recording harness
(scripts/record_user_session.py) and drop the resulting req_*.json into
tests/fixtures/."""
from __future__ import annotations
import json
import pathlib
from datetime import date

import pytest

from flight_cli.domain import (
    Cabin, Pax, Leg, SearchOptions, TimeOfDay,
    SpecificDateSearch, CalendarSearch, CalendarFollowup,
)
from flight_cli.domain import _CalendarWindow
from flight_cli.wire import to_wire

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _strip(d: dict, *keys: str) -> dict:
    """Drop top-level keys we don't reproduce (bgProgramResponse: the
    anti-abuse token; the SPA sends it but the server doesn't validate.
    session/id: server context, not part of the user-facing request)."""
    return {k: v for k, v in d.items()
            if k not in keys and k not in ("bgProgramResponse", "session", "id")}


# ─────────────────────────────── fixtures ──────────────────────────────────
# Each fixture is a captured SPA POST /v1/search body. We reconstruct the
# equivalent domain search by hand and assert that `to_wire().as_json()`
# matches the captured body byte-for-byte (modulo bgProgramResponse).


def test_calendar_round_trip_multi_airport():
    """NYC → [MUC, FRA] round-trip, 5-7 nights, MAXCONNECT 2:00 outbound."""
    captured = _strip(_load("calendar_nyc_munich_frankfurt.json"),
                       "bgProgramResponse")

    search = CalendarSearch(
        legs=(
            Leg.of("NYC", ["MUC", "FRA"],
                    route_language="LH+", extension="MAXCONNECT 2:00"),
            Leg.of(["MUC", "FRA"], "NYC"),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=_CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=captured["inputs"]["layover"]["min"],
            duration_max=captured["inputs"]["layover"]["max"],
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_calendarFollowup_after_pick():
    """Same NYC → [MUC,FRA] search but with picked dates 6/7 → 6/11."""
    captured = _strip(_load("followup_nyc_munich_frankfurt.json"),
                       "bgProgramResponse")

    search = CalendarFollowup(
        legs=(
            Leg.of("NYC", ["MUC", "FRA"],
                    date.fromisoformat("2026-06-07"),
                    route_language="LH+", extension="MAXCONNECT 2:00"),
            Leg.of(["MUC", "FRA"], "NYC",
                    date.fromisoformat("2026-06-11")),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=_CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=captured["inputs"]["layover"]["min"],
            duration_max=captured["inputs"]["layover"]["max"],
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_specific_with_time_ranges():
    """JFK → LHR round-trip with Early Morning + Evening time filters
    selected on the outbound."""
    captured = _strip(_load("specific_jfk_lhr_timeofday.json"),
                       "bgProgramResponse")
    out_slice = captured["inputs"]["slices"][0]
    # Reconstruct: from captured timeRanges, identify which TimeOfDay
    # values were selected.
    times = []
    for tr in out_slice.get("timeRanges", []):
        key = (tr["min"], tr["max"])
        for t in TimeOfDay:
            from flight_cli.domain import _TIME_RANGE_FOR
            if _TIME_RANGE_FOR[t] == key:
                times.append(t)
                break

    search = SpecificDateSearch(
        legs=(
            Leg.of("JFK", "LHR", date.fromisoformat(out_slice["date"]),
                    time_ranges=tuple(times)),
            Leg.of("LHR", "JFK",
                    date.fromisoformat(captured["inputs"]["slices"][1]["date"])),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


# ─────────────────────────────── helpers ───────────────────────────────────

def _diff(expected: dict, actual: dict, path: str = "") -> str:
    """Return a human-readable diff between two dicts, recursively."""
    lines: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            sub = f"{path}.{k}" if path else k
            if k not in expected:
                lines.append(f"  + {sub}: {actual[k]!r}")
            elif k not in actual:
                lines.append(f"  - {sub}: {expected[k]!r}")
            elif expected[k] != actual[k]:
                lines.extend(_diff(expected[k], actual[k], sub).splitlines())
    elif expected != actual:
        lines.append(f"  ≠ {path}: expected={expected!r} actual={actual!r}")
    return "Differences:\n" + "\n".join(lines) if lines else ""
