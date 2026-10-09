"""`cheapest_dates` picks the K cheapest priced dates of a calendar's rows.

The rows are envelope calendar rows, so a date is a row's `departure` and
`return`. No network and no CLI: the function only orders and trims rows.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from flight_cli import _calendar_split
from flight_cli._envelope import CalendarRow

if TYPE_CHECKING:
    from collections.abc import Callable


def _row(price: float | None, departure: date | None, back: date | None = None) -> CalendarRow:
    return CalendarRow.model_validate(
        {"price": price, "currency": "USD", "row": {}, "departure": departure, "return": back}
    )


def _departures(rows: list[CalendarRow]) -> list[date | None]:
    return [r.departure for r in rows]


def _pick(rows: list[CalendarRow], k: int) -> list[CalendarRow]:
    found: Callable[[list[CalendarRow], int], list[CalendarRow]] | None = getattr(
        _calendar_split, "cheapest_dates", None
    )
    assert found is not None, "_calendar_split.cheapest_dates does not exist"
    return found(rows, k)


_OCT = [date(2026, 10, d) for d in range(19, 25)]


def test_the_k_cheapest_skip_an_unpriced_day_and_break_a_tie_by_earlier_date() -> None:
    rows = [
        _row(300, _OCT[1]),
        _row(None, _OCT[2]),
        _row(200, _OCT[3]),
        _row(200, _OCT[4]),
        _row(250, _OCT[5]),
        _row(200, _OCT[0]),
    ]
    assert _departures(_pick(rows, 3)) == [_OCT[0], _OCT[3], _OCT[4]]
    assert _departures(_pick(rows, 4)) == [_OCT[0], _OCT[3], _OCT[4], _OCT[5]]


def test_fewer_priced_days_than_k_returns_them_all_cheapest_first() -> None:
    rows = [_row(None, _OCT[0]), _row(90, _OCT[2]), _row(80, _OCT[1])]
    assert _departures(_pick(rows, 5)) == [_OCT[1], _OCT[2]]


def test_a_nonpositive_k_picks_nothing() -> None:
    rows = [_row(90, _OCT[0]), _row(80, _OCT[1])]
    assert _pick(rows, 0) == []
    assert _pick(rows, -1) == []


def test_a_row_with_no_departure_is_skipped() -> None:
    rows = [_row(10, None), _row(20, _OCT[0])]
    assert _departures(_pick(rows, 2)) == [_OCT[0]]


def test_a_non_finite_price_is_skipped() -> None:
    rows = [_row(float("nan"), _OCT[0]), _row(float("inf"), _OCT[1]), _row(20, _OCT[2])]
    assert _departures(_pick(rows, 3)) == [_OCT[2]]


def test_one_departure_priced_at_two_returns_ties_by_earlier_return_and_none_last() -> None:
    rows = [
        _row(100, _OCT[0], None),
        _row(100, _OCT[0], date(2026, 10, 30)),
        _row(100, _OCT[0], date(2026, 10, 26)),
    ]
    assert [r.return_date for r in _pick(rows, 3)] == [date(2026, 10, 26), date(2026, 10, 30), None]
