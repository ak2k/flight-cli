"""Calendar per-destination fan-out + merge (work-on0dw).

Matrix under-reports multi-airport calendar grids under compute-budget pressure,
so a multi-airport calendar is queried one destination at a time (groupable via
--max-per-query) and merged. These tests cover the split, merge, and
`_run_calendar` orchestration (no network).
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, override

from flight_cli import cli
from flight_cli._calendar_split import (
    is_empty_calendar,
    merge_calendar_results,
    split_calendar_search,
)
from flight_cli.domain import Cabin, CalendarSearch, CalendarWindow, Leg, SearchOptions
from flight_cli.models import CalendarResult

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
