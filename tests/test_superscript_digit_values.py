# pyright: reportPrivateUsage=false
"""A superscript digit in a Google seat pitch or a Matrix day price reads as no
value. `str.isdigit` accepts it, and `int` and `float` then raise out of the
parser, so one odd cell would fail the whole search or table."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from flight_cli._gflight_ids import _LEG_PITCH_IDX, _parse_leg_amenities, _parse_pitch
from flight_cli.models import CalendarDay
from test_calendar_formats import _BODY, _GOOGLE_7N, _ROUTE, _graph, _graphs, _run, _serve

if TYPE_CHECKING:
    import pytest


def _day(price: str | None) -> CalendarDay:
    return CalendarDay.model_validate({"date": 1, "minPrice": price})


def test_a_pitch_with_a_superscript_or_overlong_digit_run_is_none() -> None:
    assert _parse_pitch("31 in") == 31
    assert _parse_pitch("² in") is None
    assert _parse_pitch("1" * 5000 + " in") is None
    assert _parse_pitch("² 31 in") == 31


def test_a_leg_with_a_superscript_pitch_keeps_its_other_fields() -> None:
    leg: list[object] = [None] * (_LEG_PITCH_IDX + 1)
    leg[_LEG_PITCH_IDX] = "² in"
    assert _parse_leg_amenities(leg).pitch_inches is None


def test_a_day_price_with_a_superscript_digit_has_no_price_value() -> None:
    assert _day("USD595.00").price_value == 595.0
    assert _day("USD²595.00").price_value is None
    assert _day(None).price_value is None


def test_the_calendar_table_survives_a_day_priced_with_a_superscript_digit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = copy.deepcopy(_BODY)
    odd = next(
        day
        for week in body["calendar"]["months"][0]["weeks"]
        for day in week["days"]
        if day.get("minPrice") and not day.get("disabled")
    )
    odd["minPrice"] = "USD²595.00"
    _serve(monkeypatch, body)
    _graphs(monkeypatch, {7: _graph(7, {d: float(p) for d, p in _GOOGLE_7N.items()})})
    result = _run(*_ROUTE)
    assert result.exception is None, result.output
    assert result.exit_code == 0, result.output
