"""A Matrix answer whose nested list holds a bad element names the element by its
path in the body, as the guard on the list itself does, not by the path inside the
model."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from flight_cli.client import MatrixShapeError, _parsed  # pyright: ignore[reportPrivateUsage]
from flight_cli.models import CalendarResult, SearchResult


def _locs(error: ValidationError) -> list[tuple[int | str, ...]]:
    return [e["loc"] for e in error.errors()]


def test_a_bad_month_is_named_by_its_path_in_the_body() -> None:
    with pytest.raises(ValidationError) as caught:
        CalendarResult.from_api({"calendar": {"months": [{"weeks": 1}]}})

    assert _locs(caught.value) == [("calendar", "months", 0, "weeks")]


def test_an_error_keeps_its_context_when_its_path_moves() -> None:
    with pytest.raises(ValidationError) as caught:
        CalendarResult.from_api({"calendar": {"months": [1]}})

    (error,) = caught.value.errors()
    assert error["loc"] == ("calendar", "months", 0)
    assert error.get("ctx") == {"class_name": "CalendarMonth"}


def test_a_bad_solution_is_named_by_its_path_in_the_body() -> None:
    with pytest.raises(ValidationError) as caught:
        SearchResult.from_api({"solutionList": {"solutions": [1]}})

    assert _locs(caught.value) == [("solutionList", "solutions", 0)]


def test_a_field_outside_the_nested_list_keeps_its_own_path() -> None:
    with pytest.raises(ValidationError) as in_calendar:
        CalendarResult.from_api({"calendar": {"months": []}, "session": 1})
    with pytest.raises(ValidationError) as in_search:
        SearchResult.from_api({"solutionList": {"solutions": []}, "session": 1})

    assert _locs(in_calendar.value) == [("session",)]
    assert _locs(in_search.value) == [("session",)]


@pytest.mark.parametrize(
    ("parse", "body", "where"),
    [
        (
            CalendarResult.from_api,
            {"calendar": {"months": [{"weeks": 1}]}},
            "calendar.months.0.weeks",
        ),
        (SearchResult.from_api, {"solutionList": {"solutions": [1]}}, "solutionList.solutions.0"),
    ],
    ids=["calendar", "search"],
)
def test_the_shape_line_names_the_path_in_the_body(
    parse: Any, body: dict[str, Any], where: str
) -> None:
    with pytest.raises(MatrixShapeError) as caught:
        _parsed(body, parse)

    assert caught.value.detail.startswith(f"Matrix's answer does not parse at {where}: ")
