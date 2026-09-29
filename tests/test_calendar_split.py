"""Calendar per-destination fan-out + merge (work-on0dw).

Matrix under-reports multi-airport calendar grids under compute-budget pressure,
so a multi-airport calendar is queried one destination at a time (groupable via
--max-per-query) and merged. These tests cover the split, merge, and
`_run_calendar` orchestration (no network).
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, cast, override

import anyio
import httpx
import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _config, cli
from flight_cli._api_key import ApiKeyResolutionError
from flight_cli._calendar_split import (
    is_empty_calendar,
    merge_calendar_results,
    split_calendar_search,
)
from flight_cli._enrich import MergedRow
from flight_cli._gf_dategrid import GfGridUnavailableError
from flight_cli._gflight_ids import GfThrottledError
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.client import MatrixApiError
from flight_cli.domain import Cabin, CalendarSearch, CalendarWindow, Leg, SearchOptions
from flight_cli.models import (
    CalendarResult,
    Itinerary,
    LegInfo,
    Location,
    SearchResult,
    Slice,
    SliceEndpoint,
)

if TYPE_CHECKING:
    from types import ModuleType

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


def _spy_renderers(monkeypatch: Any, into: dict[str, int] | None = None) -> dict[str, int]:
    """Replace the calendar renderers + URL emitter with call-counting spies.

    `into` counts in a dict the caller already holds, which is the only way a
    drive that RAISES can hand its counters back: its return value never reaches
    the test that called it. Both counters are reset here, so a caller that put a
    count into that dict before calling loses it."""
    calls: dict[str, int] = {} if into is None else into
    calls.update({"grid": 0, "calendar": 0})

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


def test_calendar_enriched_paints_grid_then_matrix(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()
    assert calls["grid"] == 1  # GF grid painted (fast, first)
    assert calls["calendar"] == 1  # authoritative Matrix calendar painted
    # Nothing on stderr but the paint's own status line: the weave reports whatever
    # it stashed on both branches, so the branch where it stashed nothing names no
    # failure, and the line saying Matrix is still coming is not one.
    assert _flat(capsys.readouterr().err) == "…refining with Matrix (full grid + durations)…"


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


# ──────────── a failure the weave stashed is read on every branch ──────────
# `_run_calendar_weave` catches everything leaving the event loop and stashes it,
# and the reporter that reads the stash sits behind "Matrix produced no result".
# A client teardown, a renderer or a closed pipe that fails AFTER Matrix answered
# therefore had no reader at all: the command exited 0 with a full calendar on
# stdout and nothing on stderr, where the same failure one path over is a typed
# line and exit 1. The exit code follows what the reader got: an answer that was
# delivered stands, and says on stderr what went wrong after it; an answer that
# was not is exit 1.


class _TeardownFailsClient(_PricedClient):
    """Answers, then fails on the way out — a client teardown after the answer."""

    @override
    async def __aexit__(self, *_exc: object) -> None:
        raise RuntimeError("client teardown blew up")


def _exploding_grid_renderer(*_a: object, **_k: object) -> None:
    raise RuntimeError("the date-grid renderer blew up")


def test_a_teardown_that_fails_after_the_answer_is_still_reported(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _TeardownFailsClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # the calendar was delivered, so the failure is not an exit code
    assert calls["calendar"] == 1  # and it is still painted
    assert "client teardown blew up" in _flat(capsys.readouterr().err)


def test_a_first_paint_that_fails_after_the_answer_is_still_reported(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same silence by the other route: the grid renderer raises after Matrix
    has answered, so the failure happens where only the stash can see it and the
    calendar goes out with nothing said about the half that died."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _exploding_grid_renderer)
    _run_enriched()
    assert calls["calendar"] == 1  # Matrix answered, so the answer is still painted
    assert "the date-grid renderer blew up" in _flat(capsys.readouterr().err)


class _TeardownRefusesClient(_PricedClient):
    """Answers, then fails on the way out with a typed backend error."""

    @override
    async def __aexit__(self, *_exc: object) -> None:
        raise MatrixApiError(
            "the session could not be closed", kind="unavailable", request_id="req-9"
        )


def test_a_backend_error_after_the_answer_is_still_reported(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stash has two keys and a `MatrixApiError` leaving the weave lands in
    the other one, so a branch that reads only the unexpected key is the same
    silence one key over. Reported through the Matrix reporter, so the kind and
    the request id a reader quotes when they report an outage survive here too."""
    monkeypatch.setattr(cli, "MatrixClient", _TeardownRefusesClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # the calendar was delivered, so the failure is not an exit code
    assert calls["calendar"] == 1
    line = _flat(capsys.readouterr().err)
    assert "the session could not be closed" in line
    assert "unavailable" in line  # the kind
    assert "req-9" in line  # and the id


def test_a_paint_that_never_happened_is_not_a_success(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit gate asks what the paint put on the reader's screen, not what was
    fetched: a grid that arrived and then died in the renderer is not an answer,
    and exit 0 over an empty stdout says it was. Two failures happen here and both
    are named — the reader has to know the window was never priced AND that the
    fast half died."""

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("Matrix is down", kind="unavailable")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _exploding_grid_renderer)
    with pytest.raises(typer.Exit) as excinfo:
        _run_enriched()
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""  # nothing was painted, so nothing on stdout says otherwise
    line = _flat(cap.err)
    assert "Matrix is down" in line  # the window was never priced
    assert "the date-grid renderer blew up" in line  # and the fast half died too


def test_a_grid_that_was_painted_keeps_exit_zero_with_the_note(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other side of the gate, with the real renderer: Matrix failed but the
    grid reached the reader, so the answer is a document on stdout, the failure is
    a note on stderr, and the exit code is 0. This is the contract the case above
    is measured against."""

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("Matrix is down", kind="unavailable")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _run_enriched()  # a grid was painted, so no Exit
    cap = capsys.readouterr()
    assert "2026-09-09" in _flat(cap.out)  # the grid the reader was given
    assert "Matrix is down" in _flat(cap.err)  # and what they did not get


class _QueryAndTeardownFailClient(_PricedClient):
    """Refuses the query, then refuses to close the session on the way out."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        raise MatrixApiError("the query was refused", kind="internal", request_id="req-QUERY")

    @override
    async def __aexit__(self, *_exc: object) -> None:
        raise MatrixApiError(
            "the session could not be closed", kind="unavailable", request_id="req-TEARDOWN"
        )


def test_two_matrix_failures_in_one_run_are_both_named(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The weave's Matrix task and the guard outside the event loop both stash, and
    the two failures are not interchangeable: a query Matrix refused is the reason
    there is no calendar, where a session that would not close is what happened
    afterwards. One slot keeps whichever was written second, so the reader is sent
    after the teardown and never learns the query was refused at all."""
    monkeypatch.setattr(cli, "MatrixClient", _QueryAndTeardownFailClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _unavailable)
    _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _run_enriched()
    assert excinfo.value.exit_code == 1
    line = _flat(capsys.readouterr().err)
    assert "the query was refused" in line  # why there is no calendar
    assert "req-QUERY" in line  # and the id a reader quotes for it
    assert "the session could not be closed" in line  # and what happened next
    assert "req-TEARDOWN" in line


def test_a_first_paint_that_raised_is_not_reported_as_a_matrix_outage(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first paint is the Google Flights half's own output and the renderer
    drawing it is this process. Named under Matrix it reads as a backend outage,
    which is an operator going after a service that never went down."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _exploding_grid_renderer)
    _run_enriched()
    assert calls["calendar"] == 1  # Matrix answered, so the answer is still painted
    line = _flat(capsys.readouterr().err)
    assert "the date-grid renderer blew up" in line
    assert "Google Flights" in line  # the half that actually failed
    assert "Matrix calendar failed" not in line  # and not the one that did not
    # Matrix still priced the window, so this is a degradation standing beside an
    # answer rather than an outage, and the softer verb is the whole difference.
    assert "could not be shown" in line


def test_a_reader_that_hung_up_under_the_first_paint_is_not_a_group(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rich.Console.on_broken_pipe` answers a reader that closed the pipe with
    `SystemExit`, which is a `BaseException`, and a task group wraps whatever leaves
    its host body. A `BaseExceptionGroup` holding one is not an `Exception`, so the
    guard round the weave cannot see it and neither can click: whoever ran `| head`
    gets the group's traceback where every other calendar arm gives them a quiet
    exit. This is the only arm that writes to a console from inside a task group,
    which is why it is the only one with the shape."""

    def _reader_hung_up(*_a: object, **_k: object) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _reader_hung_up)
    with pytest.raises(SystemExit) as excinfo:
        _run_enriched()
    assert excinfo.value.code == 1  # the exit a closed pipe has of its own
    line = _flat(capsys.readouterr().err)
    assert "ExceptionGroup" not in line  # never the plumbing round the cause
    assert "sub-exception" not in line


def test_a_matrix_brownout_behind_the_standing_gate_leaves_stdout_empty(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 before the answer is written is a promise about stdout: no document at
    all. The grid RPC is gated,
    so every weave run today takes a branch with no grid to paint and says so while
    Matrix is still in flight — and Matrix failing behind that is the everyday
    brownout. Real renderers, because a spy is exactly what would hide a status line
    landing in the stream the answer uses."""

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("Matrix is down", kind="unavailable", request_id="req-7")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _unavailable)
    with pytest.raises(typer.Exit) as excinfo:
        _run_enriched()
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""  # the stream a caller reads for the answer carries none
    line = _flat(cap.err)
    assert "price grid unavailable" in line  # why there was no fast half
    assert "Matrix is down" in line  # and why there is no calendar
    # Under the same prefix every other calendar failure carries: this is the shape
    # production takes for a single-airport one-way, so a caller matching on the
    # prefix cannot be asked to know which runner served the query.
    assert "Matrix calendar failed" in line
    assert "req-7" in line  # and the id an outage report quotes survives the prefix


class _DownClient(_PricedClient):
    """Matrix refusing, so the weave exits 1 with only the first paint on record."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        raise MatrixApiError("Matrix is down", kind="unavailable")


def _empty_grid(_search: object) -> dict[str, float]:
    return {}


def _throttled_grid(_search: object) -> dict[str, float]:
    raise GfThrottledError("rate-limited")


def _broken_grid(_search: object) -> dict[str, float]:
    raise RuntimeError("the date-grid blew up")


@pytest.mark.parametrize(
    ("grid", "note"),
    [
        (_throttled_grid, "Google Flights rate-limited"),
        (_broken_grid, "Google Flights date grid failed"),
        (_empty_grid, "awaiting Matrix calendar"),
    ],
    ids=["throttled", "date-grid raised", "empty grid"],
)
def test_every_weave_branch_with_no_grid_to_show_keeps_stdout_empty(
    grid: Any, note: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first paint has five branches and four of them have no document. Each
    says why on stderr, because Matrix can still fail behind any of them — which is
    exit 1, and exit 1 is a promise that the stream a caller reads for the answer
    holds nothing to mistake for one. Real renderers, since a spy is exactly what
    would hide a status line landing in the answer's stream."""
    monkeypatch.setattr(cli, "MatrixClient", _DownClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", grid)
    with pytest.raises(typer.Exit) as excinfo:
        _run_enriched()
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert note in line  # why there is no fast half
    assert "Matrix is down" in line  # and why there is no calendar behind it


def test_the_paint_says_matrix_is_coming_without_saying_it_on_stdout(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fifth branch, the one that DOES have a document: the grid goes to stdout
    and the line promising the Matrix refinement does not. It is status about an
    answer still in flight, exactly like the four branches with no grid at all, and
    a caller reading stdout for the document gets the document."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    _run_enriched()
    cap = capsys.readouterr()
    assert "refining" in _flat(cap.err)
    assert "refining" not in cap.out


def test_the_fanout_provenance_note_is_beside_the_grid_not_in_it(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Why the grid was assembled from several queries qualifies the answer rather
    than being part of it — the same claim as the coverage note it sits beside, and
    on the same stream. It is also written before the answer is delivered, so on
    stdout a delivery that then failed would leave it there alone under exit 1,
    where a caller reading the stream finds a document that is only provenance."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, destination="VIE,PAR")
    cap = capsys.readouterr()
    assert "separately and merged" in _flat(cap.err)
    assert "separately and merged" not in _flat(cap.out)


def test_the_fanout_provenance_note_counts_the_sub_queries_it_actually_ran(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The split is one sub-query per (origin, destination group), origins
    outermost, so two origins asking for one destination is two queries and still
    one destination. Counting them as destinations tells a reader the fan-out
    covered ground it never went near, and this is the only count they see."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, origin="JFK,BOS", destination="LHR")
    note = _flat(capsys.readouterr().err)
    assert "Queried 2 origin/destination groups separately" in note
    assert "2 destinations" not in note  # one destination was asked for, not two


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
    # On stderr: there is no grid to show, so this is status about an answer that
    # has not arrived, and Matrix may still fail behind it — which is exit 1 with
    # a stream a caller reads for the document.
    note = _flat(cap.err)
    assert note.count("price grid unavailable") == 1  # said once, not per chunk
    # The note is printed while Matrix is still in flight, so it may only promise
    # to wait — Matrix can still fail after it (and today, on one-way, it does).
    assert "awaiting Matrix calendar" in note
    assert "Showing the Matrix calendar" not in note
    assert "date grid failed" not in note  # a standing gate is not a failure
    assert cap.out == ""  # and nothing that is not the answer went to the answer


def test_calendar_enriched_city_code_gets_the_gate_note_not_an_attribute_error(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """NYC is a place a user can ask for and fli's `Airport` enum has no member
    for. Real `date_grid` here: the gate has to answer before `_grid_filters` does,
    or the weave's broad except turns a standing gate into `date grid failed: type
    object 'Airport' has no attribute 'NYC'` — and Matrix still prices it either
    way, so the note is the whole difference the user sees."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    _run_enriched(origin="NYC")
    cap = capsys.readouterr()
    assert calls["calendar"] == 1  # Matrix priced the window regardless
    assert calls["grid"] == 0
    note = _flat(cap.err)  # status, not a document; see the gate note above
    assert note.count("price grid unavailable") == 1
    assert "date grid failed" not in note
    assert "no attribute" not in note
    assert cap.out == ""


def _no_browser(*_a: object, **_k: object) -> object:
    """A browser session nobody may ask for: a refusal comes before any page load."""
    raise AssertionError("a refused calendar reached the browser")


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
        "gf_transport": "http",
        "gf_headed": False,
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
@pytest.mark.parametrize("transport", ["http", "browser"])
def test_calendar_fast_unresolvable_origin_is_refused_by_name(
    origin: str, transport: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # Neither a city code (NYC — a real place fli's `Airport` enum has no member
    # for) nor a bad IATA can be resolved into an fli filter. The gate names the
    # code before anything builds one, so what the user reads is the reason and
    # not `type object 'Airport' has no attribute 'NYC'` dressed up as a
    # transport failure — on either transport, and before any page loads.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_browser.session", _no_browser)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(origin=origin, gf_transport=transport)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1  # no grid is still no grid
    assert calls["grid"] == 0
    err_out = _flat(cap.err)
    assert "no attribute" not in err_out  # not an AttributeError in prose
    assert "date grid failed" not in err_out
    assert f"a city code rather than an airport ({origin})" in err_out
    assert "Run without --fast for Matrix" in err_out
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
    assert "date grid failed" in err_out  # the reason
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


@pytest.mark.parametrize("transport", ["http", "browser"])
def test_fast_refuses_a_trip_length_range(
    transport: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page's graph prices ONE trip length, so the default `5-7` has no single
    question to ask it, on either transport."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_browser.session", _no_browser)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(one_way=False, gf_transport=transport)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    # The refusal is a diagnostic, so it goes to stderr on EVERY shape — one of the
    # shapes it refuses is `--format json`, and the stream must not depend on which.
    assert "--fast applies only to" in _flat(cap.err)
    assert "a trip-length range (5-7 nights)" in _flat(cap.err)
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work


@pytest.mark.parametrize(
    "overrides", [{"fmt": "json"}, {"one_way": False, "duration": "7"}], ids=["json", "rt"]
)
def test_fast_over_http_names_the_browser_for_json_and_round_trip(
    overrides: dict[str, Any], monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both shapes are the page grid's. Over http there is nothing to serve them,
    so the refusal is the gate's note with the transport that does."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(**overrides)
    cap = capsys.readouterr()
    assert excinfo.value.exit_code == 1
    err_out = _flat(cap.err)
    assert err_out.count("price grid unavailable") == 1
    assert "--gf-transport browser" in err_out
    assert "drop --fast for Matrix" in err_out
    # Under a JSON request stdout carries a JSON document or nothing — never prose,
    # or a caller piping to `jq` gets a parse error instead of an empty result.
    assert cap.out == ""
    assert calls["calendar"] == 0  # refused before any Matrix work
    assert calls["grid"] == 0


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


# ──────────── a calendar that cannot reach Matrix says so and exits 1 ───────
# `MatrixClient(...)` is built inside the coroutine both calendar paths run, so an
# unresolvable API key, a refused connection or a DNS failure raises where neither
# `execute`'s `MatrixApiError` arm nor the weave's `_matrix` task can see it.
# Unguarded that ends the command as a rich traceback with stdout empty, which is
# the one outcome a caller reading exit codes and streams cannot act on. Every
# other Matrix path answers with one typed line and exit 1; so do these.

_ORDERLY_EXIT_CODE = 3  # neither 0 nor 1, so "kept its own code" is checkable


class _UnbuildableClient(_PricedClient):
    """A client whose CONSTRUCTOR fails, which is what an unresolvable key does."""

    def __init__(self, **_kwargs: object) -> None:
        raise ApiKeyResolutionError("could not resolve the Matrix API key")


def test_the_calendar_weave_types_a_client_that_cannot_be_built(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _UnbuildableClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _run_enriched()
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert calls == {"grid": 0, "calendar": 0}  # it failed before either paint
    assert cap.out == ""  # so nothing on stdout can read as an answer
    line = _flat(cap.err)
    assert line.count("Matrix calendar failed:") == 1  # one line, said once
    assert "could not resolve the Matrix API key" in line  # naming the cause


def test_the_matrix_calendar_types_a_client_that_cannot_be_built(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same failure on the path that has no Google half to fall back to:
    round trip, multi-airport, Tier-2/3 routing and `--format json` all land here.
    """
    monkeypatch.setattr(cli, "MatrixClient", _UnbuildableClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert line.count("Matrix calendar failed:") == 1
    assert "could not resolve the Matrix API key" in line


class _FanoutFailClient(_PricedClient):
    """Prices every destination but the ones named, which Matrix refuses."""

    fails: ClassVar[frozenset[str]] = frozenset()

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        dest = next(iter(search.legs[0].destinations), "?")
        if dest in type(self).fails:
            # kind and request_id, because what a refusal keeps of a Matrix error
            # is the half of it the reader quotes when they report the outage.
            raise MatrixApiError(f"{dest} UNAVAILABLE", kind="internal", request_id=f"req-{dest}")
        return await super().execute(search, cache=cache)


def test_a_calendar_fanout_that_loses_every_sub_query_refuses(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merged grid with no priced day renders as "Calendar empty", which tells
    the reader Matrix priced the window and found nothing. When every sub-query
    failed it priced nothing at all, so exit 0 under that sentence is a wrong
    answer with nothing on stderr and no exit code to tell it from a right one."""
    _FanoutFailClient.fails = frozenset({"VIE", "PAR"})
    monkeypatch.setattr(cli, "MatrixClient", _FanoutFailClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""  # the brownout advice never gets printed as the answer
    line = _flat(cap.err)
    assert "all 2 sub-queries failed" in line  # how many of how many
    # And every destination that dropped, because two sub-queries refused for two
    # reasons is two things to fix: a report naming one leaves the count as the
    # only true half of it.
    assert "VIE UNAVAILABLE" in line
    assert "PAR UNAVAILABLE" in line
    # Lowest-index first, so the same outage reads the same way on every run:
    # sub-queries are indexed in destination order and finish in whatever order the
    # network gives them.
    assert line.index("VIE UNAVAILABLE") < line.index("PAR UNAVAILABLE")
    # Each arrives through the shared Matrix reporter, so the kind and the request
    # id survive the fan-out the way they do on a single query — for every cause,
    # not just whichever one led.
    assert "internal" in line
    assert "req-VIE" in line
    assert "req-PAR" in line


def test_a_calendar_fanout_that_loses_some_sub_queries_says_how_many(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """What answered is still worth reading, so this is a note beside the grid
    rather than a refusal — but a destination Matrix refused looks exactly like a
    destination with no fares, and the count is what tells them apart."""
    _FanoutFailClient.fails = frozenset({"VIE"})
    monkeypatch.setattr(cli, "MatrixClient", _FanoutFailClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n == 2  # what answered was merged and is still rendered
    assert not is_empty_calendar(res)
    assert "1 of 2 sub-queries failed" in _flat(capsys.readouterr().err)


def test_a_partly_lost_fanout_names_each_lost_group_with_its_cause(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The count says a group is missing; the cause says what to do about it. A
    brownout is waited out and a refused query is rewritten, and a note that
    names the route alone leaves the reader unable to tell which one this was."""
    _FanoutFailClient.fails = frozenset({"VIE"})
    monkeypatch.setattr(cli, "MatrixClient", _FanoutFailClient)
    cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    cap = capsys.readouterr()
    line = _flat(cap.err)
    assert "MIA→VIE" in line
    assert line.count("VIE UNAVAILABLE") == 1  # named, and named once
    assert "internal" in line
    assert "req-VIE" in line
    assert cap.out == ""


class _FanoutEmptyRestClient(_PricedClient):
    """Refuses the destinations named, and prices no day at all for the rest."""

    fails: ClassVar[frozenset[str]] = frozenset()

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = cache
        dest = next(iter(search.legs[0].destinations), "?")
        if dest in type(self).fails:
            raise MatrixApiError(f"{dest} UNAVAILABLE", kind="internal")
        return CalendarResult.from_api(_EMPTY)


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_a_partly_lost_fanout_that_priced_nothing_refuses_in_both_arms(
    fmt: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A destination dropped and the rest priced no day. Rendered, that is
    "Calendar empty" and the advice to retry a brownout; under `--format json` it
    is `solutionCount: 0`. Both documents say Matrix priced this window and found
    nothing, which is the one claim a destination that never answered cannot
    support — and the exit code says it a second time, to the caller least able to
    read the note on stderr. The count alone was already there, so what this pins
    is the two channels automation reads: exit 1, and no document at all."""
    _FanoutEmptyRestClient.fails = frozenset({"VIE"})
    monkeypatch.setattr(cli, "MatrixClient", _FanoutEmptyRestClient)
    calls = _spy_renderers(monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=False, fmt=fmt, destination="VIE,PAR")
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""  # no grid, no brownout advice, and no JSON document
    assert calls["calendar"] == 0
    line = _flat(cap.err)
    assert "1 of 2 sub-queries failed" in line  # how many of how many
    assert "VIE UNAVAILABLE" in line  # and the first cause, not just the count
    assert "grid below" not in line  # the note that says that never prints here


class _ExitingClient(_PricedClient):
    """Raises an orderly exit from inside a Matrix task."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        raise typer.Exit(_ORDERLY_EXIT_CODE)


def _exiting_grid(_search: object) -> dict[str, float]:
    raise typer.Exit(_ORDERLY_EXIT_CODE)


def _exit_from_a_fanout_sub_query(monkeypatch: Any, calls: dict[str, int]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _ExitingClient)
    cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )


def _exit_from_the_weave_matrix_task(monkeypatch: Any, calls: dict[str, int]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _ExitingClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch, calls)
    _run_enriched()


def _exit_from_the_weave_date_grid(monkeypatch: Any, calls: dict[str, int]) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _exiting_grid)
    _spy_renderers(monkeypatch, calls)
    _run_enriched()


class _ExitBesideFailureClient(_PricedClient):
    """One Matrix call ending as an orderly exit AND a failure, in one task group.

    Two children that raise without awaiting: anyio runs both before the first
    cancels the group, so it collects the pair — the shape the guard outside the
    group has to answer, and the one where honouring the exit decides what happens
    to the failure next to it."""

    exit_code: ClassVar[int] = _ORDERLY_EXIT_CODE

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)

        async def _stop() -> None:
            raise typer.Exit(type(self).exit_code)

        async def _fail() -> None:
            raise MatrixApiError("Matrix is down", kind="unavailable")

        async with anyio.create_task_group() as tg:
            tg.start_soon(_stop)
            tg.start_soon(_fail)
        raise AssertionError  # unreachable: the group above always raises


def _exit_beside_a_failure(monkeypatch: Any, code: int) -> None:
    _ExitBesideFailureClient.exit_code = code
    monkeypatch.setattr(cli, "MatrixClient", _ExitBesideFailureClient)
    cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )


def _exit_zero_beside_a_failure(monkeypatch: Any, calls: dict[str, int]) -> None:
    # Exit(0) is the one that reads as success on every channel a caller has.
    _exit_beside_a_failure(monkeypatch, 0)


def _exit_three_beside_a_failure(monkeypatch: Any, calls: dict[str, int]) -> None:
    _exit_beside_a_failure(monkeypatch, _ORDERLY_EXIT_CODE)


def _exit_from_the_fast_date_grid(monkeypatch: Any, calls: dict[str, int]) -> None:
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _exiting_grid)
    _spy_renderers(monkeypatch, calls)
    cli._run_fast_calendar_grid(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _oneway_cal(),
        origins=("JFK",),
        dests=("LHR",),
        sd=W.start,
        ed=W.end,
        matrix_url=False,
        google_url=False,
    )


@pytest.mark.parametrize(
    ("drive", "code", "beside", "delivered"),
    [
        (_exit_from_a_fanout_sub_query, _ORDERLY_EXIT_CODE, None, False),
        (_exit_from_the_weave_matrix_task, _ORDERLY_EXIT_CODE, None, True),
        (_exit_from_the_weave_date_grid, _ORDERLY_EXIT_CODE, None, False),
        (_exit_from_the_fast_date_grid, _ORDERLY_EXIT_CODE, None, False),
        (_exit_zero_beside_a_failure, 0, "Matrix is down", False),
        (_exit_three_beside_a_failure, _ORDERLY_EXIT_CODE, "Matrix is down", False),
    ],
    ids=[
        "fanout sub-query",
        "weave matrix task",
        "weave date-grid",
        "fast date-grid",
        "exit 0 beside a failure",
        "exit 3 beside a failure",
    ],
)
def test_an_orderly_exit_inside_a_calendar_guard_keeps_its_own_code(
    drive: Any,
    code: int,
    beside: str | None,
    delivered: bool,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`typer.Exit` and `typer.Abort` subclass `RuntimeError` on the installed
    click, and a task group wraps everything that leaves it — the host body's own
    exception included. Between them, every broad arm on these paths would catch an
    orderly exit and answer it with a backend's name: the grid arms as a date-grid
    failure, the two outer guards as a Matrix failure with exit 1.

    An exit keeps its code even where something failed beside it, because a stop is
    the outcome somebody asked for — but the exit ends the command, so nothing
    below would ever mention the failure, and with `Exit(0)` the process reports
    success for a fan-out that half went down. The code is the caller's; stderr is
    where what it cost gets said."""
    calls: dict[str, int] = {"grid": 0, "calendar": 0}
    with pytest.raises(typer.Exit) as excinfo:
        drive(monkeypatch, calls)
    assert excinfo.value.exit_code == code  # its own code, not 1
    cap = capsys.readouterr()
    if delivered:
        # The one arm where the grid reached the reader BEFORE the stop: the paint
        # is synchronous and the thread hop ahead of it is the last place a cancel
        # can land, so what was given stays given. The renderer having RUN is the
        # claim — a status line saying the grid is there is not the grid.
        assert calls["grid"] == 1
        # And on stderr, like every other line the weave writes while the answer is
        # still in flight.
        assert "refining with Matrix" in _flat(cap.err)
    # A stop is not an answer, and leaves nothing on stdout for a caller to read
    # as one whatever the exit code says.
    assert cap.out == ""
    line = _flat(cap.err)
    if beside is None:
        assert "failed" not in line  # and no backend blamed
    else:
        assert beside in line  # the failure the exit would otherwise bury
        assert "1 failure beside a deliberate stop" in line  # and how many there were
        # Named by the shared Matrix reporter, so the kind survives a failure that
        # was only ever mentioned because something else ended the command.
        assert "unavailable" in line


class _TwoFailureClient(_PricedClient):
    """Two unrelated real failures in one task group, and no exit anywhere."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)

        async def _dns() -> None:
            raise OSError("nodename nor servname provided")

        async def _matrix() -> None:
            raise MatrixApiError("Matrix is down", kind="unavailable")

        async with anyio.create_task_group() as tg:
            tg.start_soon(_dns)
            tg.start_soon(_matrix)
        raise AssertionError  # unreachable: the group above always raises


class _OneFailureClient(_PricedClient):
    """A single failure inside a task group, which anyio still wraps in a group."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)

        async def _fail() -> None:
            raise RuntimeError("the calendar task blew up")

        async with anyio.create_task_group() as tg:
            tg.start_soon(_fail)
        raise AssertionError  # unreachable: the group above always raises


def test_a_lone_failure_is_named_without_the_group_around_it(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A task group wraps whatever leaves it, so the commonest failure of all —
    one thing went wrong — arrives as a group of one. That wrapper is plumbing:
    reported as itself it reads "1 concurrent failures" or the group's own repr,
    where what the reader can act on is the cause inside it."""
    monkeypatch.setattr(cli, "MatrixClient", _OneFailureClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert "Matrix calendar failed: the calendar task blew up" in line
    assert "concurrent failures" not in line  # the count belongs to a group of several
    assert "ExceptionGroup" not in line
    assert "sub-exception" not in line


def test_two_concurrent_calendar_failures_are_each_named(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A group of one is plumbing and unwraps to its member. A group of several
    cannot, and its own `str` is a count — "unhandled errors in a TaskGroup
    (2 sub-exceptions)" names neither cause, so the reader is told only that there
    were two and every message the failures carried is dropped on the floor."""
    monkeypatch.setattr(cli, "MatrixClient", _TwoFailureClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert "2 concurrent failures" in line  # how many
    assert "nodename nor servname" in line  # the transport half
    assert "Matrix is down" in line  # and the Matrix half
    assert "TaskGroup" not in line  # never the plumbing the reader cannot act on
    # In member order, which is task-start order rather than the order they raised.
    # Two things read it: the join above, and the "first cause" a deliberate stop
    # beside a failure names — so the same outage has to report the same way twice.
    assert line.index("nodename nor servname") < line.index("Matrix is down")


def test_a_lone_cancellation_comes_back_inside_the_group_around_it() -> None:
    """A group of one usually unwraps, because the wrapper is plumbing. Not when
    the member is not an `Exception`: both guards' arms are typed to `Exception`,
    so handing them a cancellation out of its group gives them something they are
    written not to catch, and the classifier is the last place that can tell."""

    async def _cancelled_class() -> type[BaseException]:
        # Named by the backend rather than hardcoded: which class a cancel wears
        # is anyio's to choose, and the point is that it is not an `Exception`.
        return anyio.get_cancelled_exc_class()

    group = BaseExceptionGroup("task group", [anyio.run(_cancelled_class)()])
    # The annotation says `Exception`, and a group holding a `BaseException` is not
    # one: the guard above this never passes such a group down, which is exactly why
    # the classifier has to keep saying so rather than assume it.
    assert cli._calendar_cause(cast("Exception", group)) is group  # pyright: ignore[reportPrivateUsage] — the classifier IS the unit


def test_a_group_of_one_names_its_failure_without_counting_it() -> None:
    """The count exists because a group's own `str` gives a reader a number and no
    message. One message needs no number, and "1 concurrent failures" reads as a
    fault in the reporter to the reader least placed to tell it from one."""
    group = BaseExceptionGroup("task group", [RuntimeError("just the one")])
    assert cli._failure_text(group) == "just the one"  # pyright: ignore[reportPrivateUsage] — the formatter IS the unit


def test_a_single_query_calendar_failure_names_the_command_once(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """One query refused is the commonest calendar failure there is, and it prints
    the same prefix as the fan-out and the beside-a-stop line, so a matcher on
    `Matrix calendar failed` catches all three. Once, because the backend's message
    belongs under the prefix rather than in it: folding the two prints together is
    what drops the kind and the request id."""

    class _ErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("Matrix is down", kind="unavailable", request_id="req-7")

    monkeypatch.setattr(cli, "MatrixClient", _ErrClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert "Matrix calendar failed" in line  # the prefix every other shape prints
    assert line.count("Matrix is down") == 1  # and the message once, not twice
    assert "unavailable" in line  # with the kind
    assert "req-7" in line  # and the id


class _ExitBesideTwoFailuresClient(_PricedClient):
    """A deliberate stop and TWO unrelated failures, all in one task group."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)

        async def _stop() -> None:
            raise typer.Exit(0)

        async def _dns() -> None:
            raise OSError("nodename nor servname provided")

        async def _refused() -> None:
            raise MatrixApiError("Matrix is down", kind="unavailable")

        async with anyio.create_task_group() as tg:
            tg.start_soon(_stop)
            tg.start_soon(_dns)
            tg.start_soon(_refused)
        raise AssertionError  # unreachable: the group above always raises


def test_every_failure_beside_a_deliberate_stop_is_named(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The count sentence says how many failures stood beside the stop, and the
    stop ends the command where it is, so nothing downstream will ever mention
    them. Naming one leaves the count as the only true half of that sentence — and
    under `Exit(0)` the process reports success on every channel a caller has, so
    stderr is the only place the rest of it can be said."""
    monkeypatch.setattr(cli, "MatrixClient", _ExitBesideTwoFailuresClient)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert excinfo.value.exit_code == 0  # the stop's own code, not 1
    cap = capsys.readouterr()
    assert cap.out == ""
    line = _flat(cap.err)
    assert "2 failures beside a deliberate stop" in line  # how many there were
    assert "nodename nor servname" in line  # and each of them, not the first alone
    assert "Matrix is down" in line
    assert "unavailable" in line  # the backend error keeping its kind


def test_a_teardown_after_a_calendar_keeps_the_answer_on_the_plain_path(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Matrix priced the window and the client then refused to close. The exit code
    follows what the reader got, as it does on the weave: an answer that arrived
    stands, and the teardown is a line beside it. Discarding it reports a query that
    succeeded as one that never ran, on the two channels automation reads."""
    monkeypatch.setattr(cli, "MatrixClient", _TeardownFailsClient)
    res, n = cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
        _cal(["PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert not is_empty_calendar(res)  # the calendar Matrix priced, not discarded
    assert n == 0
    assert "client teardown blew up" in _flat(capsys.readouterr().err)


class _SubQueryStop(BaseException):
    """A `BaseException` from a sub-query that is not the interpreter's own stop.

    Not `SystemExit`, and the difference is measured rather than stylistic: the
    event loop re-raises a `SystemExit` or a `KeyboardInterrupt` out of the task
    that raised it before the group is ever built, so those two never reach a guard
    round `anyio.run` as a group at all. Every other `BaseException` a child raises
    does, which makes this the shape the arm below exists for."""


class _StoppingSubQueryClient(_PricedClient):
    """One sub-query ending on something the fan-out's own arm cannot catch."""

    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        if next(iter(search.legs[0].destinations), "?") == "VIE":
            raise _SubQueryStop("the sub-query stopped")
        return await super().execute(search, cache=cache)


def test_a_base_exception_inside_a_fanout_is_not_a_group(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fan-out's own arm catches `Exception`, so a `BaseException` from a
    sub-query leaves the task group wrapped in a `BaseExceptionGroup` — which the
    guard below cannot see for the same reason the weave's cannot, and which click
    cannot map to an exit code either. Unwrapped it ends the command as it would
    have with no group round it, rather than as the group's own traceback."""
    monkeypatch.setattr(cli, "MatrixClient", _StoppingSubQueryClient)
    with pytest.raises(_SubQueryStop) as excinfo:
        cli._run_calendar(  # pyright: ignore[reportPrivateUsage] — the runner IS the unit
            _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
        )
    assert str(excinfo.value) == "the sub-query stopped"  # the cause, not the wrapper
    assert "ExceptionGroup" not in _flat(capsys.readouterr().err)


# ──────────── the call that writes the answer is inside the guard ───────────
# `_render_calendar`, `_render_date_grid` and `_emit_urls` are what put the
# document on stdout. A raise in one is a calendar that failed as surely as a
# backend that never answered — a price the table cannot format, a reader that
# closed the pipe — and on the weave the grid has already been delivered, so it is
# a calendar the reader is holding reported as a crash.


def _exploding_calendar_renderer(*_a: object, **_k: object) -> None:
    raise RuntimeError("the calendar renderer blew up")


def _exploding_url_emitter(*_a: object, **_k: object) -> None:
    raise RuntimeError("the URL emitter blew up")


def _weave_renderer_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_calendar", _exploding_calendar_renderer)
    _run_enriched()


def _weave_emitter_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_emit_urls", _exploding_url_emitter)
    _run_enriched()


def _plain_renderer_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_calendar", _exploding_calendar_renderer)
    _calendar_fast(fast=False, one_way=False)


def _fanout_emitter_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_emit_urls", _exploding_url_emitter)
    _calendar_fast(fast=False, destination="VIE,PAR")


def _fast_renderer_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)  # never dial Matrix from a test
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _exploding_grid_renderer)
    _calendar_fast(fast=True, one_way=True)


@pytest.mark.parametrize(
    ("drive", "message", "prefix"),
    [
        (_weave_renderer_raises, "the calendar renderer blew up", "Matrix calendar failed"),
        (_weave_emitter_raises, "the URL emitter blew up", "Matrix calendar failed"),
        (_plain_renderer_raises, "the calendar renderer blew up", "Matrix calendar failed"),
        (_fanout_emitter_raises, "the URL emitter blew up", "Matrix calendar failed"),
        (
            _fast_renderer_raises,
            "the date-grid renderer blew up",
            "Google Flights date grid failed",
        ),
    ],
    ids=["weave render", "weave emit", "plain render", "fan-out emit", "fast render"],
)
def test_a_raise_writing_the_answer_is_a_typed_line(
    drive: Any, message: str, prefix: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_deliver_calendar` guards three call sites, and each writes a document out
    of a renderer and a URL emitter: six pairs, five of them arms here — weave x
    render, weave x emit, tail x render, tail x emit, and the `--fast` grid render.
    The sixth, `--fast` x emit, has no arm of its own: it shares its guard, its
    `_write_answer` and its backend name with the `--fast` render arm, and while the
    date-grid RPC gate stands neither is reachable outside a test. Each ends as the
    typed line and exit 1 that every other cause on these paths ends as.

    The prefix names the backend that built the document, not the command: a
    `--fast` run has no Matrix behind it at all, and reporting its renderer as a
    Matrix failure sends a reader after an outage nobody asked about. The `--fast`
    arm is the one that reaches a site production cannot while the date-grid RPC
    gate stands, which is why it is driven with the grid stubbed."""
    with pytest.raises(typer.Exit) as excinfo:
        drive(monkeypatch)
    assert excinfo.value.exit_code == 1
    line = _flat(capsys.readouterr().err)
    assert prefix in line
    assert message in line


def _matrix_error_grid_renderer(*_a: object, **_k: object) -> None:
    raise MatrixApiError("the date-grid renderer blew up", kind="internal")


def test_a_backend_error_writing_the_answer_names_the_backend_it_was_handed(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The no-`lost` branch of the same reporter — the one a single query reaches,
    where the backend name stands alone under a full stop. `MatrixApiError` is the
    class every backend error on these paths wears, so the name has to come from
    the guard the delivery ran under: under `--fast` there is no Matrix behind the
    document at all, and naming one sends a reader after an outage nobody had."""
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)  # never dial Matrix
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    _spy_renderers(monkeypatch)
    monkeypatch.setattr(cli, "_render_date_grid", _matrix_error_grid_renderer)
    with pytest.raises(typer.Exit) as excinfo:
        _calendar_fast(fast=True, one_way=True)
    assert excinfo.value.exit_code == 1
    line = _flat(capsys.readouterr().err)
    assert "Google Flights date grid failed." in line  # the backend that wrote it
    assert "Matrix calendar" not in line  # and not the one the class is named for
    assert "the date-grid renderer blew up" in line  # with the cause under it


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


@pytest.mark.parametrize("destination", ["LHR", "VIE,PAR,FCO,MAD"], ids=["single", "fan-out"])
def test_calendar_one_way_note_survives_json_output(
    destination: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--format json` is exactly when a dropped flag is least visible, and stderr is
    # where the remark can go without putting prose in front of `jq`. Both trip
    # shapes, because only the fan-out enters the multi branch of `_run_calendar`
    # and writes its document out of a merged `res.raw`: a `console` write anywhere
    # in that branch reaches `jq` on an arm the single-query drive never runs. The
    # two arms write the same stderr, so the branch is what separates them.
    monkeypatch.setattr(cli, "MatrixClient", _PricedClient)
    _spy_renderers(monkeypatch)
    _calendar_fast(fast=False, fmt="json", duration="9-3", destination=destination)
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
        # `int()` swallows every Unicode space, so `5-\xa07` would reach it as a
        # range while reading as one token. U+001C..1F are `isspace()`-true and
        # `int()` rejects them, so a bound is matched against digits rather than
        # handed to `int()` to be lenient about which of the two it got.
        ("5-\xa07", "use nights as"),
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


def test_a_backend_error_behind_a_painted_grid_reports_under_the_same_prefix(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The contrast with the transport failure above: same weave, same painted
    grid, and a `MatrixApiError` instead — the class that carries a kind and a
    request id. Those belong BELOW the prefix, not instead of it, so the line a
    caller matches on is the same one every other calendar failure prints and the
    fields an outage report quotes follow it."""

    class _TypedErrClient(_PricedClient):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            _ = (search, cache)
            raise MatrixApiError("Matrix is down", kind="unavailable", request_id="req-11")

    monkeypatch.setattr(cli, "MatrixClient", _TypedErrClient)
    monkeypatch.setattr("flight_cli._gf_dategrid.date_grid", _fake_grid)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # the grid painted, so the Matrix half only reports
    line = _flat(capsys.readouterr().err)
    assert calls["grid"] == 1
    assert line.index("Matrix calendar failed") < line.index("unavailable")
    assert line.index("Matrix calendar failed") < line.index("request_id")
    assert "req-11" in line
    assert "Matrix is down" in line


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
# The Google Flights table shows one flight number twice: in the legs column
# and again in the legroom sub-line. Both are cells, so both are sinks.
_BOTH_COLUMNS = 2


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
# Which slice of the trip carries it. Outbound and return come out of one
# formatter but are bound to separate names in each renderer, so they are two
# claims — and a row hostile in both stays green when either wrap goes, because
# the other cell still carries the payload into the same line.
_SLICE_SLOTS = ("outbound", "return")


def _hostile_slice(field: str, payload: str) -> dict[str, Any]:
    """One itinerary slice carrying `payload` in `field` and benign in the rest.

    The timestamps are driven as a PAIR and deliberately unreadable as dates:
    `_fmt_slice_times` formats two datetimes when it can parse them and falls back
    to Matrix's own two strings when it cannot, and only the fallback is remote.
    """
    return {
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


def _slice_solution(
    field: str, payload: str, *, slot: str = "outbound", carrier: str = "BA"
) -> dict[str, Any]:
    """A round-trip solution whose `slot` slice carries `payload` in `field`.

    TWO slices, because the return cell exists only when there is one: given a
    single slice both itinerary renderers take the literal '—' down that branch, so
    the wrap on the return cell is a claim no payload ever reaches."""
    return {
        "ext": {"price": "USD421.00"},
        "itinerary": {
            "carriers": [{"code": carrier}],
            "slices": [
                _hostile_slice(field if slot == "outbound" else "", payload),
                _hostile_slice(field if slot == "return" else "", payload),
            ],
        },
    }


def _slice_result(
    field: str, payload: str, *, slot: str = "outbound", carrier: str = "BA"
) -> SearchResult:
    """A search response holding one such solution and nothing else hostile."""
    return SearchResult.from_api(
        {
            "solutionCount": 1,
            "currencyNotice": {"ext": {"price": "USD421.00"}},
            "solutionList": {
                "solutions": [_slice_solution(field, payload, slot=slot, carrier=carrier)]
            },
        }
    )


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("slot", _SLICE_SLOTS)
@pytest.mark.parametrize("field", _SLICE_FIELDS)
def test_search_itinerary_cells_survive_a_hostile_matrix_field(
    field: str, slot: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The itinerary cells are the half of the table the summary cases never
    reached: airport codes, connection codes, flight numbers, the raw-ISO timestamp
    fallback and the seat-type name all land in a Rich cell, which parses markup
    exactly as the title above it does. Both columns, because they are two cells
    and two claims."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_search(_slice_result(field, payload, slot=slot))  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":  # the ESC is dropped, so only its letters remain
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("slot", _SLICE_SLOTS)
@pytest.mark.parametrize("field", _SLICE_FIELDS)
def test_multi_cabin_itinerary_cells_survive_a_hostile_matrix_field(
    field: str, slot: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The multi-cabin table builds its cells from the same formatter, and the two
    renderers have drifted apart on a shared field before, so it is pinned on its
    own rather than through the one above."""
    row = MultiCabinRow(
        itinerary=_slice_result(field, payload, slot=slot).solutions[0],
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


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("field", ["carrier", "price"])
def test_multi_cabin_rows_survive_a_hostile_carrier_and_price(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The carrier column and the per-cabin price columns are the two the itinerary
    arm above cannot reach: its fixture writes one carrier code and hands the row
    one price, and both are this file's own strings. Matrix chooses both on a real
    response, and the single-cabin table's carrier cell is pinned through a path
    the multi-cabin one does not use."""
    row = MultiCabinRow(
        itinerary=_slice_result("", "", carrier=payload if field == "carrier" else "BA").solutions[
            0
        ],
        prices={Cabin.COACH: payload if field == "price" else "USD421.00"},
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
    # `force_terminal` alone is not enough to make this assertion about OUR code:
    # `TERM=dumb` leaves `color_system=None` and `NO_COLOR` sets `no_color`, and
    # under either one rich emits no SGR whatever the markup said. Both are
    # ordinary CI environments, so both are pinned here rather than inherited.
    monkeypatch.setattr(
        cli,
        "console",
        Console(
            file=buffer, force_terminal=True, color_system="truecolor", no_color=False, width=400
        ),
    )
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

    def _search_with_ids(
        search: Any,
        top_n: int = 5,
        transport: Any = None,
        currency: str = "USD",
        keep: Any = None,
        checks: str = "the routing",
    ) -> list[Any]:
        _ = top_n, transport, currency, keep, checks
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


def _hostile_config(payload: str, monkeypatch: Any, *, in_path: bool) -> None:
    """Point `_config` at a path or an error carrying the payload, not both."""
    where = payload if in_path else "cfg"
    text = "bad toml at line 1" if in_path else f"bad toml at line 1: {payload}"

    def _boom() -> dict[str, Any]:
        raise ValueError(text)

    monkeypatch.setattr("flight_cli._config.load", _boom)
    monkeypatch.setattr(
        "flight_cli._config.config_path", lambda: Path(f"/nonexistent/{where}/config.toml")
    )


@pytest.mark.parametrize("payload", ["[/x]", "[bold]x", "a\x1b[2Jb"])
@pytest.mark.parametrize("arm", ["the option token", "the config path", "the config error"])
def test_the_provider_diagnostics_show_the_value_they_are_about(
    arm: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three remote-or-typed values reach this one console: the token
    `--provider-opt` could not parse, the path the config was read from — which
    `FLIGHT_CLI_CONFIG_DIR` moves, so a hardcoded default names a file the user
    may not have — and whatever tomllib said about its contents. Each sentence
    exists to name the thing that failed, so an unbalanced tag answers a mistyped
    flag with a MarkupError and a well-formed one eats the token.

    The expectation is per arm because the two halves of the config sentence
    sanitize differently on purpose, as the convention says they should: a value
    the user typed goes through `_quote`, which reprs a control character into an
    escape sequence, and text from a library goes through `_safe_text`, which
    drops it."""
    if arm == "the option token":
        provider_opt = (payload,)
    else:
        provider_opt = ()
        _hostile_config(payload, monkeypatch, in_path=arm == "the config path")
    with pytest.raises(typer.Exit) as excinfo:
        cli._resolve_providers(  # pyright: ignore[reportPrivateUsage] — the resolver IS the unit
            providers=None, cash_only=False, awards_only=False, provider_opt=provider_opt
        )
    assert excinfo.value.exit_code == 2  # a typed usage error, not a traceback
    message = _flat(_SGR.sub("", capsys.readouterr().err))
    for driver in _DRIVERS:
        assert driver not in message, f"{driver!r} reached the console"
    if arm == "the option token":
        assert "missing '='" in message  # and what was wrong with it
    quoted = arm != "the config error"  # `_quote` reprs, `_safe_text` drops
    assert payload.replace("\x1b", "\\x1b" if quoted else "") in message


def test_the_provider_opt_help_names_the_file_this_process_reads() -> None:
    """The help says WHERE to put the option it is showing a flag for, and
    `FLIGHT_CLI_CONFIG_DIR` moves that file — a hardcoded default names a path the
    user may not have. Same defect, same fix, as the diagnostic one function over."""
    assert str(_config.config_path()) in (cli._PROVIDER_OPT.help or "")  # pyright: ignore[reportPrivateUsage] — the option IS the unit


def _cli_module_with_config_dir(monkeypatch: Any, config_dir: str) -> ModuleType:
    """A second `cli` module object, imported with the config directory set.

    The help string interpolates the config path while the module is IMPORTED, so
    an environment patched after that changes nothing — the string a real
    `flight search --help` renders is the one built at import, and a fresh module
    object is what puts a hostile directory name into it without a subprocess. The
    name sits under `flight_cli.` so the module's relative imports resolve, and it
    is never registered in `sys.modules`, so the real `cli` is untouched."""
    monkeypatch.setenv(_config.CONFIG_DIR_ENV, config_dir)
    monkeypatch.setenv("COLUMNS", "500")  # wide enough that rich wraps no path
    spec = importlib.util.spec_from_file_location("flight_cli._cli_help_probe", cli.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("hostile", ["[/x]", "\x1b[2J"], ids=["an unmatched closing tag", "an ESC"])
def test_search_help_survives_a_hostile_config_directory(
    hostile: str, monkeypatch: Any, tmp_path: Path
) -> None:
    """A help string is a markup sink like any other: the app sets
    `rich_markup_mode="rich"`, so Typer renders `help=` through the same parser
    `console.print` uses, and the config path in it is whatever the environment
    says. An unmatched `[/x]` in the directory name aborted `search --help` with a
    MarkupError; an ESC took Typer's from-ANSI branch, which drops the path the
    sentence exists to give — which is why the wrapper here is `_safe_text` and not
    `escape`, since `escape` neutralises the first and leaves the second."""
    module = _cli_module_with_config_dir(monkeypatch, str(tmp_path / hostile))
    result = CliRunner().invoke(module.app, ["search", "--help"])
    assert result.exception is None  # a MarkupError arrives as one of these
    assert result.exit_code == 0
    shown = _flat(result.output)
    # The path shown is the configured one minus its control characters, which is
    # what `_safe_text` promises and `escape` does not: an ESC left in place takes
    # the from-ANSI branch, which eats the sequence around it and names a
    # directory nobody configured.
    assert str(_config.config_path()).replace("\x1b", "") in shown
    assert "[providers.<name>]" in shown


def test_the_provider_opt_help_shows_the_config_section_it_names(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The half that needs no hostile input at all: `[providers.<name>]` is a
    well-formed style tag, so unescaped the parser ate it and the rendered help
    ended "Overrides …/config.toml ." — pointing at a file and naming no section
    in it, on every machine."""
    module = _cli_module_with_config_dir(monkeypatch, str(tmp_path))
    result = CliRunner().invoke(module.app, ["search", "--help"])
    assert result.exit_code == 0
    # `config_path` reads the same environment the fresh module was imported under.
    assert f"{_config.config_path()} [providers.<name>]." in _flat(result.output)


def _fake_gf(
    flight_number: str,
    *,
    airline: str = "UA",
    price: object = 421.0,
    currency: object = "USD",
    stops: object = 0,
    duration: object = 185,
    pitch_inches: object = 31,
    legroom_class: object = "BELOW",
    marketing_flights: object = (),  # `object`, so a `**{field: payload}` arm still checks
) -> SimpleNamespace:
    """An fli-shaped Google Flights result: every field the table and the legroom
    line read, each one overridable and none of them silently — a keyword this
    does not take is a TypeError, where a `**kwargs` would swallow the typo and
    leave the arm testing the default. A renderer gets a hostile arm per field it
    interpolates, which is why the columns beside the flight number are here."""
    leg = SimpleNamespace(airline=SimpleNamespace(name=airline), flight_number=flight_number)
    amenities = SimpleNamespace(
        cabin="ECONOMY",
        pitch_inches=pitch_inches,
        legroom_class=legroom_class,
        wifi=None,
        power=None,
        video=None,
        marketing_flights=marketing_flights,
    )
    flight = SimpleNamespace(
        legs=[leg], price=price, currency=currency, stops=stops, duration=duration
    )
    return SimpleNamespace(flight=flight, amenities=[amenities])


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
def test_gflight_table_survives_a_hostile_flight_number(
    payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Google Flights table builds the same flight number into two columns
    through two different formatters, and only one of them wrapped it — so the
    legs column and the legroom column disagreed about the same value, and a `[/x]`
    in it lost a search that had succeeded. This renderer has its own hostile-field
    case because the Matrix matrix never drives it."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(
            file=buffer, force_terminal=True, color_system="truecolor", no_color=False, width=400
        ),
    )
    cli._render_gflight_table(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [_fake_gf(f"{payload}117")],
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        top_n=5,
    )
    written = buffer.getvalue()
    probe = _flat(_SGR.sub("", written))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":
        assert probe.count(f"{payload}117") == _BOTH_COLUMNS  # legs AND legroom
    # `_fmt_gflight_legroom` writes a real `[red]` on a below-average pitch, so the
    # wrap has to be on the leaf: one around the finished cell shows the tag.
    assert re.search(r"\x1b\[[0-9;]*31[;m]", written), "the legroom colour was escaped away"


# The rest of what the Google Flights table interpolates. Two reach a cell as text
# and must arrive escaped; two reach one under a numeric spec or through integer
# arithmetic, where the guard reads the spec as a PROOF that no string can be here
# rather than a promise about the name — so for those the pin is that a string
# raises before anything is printed.
_GF_TEXT_FIELDS = ("currency", "stops")
_GF_NUMERIC_FIELDS = ("price", "duration")
# The legroom cell's own two, read off a duck-typed Google Flights object one
# level further down: `_fmt_gflight_legroom` composes them into the string the
# table then prints, so they reach a cell as text with nothing between them and
# the parser but the wrap inside that formatter.
_GF_AMENITY_FIELDS = ("pitch_inches", "legroom_class")


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("field", _GF_TEXT_FIELDS)
def test_gflight_table_survives_a_hostile_result_field(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The currency prefixes the price cell and the stop count is a cell of its own.
    Both are Google Flights' strings, both land in a Rich table, and neither had an
    arm of its own — which is what lets an allowlisted local beside them be rebound
    to one without any test noticing."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_gflight_table(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [_fake_gf("UA117", **{field: payload})],
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        top_n=5,
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":  # the ESC is dropped, so only its letters remain
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
@pytest.mark.parametrize("field", _GF_AMENITY_FIELDS)
def test_gflight_table_survives_a_hostile_amenity_field(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pitch and the seat-type word are Google Flights' own values, and the
    legroom cell is the one place they are interpolated. That cell also carries a
    real `[red]` of ours, so the wrap has to be on each leaf inside the formatter
    rather than around the finished string — which is exactly the arrangement that
    goes silent when one leaf loses its wrapper. Every payload here falls outside
    the three seat-type words the module knows, so the branch printing an unknown
    one is entered rather than skipped."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_gflight_table(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [_fake_gf("UA117", **{field: payload})],
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        top_n=5,
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":  # the ESC is dropped, so only its letters remain
        assert payload in probe, f"{field} was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("field", _GF_NUMERIC_FIELDS)
def test_a_gflight_number_column_refuses_a_string(field: str, monkeypatch: Any) -> None:
    """`f"{fr.price:.2f}"` and `fr.duration // 60` are why these two columns need no
    wrapper: a string cannot survive either, so a markup payload in one raises here
    instead of reaching the console. That is the claim the guard makes about a
    numeric presentation type, driven through the renderer that relies on it."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    with pytest.raises((TypeError, ValueError)):
        cli._render_gflight_table(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
            [_fake_gf("UA117", **{field: "[/x]"})],
            legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
            top_n=5,
        )


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
def test_the_multi_cabin_title_survives_a_hostile_currency(
    payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The multi-cabin title interpolates a currency tag beside two labels this
    module composed, and the currency is the only remote one of the three. It is
    driven through `_split_price` because `_PRICE_RE` bounds a currency to three
    letters, so the render site is pinned without depending on that regex."""

    def _hostile_currency(_s: str | None) -> tuple[str, str]:
        return payload, "421.00"

    monkeypatch.setattr(cli, "_split_price", _hostile_currency)
    row = MultiCabinRow(itinerary=_search_result().solutions[0], prices={Cabin.COACH: "USD421.00"})
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_multi_cabin_search(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [row], cabins=(Cabin.COACH,), sort_by=Cabin.COACH
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":
        assert payload in probe, "the currency tag was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("payload", _HOSTILE_FIELD_VALUES)
def test_the_calendar_title_survives_a_hostile_currency(
    payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The calendar's summary line and table title each interpolate a currency tag,
    and the currency is Matrix's. It is the third renderer with that tag and the one
    whose branch no arm rendered: the nearest case passes a price `_PRICE_RE` does
    not match, so the currency comes back empty and the tag is the empty string.
    Driven through `_split_price` because that regex bounds a currency to three
    letters, so the render site is pinned without depending on it staying as it is."""

    def _hostile_currency(_s: str | None) -> tuple[str, str]:
        return payload, "421.00"

    monkeypatch.setattr(cli, "_split_price", _hostile_currency)
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        _result({9: {7: ("USD500.00", 1, {5: "USD501.00", 7: "USD502.00"})}}),
        dmin=5,
        dmax=7,
        origin=("JFK",),
        destination=("LHR",),
        sd=W.start,
        ed=W.end,
        round_trip=True,
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload != "\x1b[2J":
        assert payload in probe, "the currency tag was eaten"
    _ = capsys.readouterr()


@pytest.mark.parametrize("payload", [*_HOSTILE_FIELD_VALUES, _DRIVES_THE_TERMINAL])
def test_the_date_grid_survives_a_hostile_day_key(
    payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every string in this table is a key off the Google Flights grid: the day
    cells, and the count in the summary line above them the moment anyone binds it
    to something read rather than counted. The renderer has six print sites and no
    test drove it — the weave replaces it with a spy — so the wrap in the cell and
    the count in the line were claims nothing checked."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_date_grid(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        {f"{payload}2026-10-01": 421.0},
        origin=("JFK",),
        destination=("LHR",),
        sd=W.start,
        ed=W.end,
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    # The line OPENS with the count, and a substring test would not say that: a
    # day key standing in for it reads "2026-10-01 priced days", which contains
    # "1 priced days" and would pass.
    assert probe.startswith("1 priced days")
    if payload in {"[/x]", "[bold]"}:
        assert payload in probe, "the day was eaten"
    _ = capsys.readouterr()


def _merged_slice(payload: str) -> Slice:
    """A slice carrying the payload everywhere Matrix puts text: a flight number,
    an airport code and a timestamp are all remote strings."""
    return Slice.model_validate(
        {
            "flights": [f"B6 {payload}"],
            "departure": f"2026-10-01T08:00:00{payload}",
            "arrival": "2026-10-01T11:00:00",
            "duration": 180,
            "origin": SliceEndpoint(code=f"JF{payload}"),
            "destination": SliceEndpoint(code="LHR"),
        }
    )


def _merged_row(payload: str, *, field: str) -> MergedRow:
    """One reconciled row hostile in exactly ONE of its three remote slots.

    One at a time because they are three separate claims, and a row hostile in
    all of them stays green when any one stops being wrapped: the others still
    carry the payload into the same line."""
    slices = [_merged_slice(payload if field == leg else "") for leg in ("outbound", "return")]
    return MergedRow(
        itinerary=Itinerary.model_validate({"itinerary": {"slices": slices}}),
        gf_price="USD421.00",
        matrix_price="USD430.00",
        source=payload if field == "source" else "both",
    )


@pytest.mark.parametrize("payload", [*_HOSTILE_FIELD_VALUES, _DRIVES_THE_TERMINAL])
@pytest.mark.parametrize("field", ["outbound", "return", "source"])
def test_the_merged_table_survives_a_hostile_slice(
    field: str, payload: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reconciled table's two itinerary columns are composed from leaves each
    wrapped where it was read, which is why the composed cells are allowlisted —
    a claim about `_fmt_slice_cell` and friends that no test made this renderer
    keep. The source tag beside them falls back to whatever it was handed, which
    on a row this module did not build is a third remote string."""
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=400))
    cli._render_merged(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        [_merged_row(payload, field=field)],
        legs=(Leg.of(["JFK"], ["LHR"], date(2026, 10, 1)),),
        top_n=5,
    )
    probe = _flat(_SGR.sub("", buffer.getvalue()))
    for driver in _DRIVERS:
        assert driver not in probe, f"{driver!r} reached the console"
    if payload in {"[/x]", "[bold]"}:
        assert payload in probe, "the flight number was eaten"
    _ = capsys.readouterr()


def test_the_carrier_filter_is_tested_against_the_code_that_was_sent() -> None:
    """`--routing LH+` names a carrier as Google Flights spells it, so the
    membership test has to run on the code that arrived. Escaping first compares a
    string the user could never have named: the leg stops matching its own filter
    entry and the label falls through to a codeshare identity instead."""
    gf = _fake_gf("1", airline="[x]", marketing_flights=("BA999",))
    shown = cli._leg_display(gf.flight.legs[0], gf.amenities[0], frozenset({"[x]", "BA"}))  # pyright: ignore[reportPrivateUsage] — the formatter IS the unit
    # Its own code matched, so this is the booking identity, escaped for display.
    assert shown == "\\[x] 1"
    assert "BA999" not in shown  # and not the codeshare it would have fallen to


def test_the_legroom_pad_counts_what_the_reader_sees() -> None:
    """Escaping LENGTHENS a value, so padding the escaped string spends the column
    on a backslash nobody sees and the sub-line drifts by one per bracket. The pad
    is measured on the sanitized value and escaped after, so the amenities start in
    the same place whatever the backend called the flight."""
    leg = LegInfo(pitch_inches=31, legroom_class="AVERAGE")

    def rendered(flight_no: str) -> str:
        buffer = io.StringIO()
        line = cli._fmt_legroom_one(flight_no, leg)  # pyright: ignore[reportPrivateUsage] — the formatter IS the unit
        Console(file=buffer, width=200, no_color=True).print(line, highlight=False)
        return buffer.getvalue()

    # A benign number, an escaped tag, a longer escaped tag, and a value a control
    # character shortens — each a different distance between raw and rendered.
    starts = {
        flight_no: rendered(flight_no).index('31"')
        for flight_no in ("UA123", "[x]1", "[red]", "A\x00B")
    }
    assert len(set(starts.values())) == 1, starts


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
# Every function is scanned. There is no per-function escape hatch: one exempts
# every FUTURE print in a function rather than one value, and every MarkupError
# this guard has caught arrived behind one. What a function
# may print without a wrapper is said one identifier at a time, below, where the
# claim is small enough to be checked and a test exists that checks it.

# There are two ways a value becomes printable: `_quote`, which elides, quotes
# and escapes a value the user typed, and `_safe_text`, which strips the control
# characters and escapes anything remote. Bare `rich.markup.escape` is neither.
# It neutralises `[` and leaves every ESC, 8-bit CSI, bidi control and lone
# surrogate in place, and telling which values can carry one means tracking taint
# through locals, `str()` calls and attribute chains — which the scan cannot do.
# Both wrappers end in `escape`, so nothing is lost. `_amount` and `_failure_text`
# are the other two because every return path of each ends in `_safe_text`: one
# formats a price, the other an exception that may be a group of them, and
# sanitizing inside a formatter is what keeps the wrap off every call site.
_SAFE_WRAPPERS = frozenset({"_quote", "_safe_text", "_amount", "_failure_text"})

# Identifiers that need no wrapper at the print site. Four things can back an
# entry, and they are the four the memo and this scan's own docstring name: a
# hostile-field arm that fails when the value stops being this module's own; a
# type at the response or enum boundary; a number this module computed; a
# constant it wrote. Matched by IDENTIFIER — a bare name or an attribute chain —
# never by source text: an expression that happens to read the same way is not
# the same value. Keyed per FUNCTION for the same reason one step further: `n`
# is a fan-out counter in `_run_calendar` and could be anything anywhere else,
# and a bare name is exactly what a user value looks like once it is in a local.
_PRINTABLE_IDENTIFIERS = frozenset(
    {
        ("_parse_duration", "lo"),  # the ints it just parsed
        ("_parse_duration", "hi"),
        ("_parse_duration", "_MAX_NIGHTS"),  # and the cap it is comparing them to
        ("_run_calendar", "n"),  # fan-out counters
        ("_run_calendar", "rounds"),
        ("_run_calendar", "conc"),
        ("calendar", "n_split"),  # how many sub-searches were merged
        ("_run_fast_calendar_grid", "_GF_GRID_UNAVAILABLE_NOTE"),
        ("_paint_calendar_first", "_GF_GRID_UNAVAILABLE_WEAVE_NOTE"),
        ("_resolve_format", "_FORMAT_CHOICES"),
        ("query_cabin", "cab.value"),  # a member of this module's own enum
        ("note_missing_column", "cab.value"),  # the same enum, on the series runner
        ("_gflight_cabins_in_series", "cab.value"),
        # One of two sentences this module wrote, chosen by a flag. Each names
        # what happens next to a link, so neither can be a bare clause the
        # sentence above it carries.
        ("_pick_in_range", "fallback"),
        ("_run_enriched_path", "unpinned"),
        ("_reraise_if_orderly", "plural"),  # "" or "s", off a count beside it
        ("_answer_gf_empty", "plural"),
        # `_gf_refusal` sanitizes every remote field it reads and leaves both of
        # its own console-ready, so a wrapper at the sink would show a backslash
        # in front of every bracket the remote text carried. The exactly-once
        # tests in `tests/test_gf_browser.py` are what hold these.
        ("_run_gflight_path", "refusal.message"),
        ("_report_enriched_gf_failure", "refusal.note"),
        ("_report_enriched_gf_failure", "refusal.message"),
        ("query_cabin", "refusal.note"),
        ("note_missing_column", "note"),
        # Built here from the pin budget, and every part of it is ours.
        ("_run_gflight_path_multi", "join_note"),
        ("_validate_sort_cabin", "sort_by.value"),  # the same enum, one command over
        ("_validate_sort_cabin", "names"),  # its members joined into a list
        ("_emit_urls", "pinned_label"),  # "#N" or "cheapest", built from an int
        ("_render_search", "res.solution_count"),  # counts and dates off the response
        ("_render_calendar", "res.solution_count"),
        ("_render_calendar", "duration_note"),
        ("_render_date_grid", "priced_days"),
        # Empty, or a literal around the trip length its `:d` spec proves a number.
        ("_render_date_grid", "trip"),
        ("_render_gflight_table", "_LEGROOM_KEY"),  # the legend it wrote
        # Sanitized where the currency was read, so the summary line and the table
        # title interpolate one value that was wrapped once.
        ("_render_search", "ccy_tag"),
        ("_render_calendar", "ccy_tag"),
        ("_render_multi_cabin_search", "ccy_tag"),
        # Cells and rows composed in the renderer from leaves each wrapped where
        # it was read — `_fmt_slice_cell`, `_leg_display`, `_gflight_route`,
        # `_fmt_gflight_legroom` and `_amount` — and NOT wrapped again around the
        # composition, because `_fmt_legroom_one` writes a `[red]` on the pitch
        # token on purpose.
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
        # Empty, or one of the three literals `_bag_cell` writes.
        ("_render_gflight_table", "bag_cell"),
        # The cabin letters are this module's own map, keyed by its own enum.
        ("_render_multi_cabin_search", "cabin_labels"),
        ("_render_multi_cabin_search", "sort_label"),
        ("_render_multi_cabin_search", "letter"),
    }
)
# A table printed whole needs no entry: `_renderables_built_in` reads the
# assignment instead, which is a claim about the BINDING rather than the name.

# What "reaches rich" means, one call shape at a time. A Rich Table parses markup
# in its title, in its caption, in every column header and footer and in every
# cell — all of which are read here — so the calls that FILL one are sinks exactly
# as `console.print` is: the text is chosen there, and the `console.print(t)` a
# hundred lines later adds none of its own. Exempting that print instead, and
# never reading a cell, is the shape that hides a MarkupError from a green scan.
_TEXT_SINK_METHODS = frozenset(
    {"print", "log", "rule", "status", "add_row", "add_column", "from_markup", "render_str"}
)
# Constructors whose arguments reach the markup parser when the object renders,
# read at the constructor for the same reason. Membership does two jobs — it reads
# the arguments AND exempts `x = Ctor(...)` from the print check — so a member that
# reads nothing still hands out that exemption. `Text` is a member even though the
# bare constructor takes its argument LITERALLY and parses no markup: what naming
# it buys is the other half, the binding, because an allowlisted local assigned
# `Text(<remote>)` and handed to a sink is otherwise read as this module's own
# text and never looked at again. The price is a false positive on
# `Text(<literal-or-wrapped>)`, which the wrapper already there settles.
# `Text.from_markup` is a sink above, and that one does parse markup.
_RENDERABLE_SINKS = frozenset({"Table", "Panel", "Text"})

# Presentation types only a number survives: `format("x", "d")` raises, so a field
# carrying one cannot be a string and cannot carry markup. The alternative is
# allowlisting the name, and for `fr.price` — a `getattr` off a duck-typed Google
# Flights result — that would be a promise nobody here can keep.
#
# Two characters, not the thirteen numeric presentation types: a set with no
# inertness test grew eleven members that allowed nothing, and every one of them
# was also a place the proof stops being one. What the proof does NOT cover is
# stated at `_spec_proves_a_number`.
_NUMERIC_PRESENTATION = frozenset("df")

# The corpus the date test walks, beside the set it has to cover rather than beside
# the test, because adjacency is what keeps the two from drifting apart. `%` is not
# one of the thirteen; `_spec_proves_a_number` refuses it.
_NUMERIC_PRESENTATION_CORPUS = "bcdoxXneEfFgG"


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


def _own_scope_nodes(fn: ast.AST) -> list[ast.AST]:
    """Nodes in this scope, not descending into nested ones.

    A nested `def` or lambda binds only its own name here; its body is a different
    scope and gets its own pass."""
    out: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            out.append(child)
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                walk(child)

    walk(fn)
    return out


def _assignments_in(fn: ast.AST) -> dict[str, list[ast.expr]]:
    """Every name this scope ASSIGNS, mapped to the expressions assigned to it.

    Assignment only. A parameter, a `for` target, an `except … as` and an import
    bind a name to no expression this scan can read, so such a name is absent here
    rather than present with nothing — the difference is what lets a caller tell
    "assigned something I checked" from "bound somewhere I cannot see"."""
    found: dict[str, list[ast.expr]] = {}
    for node in _own_scope_nodes(fn):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            for name in _target_names(target):
                found.setdefault(name, []).append(node.value)
    return found


def _target_names(target: ast.expr) -> list[str]:
    """The bare names an assignment target binds, tuple and starred forms included."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _renderables_built_in(fn: ast.AST) -> frozenset[str]:
    """Names this scope assigns EXACTLY ONCE, from a renderable constructor.

    Once, because the scan reads no flow: `t = Table(...)` three lines above
    `t = f"{e}"` says nothing about what `console.print(t)` hands rich. This is a
    claim about the binding, which is why a printed table needs no allowlist entry
    — an entry would be a claim about the name, and the name can be reassigned."""
    names: set[str] = set()
    for name, values in _assignments_in(fn).items():
        if len(values) != 1:
            continue
        value = values[0]
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _RENDERABLE_SINKS
        ):
            names.add(name)
    return frozenset(names)


class _Names(NamedTuple):
    """What the scan learned about the names in the file it is reading.

    `assigned` is per function NAME rather than per scope, matching the granularity
    `_PRINTABLE_IDENTIFIERS` is keyed at; `renderables` is per node, because a
    closure inherits what the function around it built."""

    assigned: dict[str, dict[str, list[ast.expr]]]
    renderables: dict[ast.AST, frozenset[str]]


def _read_names(tree: ast.Module) -> _Names:
    assigned: dict[str, dict[str, list[ast.expr]]] = {}
    renderables: dict[ast.AST, frozenset[str]] = {}

    def walk(node: ast.AST, built: frozenset[str]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = built
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = built | _renderables_built_in(child)
                per_name = assigned.setdefault(child.name, {})
                for name, values in _assignments_in(child).items():
                    per_name.setdefault(name, []).extend(values)
            renderables[child] = inner
            walk(child, inner)

    walk(tree, frozenset())
    return _Names(assigned, renderables)


def _rebound_to_interpolated_text(values: list[ast.expr]) -> bool:
    """Whether any assignment to this name interpolates another name into it.

    Narrower than it reads: only a top-level f-string whose field is a bare name or
    an attribute chain, which no binding in `cli.py` is. A call, a conditional, a
    list, a comprehension, a subscript, a tuple-unpack, an arithmetic expression and
    a name with no assignment at all are each unread; so is the one top-level
    f-string among them, `_render_gflight_table.dur`, because its two fields are
    arithmetic rather than names. So this is NOT what makes an entry checkable. The
    hostile-field tests around the renderers are, one payload per field. What this
    catches and nothing else here does is the bypass case `an allowlisted local
    rebound to interpolated text`."""
    for value in values:
        if not isinstance(value, ast.JoinedStr):
            continue
        for part in value.values:
            if isinstance(part, ast.FormattedValue) and isinstance(
                part.value, (ast.Name, ast.Attribute)
            ):
                return True
    return False


def _is_ours(name: ast.expr, chain: list[str], names: _Names) -> bool:
    """Whether this identifier is one that needs no wrapper here.

    The INNERMOST function, and no scope around it. An entry is a claim about a
    name in one body, so a closure that shadows the name with a parameter or binds
    it to something else holds no such claim: `cab.value` is keyed on
    `query_cabin`, which prints it, not on the two functions that define it. The
    price is that two bodies of one name share their entries, which is what those
    two `query_cabin` closures do on purpose."""
    dotted = _dotted_name(name)
    if dotted is None or not chain:
        return False
    if (chain[0], dotted) not in _PRINTABLE_IDENTIFIERS:
        return False
    return not _rebound_to_interpolated_text(names.assigned.get(chain[0], {}).get(dotted, []))


def _is_safe_field(value: ast.expr, chain: list[str], names: _Names) -> bool:
    """A printed f-string field is safe when it is wrapped, or is one of ours.

    Judged on the AST shape — an `ast.Call` whose `func` is an `ast.Name` in
    `_SAFE_WRAPPERS` — never on the source text. `_safe_text` and `not_safe_text`
    share a prefix, and `obj._safe_text(x)` is an attribute call on something else
    entirely; both read as safe to a string comparison.
    """
    if isinstance(value, ast.Call):
        return isinstance(value.func, ast.Name) and value.func.id in _SAFE_WRAPPERS
    return _is_ours(value, chain, names)


def _prints_a_renderable(node: ast.Call, built: frozenset[str]) -> bool:
    """A print whose only argument is one renderable this scope built.

    The exemption is the BINDING, not the name: `_renderables_built_in` saw the
    single assignment from `Table(...)`. Everything that filled that table was read
    at the `Table(...)`, `add_column` and `add_row` calls, so this print adds none
    of its own — and an f-string beside it in the same call still is."""
    return (
        not node.keywords
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in built
    )


def _is_a_print(node: ast.Call) -> bool:
    """Whether this call hands text to a markup console — `console.print`,
    `err.log`, a rule or a status, bare `print`, or a renderable's title, columns
    and rows, which are the same console one object later."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _TEXT_SINK_METHODS
    return isinstance(func, ast.Name) and (func.id == "print" or func.id in _RENDERABLE_SINKS)


# A typer `help=` / `epilog=` string is a markup sink the way a table cell is: the
# app sets `rich_markup_mode="rich"`, so Typer renders it through rich's markup
# parser, and the value in it is chosen here rather than at any console. What is
# read there is NARROWER than at a print — an f-string field that is a call or an
# attribute read, which is where a runtime value comes from. A bare name is not
# read: these strings are built at module scope, where there is no function to key
# an allowlist entry on, and every name in one today is a constant of literal text
# this file wrote. `console_sanitizing.md` states that boundary.
_HELP_SINKS = frozenset({"Option", "Argument", "Typer"})


def _help_faults(src: str, node: ast.Call, names: _Names) -> list[str]:
    """Runtime values interpolated into a typer help string, or nothing."""
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else _dotted_name(func) or ""
    if called not in _HELP_SINKS:
        return []
    fields = [
        part
        for kw in node.keywords
        if kw.arg in {"help", "epilog"} and isinstance(kw.value, ast.JoinedStr)
        for part in kw.value.values
        if isinstance(part, ast.FormattedValue)
        and isinstance(part.value, (ast.Call, ast.Attribute))
    ]
    return [
        f"{called}:{node.lineno} unwrapped help field "
        f"{ast.get_source_segment(src, field) or ast.dump(field)}"
        for field in fields
        if not _is_safe_field(field.value, [], names)
    ]


def _spec_has_field(spec: ast.expr | None) -> bool:
    """Whether a format spec interpolates anything. `f"{escape(a):{e}}"` makes `e`
    the padding character, which reaches rich without passing the wrapper."""
    return spec is not None and any(isinstance(n, ast.FormattedValue) for n in ast.walk(spec))


def _spec_proves_a_number(spec: ast.expr | None) -> bool:
    """Whether a `str` reaching this format spec would raise, which is what makes
    the field a number whatever the name says.

    A spec with a field of its own proves nothing — the fill is interpolated too —
    so only a single literal counts. `%` anywhere disqualifies it: `date.__format__`
    is `strftime`, so `format(date(...), "%Y-%m-%d")` returns a string and the last
    character would otherwise read as a proof about a date.

    The bound of the claim: it holds for `str`, and for anything whose `__format__`
    is the builtin one. An object defining its own `__format__` can return whatever
    it likes from any spec, so for such a value this is not a proof — nothing in
    `cli.py` routes one into a numeric spec today, and the hostile-field tests are
    what would catch it if one arrived."""
    if not isinstance(spec, ast.JoinedStr) or len(spec.values) != 1:
        return False
    only = spec.values[0]
    return (
        isinstance(only, ast.Constant)
        and isinstance(only.value, str)
        and "%" not in only.value
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


def _argument_faults(src: str, arg: ast.expr, chain: list[str], names: _Names) -> list[str]:
    """Why this print argument could reach rich unescaped, or nothing."""
    if isinstance(arg, ast.Constant):
        # A literal the author wrote, of any type: rich's own knobs arrive as
        # `no_wrap=True` and `width=200` as often as `style="red"`.
        return []
    parts = _printed_parts(arg)
    if parts is not None:
        return [fault for part in parts for fault in _argument_faults(src, part, chain, names)]
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
            if not isinstance(part, ast.FormattedValue) or not _is_safe_field(
                part.value, chain, names
            ):
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
    if _is_safe_field(arg, chain, names):
        return []  # a wrapper call, or one of ours, standing as the whole argument
    shown = ast.get_source_segment(src, arg) or ast.dump(arg)
    return [f"{type(arg).__name__} argument {shown}"]


def escape_scan(src: str) -> list[str]:
    """Console prints in `src` that could hand rich unescaped text.

    Checks positional AND keyword arguments, and demands a literal, a fully
    wrapped f-string, or an allowlisted name. A local holding a message, a
    `.format()`, a `%`, or a concatenation of one is none of those, and neither is
    a field whose conversion or format spec runs after the wrapper.

    It reads CALLS. A markup slot filled by assignment (`t.title = x`,
    `t.caption = x`, `t.columns[0].header = x`), or by an API this file does not
    name, is not read — none is live in `cli.py` today, and `Panel` and `Text` sit
    in `_RENDERABLE_SINKS` unimported, so an aliased import of either would have
    coverage that looks present and is not. A typer `help=` / `epilog=` f-string is read
    too, for the runtime values in it — Typer renders those through the same markup
    parser, a console away from any print.

    It models scope only as far as the INNERMOST function: an entry is a claim
    about a name in one body, so a closure that shadows or rebinds the name is
    scanned like any other function and inherits nothing. What it cannot tell apart
    is two bodies of the same name, which share their entries — the two
    `query_cabin` closures printing `cab.value` are that shape on purpose. What an
    entry does NOT get from this is a check on the value behind it. Four things back
    one: a hostile-field arm that fails when the value stops being this module's
    own; a type at the response or enum boundary; a number this module computed; a
    constant it wrote. The hostile-field tests above are what pin the values a type
    at the boundary cannot, one payload per field through the renderer that reads
    it, and an entry backed by none of the four is a claim nothing checks. Wrapping
    at the sink is not a fifth backing — it is how a value avoids needing an entry
    at all.
    """
    tree = ast.parse(src)
    chains = _enclosing_functions(tree)
    names = _read_names(tree)
    faults: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        faults += _help_faults(src, node, names)
        if not _is_a_print(node):
            continue
        if _prints_a_renderable(node, names.renderables.get(node, frozenset())):
            continue
        chain = chains.get(node, [])
        where = chain[0] if chain else "<module>"
        args = list(node.args)
        args += [k.value for k in node.keywords]
        for arg in args:
            faults += [
                f"{where}:{node.lineno} {fault}"
                for fault in _argument_faults(src, arg, chain, names)
            ]
    return faults


def test_calendar_paths_escape_every_printed_value() -> None:
    faults = escape_scan(Path(cli.__file__).read_text(encoding="utf-8"))
    assert not faults, (
        "wrap these in _quote (a value the user typed), _safe_text (anything "
        "remote) or a formatter that calls one; add the name to "
        "_PRINTABLE_IDENTIFIERS only if the value is this module's own, and say "
        "which of the four backs it — a hostile-field arm that fails when it stops "
        "being, a type at the response or enum boundary, a number this module "
        f"computed, or a constant it wrote: {faults}"
    )


def test_printable_identifiers_are_all_load_bearing() -> None:
    """An entry that allows nothing still pre-approves whatever later takes its
    name in that function, and nothing else in the file would speak when it did.
    It is also what speaks when an allowlisted function is DELETED — its entries
    go inert — where a rename is caught by the scan itself, with the faults at
    the prints rather than a set difference."""
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


@pytest.mark.parametrize(
    "label",
    [
        "_SAFE_WRAPPERS",
        "_NUMERIC_PRESENTATION",
        "_RENDERABLE_SINKS",
        "_TEXT_SINK_METHODS",
        "_HELP_SINKS",
    ],
)
def test_every_set_the_scan_consults_is_load_bearing(label: str) -> None:
    """A member that changes nothing reads like a decision and is not one, and it
    pre-approves whatever later carries its name. Measured over every source the
    scan reads, because the directions look opposite: dropping a member that
    ALLOWS makes `cli.py` speak where it was silent, dropping one that READS makes
    a corpus case go silent where it spoke, and no change at all is what inert
    means either way. One of these sets grew eleven dead members."""
    sources = [Path(cli.__file__).read_text(encoding="utf-8"), *_KNOWN_BYPASSES.values()]
    before = [escape_scan(source) for source in sources]
    members: frozenset[str] = getattr(sys.modules[__name__], label)
    inert: list[str] = []
    for member in sorted(members):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys.modules[__name__], label, members - {member})
            if all(escape_scan(s) == was for s, was in zip(sources, before, strict=True)):
                inert.append(member)
    assert not inert, f"{label}: these members allow nothing; delete them: {inert}"


# The scan's own regression net: one source per shape that reaches rich holding
# text it did not escape, or escaped too weakly. The delete-one test above reads it
# too — a member changing nothing here and nothing in `cli.py` allows nothing.
_KNOWN_BYPASSES = {
    "local variable": 'def calendar():\n    msg = f"{e}"\n    err.print(msg)\n',
    "keyword argument": 'def calendar():\n    err.print(text=f"{e}")\n',
    "str.format": 'def calendar():\n    err.print("{}".format(e))\n',
    "percent formatting": 'def calendar():\n    err.print("%s" % e)\n',
    "concatenation": 'def calendar():\n    err.print("bad " + str(e))\n',
    "a newly extracted helper": 'def _brand_new_helper():\n    err.print(f"{e}")\n',
    "console.log": 'def calendar():\n    console.log(f"{e}")\n',
    "console.rule": 'def calendar():\n    console.rule(f"{e}")\n',
    "console.status": 'def calendar():\n    console.status(f"{e}")\n',
    # A renderable built from markup, and a console asked to parse a string into
    # one: neither is a print, and both hand rich text it will read as markup.
    "text built from markup": 'def calendar():\n    body = Text.from_markup(f"{e}")\n',
    "a string rendered by the console": (
        'def calendar():\n    body = console.render_str(f"{e}")\n'
    ),
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
    "escape on str() of an exception": ('def calendar():\n    err.print(f"{escape(str(e))}")\n'),
    "escape on a local holding remote text": (
        'def calendar():\n    msg = matrix_message()\n    err.print(f"{escape(msg)}")\n'
    ),
    "escape on a value the user typed": ('def calendar():\n    err.print(f"{escape(routing)}")\n'),
    "escape on a response field": (
        'def calendar():\n    err.print(f"{escape(res.cheapest_price)}")\n'
    ),
    # A Rich table is a console one object later. Its title, its headers and
    # its cells all parse markup, and a value that reaches one never passes
    # through the `console.print(t)` this scan would otherwise be reading.
    "a raw name in a table cell": ("def _render_search():\n    st.add_row(res.cheapest_price)\n"),
    "a raw field in a table cell": ('def _render_search():\n    st.add_row(f"{it.price}")\n'),
    "a raw field in a column header": (
        'def _render_search():\n    st.add_column(f"{col.label.code}")\n'
    ),
    "a raw field in a table title": ('def _render_search():\n    t = Table(title=f"{query}")\n'),
    "a raw field in a panel": ('def _render_search():\n    p = Panel(f"{e}")\n'),
    # A `Text` holds its argument literally, so this one is not a MarkupError
    # waiting to happen — it is the allowlist being handed a value it does not
    # cover: the name is exempt, the binding is a call the scan cannot follow,
    # and the remote field rides into the row inside the object.
    "an allowlisted local assigned Text(remote)": (
        "def _render_search():\n    out = Text(res.cheapest_price)\n    st.add_row(out)\n"
    ),
    # A starred list is the argument list, so the cells come out of it.
    "a raw name starred into a row": (
        "def _render_calendar():\n    t.add_row(*[res.cheapest_price])\n"
    ),
    # A printed name is exempt because THIS scope assigned it a renderable, not
    # because of how it is spelled: `t` and `st` are the names the renderers
    # use, and neither buys a pass on its own.
    "a name printed whole that was never a renderable": (
        "def _render_search():\n    st = remote_renderable(res.raw)\n    console.print(st)\n"
    ),
    "a renderable name assigned twice, once from text": (
        "def _render_search():\n"
        "    t = Table(title='x')\n"
        '    t = f"{res.cheapest_price}"\n'
        "    console.print(t)\n"
    ),
    # An allowlisted local rebound to interpolated text loses the pass: the
    # entry claims the value was sanitized where it was read.
    "an allowlisted local rebound to interpolated text": (
        'def _render_search():\n    out = f"{it.raw_note}"\n    st.add_row(out)\n'
    ),
    # `date.__format__` is `strftime`, so a spec ending in `d` or `f` proves
    # nothing when it holds a `%`.
    "a strftime spec whose last character is a numeric type": (
        'def calendar():\n    err.print(f"{when:%Y-%m-%d}")\n'
    ),
    "a strftime spec ending in microseconds": (
        'def calendar():\n    err.print(f"{when:%H:%M:%S%f}")\n'
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
    "a padding spec, which any string survives": ('def calendar():\n    err.print(f"{code:<6}")\n'),
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
    # An entry is a claim about a name in ONE body: a nested helper reusing the name
    # gets no pass from the function around it, as a parameter or bound inside.
    "nested helper reusing an excluded name": (
        'def calendar():\n    def _render_search():\n        err.print(f"{e}")\n'
    ),
    "an allowlisted name shadowed by a nested parameter": (
        "def _render_search():\n    def helper(out):\n        st.add_row(out)\n"
    ),
    "an allowlisted name rebound inside a nested scope": (
        'def _render_search():\n    def helper():\n        out = f"{it.raw}"\n'
        "        st.add_row(out)\n"
    ),
    # Typer renders a help string through the markup parser, so a path read out of
    # the environment reaches rich with no print anywhere near it.
    "a runtime value in an option help string": (
        '_OPT = typer.Option(None, "--provider-opt", help=f"Overrides {_config.config_path()}.")\n'
    ),
    "a runtime value in an argument help string": (
        '_ARG = typer.Argument(help=f"reads {_config.config_path()}")\n'
    ),
    "a runtime value in a command-group help string": (
        'app = typer.Typer(help=f"reads {_config.config_path()}")\n'
    ),
}


def test_a_help_string_is_read_only_for_the_values_it_looks_up() -> None:
    """The boundary the memo states, in the direction that keeps the scan honest:
    a bare name in a help string is not read, so a constant of literal text needs
    no wrapper — and the wrapped call, which is the shape the live site now has,
    is silent too."""
    assert not escape_scan('_OPT = typer.Option(None, "--x", help=f"one of {_FORMAT_CHOICES}.")\n')
    assert not escape_scan(
        '_OPT = typer.Option(None, help=f"{_safe_text(_config.config_path())} section.")\n'
    )


def test_escape_scan_finds_every_known_bypass() -> None:
    missed = [name for name, source in _KNOWN_BYPASSES.items() if not escape_scan(source)]
    assert not missed, f"the scan does not catch: {missed}"


def test_escape_scan_passes_clean_source() -> None:
    """And it must not cry wolf, or the next author will route around it."""
    clean = (
        "def calendar():\n"
        '    err.print("[red]a plain literal[/]")\n'
        '    err.print(f"[red]window {_safe_text(sd.isoformat())}[/]")\n'
        "    err.print(_safe_text(e))\n"
        # Rich's own knobs are literals of whatever type rich takes.
        '    err.print("hi", no_wrap=True, width=200)\n'
        "def _run_fast_calendar_grid():\n"
        '    err.print(f"[dim]{_GF_GRID_UNAVAILABLE_NOTE}[/]")\n'  # allowed HERE
        # Either kind of safe value may stand as the whole argument.
        "    err.print(_GF_GRID_UNAVAILABLE_NOTE)\n"
        "def _parse_duration():\n"
        '    err.print(f"[red]bad duration {_quote(s)}[/]")\n'
        '    err.print(f"max ({hi}) is below min ({lo})", style="red")\n'  # allowed HERE
        # Allowed in the function that prints it, which is where the entry is keyed.
        "def _paint_calendar_first():\n"
        '    console.print(f"[dim]{_GF_GRID_UNAVAILABLE_WEAVE_NOTE}[/]")\n'
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
        # What `_printed_parts` buys, which the corpus above cannot show: these
        # four shapes are opaque to `_dotted_name`, so without the recursion they
        # could not be allowlisted at all and would have to be rewritten.
        "def _render_search():\n"
        "    t.add_row(*cells)\n"
        '    t.add_row(it_carriers or "?")\n'
        '    t.add_row(out if slcs else "—")\n'
        '    t.add_column("a" + "b")\n'
    )
    assert not escape_scan(clean)


def test_a_date_survives_every_numeric_presentation_type() -> None:
    """Why the proof is two characters and refuses a `%`. `format` raises for a
    `str` on all of these, which is the whole claim — but `date.__format__` is
    `strftime`, so a date passes each of them and comes back a string. The `%`
    clause is what keeps a strftime spec from reading as a proof about a number."""
    # A loop over a corpus can only fail by GROWING, so this is what makes a
    # shortened one speak: drop a character the scan accepts and the proof stops
    # covering it.
    assert frozenset(_NUMERIC_PRESENTATION_CORPUS) >= _NUMERIC_PRESENTATION
    when = date(2026, 10, 1)
    for presentation in _NUMERIC_PRESENTATION_CORPUS:
        assert format(when, presentation) == presentation  # returned, not formatted
        with pytest.raises((ValueError, TypeError)):
            format("a string", presentation)
    assert format(when, "%Y-%m-%d") == "2026-10-01"  # the shape the corpus rejects
