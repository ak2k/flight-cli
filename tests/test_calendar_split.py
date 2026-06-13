"""Calendar split-on-empty recovery (work-on0dw).

Matrix silently sheds multi-airport + routing calendar queries that exceed its
per-query compute budget. We split into per-destination sub-searches and merge
their grids. These tests cover the pure split + merge logic (no network).
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


# ───────────────── orchestration: _run_calendar split-on-empty ──────────────
# Deterministic end-to-end of the split path (no network): a fake client returns
# empty for the multi-destination combined query and populated grids for the
# single-destination sub-queries, so _run_calendar must split, merge, recover.

_EMPTY: dict[str, Any] = {"solutionCount": 0, "calendar": {"months": []}}


class _FakeClient:
    """Combined (multi-dest) query → empty; per-destination sub-query → priced."""

    _PRICE: ClassVar[dict[str, str]] = {
        "VIE": "USD800.00",
        "PAR": "USD600.00",
        "FCO": "USD700.00",
        "MAD": "USD900.00",
    }

    def __init__(self, **_kwargs: object) -> None: ...

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = cache
        dests = search.legs[0].destinations
        if len(dests) != 1:  # combined multi-destination query → shed (empty)
            return CalendarResult.from_api(_EMPTY)
        price = self._PRICE.get(dests[0], "USD999.00")
        return _result({9: {7: (price, 3, {5: price, 7: price})}}, cheapest=price)


class _HealthyClient(_FakeClient):
    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        return _result({9: {7: ("USD500.00", 2, {5: "USD500.00"})}}, cheapest="USD500.00")


class _AllEmptyClient(_FakeClient):
    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        _ = (search, cache)
        return CalendarResult.from_api(_EMPTY)


def test_run_calendar_recovers_via_split_on_empty(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _FakeClient)
    res, n_split = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR", "FCO", "MAD"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n_split == 4  # split into 4 per-destination searches
    assert not is_empty_calendar(res)
    assert res.cheapest_price == "USD600.00"  # PAR is the cheapest destination


def test_run_calendar_no_split_when_combined_succeeds(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _HealthyClient)
    res, n_split = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n_split == 0
    assert not is_empty_calendar(res)


def test_run_calendar_genuine_empty_is_not_masked(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _AllEmptyClient)
    res, n_split = cli._run_calendar(  # pyright: ignore[reportPrivateUsage]
        _cal(["VIE", "PAR"]), rps=10.0, impersonate="chrome", no_cache=True
    )
    assert n_split == 0  # sub-queries also empty → genuinely flight-less
    assert is_empty_calendar(res)
