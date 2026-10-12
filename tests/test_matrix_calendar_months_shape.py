"""A Matrix calendar answer whose `calendar.months` is present but not a list is
a shape change, not an empty grid."""

from __future__ import annotations

import pytest

from flight_cli.models import CalendarResult


@pytest.mark.parametrize("months", [{}, "", 0, False], ids=repr)
def test_a_months_that_is_present_and_not_a_list_is_refused(months: object) -> None:
    with pytest.raises(ValueError, match="months"):
        CalendarResult.from_api({"solutionCount": 0, "calendar": {"months": months}})


@pytest.mark.parametrize("calendar", [{}, {"months": None}, {"months": []}], ids=repr)
def test_an_absent_null_or_empty_months_is_still_an_empty_grid(
    calendar: dict[str, object],
) -> None:
    assert CalendarResult.from_api({"solutionCount": 0, "calendar": calendar}).days == []
