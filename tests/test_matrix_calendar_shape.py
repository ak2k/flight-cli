"""A Matrix calendar answer whose `calendar` is present but not an object is a
shape change, kept and reported, not an unpriced grid."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.client import REPORT_URL, MatrixShapeError
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg
from flight_cli.models import CalendarResult
from test_matrix_shape_capture import (
    _execute,  # pyright: ignore[reportPrivateUsage]
    _matrix_answers,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    import pathlib

_START = dt.date.today() + dt.timedelta(days=30)
_END = _START + dt.timedelta(days=7)


def _search() -> CalendarSearch:
    window = CalendarWindow(start=_START, end=_END, duration_min=0, duration_max=0)
    return CalendarSearch(legs=(Leg.of("JFK", "LAX"),), window=window)


def test_a_calendar_answer_whose_calendar_is_a_list_is_a_shape_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    body: dict[str, object] = {"solutionCount": 0, "calendar": []}
    _matrix_answers(monkeypatch, tmp_path, body)

    with pytest.raises(MatrixShapeError) as caught:
        _execute(_search())

    err = caught.value
    assert err.detail.startswith("Matrix's answer does not parse at calendar: ")
    assert err.captured is not None
    assert err.captured.parent == tmp_path / "shape-changes"
    assert json.loads(err.captured.read_text()) == body
    assert REPORT_URL in str(err)


@pytest.mark.parametrize("calendar", [["x"], "months", 0, False], ids=repr)
def test_any_calendar_that_is_present_and_not_an_object_is_refused(calendar: object) -> None:
    with pytest.raises(ValueError, match="calendar"):
        CalendarResult.from_api({"calendar": calendar})


@pytest.mark.parametrize("body", [{}, {"calendar": None}, {"calendar": {}}], ids=repr)
def test_an_absent_null_or_empty_calendar_is_still_an_empty_grid(body: dict[str, object]) -> None:
    assert CalendarResult.from_api(body).days == []


def test_the_command_exits_1_with_the_report_line_and_no_unpriced_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {"solutionCount": 0, "calendar": []})
    argv = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", _END.isoformat()]

    result = CliRunner().invoke(cli.app, [*argv, "--no-cache", "--gf-transport", "http"])

    assert result.exit_code == 1, result.output
    assert REPORT_URL in result.stderr
    assert "priced no fare" not in result.stderr
    assert list((tmp_path / "shape-changes").glob("matrix-*.json"))
