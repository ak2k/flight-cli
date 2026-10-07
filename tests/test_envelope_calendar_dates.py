# pyright: reportPrivateUsage=false
"""`--format envelope` dates each calendar row.

A Matrix day's `row` is the day object Matrix sent, its day of the month alone,
so each calendar row also carries the `departure` and `return` it prices. The
fixture is a live JFK-LAX answer for 7-night round trips departing 2026-10-20 to
2026-11-16 (`test_calendar_formats`), and Matrix is a stub that serves it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast

import pytest

from conftest import LITERAL_DATES_NOW
from test_calendar_formats import (
    _BODY,
    _ROUTE,
    _body_of,
    _envelope_of,
    _graph,
    _graphs,
    _run,
    _serve,
)
from test_calendar_split import _result

# The fixture's dates are literal; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_ENVELOPE = ("--format", "envelope")


def _dates(env: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
    """Each result row as its day of the month, its departure and its return."""
    rows = cast("list[dict[str, Any]]", env["results"])
    return [(r["row"]["date"], r.get("departure"), r.get("return")) for r in rows]


def _calendar(*window: str) -> dict[str, Any]:
    return _envelope_of(
        _run("calendar", "JFK", "LAX", *window, "--gf-transport", "http", *_ENVELOPE)
    )


def test_a_round_trip_day_names_its_departure_and_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every priced day of the fixture, October 20-25 and 31 and November 1-16,
    is dated by its month, and returns 7 nights after it."""
    _serve(monkeypatch, _BODY)
    result = _run(*_ROUTE, "--gf-transport", "http", *_ENVELOPE)
    env = _envelope_of(result)
    priced = [date(2026, 10, d) for d in (20, 21, 22, 23, 24, 25, 31)]
    departures = [*priced, *(date(2026, 11, d) for d in range(1, 17))]
    assert _dates(env) == [
        (d.day, d.isoformat(), (d + timedelta(days=7)).isoformat()) for d in departures
    ]
    assert [r["row"]["minPrice"] for r in env["results"]][:2] == ["USD467.00", "USD677.00"]


def test_a_day_returns_after_its_cheapest_trip_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """The day's own price is its cheapest length's, and the shortest of a tie."""
    days = {
        20: ("USD300.00", 3, {7: "USD320.00", 5: "USD300.00"}),
        21: ("USD300.00", 3, {7: "USD300.00", 6: "USD300.00"}),
        22: ("USD300.00", 3, dict[int, str]()),
    }
    _serve(monkeypatch, _body_of(_result({10: days}, year=2026)))
    window = ["--start", "2026-10-20", "--end", "2026-10-23", "-d", "5-7"]
    env = _calendar(*window)
    assert _dates(env) == [
        (20, "2026-10-20", "2026-10-25"),
        (21, "2026-10-21", "2026-10-27"),
        (22, "2026-10-22", None),
    ]


def test_a_trip_length_with_an_empty_price_is_not_the_cheapest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An option Matrix sent without a price names no trip, so no return."""
    days = {
        20: ("USD300.00", 3, {7: "USD300.00"}),
        21: ("USD320.00", 3, {7: "USD320.00", 6: ""}),
        22: ("USD330.00", 3, {6: ""}),
    }
    _serve(monkeypatch, _body_of(_result({10: days}, year=2026)))
    window = ["--start", "2026-10-20", "--end", "2026-10-22", "-d", "6-7"]
    env = _calendar(*window)
    assert _dates(env) == [
        (20, "2026-10-20", "2026-10-27"),
        (21, "2026-10-21", "2026-10-28"),
        (22, "2026-10-22", None),
    ]


def test_a_one_way_day_has_no_return(monkeypatch: pytest.MonkeyPatch) -> None:
    days = {d: ("USD200.00", 2, dict[int, str]()) for d in (20, 21)}
    _serve(monkeypatch, _body_of(_result({10: days}, year=2026)))
    window = ["--start", "2026-10-20", "--end", "2026-10-21", "--one-way"]
    env = _calendar(*window)
    assert _dates(env) == [(20, "2026-10-20", None), (21, "2026-10-21", None)]


def test_a_window_holding_a_day_of_the_month_twice_dates_it_by_its_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-20 to 2027-10-20 holds October 20 twice; the month's year picks one."""
    _serve(monkeypatch, _BODY)
    window = ["--start", "2026-10-20", "--end", "2027-10-20", "-d", "7"]
    env = _calendar(*window)
    assert _dates(env)[0] == (20, "2026-10-20", "2026-10-27")


def test_a_priced_day_outside_the_window_has_no_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix may price a day the window did not ask; its row stays, undated."""
    days = {20: ("USD300.00", 3, {7: "USD300.00"}), 28: ("USD310.00", 3, {7: "USD310.00"})}
    _serve(monkeypatch, _body_of(_result({10: days}, year=2026)))
    window = ["--start", "2026-10-20", "--end", "2026-10-23", "-d", "7"]
    env = _calendar(*window)
    assert _dates(env) == [(20, "2026-10-20", "2026-10-27"), (28, None, None)]


def test_a_google_graph_cell_names_its_departure_and_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `--fast` calendar's rows are Google's cells, each dated as its own `row` is."""
    _serve(monkeypatch, _BODY)
    prices = {"2026-10-20": 398.0, "2026-10-21": 410.0}
    _graphs(monkeypatch, {7: _graph(7, prices)})
    window = ["--start", "2026-10-20", "--end", "2026-10-21", "-d", "7"]
    argv = ["calendar", "JFK", "LAX", *window, "--fast", "--gf-transport", "browser", *_ENVELOPE]
    env = _envelope_of(_run(*argv))
    rows = cast("list[dict[str, Any]]", env["results"])
    assert [(r.get("departure"), r.get("return")) for r in rows] == [
        ("2026-10-20", "2026-10-27"),
        ("2026-10-21", "2026-10-28"),
    ]
    assert [(r["row"]["departure"], r["row"]["return"]) for r in rows] == [
        (r.get("departure"), r.get("return")) for r in rows
    ]
