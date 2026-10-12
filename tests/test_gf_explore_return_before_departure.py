# pyright: reportPrivateUsage=false
"""`flight explore` reports no trip length for a row whose return is dated before
its departure: the row, its fare and both dates stay, and nights is unknown."""

from __future__ import annotations

import json
from datetime import date

import pytest

from flight_cli import _gf_explore as ge
from test_gf_explore import (
    _explore,
    _explore_body,
    _fare,
    _fixed_today_and_wide_consoles,
    _info,
    _serve,
)

__all__ = ["_fixed_today_and_wide_consoles"]


def _austin(departure: date, back: date) -> ge.Destination:
    return ge.Destination(
        "Austin", "United States", "AUS", 150, departure, back, "American", 0, 200
    )


@pytest.mark.parametrize(
    ("back", "nights"),
    [
        pytest.param(date(2026, 11, 6), None, id="return-before-departure"),
        pytest.param(date(2026, 11, 8), 0, id="same-day"),
        pytest.param(date(2026, 11, 15), 7, id="a-week"),
    ],
)
def test_nights_are_none_when_the_return_precedes_the_departure(
    back: date, nights: int | None
) -> None:
    row = _austin(date(2026, 11, 8), back)
    assert row.nights == nights
    assert ge.document(ge.ExploreAnswer("USD", (row,)))[0]["nights"] == nights


def test_json_keeps_the_row_and_both_dates_with_null_nights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        _explore_body([_info("/m/1", "Austin", "2026-11-08", "2026-11-06")], [_fare("/m/1", 150)]),
    )
    result = _explore("JFK", "--format", "json")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    assert (row["price"], row["departure"], row["return"], row["nights"]) == (
        150,
        "2026-11-08",
        "2026-11-06",
        None,
    )


@pytest.mark.parametrize(
    ("dep", "ret", "dashes"),
    [
        pytest.param("2026-11-08", "2026-11-06", 1, id="return-before-departure"),
        pytest.param("2026-11-08", "2026-11-08", 0, id="same-day"),
    ],
)
def test_the_table_shows_a_dash_for_nights_only_when_the_return_precedes_the_departure(
    dep: str, ret: str, dashes: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _explore_body([_info("/m/1", "Austin", dep, ret)], [_fare("/m/1", 150)]))
    result = _explore("JFK")
    assert result.exit_code == 0, result.output
    (row,) = (line for line in result.stdout.splitlines() if "Austin" in line)
    assert "USD150.00" in row
    assert row.count("—") == dashes
    assert " -2 " not in row
