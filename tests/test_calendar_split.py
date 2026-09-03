"""Calendar per-destination fan-out + merge (work-on0dw).

Matrix under-reports multi-airport calendar grids under compute-budget pressure,
so a multi-airport calendar is queried one destination at a time (groupable via
--max-per-query) and merged. These tests cover the split, merge, and
`_run_calendar` orchestration (no network).
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, override

import httpx
import pytest
import typer
from rich.console import Console

from flight_cli import cli
from flight_cli._calendar_split import (
    is_empty_calendar,
    merge_calendar_results,
    split_calendar_search,
)
from flight_cli._gf_dategrid import GfGridUnavailableError
from flight_cli._gflight_ids import GfThrottledError
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.client import MatrixApiError
from flight_cli.domain import Cabin, CalendarSearch, CalendarWindow, Leg, SearchOptions
from flight_cli.models import CalendarResult, Location, SearchResult

W = CalendarWindow(start=date(2026, 9, 7), end=date(2026, 10, 7), duration_min=5, duration_max=7)


def _cal(
    dests: list[str],
    origins: tuple[str, ...] = ("MIA",),
    routing: str | None = "LH+",
    ext: str | None = None,
) -> CalendarSearch:
    out = Leg.of(list(origins), dests, route_language=routing, extension=ext)
    ret = Leg.of(dests, list(origins))
    return CalendarSearch(legs=(out, ret), options=SearchOptions(cabin=Cabin.COACH), window=W)


def _result(
    by_month_day: dict[int, dict[int, tuple[str, int, dict[int, str]]]],
    cheapest: str | None = None,
) -> CalendarResult:
    """by_month_day: {month: {date: (minPrice, solutionCount, {duration: price})}}."""
    months: list[dict[str, Any]] = []
    total = 0
    for month, days in by_month_day.items():
        day_list: list[dict[str, Any]] = []
        for dt, (mp, sols, durs) in days.items():
            total += sols
            day_list.append(
                {
                    "date": dt,
                    "solutionCount": sols,
                    "minPrice": mp,
                    "tripDuration": {
                        "options": [{"tripLength": k, "minPrice": v} for k, v in durs.items()]
                    },
                }
            )
        months.append({"month": month, "weeks": [{"days": day_list}]})
    body: dict[str, Any] = {"solutionCount": total, "calendar": {"months": months}}
    if cheapest:
        body["currencyNotice"] = {"ext": {"price": cheapest}}
    return CalendarResult.from_api(body)


# ───────────────────────────── split ───────────────────────────────────────


def test_split_multi_destination_produces_one_per_dest() -> None:
    subs = split_calendar_search(_cal(["VIE", "PAR", "FCO"]))
    assert len(subs) == 3
    seen: set[str] = set()
    for s in subs:
        out, ret = s.legs
        assert out.origins == ("MIA",)
        assert len(out.destinations) == 1
        assert out.route_language == "LH+"  # routing preserved
        d = out.destinations[0]
        seen.add(d)
        assert ret.origins == (d,)  # return leg mirrored
        assert ret.destinations == ("MIA",)
    assert seen == {"VIE", "PAR", "FCO"}


def test_split_single_airport_returns_empty() -> None:
    assert split_calendar_search(_cal(["PAR"])) == []


def test_split_preserves_extension_and_window() -> None:
    subs = split_calendar_search(_cal(["VIE", "PAR"], ext="MAXCONNECT 2:00"))
    assert subs
    assert all(s.legs[0].extension == "MAXCONNECT 2:00" for s in subs)
    assert all(s.window == W for s in subs)


def test_split_multi_origin_is_cartesian() -> None:
    subs = split_calendar_search(_cal(["PAR", "FRA"], origins=("JFK", "EWR")))
    assert len(subs) == 4  # 2 origins x 2 destinations


def test_split_groups_destinations_by_max_per_query() -> None:
    subs = split_calendar_search(_cal(["VIE", "PAR", "FCO", "MAD"]), max_per_query=2)
    assert len(subs) == 2  # 4 destinations / 2 per query
    assert all(len(s.legs[0].destinations) == 2 for s in subs)
    covered = {d for s in subs for d in s.legs[0].destinations}
    assert covered == {"VIE", "PAR", "FCO", "MAD"}  # union still complete


def test_split_max_per_query_uneven_last_group() -> None:
    subs = split_calendar_search(_cal(["VIE", "PAR", "FCO"]), max_per_query=2)
    assert len(subs) == 2  # [VIE,PAR] + [FCO]
    assert sorted(len(s.legs[0].destinations) for s in subs) == [1, 2]


def test_split_max_per_query_covering_all_is_noop() -> None:
    # one query already covers it (k >= #destinations, single origin) → no split
    assert split_calendar_search(_cal(["VIE", "PAR"]), max_per_query=5) == []


# ───────────────────────────── is_empty ────────────────────────────────────


def test_is_empty_calendar_true_for_zero() -> None:
    empty = CalendarResult.from_api({"solutionCount": 0, "calendar": {"months": []}})
    assert is_empty_calendar(empty)


def test_is_empty_calendar_false_when_priced() -> None:
    assert not is_empty_calendar(_result({9: {7: ("USD500.00", 2, {5: "USD500.00"})}}))


# ───────────────────────────── merge ───────────────────────────────────────


def test_merge_takes_per_day_and_per_duration_min() -> None:
    a = _result({9: {7: ("USD800.00", 5, {5: "USD800.00", 7: "USD850.00"})}}, cheapest="USD800.00")
    b = _result({9: {7: ("USD600.00", 3, {5: "USD650.00", 7: "USD600.00"})}}, cheapest="USD600.00")
    merged = merge_calendar_results([a, b])
    days = {d.date: d for d in merged.priced_days}
    assert merged.solution_count == 8  # summed across destinations
    assert days[7].min_price == "USD600.00"  # per-day min
    assert merged.cheapest_price == "USD600.00"  # overall cheapest
    opts = {o.trip_length: o.min_price for o in days[7].options}
    assert opts[5] == "USD650.00"  # per-duration min: min(800, 650)
    assert opts[7] == "USD600.00"  # per-duration min: min(850, 600)


def test_merge_unions_distinct_days() -> None:
    a = _result({9: {7: ("USD500.00", 1, {5: "USD500.00"})}})
    b = _result({9: {8: ("USD400.00", 2, {5: "USD400.00"})}})
    merged = merge_calendar_results([a, b])
    assert {d.date for d in merged.priced_days} == {7, 8}


def test_merge_keeps_same_date_in_different_months_distinct() -> None:
    a = _result({9: {7: ("USD500.00", 1, {5: "USD500.00"})}})
    b = _result({10: {7: ("USD400.00", 1, {5: "USD400.00"})}})
    merged = merge_calendar_results([a, b])
    assert len(merged.priced_days) == 2  # Sep-7 and Oct-7 must not collide


# ───────────────── orchestration: _run_calendar per-destination ─────────────
# Deterministic end-to-end (no network): a multi-airport calendar must fan out to
# one query per destination and merge; a single airport runs one query; an
# all-empty fan-out is surfaced as a genuine empty (not masked as "recovered").

_EMPTY: dict[str, Any] = {"solutionCount": 0, "calendar": {"months": []}}


class _PricedClient:
    """Every (single-destination) query returns a priced grid keyed by destination."""

    _PRICE: ClassVar[dict[str, str]] = {
        "VIE": "USD800.00",
        "PAR": "USD600.00",
        "FCO": "USD700.00",
        "MAD": "USD900.00",
    }

    def __init__(self, **_kwargs: object) -> None: ...

    async def __aenter__(self) -> _PricedClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = cache
        dest = next(iter(search.legs[0].destinations), "?")
        price = self._PRICE.get(dest, "USD999.00")
        return _result({9: {7: (price, 3, {5: price, 7: price})}}, cheapest=price)


class _EmptyClient(_PricedClient):
    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        return CalendarResult.from_api(_EMPTY)


def test_run_calendar_fans_out_multi_airport_and_merges(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR", "FCO", "MAD"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n == 4  # one query per destination
    assert not is_empty_calendar(res)
    assert res.cheapest_price == "USD600.00"  # PAR is the cheapest destination


def test_run_calendar_single_airport_runs_one_query(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n == 0  # nothing to fan out
    assert not is_empty_calendar(res)


def test_run_calendar_all_empty_is_not_masked(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _EmptyClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n == 0  # every destination empty → genuinely flight-less
    assert is_empty_calendar(res)


def test_run_calendar_large_fanout_proceeds(monkeypatch: Any) -> None:
    # 7 origins x 6 destinations = 42 (origin,dest) pairs: there is no hard cap —
    # it warns and proceeds (the user's call), rather than refusing.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    origins = ("JFK", "EWR", "LGA", "BOS", "PHL", "IAD", "BWI")
    dests = ["LHR", "CDG", "FRA", "AMS", "MAD", "FCO"]
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(dests, origins=origins), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n == 42  # fanned out, not refused
    assert not is_empty_calendar(res)


class _CapturingClient(_PricedClient):
    """Records the kwargs it was constructed with, to assert threaded settings."""

    last_kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, **kwargs: object) -> None:
        type(self).last_kwargs = dict(kwargs)


def test_run_calendar_max_per_query_reduces_queries(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR", "FCO", "MAD"]),
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
        max_per_query=2,
    )
    assert n == 2  # 4 destinations in groups of 2
    assert not is_empty_calendar(res)


def test_run_calendar_threads_max_concurrency(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _CapturingClient)
    cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR", "FCO", "MAD"]),
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
        max_concurrency=3,
    )
    # conc = min(n=4, max_concurrency=3) = 3
    assert _CapturingClient.last_kwargs.get("concurrency") == 3


# ──────────── orchestration: _run_calendar_enriched (concurrent weave) ───────
# The one-way / single-airport calendar dispatches the GF date-grid and the
# Matrix calendar concurrently (work-6nrqf). These assert the weave paints both
# and isolates per-backend failures — no network, fakes for both backends.


def _oneway_cal(origin: str = "JFK") -> CalendarSearch:
    return CalendarSearch(
        legs=(Leg.of([origin], ["LHR"]),),
        options=SearchOptions(cabin=Cabin.COACH),
        window=W,
    )


def _spy_renderers(monkeypatch: Any) -> dict[str, int]:
    """Replace the calendar renderers + URL emitter with call-counting spies."""
    calls: dict[str, int] = {"grid": 0, "calendar": 0}

    def _grid(*_a: object, **_k: object) -> None:
        calls["grid"] += 1

    def _cal_render(*_a: object, **_k: object) -> None:
        calls["calendar"] += 1

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(cli, "_render_date_grid", _grid)
    monkeypatch.setattr(cli, "_render_calendar", _cal_render)
    monkeypatch.setattr(cli, "_emit_urls", _noop)
    return calls


def _run_enriched(origin: str = "JFK") -> None:
    cli._run_calendar_enriched(  # pyright: ignore[reportPrivateUsage]
        _oneway_cal(origin),
        origins=(origin,),
        dests=("LHR",),
        sd=W.start,
        ed=W.end,
        dmin=5,
        dmax=7,
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
        matrix_url=False,
        google_url=False,
    )


def _fake_grid(_search: object) -> dict[str, float]:
    return {"2026-09-09": 600.0}


def test_calendar_enriched_paints_grid_then_matrix(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()
    assert calls["grid"] == 1  # GF grid painted (fast, first)
    assert calls["calendar"] == 1  # authoritative Matrix calendar painted


def test_calendar_enriched_gf_throttle_still_paints_matrix(monkeypatch: Any) -> None:
    def _throttle(_search: object) -> dict[str, float]:
        raise GfThrottledError("rate-limited")

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _throttle)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()
    assert calls["grid"] == 0  # throttled → no grid
    assert calls["calendar"] == 1  # Matrix still ran + painted (error isolation)


def test_calendar_enriched_matrix_error_with_grid_does_not_raise(monkeypatch: Any) -> None:
    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("boom", kind="internal")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # grid was shown → must NOT raise typer.Exit
    assert calls["grid"] == 1
    assert calls["calendar"] == 0  # Matrix errored → no calendar paint


@pytest.mark.parametrize(
    "message",
    [
        # Matrix quotes the routing string back at you, so its error message is
        # user text arriving from the far side of the network.
        "QPX Warning.  Illegal COMMAND-LINE prefix: BA[/weird]AA",
        "QPX Warning.  Illegal COMMAND-LINE prefix: O:LH[bold]+",
    ],
)
def test_calendar_matrix_error_survives_markup_in_the_message(
    message: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The non-fast path has no refusal to catch a bad routing string first, so
    Matrix is where the user learns it was bad — and the message comes back with
    their brackets in it. Unescaped, an unbalanced tag raises MarkupError over the
    error it was reporting."""

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError(message, kind="input", request_id="req[/x]42")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    _run_enriched()  # a grid was painted, so no Exit — only the report
    cap = _flat(capsys.readouterr().err)
    # `_flat` collapses the wrap, so flatten the expectation the same way; the
    # brackets are what matters and they survive both.
    assert _flat(message) in cap
    assert "req[/x]42" in cap


def test_calendar_enriched_unexpected_matrix_error_still_shows_grid(monkeypatch: Any) -> None:
    # A NON-MatrixApiError from execute() (e.g. a raw httpx transport/status error)
    # must not tear down the task group / cancel the grid paint — the grid still shows
    # and the command does not raise (per-backend isolation for every failure class).
    class _RawErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise RuntimeError("raw transport blip")  # NOT a MatrixApiError

    monkeypatch.setattr(cli, "MatrixClient", _RawErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # must NOT raise / traceback — grid was shown
    assert calls["grid"] == 1  # grid still painted despite the unexpected Matrix error
    assert calls["calendar"] == 0


def test_calendar_enriched_both_backends_fail_exits_nonzero(monkeypatch: Any) -> None:
    # GF throttled AND Matrix errored → nothing to show → typer.Exit(1).
    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("boom", kind="internal")

    def _throttle(_search: object) -> dict[str, float]:
        raise GfThrottledError("rate-limited")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _throttle)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit):
        _run_enriched()


def test_calendar_enriched_generic_gf_error_still_paints_matrix(monkeypatch: Any) -> None:
    # A non-throttle GF failure routes through the broad except → Matrix still paints.
    def _boom(_search: object) -> dict[str, float]:
        raise RuntimeError("network blip")

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _boom)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()
    assert calls["grid"] == 0  # GF errored → no grid
    assert calls["calendar"] == 1  # Matrix still painted (error isolation)


def test_calendar_enriched_paints_grid_before_matrix(monkeypatch: Any) -> None:
    # The progressive-reveal contract: the GF grid is painted BEFORE the Matrix calendar.
    order: list[str] = []

    def _grid(*_a: object, **_k: object) -> None:
        order.append("grid")

    def _cal_render(*_a: object, **_k: object) -> None:
        order.append("calendar")

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    monkeypatch.setattr(cli, "_render_date_grid", _grid)
    monkeypatch.setattr(cli, "_render_calendar", _cal_render)
    monkeypatch.setattr(cli, "_emit_urls", _noop)
    _run_enriched()
    assert order == ["grid", "calendar"]


# ──────────── GF date-grid RPC gate: honest degrade (work-h70kv.5) ──────────
# GetCalendarGraph returns no rows to a plain HTTP client, so `date_grid` raises
# GfGridUnavailableError. The weave must say so once and still paint Matrix, on
# stdout, where its Matrix calendar follows. `--fast` has no Matrix to fall back
# to, so it says so on STDERR and exits non-zero: stdout under `--fast` carries a
# grid or nothing, the same contract the up-front refusals keep.


def _flat(s: str) -> str:
    """rich wraps console output at ~80 columns, so a phrase can straddle two
    lines. Collapse whitespace before matching on it."""
    return " ".join(s.split())


def _unavailable(_search: object) -> dict[str, float]:
    raise GfGridUnavailableError("calendar RPC returns no data to this client")


def test_calendar_enriched_grid_unavailable_notes_once_and_paints_matrix(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _unavailable)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()
    cap = capsys.readouterr()
    assert calls["grid"] == 0  # gated → never show a GF half that isn't there
    assert calls["calendar"] == 1  # Matrix priced the window and painted
    out = _flat(cap.out)
    assert out.count("price grid unavailable") == 1  # said once, not per chunk
    # The note is printed while Matrix is still in flight, so it may only promise
    # to wait — Matrix can still fail after it (and today, on one-way, it does).
    assert "awaiting Matrix calendar" in out
    assert "Showing the Matrix calendar" not in out
    # `err` is a stderr Console, so the broad except's message lands on cap.err.
    assert "date-grid failed" not in cap.err


def test_calendar_enriched_city_code_gets_the_gate_note_not_an_attribute_error(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """NYC is a place a user can ask for and fli's `Airport` enum has no member
    for. Real `date_grid` here: the gate has to answer before `_grid_filters` does,
    or the weave's broad except turns a standing gate into `date-grid failed: type
    object 'Airport' has no attribute 'NYC'` — and Matrix still prices it either
    way, so the note is the whole difference the user sees."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    _run_enriched(origin="NYC")
    cap = capsys.readouterr()
    assert calls["calendar"] == 1  # Matrix priced the window regardless
    assert calls["grid"] == 0
    out = _flat(cap.out)
    assert out.count("price grid unavailable") == 1
    assert "date-grid failed" not in _flat(cap.err)
    assert "no attribute" not in _flat(cap.err)


def _calendar_fast(**overrides: Any) -> None:
    """Drive the `calendar` command function directly (no CliRunner in this repo).
    Every typer.Option default has to be passed explicitly — an unpassed one is an
    OptionInfo object, not a value."""
    kwargs: dict[str, Any] = {
        "origin": "JFK",
        "destination": "LHR",
        "start": "2026-09-07",
        "end": "2026-10-07",
        "duration": "5-7",
        "one_way": True,
        "cabin": "economy",
        "adults": 1,
        "children": 0,
        "seniors": 0,
        "youth": 0,
        "routing": None,
        "extension": None,
        "routing_return": None,
        "extension_return": None,
        "depart_times": None,
        "return_times": None,
        "stops": None,
        "allow_airport_changes": True,
        "only_available": True,
        "rps": 10.0,
        "impersonate": "chrome",
        "fmt": "table",
        "json_out": False,
        "matrix_url": False,
        "google_url": False,
        "no_cache": True,
        "fast": True,
        "max_per_query": 1,
        "max_concurrency": 12,
    }
    kwargs.update(overrides)
    cli.calendar(**kwargs)


def test_calendar_fast_grid_unavailable_notes_and_exits_one(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)  # never dial Matrix from a test
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _unavailable)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast()
    cap = capsys.readouterr()
    err_out = _flat(cap.err)
    assert excinfo.value.exit_code == 1  # --fast had nothing to serve
    assert calls["grid"] == 0
    assert calls["calendar"] == 0  # --fast never silently runs the ~45s Matrix calendar
    assert err_out.count("price grid unavailable") == 1
    assert "drop --fast for Matrix" in err_out
    assert cap.out == ""  # a --fast run leaves stdout a grid or nothing


def test_calendar_fast_throttled_exits_one(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # A throttle is a different reason for the same outcome: no grid, and no Matrix
    # under --fast to fall back to. Same line, same exit code as the gate.
    def _throttled(_search: object) -> dict[str, float]:
        raise GfThrottledError("rate-limited")

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _throttled)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast()
    cap = capsys.readouterr()
    err_out = _flat(cap.err)
    assert excinfo.value.exit_code == 1
    assert calls["grid"] == 0
    assert "rate-limited; no grid to show." in err_out
    assert err_out.count("drop --fast for Matrix") == 1  # said once, by the single exit
    assert cap.out == ""


@pytest.mark.parametrize("origin", ["NYC", "ZZZ"])
def test_calendar_fast_unresolvable_origin_gets_the_gate_note(
    origin: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # Neither a city code (NYC — a real place fli's `Airport` enum has no member
    # for) nor a bad IATA can be resolved into an fli filter, and while the gate
    # stands neither would have been priced anyway. The gate answers first, so what
    # the user reads is the standing reason and not `type object 'Airport' has no
    # attribute 'NYC'` dressed up as a transport failure. Real `date_grid` here
    # (offline: it never reaches a client), so the path is genuine.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(origin=origin)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1  # no grid is still no grid
    assert calls["grid"] == 0
    err_out = _flat(cap.err)
    assert "no attribute" not in err_out  # not an AttributeError in prose
    assert "date-grid failed" not in err_out
    assert err_out.count("price grid unavailable") == 1
    assert "drop --fast for Matrix" in err_out
    assert cap.out == ""


def test_calendar_fast_unexpected_grid_error_exits_one(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # The broad except still has to exit 1 for a reason with no handler of its own
    # (the shape a live transport fault takes once the gate flips).
    def _boom_grid(_search: object) -> dict[str, float]:
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _boom_grid)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast()
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    assert calls["grid"] == 0
    err_out = _flat(cap.err)
    assert "date-grid failed" in err_out  # the reason
    assert "connection reset by peer" in err_out
    assert "drop --fast for Matrix" in err_out  # and the outcome, on the same stream
    assert cap.out == ""


def test_calendar_fast_empty_grid_exits_one(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fourth no-grid outcome — the one with no handler — and the one that goes
    # live the moment the gate flips: date_grid returns {} with no exception (a
    # window Google has no fares for, or a cold session that never warmed). Nothing
    # explains it, so only the exit says so.
    def _empty(_search: object) -> dict[str, float]:
        return {}

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _empty)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast()
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    assert calls["grid"] == 0  # nothing to paint
    assert calls["calendar"] == 0
    assert _flat(cap.err).count("drop --fast for Matrix") == 1
    assert cap.out == ""


# ──────────── one-way calendars have no trip length to render ───────────────
# `wire._set_trip_length` attaches `layover` round-trip only, so a one-way request
# never asks for per-night prices and Matrix never returns any. The REAL renderer
# is driven here — the spies above only count calls (work-h70kv.10).


def test_render_calendar_one_way_omits_nights_and_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A day with NO tripDuration options, which is what a one-way calendar returns.
    res = _result({10: {14: ("USD179.00", 2, {})}}, cheapest="USD179.00")
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the renderer IS the unit
        res,
        dmin=3,
        dmax=5,
        origin=("JFK",),
        destination=("LAX",),
        sd=date(2026, 10, 10),
        ed=date(2026, 10, 20),
        round_trip=False,
    )
    out = _flat(capsys.readouterr().out)
    assert "nights" not in out  # no duration range the backend never saw
    for dur in ("3n", "4n", "5n"):
        assert dur not in out  # and no column of em-dashes under it
    assert "departure" in out and "sols" in out  # the real columns stay
    assert "179.00" in out


def test_render_calendar_round_trip_keeps_nights_and_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = _result(
        {10: {14: ("USD478.00", 6, {3: "USD599.00", 4: "USD503.00", 5: "USD478.00"})}},
        cheapest="USD478.00",
    )
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the renderer IS the unit
        res,
        dmin=3,
        dmax=5,
        origin=("JFK",),
        destination=("LAX",),
        sd=date(2026, 10, 10),
        ed=date(2026, 10, 20),
        round_trip=True,
    )
    out = _flat(capsys.readouterr().out)
    assert "duration 3-5 nights" in out
    for dur in ("3n", "4n", "5n"):
        assert dur in out
    assert "599.00" in out  # the per-night prices are actually placed


# ──────────── --fast fails closed outside the grid branch (work-h70kv.9) ────
# `--fast` promises the GF grid alone in ~1s. Where the grid branch does not apply
# it used to fall through to the ~45s Matrix calendar and exit 0, so a wrapper doing
# `--fast || fallback` read Matrix output as a fast grid. Each shape now refuses.


def test_fast_refuses_round_trip(monkeypatch: Any, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(one_way=False)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    # The refusal is a diagnostic, so it goes to stderr on EVERY shape — one of the
    # shapes it refuses is `--format json`, and the stream must not depend on which.
    assert "--fast applies only to one-way" in _flat(cap.err)
    assert "a round-trip window" in _flat(cap.err)
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


def test_fast_refuses_json_output(monkeypatch: Any, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fmt="json")
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    assert "JSON output" in _flat(cap.err)
    # Under a JSON request stdout carries a JSON document or nothing — never prose,
    # or a caller piping to `jq` gets a parse error instead of an empty result.
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


def test_fast_refuses_multi_airport(monkeypatch: Any, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(destination="LHR,CDG")
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    assert "a multi-airport route" in _flat(cap.err)
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


def test_fast_refuses_tier2_routing(monkeypatch: Any, capsys: pytest.CaptureFixture[str]) -> None:
    # `O:LH+` is an operating-carrier predicate: the grid has no itineraries to
    # post-filter, so `grid_can_serve` declines and only Matrix could answer.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(routing="O:LH+")
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    assert "this is Tier-2 routing" in _flat(cap.err)  # a noun, like the other three
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


@pytest.mark.parametrize(
    ("ext", "phrase"),
    [
        ("-CODESHARE", "this is a Tier-2 extension code"),
        ("MINCONNECT 1:00", "this is a Tier-2 extension code"),
        # `--extension` takes a `;`-separated list, so the phrase counts it.
        ("-CODESHARE;MINCONNECT 1:00", "this is Tier-2 extension codes"),
        ("-CODESHARE;-REDEYES;MINCONNECT 1:00", "this is Tier-2 extension codes"),
    ],
)
def test_fast_refuses_a_tier2_extension_without_calling_it_routing(
    ext: str, phrase: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same tier, other flag. The phrase completes "this is …" so the reader knows
    # which option to go edit, and `--routing` is not set on this command line.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(extension=ext)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    msg = _flat(cap.err)
    assert phrase in msg
    assert "routing" not in msg
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


@pytest.mark.parametrize(
    ("ext", "phrase"),
    [
        ("F bc=y", "this is a Matrix-only extension code"),
        ("F bc=y;Q zz=1", "this is Matrix-only extension codes"),
    ],
)
def test_fast_refusal_agrees_in_number_with_the_declining_directives(
    ext: str, phrase: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # One directive takes an article, several do not. The reasons list every one,
    # so the count in the phrase and the count in the parentheses match.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(extension=ext)
    msg = _flat(capsys.readouterr().err)
    assert excinfo.value.exit_code == 1
    assert phrase in msg
    for directive in ext.split(";"):
        assert directive in msg


@pytest.mark.parametrize(
    ("kwargs", "quoted", "phrase"),
    [
        # booking class: fare construction, carried by --extension
        ({"extension": "F bc=y"}, "F bc=y", "this is a Matrix-only extension code"),
        # ordered routing: not GF-expressible, carried by --routing
        ({"routing": "BA AA"}, "BA AA", "this is Matrix-only routing"),
    ],
)
def test_fast_refuses_tier3_without_calling_it_tier2(
    kwargs: dict[str, Any],
    quoted: str,
    phrase: str,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `grid_can_serve` is False for Tier-2 and Tier-3 alike, so the refusal has to
    # ask which. Tier-2 is post-filterable and only wants the itineraries a grid
    # does not carry; Tier-3 is fare construction GF can neither request nor
    # reconstruct. Naming the wrong one sends the reader after a fix that does not
    # exist.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(**kwargs)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    msg = _flat(cap.err)
    assert "Tier-2" not in msg
    assert phrase in msg  # naming the flag that carried it, not just the tier
    assert quoted in msg  # and it names the constraint that did it
    if "routing" not in kwargs:
        assert "routing" not in msg  # nothing on this command line is routing
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


# ──────────── --duration is resolved against the trip shape (one-way) ───────
# The trip LENGTH exists only between an outbound and a return: `_set_trip_length`
# and `_spa_calendar_leg` both attach it round-trip only. A one-way therefore
# decides the shape BEFORE parsing the number, so `--duration` is either read or
# reported as ignored, never both. `--duration` never raises out of a command: on
# a one-way it is a note and exit 0, on a round trip a bad value is exit 2.


class _RecordingClient(_PricedClient):
    """`_PricedClient` that keeps the search Matrix was actually handed."""

    seen: ClassVar[list[CalendarSearch]] = []

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        type(self).seen.append(search)
        return await super().execute(search, cache=cache)


def test_calendar_one_way_ignores_a_reversed_duration_without_a_traceback(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _RecordingClient.seen = []
    monkeypatch.setattr(cli, "MatrixClient", _RecordingClient)
    calls = _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, duration="9-3")  # returns: no typer.Exit, no exception
    cap = capsys.readouterr()
    assert calls["calendar"] == 1  # the window was priced
    err_out = _flat(cap.err)
    assert err_out.count("--duration is ignored") == 1  # said once
    assert "ValidationError" not in err_out
    # Ignored means ignored: the window carries the default, not 9-3, and nothing
    # downstream reads it on a one-way anyway.
    window = _RecordingClient.seen[0].window
    assert (window.duration_min, window.duration_max) == (5, 7)


def test_calendar_one_way_default_duration_is_silent(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # An explicit `5-7` is indistinguishable from the default, so the note would be
    # noise on every plain one-way calendar.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False)
    assert "--duration is ignored" not in _flat(capsys.readouterr().err)


def test_calendar_one_way_note_survives_json_output(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--format json` is exactly when a dropped flag is least visible, and stderr is
    # where the remark can go without putting prose in front of `jq`.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, fmt="json", duration="9-3")
    cap = capsys.readouterr()
    assert "--duration is ignored" in _flat(cap.err)
    assert json.loads(cap.out)  # stdout is still a document, not prose


@pytest.mark.parametrize(
    ("duration", "phrase"),
    [
        ("9-3", "max (3) is below min (9)"),  # reversed range
        ("abc", "use nights as"),  # not a number at all
        ("5-", "use nights as"),  # half a range
        ("5 7", "use nights as"),  # two numbers, no separator: not a range
        # An empty bound is a malformed range, not a max of -7: reading the second
        # dash as a sign would answer a typo with a number nobody wrote.
        ("5-- 7", "use nights as"),
        ("5-7-9", "use nights as"),  # three bounds
        # `int()` swallows every Unicode space, U+001C..1F among them, so a bound
        # is matched against digits rather than handed to `int()` to be lenient.
        ("5-\x1c7", "use nights as"),
        ("5\x1d-7", "use nights as"),
        # A bound is digits and nothing else, line endings included: `\Z` anchors
        # where `$` would match before a trailing newline, which is exactly what a
        # pipeline hands over.
        ("5-7\n", "use nights as"),
        ("5-7\r", "use nights as"),
        ("5\n-7", "use nights as"),
        # A bound that is a number and only too WIDE gets told the width. `int()`
        # refuses 4300+ digits (CPython's int/str cap), so an unbounded match
        # tracebacks where this should be a usage error — and answering a
        # ten-digit range with "use nights as '5' or '5-7'" would hand back the
        # shape the user already used.
        ("9" * 4301, "each bound is at most 9 digits"),
        ("5-" + "9" * 4301, "each bound is at most 9 digits"),
        ("1000000000", "each bound is at most 9 digits"),  # the first refused width
        # A bound in the parser's width but past the domain's size. `_render_calendar`
        # opens a column per night and fills one per priced day, so the number typed
        # here multiplies the render — and it is spent AFTER Matrix has answered,
        # which is what makes a late refusal the wrong answer.
        ("1-366", "366 nights is past the 365-night maximum"),  # one over
        ("1-10000", "10000 nights is past the 365-night maximum"),
        ("1-999999999", "999999999 nights is past the 365-night maximum"),
        ("400", "400 nights is past the 365-night maximum"),  # a bare bound too
    ],
)
def test_calendar_round_trip_bad_duration_is_a_typed_error(
    duration: str, phrase: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # Round-trip DOES read the number, so a bad one is a usage error — exit 2 and a
    # line, not the pydantic traceback `CalendarWindow`'s validator raises.
    _RecordingClient.seen = []
    monkeypatch.setattr(cli, "MatrixClient", _RecordingClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, one_way=False, duration=duration)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 2  # usage, not failure
    err_out = _flat(cap.err)
    assert phrase in err_out
    assert "ValidationError" not in err_out and "Traceback" not in err_out
    assert _RecordingClient.seen == []  # refused before any Matrix work


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", (5, 5)),  # a bare number is the degenerate range
        ("07", (7, 7)),  # zero-padded, still one number
        ("0", (0, 0)),  # the floor CalendarWindow allows
        ("5-7", (5, 7)),
        ("5..7", (5, 7)),  # the SPA's range spelling
        (" 5 - 7 ", (5, 7)),  # blanks around either end
        ("3-3", (3, 3)),  # an explicit degenerate range
        ("05-07", (5, 7)),  # zero-padded bounds
        ("+5-+7", (5, 7)),  # signed bounds
        # The accept side of the size bound, whose reject list holds the neighbour
        # one night over. A year is where a nights range stops being one; a trip
        # of most of one is a trip somebody takes.
        ("365", (365, 365)),
        ("1-365", (1, 365)),
        ("90-180", (90, 180)),
    ],
)
def test_parse_duration_accepts_every_spelling_of_a_valid_range(
    raw: str, expected: tuple[int, int]
) -> None:
    """The half of `_parse_duration` the error tests never reach. `--duration 5`
    is the documented short form and the CLI has no other way to ask for one
    night count, so the bare-number branch is a contract, not an implementation
    detail."""
    got = cli._parse_duration(raw)  # pyright: ignore[reportPrivateUsage] — the parser IS the unit
    assert got == expected


@pytest.mark.parametrize(
    "spelling", ["5-7", "5..7", " 5-7 ", "5 - 7", "5-  7", "05-07", "+5-+7", "05..7"]
)
def test_calendar_one_way_is_silent_for_every_spelling_of_the_default(
    spelling: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value that means the default changes nothing, so it is not worth a line —
    the note exists to explain a value the user will otherwise look for in the
    output. The comparison runs on the same normalizer the parser does, down to
    the zero-padded and signed writings of a number, so the two agree on which
    spellings are the same range."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, duration=spelling)
    assert "--duration is ignored" not in _flat(capsys.readouterr().err)


@pytest.mark.parametrize("routing", ["BA[/weird]AA", "BA[bold]AA", "BA[/]AA"])
def test_fast_refusal_survives_markup_in_a_flag(
    routing: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`err` is a markup console and the refusal quotes the flag verbatim, so an
    unbalanced tag raised MarkupError instead of refusing, and a well-formed one
    ate the token the reader needs to see. Escaped at the render site: the
    reason strings come from the routing parser, which builds them from user
    text and has no console to escape for."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(routing=routing)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1  # a refusal, not a crash
    assert routing in _flat(cap.err)  # brackets and all, so the reader can see it
    assert cap.out == ""
    assert calls["calendar"] == 0


@pytest.mark.parametrize(
    ("kwargs", "shown"),
    [
        ({"duration": "[/x]"}, "'[/x]'"),  # unparseable, and unbalanced markup
        ({"duration": "9[bold]-3"}, "'9[bold]-3'"),  # a tag rich would otherwise eat
        ({"start": "[/x]"}, "'[/x]'"),  # the same crash one flag over
        ({"fmt": "[/x]"}, "'[/x]'"),  # --format, which runs first in the body
        ({"cabin": "[/x]"}, "'[/x]'"),
        ({"depart_times": "[/x]"}, "'[/x]'"),
        ({"cabin": "[bold]"}, "'[bold]'"),  # a tag rich would otherwise eat
    ],
)
def test_parse_errors_survive_markup_in_a_flag(
    kwargs: dict[str, Any], shown: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parsers quote what the user typed, so they escape it too. `repr` runs
    BEFORE `escape`: reversed, `repr` doubles the backslash `escape` prepends and
    hands the tag straight back to the markup parser."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, one_way=False, **kwargs)
    assert excinfo.value.exit_code == 2  # a usage error, not a crash
    assert shown in _flat(capsys.readouterr().err)


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"duration": "9" * 4301}, "duration"),
        ({"start": "2026-10-01" + "x" * 4301}, "date"),
        ({"cabin": "e" * 4301}, "cabin"),
        ({"depart_times": "z" * 4301}, "time-of-day"),
        ({"fmt": "j" * 4301}, "format"),
        # `repr` doubles every backslash, so this value's MESSAGE is twice the
        # length of the others' while the value shown is the same size. A bound on
        # the message would pass or fail on the fill rather than on the cap.
        ({"cabin": "\\" * 4301}, "cabin"),
    ],
)
def test_parse_errors_truncate_an_oversized_value(
    kwargs: dict[str, Any], flag: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message names which value was rejected, so it has to stay readable:
    the parsers take any string a shell can pass, and echoing 4301 characters
    back buries the point under its own evidence."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, one_way=False, **kwargs)
    message = _flat(capsys.readouterr().err)
    value = next(iter(kwargs.values()))
    assert excinfo.value.exit_code == 2  # still a typed usage error
    assert flag in message  # and still says which flag
    assert "…" in message  # cut, and visibly so
    # And bounded, which the ellipsis alone does not say: a message that echoed the
    # whole value and appended "…" satisfies the line above and nothing else here.
    # Measured against the constant, not against `_quote`, whose own output would
    # grow with the mutant and move the bound out of the mutant's way.
    assert (
        len(message) < cli._MAX_ECHOED_VALUE + 200  # pyright: ignore[reportPrivateUsage] — the cap IS the unit
    )
    # What the cap actually governs: the value the user typed, measured where the
    # cap applies — before `repr`, which is where one code point stops being one
    # character. Plus one for the ellipsis.
    shown = cli._elide(value)  # pyright: ignore[reportPrivateUsage] — the cap IS the unit
    assert len(shown) <= cli._MAX_ECHOED_VALUE + 1  # pyright: ignore[reportPrivateUsage] — as above


# Text from somewhere else — a Matrix message, an exception — is not just markup.
# ESC and CSI drive the terminal, and `escape` neutralises `[` alone: it leaves an
# ESC to clear the screen or repaint the line above, and a bidi override to reorder
# what is left. A redirected stderr keeps every byte for whatever reads it next.
_DRIVES_THE_TERMINAL = (
    "\x1b[2J\x9b31m\x7f\u202e\u2067\u2028\u2029\u200e\u200f\u061c\u200b\ufeff"
    "\u00ad\u200c\u200d\u2060\U000e0041\ud800"
)
_READS_AS_TEXT = "café ¥1200 → [/x]"
# The code points that do the driving, as opposed to the letters they steer:
# a clear-screen, its 7-bit and 8-bit introducers, DEL, an override, an isolate,
# the two separators `str.splitlines` breaks on, the three bidi marks, the
# invisibles that survive `strip()` and sit unseen inside a carrier code, a tag
# character, and a lone surrogate that no utf-8 stream can write at all. Asserted
# individually, because `[`, `2` and `J` are ordinary text once the ESC is gone —
# and because a set member with no case here is a claim in a comment, not a
# property: removing the last five from `_CTRL` left the whole file green.
_DRIVERS = (
    "\x1b[2J",
    "\x1b",
    "\x9b",
    "\x7f",
    "\u202e",
    "\u2067",
    "\u2028",
    "\u2029",
    "\u200e",
    "\u200f",
    "\u061c",
    "\u200b",
    "\ufeff",
    "\u00ad",
    "\u200c",
    "\u200d",
    "\u2060",
    "\U000e0041",
    "\ud800",
)
# Rich's own colour codes, which it interleaves with the text when styling for a
# terminal — `café ¥1200` comes back in three styled pieces. Only SGR sequences,
# so a payload's `ESC [ 2J` would survive this and fail the assertion above.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize("force_terminal", [False, True])
def test_matrix_error_strips_terminal_control_characters(
    force_terminal: bool, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Redirected or on a terminal, the same bytes are dropped: whether stderr is
    a tty changes the styling, not what the payload can do to whoever renders it."""
    message = f"{_READS_AS_TEXT}{_DRIVES_THE_TERMINAL}"

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError(
                message, kind=f"input{_DRIVES_THE_TERMINAL}", request_id="r\x1b[1m"
            )

    buffer = io.StringIO()
    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    monkeypatch.setattr(cli, "err", Console(file=buffer, force_terminal=force_terminal, width=200))
    _spy_renderers(monkeypatch)
    _run_enriched()  # a grid was painted, so no Exit — only the report
    written = buffer.getvalue()

    # Rich interleaves its own SGR codes when it styles for a terminal, and it
    # styles the payload's `[` — which splits an ESC from the `[2J` that follows
    # and hides the sequence from a search of the raw stream. Strip rich's own
    # colour codes and the payload's bytes are all that is left to find.
    probe = _SGR.sub("", written)
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    assert "café ¥1200" in probe  # the readable half survives
    assert "input" in probe  # and the kind field is still readable
    _ = capsys.readouterr()


def test_safe_text_keeps_the_sentence_and_drops_the_drivers() -> None:
    """`_safe_text` is for a sentence someone has to read: no quoting, no cut. The
    part that explains a failure is as often at the end as at the start."""
    safe_text = cli._safe_text  # pyright: ignore[reportPrivateUsage] — the helper IS the unit
    out = safe_text(f"{_READS_AS_TEXT}{_DRIVES_THE_TERMINAL}")
    for driver in _DRIVERS:
        assert driver not in out, f"{driver!r} survived"
    assert "café ¥1200 →" in out
    assert "\\[/x]" in out  # still escaped, not just stripped
    # Strip THEN escape, and only a control character INSIDE a tag tells the two
    # orders apart: `escape` sees a tag only where `[` is followed by `[a-z#/@]`,
    # so the NUL hides `[red]` from it and stripping afterwards yields
    # "[red]x\\[/]" — a live red style, from a message that should read as text.
    assert safe_text("[\x00red]x[/]") == "\\[red]x\\[/]"
    assert safe_text("kept\tby\ntab and newline") == "kept\tby\ntab and newline"
    long_sentence = "why it failed. " * 40
    assert safe_text(long_sentence) == long_sentence  # never truncated, unlike _quote
    # An exception is the one value that can say nothing and still have to be
    # reported; anything else that is blank is blank because someone chose it.
    assert safe_text(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert safe_text(ValueError("\x00\x01")) == "ValueError"  # blank once sanitized
    # The fallback is not a literal this module wrote: a class built from a remote
    # payload is named by that payload, so it takes the same two steps.
    named_by_a_backend = type("Bad\x1b[2JName", (Exception,), {})
    assert safe_text(named_by_a_backend("")) == "Bad[2JName"  # the ESC gone, the rest kept
    assert safe_text("") == ""
    assert safe_text("   ") == "   "


def _detail(**overrides: Any) -> None:
    """Drive the `detail` command function directly. Same rule as `_calendar_fast`:
    every typer.Option default has to be passed explicitly."""
    kwargs: dict[str, Any] = {
        "origin": "JFK",
        "destination": "LAX",
        "dep": "2026-10-01",
        "ret": None,
        "start": None,
        "end": None,
        "duration": "5-7",
        "cabin": "economy",
        "adults": 1,
        "children": 0,
        "seniors": 0,
        "youth": 0,
        "routing": None,
        "extension": None,
        "routing_return": None,
        "extension_return": None,
        "stops": None,
        "allow_airport_changes": True,
        "rps": 10.0,
        "impersonate": "chrome",
        "fmt": "table",
        "json_out": False,
        "matrix_url": False,
        "google_url": False,
        "no_cache": True,
    }
    kwargs.update(overrides)
    cli.detail(**kwargs)


class _Utf8Console:
    """A console file that encodes what it is given, as a real stdout does.

    `StringIO` accepts a lone surrogate and hands it back; utf-8 has no encoding
    for one, so a real stream raises UnicodeEncodeError and the render of a query
    that succeeded dies on the way out."""

    def __init__(self) -> None:
        self.raw = io.BytesIO()
        self.text = io.TextIOWrapper(self.raw, encoding="utf-8", newline="")

    def written(self) -> str:
        self.text.flush()
        return self.raw.getvalue().decode("utf-8")


class _MatrixErrorClient:
    """Every query fails the way Matrix fails: a typed error whose three fields
    carry whatever the backend chose to echo back."""

    def __init__(self, **_kwargs: object) -> None: ...

    async def __aenter__(self) -> _MatrixErrorClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, search: object, *, cache: bool = True) -> object:
        _ = (search, cache)
        raise MatrixApiError(
            f"Illegal COMMAND-LINE prefix: {_DRIVES_THE_TERMINAL}",
            kind=f"input{_DRIVES_THE_TERMINAL}",
            request_id=f"r{_DRIVES_THE_TERMINAL}",
        )


@pytest.mark.parametrize("force_terminal", [False, True])
def test_detail_matrix_error_strips_terminal_control_characters(
    force_terminal: bool, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`detail` runs its query through `_run`, which reports a Matrix error through
    the same helper the calendar uses: one backend message cannot repaint the
    terminal on one command and read as text on another."""
    stream = _Utf8Console()
    monkeypatch.setattr(cli, "MatrixClient", _MatrixErrorClient)
    monkeypatch.setattr(
        cli, "err", Console(file=stream.text, force_terminal=force_terminal, width=200)
    )
    with pytest.raises(typer.Exit) as excinfo:
        _detail()
    assert excinfo.value.exit_code == 1
    written = stream.written()
    for driver in _DRIVERS:
        assert driver not in _SGR.sub("", written), f"{driver!r} reached the console"
    flat = _flat(_SGR.sub("", written))
    assert "Illegal COMMAND-LINE prefix" in flat  # message
    assert "input" in flat  # kind
    assert "request_id" in flat  # and the third field goes the same way


@pytest.mark.parametrize("force_terminal", [False, True])
def test_calendar_fast_refusal_strips_terminal_control_characters(
    force_terminal: bool, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal quotes a blocker sentence back. What that sentence carries is the
    blocker's business, so the render site strips control characters itself rather
    than depending on how a reason was spelled where it was built."""

    def _driving_blocker(*_a: object, **_k: object) -> str:
        return f"Matrix-only routing ({_DRIVES_THE_TERMINAL})"

    buffer = io.StringIO()
    monkeypatch.setattr(cli, "_grid_branch_blocker", _driving_blocker)
    monkeypatch.setattr(cli, "err", Console(file=buffer, force_terminal=force_terminal, width=200))
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast()
    assert excinfo.value.exit_code == 1
    written = buffer.getvalue()
    for driver in _DRIVERS:
        assert driver not in _SGR.sub("", written), f"{driver!r} reached the console"
    assert "Matrix-only routing" in _flat(_SGR.sub("", written))
    assert capsys.readouterr().out == ""  # a refusal leaves stdout empty


def test_a_matrix_failure_that_says_nothing_still_names_itself(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transport exception can stringify to nothing (`httpx.ConnectTimeout("")`),
    and "Matrix calendar failed:" followed by a blank tells the reader less than
    the class name does."""

    class _SilentClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise httpx.ConnectTimeout("")

    monkeypatch.setattr(cli, "MatrixClient", _SilentClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # the grid painted, so the Matrix half only reports
    cap = capsys.readouterr()
    assert calls["grid"] == 1
    assert "Matrix calendar failed: ConnectTimeout" in _flat(cap.err)


@pytest.mark.parametrize("value", ["[/x]", "[bold]x"])
def test_bad_rps_configuration_is_a_typed_error_that_shows_the_value(
    value: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`FLIGHT_RPS` reaches a markup console inside the ValueError `float()` raised,
    which repr's the setting into its own message. An unbalanced tag there answered
    a misconfiguration with a MarkupError traceback, and a well-formed one ate the
    value the message exists to name. Reached from `calendar` and from `detail`."""
    _RecordingClient.seen = []
    monkeypatch.setenv("FLIGHT_RPS", value)
    monkeypatch.setattr(cli, "MatrixClient", _RecordingClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, one_way=False, rps=None)
    assert excinfo.value.exit_code == 2  # a typed usage error, not a traceback
    message = _flat(capsys.readouterr().err)
    assert f"FLIGHT_RPS='{value}'" in message  # verbatim, tags and all
    assert "is not a number" in message
    assert _RecordingClient.seen == []  # refused before any Matrix work


def test_calendar_summary_and_cells_survive_a_markup_price(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cheapest_price` and every `minPrice` are Matrix's strings verbatim, and the
    summary line and the table cells both parse markup: an unbalanced tag lost a
    query that had succeeded, and a well-formed one ate the number."""
    res = _result(
        {9: {7: ("[/x]USD500.00", 1, {5: "[bold]USD501.00", 7: "USD502.00"})}},
        cheapest="[bold]USD421 [/x]",
    )
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=300))
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        res,
        dmin=5,
        dmax=7,
        origin=("JFK",),
        destination=("LHR",),
        sd=W.start,
        ed=W.end,
        round_trip=True,
    )
    written = _flat(buffer.getvalue())
    assert "[bold]USD421 [/x]" in written  # the summary line, shown not parsed
    assert "[/x]USD500.00" in written  # the min cell
    assert "[bold]USD501.00" in written  # a per-duration cell
    _ = capsys.readouterr()


# Everything Matrix chooses in a search response, one field at a time. `[/x]`
# raises MarkupError and loses a render that had succeeded, `[bold]` eats the
# value the cell exists to show, and the ESC drives the terminal it lands on.
_HOSTILE_FIELD_VALUES = ("[/x]", "[bold]", "\x1b[2J")


def _search_result(
    *,
    price: str = "USD421.00",
    code: str = "BA",
    short_name: str = "British Airways",
    row_label: str = "0 stops",
    carrier: str = "BA",
) -> SearchResult:
    """A one-solution search response with a carrier x stops grid, shaped the way
    `SearchResult.from_api` reads Matrix's body."""
    return SearchResult.from_api(
        {
            "solutionCount": 1,
            "currencyNotice": {"ext": {"price": price}},
            "carrierStopMatrix": {
                "columns": [{"label": {"code": code, "shortName": short_name}}],
                "rows": [{"label": row_label, "cells": [{"minPrice": "USD421.00"}]}],
            },
            "solutionList": {
                "solutions": [
                    {
                        "ext": {"price": "USD421.00"},
                        "itinerary": {"carriers": [{"code": carrier}], "slices": []},
                    }
                ]
            },
        }
    )


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize(
    "field", ["price", "currency", "code", "short_name", "row_label", "carrier"]
)
def test_search_summary_and_table_survive_a_hostile_matrix_field(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every one of these is Matrix's string verbatim, and all of them land on a
    markup console — the summary line, both table titles, a column header, a row
    label and a carrier cell. The currency half is driven through `_split_price`
    because `_PRICE_RE` bounds it to three letters, so the render site is pinned
    without depending on that regex staying as it is."""
    kwargs = {field: payload} if field != "currency" else {}
    res = _search_result(**kwargs)
    if field == "currency":

        def _hostile_currency(_s: str | None) -> tuple[str, str]:
            return payload, "421.00"

        monkeypatch.setattr(cli, "_split_price", _hostile_currency)
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":  # the ESC is dropped, so only its letters remain
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


# Everything Matrix (and, on the enriched path, Google Flights) chooses INSIDE an
# itinerary cell. The cell is composed from several of these at once, so each is
# driven one at a time: a matrix that varied them together would pass on whichever
# field happened to be wrapped.
_SLICE_FIELDS = ("origin", "destination", "stop", "flight_number", "timestamps", "legroom_class")


def _slice_solution(field: str, payload: str) -> dict[str, Any]:
    """One solution whose itinerary slice carries `payload` in `field`.

    The timestamps are driven as a PAIR and deliberately unreadable as dates:
    `_fmt_slice_times` formats two datetimes when it can parse them and falls back
    to Matrix's own two strings when it cannot, and only the fallback is remote.
    """
    return {
        "ext": {"price": "USD421.00"},
        "itinerary": {
            "carriers": [{"code": "BA"}],
            "slices": [
                {
                    "origin": {"code": payload if field == "origin" else "JFK"},
                    "destination": {"code": payload if field == "destination" else "LHR"},
                    "stops": [{"code": payload if field == "stop" else "BOS"}],
                    "flights": [payload if field == "flight_number" else "BA117"],
                    "departure": f"{payload}dep" if field == "timestamps" else "2026-10-01T08:00",
                    "arrival": f"{payload}arr" if field == "timestamps" else "2026-10-01T20:00",
                    "duration": 420,
                    "legs": [
                        {
                            "pitch_inches": 31,
                            "legroom_class": payload if field == "legroom_class" else "Lie Flat",
                        }
                    ],
                }
            ],
        },
    }


def _slice_result(field: str, payload: str) -> SearchResult:
    """A search response holding one such solution and nothing else hostile."""
    return SearchResult.from_api(
        {
            "solutionCount": 1,
            "currencyNotice": {"ext": {"price": "USD421.00"}},
            "solutionList": {"solutions": [_slice_solution(field, payload)]},
        }
    )


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("field", _SLICE_FIELDS)
def test_search_itinerary_cells_survive_a_hostile_matrix_field(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The itinerary cells are the half of the table the summary cases never
    reached: airport codes, connection codes, flight numbers, the raw-ISO timestamp
    fallback and the seat-type name all land in a Rich cell, which parses markup
    exactly as the title above it does."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_search(_slice_result(field, payload))  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":  # the ESC is dropped, so only its letters remain
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("field", _SLICE_FIELDS)
def test_multi_cabin_itinerary_cells_survive_a_hostile_matrix_field(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The multi-cabin table builds its cells from the same formatter, and the two
    renderers have drifted apart on a shared field before, so it is pinned on its
    own rather than through the one above."""
    row = MultiCabinRow(
        itinerary=_slice_result(field, payload).solutions[0],
        prices={Cabin.COACH: "USD421.00"},
    )
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_multi_cabin_search(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [row], cabins=(Cabin.COACH,), sort_by=Cabin.COACH
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


def test_a_hostile_slice_does_not_cost_the_legroom_colour(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Why the fix wraps LEAVES and not the composed cell. `_fmt_legroom_one` writes
    a real `[red]` around a below-average pitch, and one wrap around the finished
    cell would print that tag instead of colouring the number — a table that is safe
    and says less than it did."""
    solution = _slice_solution("origin", "[/x]")
    solution["itinerary"]["slices"][0]["legs"][0]["legroom_class"] = "BELOW"
    res = SearchResult.from_api(
        {
            "solutionCount": 1,
            "currencyNotice": {"ext": {"price": "USD421.00"}},
            "solutionList": {"solutions": [solution]},
        }
    )
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, force_terminal=True, width=400))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    written = buffer.getvalue()
    probe = _flat(_SGR.sub("", written))
    assert "[/x]" in probe  # the hostile code, shown and not parsed
    assert '31"' in probe  # and the pitch the colour is on
    # Rich turns `[red]` into an SGR sequence, so the styled bytes are the only
    # evidence the tag was still a tag by the time the cell reached the console.
    assert re.search(r"\x1b\[[0-9;]*31[;m]", written), "the legroom colour was escaped away"
    _ = capsys.readouterr()


def test_airport_lookup_survives_a_hostile_query_and_a_hostile_location(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`flight airport '[/x]'` needs no hostile backend to crash: the argument goes
    into the table title verbatim. The four Location fields beside it are Matrix's,
    on every success."""

    class _Locations:
        def __init__(self, **_kwargs: object) -> None: ...

        async def __aenter__(self) -> _Locations:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def airports(self, query: str) -> list[Location]:
            _ = query
            return [
                Location.model_validate(
                    {
                        "code": f"[/x]{_DRIVES_THE_TERMINAL}",
                        "displayName": "[bold]Name",
                        "cityName": "[/y]City",
                        "timezone": "[/z]TZ",
                    }
                )
            ]

    buffer = io.StringIO()
    monkeypatch.setattr(cli, "MatrixClient", _Locations)
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli.airport(query="[/x]", impersonate="chrome")
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    assert "'[/x]'" in probe  # the query, quoted, in the title
    assert "[bold]Name" in probe
    assert "[/y]City" in probe
    assert "[/z]TZ" in probe
    _ = capsys.readouterr()


def test_one_failing_cabin_does_not_take_the_other_cabins_down(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A per-cabin Google Flights failure is soft by design: that cabin is omitted
    and the rest still render. Reporting it through a markup console made it hard —
    the MarkupError raised inside the task group leaves as an ExceptionGroup,
    cancels the sibling cabin and loses a result that had already arrived."""
    good: list[Any] = [object()]
    asked: list[Cabin] = []

    def _search_with_ids(search: Any, top_n: int = 5) -> list[Any]:
        _ = top_n
        asked.append(search.options.cabin)
        if search.options.cabin is Cabin.BUSINESS:
            raise RuntimeError(f"fli said [/x]no{_DRIVES_THE_TERMINAL}")
        return good

    def _identity(search: Any) -> Any:
        # `to_fli_filter` hands the stub the search itself, so the cabin under test
        # is readable without building an fli filter.
        return search

    monkeypatch.setattr("flight_cli.fli_bridge.to_fli_filter", _identity)
    monkeypatch.setattr("flight_cli._gflight_ids.search_with_ids", _search_with_ids)
    out = cli._run_gflight_multi(  # pyright: ignore[reportPrivateUsage] — the fan-out IS the unit
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        top_n=5,
    )
    assert set(asked) == {Cabin.COACH, Cabin.BUSINESS}  # both cabins ran
    assert set(out) == {Cabin.COACH}  # the failing one is omitted, not fatal
    assert out[Cabin.COACH] is good  # and the one that answered still answers
    probe = _flat(_SGR.sub("", capsys.readouterr().err))
    assert probe.count("query failed") == 1  # one soft line, not a traceback
    assert "fli said [/x]no" in probe  # the message shown, not parsed
    assert "BUSINESS" in probe  # and it names which cabin went missing
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"


def test_a_nights_range_the_renderer_cannot_render_is_refused_before_matrix(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cost of this flag is paid in the renderer, one column per night and one
    cell per priced day — and it is paid AFTER the round trip, so an accepted range
    the table cannot hold loses an answer Matrix already computed. The refusal is
    therefore the parser's, before any Matrix work."""
    _RecordingClient.seen = []
    monkeypatch.setattr(cli, "MatrixClient", _RecordingClient)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, one_way=False, duration="1-10000")
    assert excinfo.value.exit_code == 2
    assert _RecordingClient.seen == []  # refused before any Matrix work
    assert "past the 365-night maximum" in _flat(capsys.readouterr().err)


def test_the_widest_accepted_nights_range_still_renders(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the bound: a cap is only a cap if what it admits renders.
    One column per night at the maximum, with a priced day to fill them. No timing
    assertion — the bound is the guarantee, not the clock."""
    dmin, dmax = cli._parse_duration(f"1-{cli._MAX_NIGHTS}")  # pyright: ignore[reportPrivateUsage] — the cap IS the unit
    buffer = io.StringIO()
    # Wide enough that the columns the cap admits are all readable; a narrow
    # console renders the same table and shows one character of each.
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=8000))
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        _result({9: {7: ("USD500.00", 1, {5: "USD501.00"})}}),
        dmin=dmin,
        dmax=dmax,
        origin=("JFK",),
        destination=("LHR",),
        sd=W.start,
        ed=W.end,
        round_trip=True,
    )
    written = _flat(buffer.getvalue())
    assert "500.00" in written  # the day's own cell, its currency stripped by `_amount`
    assert "365n" in written  # and the last of the columns the cap allows
    _ = capsys.readouterr()


def test_an_unknown_provider_name_is_a_typed_error_that_shows_the_value(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--providers` takes any string — `canonical_provider` lowercases an unknown
    name and passes it through — so what the user typed reaches the message saying
    it matched nothing. An unbalanced tag there answered a typo with a MarkupError
    instead of the sentence that names the typo."""
    monkeypatch.setattr("flight_cli.providers.registry.has_any_configured", lambda: True)
    monkeypatch.setattr("flight_cli.providers.pointspath.provider.is_configured", lambda: True)
    monkeypatch.setattr("flight_cli.providers.seats_aero.auth.is_configured", lambda: False)
    sel = cli.ProviderSelection(
        provider_filter=("[/]",),
        cash_only=False,
        awards_only=True,
        provider_opts={},
    )
    with pytest.raises(typer.Exit) as excinfo:
        cli._should_run_awards(sel)  # pyright: ignore[reportPrivateUsage] — the branch IS the unit
    assert excinfo.value.exit_code == 2
    message = _flat(capsys.readouterr().err)
    assert "'[/]'" in message  # the value the user typed, quoted and shown
    assert "matches no configured provider" in message


def test_multi_cabin_soft_failure_survives_a_hostile_matrix_message(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A per-cabin failure is soft — the column renders empty and the other cabins
    still show. Reporting it through a markup console made it hard: an unbalanced
    tag in the message raises MarkupError inside the task group, which leaves as an
    ExceptionGroup and takes every other cabin's result with it."""
    good = object()

    class _OneCabinFails:
        def __init__(self, **_kwargs: object) -> None: ...

        async def __aenter__(self) -> _OneCabinFails:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, search: Any, *, cache: bool = True) -> object:
            _ = cache
            if search.options.cabin is Cabin.BUSINESS:
                raise MatrixApiError(
                    f"Illegal COMMAND-LINE prefix: BA[/weird]AA{_DRIVES_THE_TERMINAL}",
                    kind=f"input{_DRIVES_THE_TERMINAL}",
                )
            return good

    monkeypatch.setattr(cli, "MatrixClient", _OneCabinFails)
    out = cli._run_matrix_multi(  # pyright: ignore[reportPrivateUsage] — the fan-out IS the unit
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
    )
    assert set(out) == {Cabin.COACH}  # the failing cabin is omitted, not fatal
    assert out[Cabin.COACH] is good  # and the cabin that answered still answers
    written = capsys.readouterr().err
    probe = _flat(_SGR.sub("", written))
    assert probe.count("query failed") == 1  # one soft line, not a traceback
    assert "BA[/weird]AA" in probe  # the message shown, not parsed
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"


def test_multi_cabin_group_failure_reports_through_the_shared_reporter(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure that escapes the whole fan-out rather than one cabin is fatal, and
    reports the same three fields the calendar does, request_id included."""

    class _ClientFailsOnOpen(_MatrixErrorClient):
        @override
        async def __aenter__(self) -> _ClientFailsOnOpen:
            raise MatrixApiError(
                f"Illegal COMMAND-LINE prefix: BA[/weird]AA{_DRIVES_THE_TERMINAL}",
                kind=f"input{_DRIVES_THE_TERMINAL}",
                request_id=f"r{_DRIVES_THE_TERMINAL}",
            )

    monkeypatch.setattr(cli, "MatrixClient", _ClientFailsOnOpen)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_matrix_multi(  # pyright: ignore[reportPrivateUsage] — the fan-out IS the unit
            legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
            opts=SearchOptions(cabin=Cabin.COACH),
            cabins=(Cabin.COACH,),
            rps=10.0,
            impersonate="chrome",
            no_cache=True,
        )
    assert excinfo.value.exit_code == 1
    probe = _flat(_SGR.sub("", capsys.readouterr().err))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    assert "BA[/weird]AA" in probe
    assert "request_id" in probe


def test_gflight_only_path_reports_a_hostile_query_failure(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """fli has no documented exception surface, so whatever it raises is reported
    verbatim — and whatever it raises can carry a backend's text."""

    def _boom(*_a: object, **_k: object) -> list[Any]:
        raise RuntimeError(f"upstream said [/x]no{_DRIVES_THE_TERMINAL}")

    monkeypatch.setattr(cli, "_gflight_results", _boom)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_gflight_path(  # pyright: ignore[reportPrivateUsage] — the path IS the unit
            legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
            opts=SearchOptions(cabin=Cabin.COACH),
            top_n=5,
            json_out=False,
        )
    assert excinfo.value.exit_code == 1
    probe = _flat(_SGR.sub("", capsys.readouterr().err))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    assert "upstream said [/x]no" in probe


@pytest.mark.parametrize("gflight_fails", [False, True])
def test_search_weave_reports_a_hostile_matrix_error_through_the_shared_reporter(
    gflight_fails: bool, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The weave reports the same Matrix error the calendar does, so a message that
    clears a terminal on one command cannot read as text on the other. Its Google
    Flights half reports whatever fli raised, which is remote text too."""

    def _no_gflight(*_a: object, **_k: object) -> list[Any]:
        if gflight_fails:
            raise RuntimeError(f"upstream said [/x]no{_DRIVES_THE_TERMINAL}")
        return []

    monkeypatch.setattr(cli, "MatrixClient", _MatrixErrorClient)
    monkeypatch.setattr(cli, "_gflight_results", _no_gflight)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_enriched_path(  # pyright: ignore[reportPrivateUsage] — the weave IS the unit
            legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
            opts=SearchOptions(cabin=Cabin.COACH),
            top_n=5,
            run_pp=False,
            sel=None,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=10.0,
            impersonate="chrome",
            no_cache=True,
        )
    assert excinfo.value.exit_code == 1  # no Google Flights half either
    probe = _flat(_SGR.sub("", capsys.readouterr().err))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    assert "Illegal COMMAND-LINE prefix" in probe
    assert "request_id" in probe  # the third field, which a partial reporter drops first
    if gflight_fails:
        assert "upstream said [/x]no" in probe


def test_quote_keeps_a_normal_value_whole() -> None:
    """Truncation is for the pathological case; an ordinary mistyped value is
    short, and cutting it would hide the typo the message exists to show."""
    quote = cli._quote  # pyright: ignore[reportPrivateUsage] — the helper IS the unit
    elide = cli._elide  # pyright: ignore[reportPrivateUsage] — as above
    cap = cli._MAX_ECHOED_VALUE  # pyright: ignore[reportPrivateUsage] — as above
    assert quote("5-7") == "'5-7'"
    assert elide("9" * cap) == "9" * cap  # exactly the cap is not cut
    assert elide("9" * (cap + 1)) == "9" * cap + "…"  # one over is
    assert quote("9" * (cap + 1)).endswith("…'")
    # A backslash costs one code point going in and two coming out of `repr`, so
    # the cap has to be applied to the value, not to what is printed.
    assert len(elide("\\" * (cap + 1))) == cap + 1
    assert len(quote("\\" * (cap + 1))) > 2 * cap


def _stub_detail_run(monkeypatch: Any) -> list[Any]:
    """Replace the Matrix round-trip with a recorder; `detail`'s renderer and URL
    emitter go with it, since neither is under test here."""
    seen: list[Any] = []

    def _run(search: Any, *_a: object, **_k: object) -> Any:
        seen.append(search)
        return object()

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(cli, "_run", _run)
    monkeypatch.setattr(cli, "_render_search", _noop)
    monkeypatch.setattr(cli, "_emit_urls", _noop)
    return seen


def test_detail_one_way_ignores_a_reversed_duration_without_a_traceback(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # `detail` without `--return` is the same one-way shape as the calendar, and
    # answers `--duration` the same way: one note, the default range, exit 0.
    seen = _stub_detail_run(monkeypatch)
    _detail(duration="9-3")
    err_out = _flat(capsys.readouterr().err)
    assert err_out.count("--duration is ignored") == 1
    assert "ValidationError" not in err_out
    assert len(seen) == 1  # the followup ran
    assert (seen[0].window.duration_min, seen[0].window.duration_max) == (5, 7)


def test_detail_round_trip_bad_duration_is_a_typed_error(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _stub_detail_run(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _detail(ret="2026-10-08", duration="9-3")
    err_out = _flat(capsys.readouterr().err)
    assert excinfo.value.exit_code == 2
    assert "max (3) is below min (9)" in err_out
    assert "ValidationError" not in err_out
    assert seen == []  # refused before any Matrix work


# ──────────── every value these paths print is escaped (work-h70kv.9) ───────
# `err` and `console` are markup-enabled, so any user string or exception message
# reaching them is markup until escaped: an unbalanced `[/x]` raises MarkupError
# instead of the message, and a well-formed `[bold]` eats the token the reader
# needs. The cases above cover the values that carry user text today; this reads
# the source, so a NEW print cannot be added without one.
#
# It reads ONE file, `src/flight_cli/cli.py`, and says nothing about any other.
# `src/flight_cli/pp/cli.py` builds a second markup console of its own and is not
# scanned here (work-h70kv.19).
#
# Polarity is inverted on purpose. An opt-IN list of functions to scan goes stale
# the moment a print moves into a new helper — the helper is simply not listed,
# and the scan stays green. Everything is scanned unless the top-level function it
# sits in is named below, so the default for new code is "checked".

# Functions this scan does not cover. A reason names WHAT the function prints and
# why that value is not this path's to wrap — never that a sink is safe, because
# there is no safe sink: a Rich Table parses markup in every cell and in its
# title, and the calls that fill one are read here for that reason.
#
# Keyed on the TOP-LEVEL function, so a nested helper inherits the exemption of
# the command it belongs to and cannot pick one up by reusing a name.
_ESCAPE_OUT_OF_SCOPE = {
    "_pinned_solution_index": "prints pick, an int typer parsed, and len(result.solutions)",
    "_validate_sort_cabin": "prints sort_by.value and the cabin names in a list it built",
    "_resolve_providers": "prints the {e} a provider config raised, owned by the provider path",
}

# There are two ways a value becomes printable: `_quote`, which elides, quotes
# and escapes a value the user typed, and `_safe_text`, which strips the control
# characters and escapes anything remote. Bare `rich.markup.escape` is neither.
# It neutralises `[` and leaves every ESC, 8-bit CSI, bidi control and lone
# surrogate in place, and telling which values can carry one means tracking taint
# through locals, `str()` calls and attribute chains — which the scan cannot do.
# Both wrappers end in `escape`, so nothing is lost. `_amount` is the third
# because it calls `_safe_text` on the way out: it is the formatter for a price,
# and sanitizing inside a formatter is what keeps the wrap off every call site.
_SAFE_WRAPPERS = frozenset({"_quote", "_safe_text", "_amount"})

# Identifiers that need no wrapper at the print site: counters and dates this
# module computed, constants it wrote, and locals already sanitized where the
# value was read. Matched by IDENTIFIER — a bare name or an attribute chain —
# never by source text: an expression that happens to read the same way is not
# the same value. Keyed per FUNCTION for the same reason one step further: `n` is
# a fan-out counter in `_run_calendar` and could be anything anywhere else, and a
# bare name is exactly what a user value looks like once it is in a local.
_PRINTABLE_IDENTIFIERS = frozenset(
    {
        ("_parse_duration", "lo"),  # the ints it just parsed
        ("_parse_duration", "hi"),
        ("_parse_duration", "_MAX_NIGHTS"),  # and the cap it is comparing them to
        ("_run_calendar", "n"),  # fan-out counters
        ("_run_calendar", "rounds"),
        ("_run_calendar", "conc"),
        ("calendar", "n_split"),  # how many sub-searches were merged
        ("calendar", "_GF_GRID_UNAVAILABLE_NOTE"),
        ("_run_calendar_enriched", "_GF_GRID_UNAVAILABLE_WEAVE_NOTE"),
        ("_resolve_format", "_FORMAT_CHOICES"),
        ("_run_matrix_multi", "cab.value"),  # a member of this module's own enum
        ("_run_gflight_multi", "cab.value"),
        ("_emit_urls", "pinned_label"),  # "#N" or "cheapest", built from an int
        ("_render_search", "res.solution_count"),  # counts and dates off the response
        ("_render_calendar", "res.solution_count"),
        ("_render_calendar", "duration_note"),
        ("_render_date_grid", "priced_days"),
        ("_render_gflight_table", "_LEGROOM_KEY"),  # the legend it wrote
        # Sanitized where the currency was read, so the summary line and the table
        # title interpolate one value that was wrapped once.
        ("_render_search", "ccy_tag"),
        ("_render_calendar", "ccy_tag"),
        ("_render_multi_cabin_search", "ccy_tag"),
        # Cells and rows composed in the renderer from leaves each wrapped where
        # it was read — `_fmt_slice_cell`, `_leg_display`, `_fmt_gflight_legroom`
        # and `_amount` — and NOT wrapped again around the composition, because
        # `_fmt_legroom_one` writes a `[red]` on the pitch token on purpose.
        ("_render_search", "cells"),
        ("_render_search", "it_carriers"),
        ("_render_search", "out"),
        ("_render_search", "ret"),
        ("_render_merged", "out"),
        ("_render_merged", "ret"),
        ("_render_multi_cabin_search", "carriers"),
        ("_render_multi_cabin_search", "out_cell"),
        ("_render_multi_cabin_search", "ret_cell"),
        ("_render_multi_cabin_search", "price_cells"),
        ("_render_calendar", "row"),
        ("_render_gflight_table", "label"),  # the row number, and its a/b suffix
        ("_render_gflight_table", "dur"),  # "3h05m", from an integer count of minutes
        ("_render_gflight_table", "legs_str"),
        ("_render_gflight_table", "legroom_str"),
        # The cabin letters are this module's own map, keyed by its own enum.
        ("_render_multi_cabin_search", "title_prefix"),
        ("_render_multi_cabin_search", "cabin_labels"),
        ("_render_multi_cabin_search", "sort_label"),
        ("_render_multi_cabin_search", "letter"),
        # The table each renderer built and then prints whole. Every value that
        # went into it was read at the `Table(...)`, `add_column` and `add_row`
        # below, which is what makes the print itself add nothing.
        ("_render_search", "t"),
        ("_render_search", "st"),
        ("_render_calendar", "t"),
        ("_render_date_grid", "t"),
        ("_render_merged", "t"),
        ("_render_multi_cabin_search", "t"),
        ("_render_gflight_table", "t"),
        ("airport", "t"),
    }
)

# What "reaches rich" means, one call shape at a time. A Rich Table parses markup
# in its title, in every column header and in every cell, so the calls that FILL
# one are sinks exactly as `console.print` is: the text is chosen there, and the
# `console.print(t)` a hundred lines later adds none of its own. Reading the fill
# rather than exempting the print is what lets a renderer be scanned at all.
_TEXT_SINK_METHODS = frozenset({"print", "log", "rule", "status", "add_row", "add_column"})
# Constructors whose arguments become markup the moment the object is printed,
# read at the constructor for the same reason.
_RENDERABLE_SINKS = frozenset({"Table", "Panel", "Text"})

# Presentation types only a number survives: `format("x", "d")` raises, so a field
# carrying one of these cannot be a string and cannot carry markup. The
# alternative is allowlisting the name, and for `fr.price` — a `getattr` off a
# duck-typed Google Flights result — that would be a promise nobody here can keep.
_NUMERIC_PRESENTATION = frozenset("bdoxXneEfFgG%")


def _def_time_expressions(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """The parts of a `def` evaluated where it sits rather than when it is called.

    A decorator and a default argument run at import, in the scope around the
    `def`, so a print in one must not inherit the function's exemption. Annotations
    are absent from this list because the module defers them (`from __future__
    import annotations`), which leaves them as strings nothing evaluates.
    """
    args = fn.args
    # `kw_defaults` carries a None per keyword-only argument that has no default;
    # `defaults` has an entry only where there is one.
    return [*fn.decorator_list, *args.defaults, *[d for d in args.kw_defaults if d is not None]]


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, list[str]]:
    """Every node mapped to the function names enclosing it, innermost first."""
    chains: dict[ast.AST, list[str]] = {}

    def walk(node: ast.AST, chain: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = chain
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = [child.name, *chain]
            chains[child] = inner
            walk(child, inner)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Re-walked last so the def-time parts keep the OUTER chain the
                # walk above just overwrote with the function's own.
                for outer in _def_time_expressions(child):
                    chains[outer] = chain
                    walk(outer, chain)

    walk(tree, [])
    return chains


def _dotted_name(node: ast.expr) -> str | None:
    """`res.solution_count` as a dotted string, or None for anything that is not a
    plain name or an attribute chain.

    A chain is still an identifier — `res.solution_count` names one value the way
    `n` does — so it can be allowlisted without the scan reading source text: a
    call, a subscript or an expression returns None and stays unallowlistable.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted_name(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _is_ours(name: ast.expr, chain: list[str]) -> bool:
    """Whether this identifier is one that needs no wrapper here.

    The chain, not just the innermost function, because a print can sit in a
    closure — and two closures in this module are both called `_go`.
    """
    dotted = _dotted_name(name)
    if dotted is None:
        return False
    return any((fn, dotted) in _PRINTABLE_IDENTIFIERS for fn in chain)


def _is_safe_field(value: ast.expr, chain: list[str]) -> bool:
    """A printed f-string field is safe when it is wrapped, or is one of ours.

    Judged on the AST shape — an `ast.Call` whose `func` is an `ast.Name` in
    `_SAFE_WRAPPERS` — never on the source text. `_safe_text` and `not_safe_text`
    share a prefix, and `obj._safe_text(x)` is an attribute call on something else
    entirely; both read as safe to a string comparison.
    """
    if isinstance(value, ast.Call):
        return isinstance(value.func, ast.Name) and value.func.id in _SAFE_WRAPPERS
    return _is_ours(value, chain)


def _is_a_print(node: ast.Call) -> bool:
    """Whether this call hands text to a markup console — `console.print`,
    `err.log`, a rule or a status, bare `print`, or a renderable's title, columns
    and rows, which are the same console one object later."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _TEXT_SINK_METHODS
    return isinstance(func, ast.Name) and (func.id == "print" or func.id in _RENDERABLE_SINKS)


def _printed_identifiers(src: str, function: str) -> set[str]:
    """Every identifier `function` hands to a console, dotted chains included."""
    tree = ast.parse(src)
    chains = _enclosing_functions(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_a_print(node):
            continue
        chain = chains.get(node, [])
        if not chain or chain[-1] != function:
            continue
        for arg in [*node.args, *[k.value for k in node.keywords]]:
            for inner in ast.walk(arg):
                if isinstance(inner, (ast.Name, ast.Attribute)):
                    dotted = _dotted_name(inner)
                    if dotted:
                        found.add(dotted)
    return found


def _spec_has_field(spec: ast.expr | None) -> bool:
    """Whether a format spec interpolates anything. `f"{escape(a):{e}}"` makes `e`
    the padding character, which reaches rich without passing the wrapper."""
    return spec is not None and any(isinstance(n, ast.FormattedValue) for n in ast.walk(spec))


def _spec_proves_a_number(spec: ast.expr | None) -> bool:
    """Whether this format spec would raise on a string, which makes the field a
    number whatever the name says. A spec with a field of its own proves nothing —
    the fill is interpolated too — so only a single literal counts."""
    if not isinstance(spec, ast.JoinedStr) or len(spec.values) != 1:
        return False
    only = spec.values[0]
    return (
        isinstance(only, ast.Constant)
        and isinstance(only.value, str)
        and only.value[-1:] in _NUMERIC_PRESENTATION
    )


def _printed_parts(arg: ast.expr) -> list[ast.expr] | None:
    """The sub-expressions of an argument that are each printed in their own right,
    or None when the argument is not composed of others.

    A concatenation is its two pieces, a conditional is whichever branch wins, an
    `or` is each of its operands, and a starred list stands in for the argument
    list itself — so each piece is judged on its own. `%` is deliberately absent:
    its left side is a template, not text beside the value.
    """
    if isinstance(arg, ast.Starred):
        return [arg.value]
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        return [arg.left, arg.right]
    if isinstance(arg, ast.IfExp):
        return [arg.body, arg.orelse]
    if isinstance(arg, ast.BoolOp):
        return list(arg.values)
    return None


def _argument_faults(src: str, arg: ast.expr, chain: list[str]) -> list[str]:
    """Why this print argument could reach rich unescaped, or nothing."""
    if isinstance(arg, ast.Constant):
        # A literal the author wrote, of any type: rich's own knobs arrive as
        # `no_wrap=True` and `width=200` as often as `style="red"`.
        return []
    parts = _printed_parts(arg)
    if parts is not None:
        return [fault for part in parts for fault in _argument_faults(src, part, chain)]
    if isinstance(arg, ast.JoinedStr):
        faults: list[str] = []
        for part in arg.values:
            if isinstance(part, ast.Constant):
                continue
            shown = ast.get_source_segment(src, part) or ast.dump(part)
            if (
                isinstance(part, ast.FormattedValue)
                and part.conversion == -1
                and _spec_proves_a_number(part.format_spec)
            ):
                continue  # a string would raise here, so this field is a number
            if not isinstance(part, ast.FormattedValue) or not _is_safe_field(part.value, chain):
                faults.append(f"unwrapped f-string field {shown}")
            elif part.conversion != -1 and isinstance(part.value, ast.Call):
                # `!r` and `!a` run AFTER the wrapper, and `repr` doubles the
                # backslash `escape` prepended — rich then reads one literal
                # backslash followed by a live tag. `_quote` exists because the
                # only safe order is the other one. Harmless on an allowlisted
                # name, which is a number or a string this module wrote.
                faults.append(f"wrapped f-string field re-armed by a conversion: {shown}")
            elif _spec_has_field(part.format_spec):
                faults.append(f"unwrapped field in the format spec of {shown}")
        return faults
    if _is_safe_field(arg, chain):
        return []  # a wrapper call, or one of ours, standing as the whole argument
    shown = ast.get_source_segment(src, arg) or ast.dump(arg)
    return [f"{type(arg).__name__} argument {shown}"]


def escape_scan(src: str) -> list[str]:
    """Console prints in `src` that could hand rich unescaped text.

    Checks positional AND keyword arguments, and demands a literal, a fully
    wrapped f-string, or an allowlisted name. A local holding a message, a
    `.format()`, a `%`, or a concatenation of one is none of those, and neither is
    a field whose conversion or format spec runs after the wrapper.

    What it does not model is scope. An allowlisted identifier is a claim about a
    NAME in a function, so a closure inside that function inherits the pass, and a
    second binding of the name in either place is invisible here — `_run_calendar`
    is the only live shape with both, and `n` / `rounds` / `conc` are the names to
    watch. The hostile-field tests above are what pin the values themselves.
    """
    tree = ast.parse(src)
    chains = _enclosing_functions(tree)
    faults: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_a_print(node):
            continue
        chain = chains.get(node, [])
        if chain and chain[-1] in _ESCAPE_OUT_OF_SCOPE:
            continue
        where = chain[0] if chain else "<module>"
        args = list(node.args)
        args += [k.value for k in node.keywords]
        for arg in args:
            faults += [
                f"{where}:{node.lineno} {fault}" for fault in _argument_faults(src, arg, chain)
            ]
    return faults


def test_calendar_paths_escape_every_printed_value() -> None:
    faults = escape_scan(Path(cli.__file__).read_text(encoding="utf-8"))
    assert not faults, (
        "wrap these in _quote (a value the user typed) or _safe_text (anything "
        "remote); add the name to _PRINTABLE_IDENTIFIERS if the value is this "
        "module's own, or the function to _ESCAPE_OUT_OF_SCOPE with a reason "
        f"naming what its prints carry: {faults}"
    )


def test_out_of_scope_names_real_functions() -> None:
    """A renamed or deleted function would sit in the exclusion list forever,
    silently exempting whatever later takes its name."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    defined = {
        fn.name
        for fn in ast.walk(ast.parse(src))
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert defined >= set(_ESCAPE_OUT_OF_SCOPE), set(_ESCAPE_OUT_OF_SCOPE) - defined
    allowed = {fn for fn, _ in _PRINTABLE_IDENTIFIERS}
    assert defined >= allowed, allowed - defined


def test_out_of_scope_entries_are_all_load_bearing() -> None:
    """An entry that exempts nothing reads like a decision and is not one: it
    survives every audit and pre-exempts whatever prints its function grows next.
    Removing any single entry has to make the scan speak."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    inert: list[str] = []
    for name in list(_ESCAPE_OUT_OF_SCOPE):
        with pytest.MonkeyPatch.context() as mp:
            mp.delitem(_ESCAPE_OUT_OF_SCOPE, name)
            if not escape_scan(src):
                inert.append(name)
    assert not inert, f"these entries exempt nothing; delete them: {inert}"


def test_printable_identifiers_are_all_load_bearing() -> None:
    """The same rule for the other allowlist, which is the longer of the two. An
    entry that allows nothing still pre-approves whatever later takes its name in
    that function, and nothing else in the file would speak when it did."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    inert: list[tuple[str, str]] = []
    for entry in sorted(_PRINTABLE_IDENTIFIERS):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                sys.modules[__name__],
                "_PRINTABLE_IDENTIFIERS",
                _PRINTABLE_IDENTIFIERS - {entry},
            )
            if not escape_scan(src):
                inert.append(entry)
    assert not inert, f"these entries allow nothing; delete them: {inert}"


def _identifiers_not_named(reason: str, printed: set[str]) -> list[str]:
    """The identifiers in `printed` that `reason` does not name.

    Word boundaries, because the highest-value name to have to justify is `e`, the
    exception variable, and a substring test finds it in any reason containing the
    letter. EVERY identifier, because naming one value does not excuse the rest,
    and the one left unnamed is the one nobody looked at.
    """
    return [
        identifier
        for identifier in sorted(printed)
        if not re.search(rf"(?<![\w.]){re.escape(identifier)}(?![\w])", reason)
    ]


def test_out_of_scope_reasons_name_what_the_function_prints() -> None:
    """A reason is the only thing telling the next reader whether an exemption can
    be lifted. "search-path weave" says where the code lives; it does not say what
    reaches a console, so nobody can check it. Naming every value that does makes
    the claim falsifiable, and this test is what falsifies it."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    vague: list[str] = []
    for name, reason in _ESCAPE_OUT_OF_SCOPE.items():
        printed = _printed_identifiers(src, name)
        if not printed:
            if "nothing" not in reason:
                vague.append(f"{name}: prints nothing this scan reads; say so or drop the entry")
            continue
        unnamed = _identifiers_not_named(reason, printed)
        if unnamed:
            vague.append(f"{name}: {reason!r} does not name {unnamed}")
    assert not vague, vague


def test_a_reason_that_names_nothing_is_rejected() -> None:
    """The test above is worth its lines only if it can fail, and the shape it
    replaces could not: `any(i in reason for i in printed)` passed on one value out
    of five, and `in` alone found the one-character `e` inside any word holding an
    "e". Each case below passed that check and names nothing."""
    printed = {"e", "cab.value", "pinned_label"}
    assert _identifiers_not_named("cheese", printed) == ["cab.value", "e", "pinned_label"]
    assert _identifiers_not_named("the values it was handed", printed) == [
        "cab.value",
        "e",
        "pinned_label",
    ]
    # A reason that names a LOCATION rather than a value — the one thing the
    # header above forbids — and one that names a single value out of three.
    assert _identifiers_not_named("search-path weave", printed) == [
        "cab.value",
        "e",
        "pinned_label",
    ]
    assert _identifiers_not_named("prints cab.value", printed) == ["e", "pinned_label"]
    # And it passes only when every one is named, in the spelling the scan reads.
    assert _identifiers_not_named("prints {e}, cab.value and pinned_label", printed) == []


def test_escape_scan_finds_every_known_bypass() -> None:
    """The scan's own regression net: one source per shape that reaches rich
    holding text it did not escape, or escaped too weakly."""
    bypasses = {
        "local variable": 'def calendar():\n    msg = f"{e}"\n    err.print(msg)\n',
        "keyword argument": 'def calendar():\n    err.print(text=f"{e}")\n',
        "str.format": 'def calendar():\n    err.print("{}".format(e))\n',
        "percent formatting": 'def calendar():\n    err.print("%s" % e)\n',
        "concatenation": 'def calendar():\n    err.print("bad " + str(e))\n',
        "a newly extracted helper": 'def _brand_new_helper():\n    err.print(f"{e}")\n',
        "console.log": 'def calendar():\n    console.log(f"{e}")\n',
        "console.rule": 'def calendar():\n    console.rule(f"{e}")\n',
        "console.status": 'def calendar():\n    console.status(f"{e}")\n',
        "builtin print": 'def calendar():\n    print(f"{e}")\n',
        "partly wrapped": 'def calendar():\n    err.print(f"{escape(a)} {b}")\n',
        "lookalike wrapper": 'def calendar():\n    err.print(f"{not_escape(e)}")\n',
        "attribute call that ends in escape": (
            'def calendar():\n    err.print(f"{shell.escape(e)}")\n'
        ),
        # An allowlisted name earns its pass in ONE function. `n` is a fan-out
        # counter in `_run_calendar`; anywhere else it is just a local, and a
        # local is what a user value looks like once it has been assigned.
        "allowlisted name in the wrong function": 'def detail():\n    err.print(f"{n}")\n',
        # `escape` neutralises markup and nothing else, so it is a fault wherever
        # it stands in for a wrapper — and the value it is handed is exactly what
        # a taint check cannot follow: a bare name, a `str()` call, a local
        # assigned three lines up, an attribute of a response model.
        "escape on an exception": 'def calendar():\n    err.print(f"{escape(e)}")\n',
        "escape on str() of an exception": (
            'def calendar():\n    err.print(f"{escape(str(e))}")\n'
        ),
        "escape on a local holding remote text": (
            'def calendar():\n    msg = matrix_message()\n    err.print(f"{escape(msg)}")\n'
        ),
        "escape on a value the user typed": (
            'def calendar():\n    err.print(f"{escape(routing)}")\n'
        ),
        "escape on a response field": (
            'def calendar():\n    err.print(f"{escape(res.cheapest_price)}")\n'
        ),
        # A Rich table is a console one object later. Its title, its headers and
        # its cells all parse markup, and a value that reaches one never passes
        # through the `console.print(t)` this scan would otherwise be reading.
        "a raw name in a table cell": (
            "def _render_search():\n    st.add_row(res.cheapest_price)\n"
        ),
        "a raw field in a table cell": ('def _render_search():\n    st.add_row(f"{it.price}")\n'),
        "a raw field in a column header": (
            'def _render_search():\n    st.add_column(f"{col.label.code}")\n'
        ),
        "a raw field in a table title": (
            'def _render_search():\n    t = Table(title=f"{query}")\n'
        ),
        "a raw field in a panel": ('def _render_search():\n    p = Panel(f"{e}")\n'),
        # A starred list is the argument list, so the cells come out of it.
        "a raw name starred into a row": (
            "def _render_calendar():\n    t.add_row(*[res.cheapest_price])\n"
        ),
        # Either half of a concatenation, and either branch of a conditional,
        # is printed on its own.
        "a raw name concatenated to a literal": (
            'def _render_search():\n    st.add_row("#" + res.cheapest_price)\n'
        ),
        "a raw name in one branch of a conditional": (
            'def _render_search():\n    st.add_row(res.cheapest_price if x else "—")\n'
        ),
        "a raw name behind an or": ('def _render_search():\n    st.add_row(carriers or "?")\n'),
        # A format spec proves a number only when it is a literal one: a numeric
        # type on a field whose fill is interpolated proves nothing about the fill.
        "an interpolated fill beside a numeric type": (
            'def calendar():\n    err.print(f"{price:{fill}.2f}")\n'
        ),
        "a padding spec, which any string survives": (
            'def calendar():\n    err.print(f"{code:<6}")\n'
        ),
        # A decorator and a default argument run at import, in the scope around
        # the `def`, so neither inherits the exemption the name would carry.
        "a print in a decorator on an excluded function": (
            '@err.print(f"{e}")\ndef _validate_sort_cabin():\n    pass\n'
        ),
        "a print in a default argument of an excluded function": (
            'def _validate_sort_cabin(x=err.print(f"{e}")):\n    pass\n'
        ),
        # `!r` runs after the wrapper: `repr` doubles the backslash `escape`
        # prepended and hands the tag straight back to the markup parser.
        "conversion applied after the wrapper": (
            'def calendar():\n    err.print(f"{_safe_text(e)!r}")\n'
        ),
        # A format spec is interpolated too, and its field becomes the fill.
        "unwrapped field inside a format spec": (
            'def calendar():\n    err.print(f"{escape(a):{e}}")\n'
        ),
        # The exemption belongs to the top-level function, so a nested helper
        # that happens to reuse an excluded name is still scanned.
        "nested helper reusing an excluded name": (
            'def calendar():\n    def _render_search():\n        err.print(f"{e}")\n'
        ),
    }
    missed = [name for name, source in bypasses.items() if not escape_scan(source)]
    assert not missed, f"the scan does not catch: {missed}"


def test_escape_scan_passes_clean_source() -> None:
    """And it must not cry wolf, or the next author will route around it."""
    clean = (
        "def calendar():\n"
        '    err.print("[red]a plain literal[/]")\n'
        '    err.print(f"[red]window {_safe_text(sd.isoformat())}[/]")\n'
        '    err.print(f"[dim]{_GF_GRID_UNAVAILABLE_NOTE}[/]")\n'  # allowed HERE
        # Either kind of safe value may stand as the whole argument.
        "    err.print(_GF_GRID_UNAVAILABLE_NOTE)\n"
        "    err.print(_safe_text(e))\n"
        # Rich's own knobs are literals of whatever type rich takes.
        '    err.print("hi", no_wrap=True, width=200)\n'
        "def _parse_duration():\n"
        '    err.print(f"[red]bad duration {_quote(s)}[/]")\n'
        '    err.print(f"max ({hi}) is below min ({lo})", style="red")\n'  # allowed HERE
        "def _run_calendar_enriched():\n"
        "    async def _go():\n"
        # Allowed through the enclosing function, not the closure it sits in.
        '        console.print(f"[dim]{_GF_GRID_UNAVAILABLE_WEAVE_NOTE}[/]")\n'
        # A table filled through the wrappers, then printed whole: the title, the
        # header and the cell are each read where the value was chosen, and the
        # name of the table is allowlisted in the function that built it.
        "def _render_merged():\n"
        '    t = Table(title=f"merged {_safe_text(origin)}", show_header=True)\n'
        '    t.add_column("price", justify="right")\n'
        "    t.add_row(_amount(row.gf_price), out, ret)\n"
        "    console.print(t)\n"
        # A numeric presentation type is a proof about the value, not a promise
        # about the name: a string reaching either of these raises instead.
        "def _render_gflight_table():\n"
        '    t.add_row(f"{fr.price:.2f}", f"{i:d}")\n'
    )
    assert not escape_scan(clean)
