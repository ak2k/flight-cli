# pyright: reportPrivateUsage=false
"""`flight explore` reads a negative stop count or duration as it reads a missing
one: unknown, shown as a dash, while the destination keeps its fare."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from typing import Any

__all__ = ["_fixed_today_and_wide_consoles"]


def _body(stops: int, minutes: int) -> str:
    fare = _fare("/m/1", 150)
    fare[6][2:4] = [stops, minutes]
    return _explore_body([_info("/m/1", "Austin")], [fare])


def test_a_negative_stop_count_and_duration_leave_the_priced_destination_unknown() -> None:
    (austin,) = ge.parse_destinations(_body(-1, -5), origin="JFK", month=None)
    assert (austin.price, austin.stops, austin.duration_min) == (150, None, None)


def test_json_shows_a_negative_stop_count_and_duration_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _body(-1, -5))
    result = _explore("JFK", "--format", "json")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    assert (row["price"], row["stops"], row["duration_min"]) == (150, None, None)


def test_the_table_shows_a_dash_for_a_negative_stop_count_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _body(-1, -5))
    result = _explore("JFK")
    assert result.exit_code == 0, result.output
    assert "USD150.00" in result.stdout
    assert "-1 stop" not in result.stdout
    assert "-1h55m" not in result.stdout
    (row,) = (line for line in result.stdout.splitlines() if "Austin" in line)
    assert row.count("—") == 2


@pytest.mark.parametrize(("stops", "minutes"), [(0, 0), (2, 95)])
def test_a_zero_or_positive_count_is_kept(stops: int, minutes: int) -> None:
    (austin,) = ge.parse_destinations(_body(stops, minutes), origin="JFK", month=None)
    assert (austin.stops, austin.duration_min) == (stops, minutes)


def test_count_refuses_only_a_negative_int() -> None:
    values: list[Any] = [-1, True, "2", None, 1.5]
    assert [ge._count(v) for v in values] == [None] * 5
