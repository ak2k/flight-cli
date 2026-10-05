# pyright: reportPrivateUsage=false
"""`_open_jaw.combine`: an open jaw's one-way boards paired into the cheapest
combinations flyable in order, each total in one currency."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from flight_cli._open_jaw import Combination, combine, flyable
from test_split_ticket import _row


def _days() -> tuple[dt.date, dt.date]:
    """An outbound day and the return day a week later, both ahead of today."""
    out = dt.date.today() + dt.timedelta(days=45)
    return out, out + dt.timedelta(days=7)


def _flights(combos: list[Combination]) -> list[tuple[str, str, int]]:
    return [
        (
            f"{a.flight.legs[0].airline.name}{a.flight.legs[0].flight_number}",
            f"{b.flight.legs[0].airline.name}{b.flight.legs[0].flight_number}",
            c.total_cents,
        )
        for c in combos
        for a, b in (c.tickets,)
    ]


def test_pairs_come_cheapest_total_first() -> None:
    out, back = _days()
    first = [_row("BA1", "JFK", "LHR", out, 295.0), _row("BA2", "JFK", "LHR", out, 400.0)]
    second = [_row("AF1", "CDG", "JFK", back, 566.0), _row("AF2", "CDG", "JFK", back, 600.0)]
    combos = combine(first, second, currency="USD", limit=10)
    assert _flights(combos) == [
        ("BA1", "AF1", 86100),
        ("BA1", "AF2", 89500),
        ("BA2", "AF1", 96600),
        ("BA2", "AF2", 100000),
    ]
    assert [(c.total, c.currency) for c in combos][0] == (861.0, "USD")


def test_equal_totals_keep_the_first_boards_order_then_the_seconds() -> None:
    """Two totals that are the same number of cents tie, whatever the floats
    they were summed from: 100.10 + 200.20 and 150.15 + 150.15."""
    out, back = _days()
    first = [_row("BA1", "JFK", "LHR", out, 100.10), _row("BA2", "JFK", "LHR", out, 150.15)]
    second = [_row("AF1", "CDG", "JFK", back, 150.15), _row("AF2", "CDG", "JFK", back, 200.20)]
    combos = combine(first, second, currency="USD", limit=10)
    assert _flights(combos) == [
        ("BA1", "AF1", 25025),
        ("BA1", "AF2", 30030),
        ("BA2", "AF1", 30030),
        ("BA2", "AF2", 35035),
    ]


@pytest.mark.parametrize(
    ("leaves", "ok"),
    [
        pytest.param(dt.time(9, 0), False, id="as-it-lands"),
        pytest.param(dt.time(9, 1), True, id="a-minute-after"),
        pytest.param(dt.time(8, 59), False, id="a-minute-before"),
    ],
)
def test_from_the_airport_it_lands_at_the_second_leaves_after_the_first_lands(
    leaves: dt.time, ok: bool
) -> None:
    out, _ = _days()
    first = _row("BA1", "JFK", "LHR", out, 300.0, at=dt.time(7, 0))  # lands 09:00
    second = _row("BA9", "LHR", "JFK", out, 300.0, at=leaves)
    assert flyable(first, second) is ok
    assert len(combine([first], [second], currency="USD", limit=5)) == int(ok)


@pytest.mark.parametrize(
    ("days_later", "leaves", "ok"),
    [
        pytest.param(0, dt.time(23, 0), False, id="same-day-late"),
        pytest.param(1, dt.time(0, 5), True, id="next-day-early"),
    ],
)
def test_from_another_airport_the_second_leaves_on_a_later_day(
    days_later: int, leaves: dt.time, ok: bool
) -> None:
    """Fourteen hours after landing at LHR is not enough on the same day, five
    minutes past midnight is: the rows state no offset, so a day is the unit."""
    out, _ = _days()
    first = _row("BA1", "JFK", "LHR", out, 300.0, at=dt.time(7, 0))  # lands 09:00
    second = _row("AF1", "CDG", "JFK", out + dt.timedelta(days=days_later), 300.0, at=leaves)
    assert flyable(first, second) is ok
    assert len(combine([first], [second], currency="USD", limit=5)) == int(ok)


def test_a_first_ticket_landing_after_midnight_counts_its_landing_day() -> None:
    out, _ = _days()
    red_eye = _row("BA1", "JFK", "LHR", out, 300.0, at=dt.time(22, 0), hours=7)  # lands +1d 05:00
    next_day = _row("AF1", "CDG", "JFK", out + dt.timedelta(days=1), 300.0, at=dt.time(20, 0))
    day_after = _row("AF2", "CDG", "JFK", out + dt.timedelta(days=2), 300.0, at=dt.time(6, 0))
    assert not flyable(red_eye, next_day)
    assert flyable(red_eye, day_after)


@pytest.mark.parametrize(
    ("cap", "kept"),
    [pytest.param(861, 1, id="at-the-cap"), pytest.param(860, 0, id="a-dollar-under")],
)
def test_no_pair_over_the_cap(cap: int, kept: int) -> None:
    out, back = _days()
    first = [_row("BA1", "JFK", "LHR", out, 295.0)]
    second = [_row("AF1", "CDG", "JFK", back, 566.0)]
    assert len(combine(first, second, currency="USD", limit=5, cap=cap)) == kept


def test_a_cap_keeps_the_cheapest_under_it_not_the_first_found() -> None:
    out, back = _days()
    first = [_row("BA1", "JFK", "LHR", out, 295.0), _row("BA2", "JFK", "LHR", out, 700.0)]
    second = [_row("AF1", "CDG", "JFK", back, 566.0), _row("AF2", "CDG", "JFK", back, 100.0)]
    combos = combine(first, second, currency="USD", limit=5, cap=861)
    assert _flights(combos) == [("BA1", "AF2", 39500), ("BA2", "AF2", 80000), ("BA1", "AF1", 86100)]


def test_at_most_limit_pairs() -> None:
    out, back = _days()
    first = [_row(f"BA{i}", "JFK", "LHR", out, 300.0 + i) for i in range(1, 6)]
    second = [_row(f"AF{i}", "CDG", "JFK", back, 500.0 + i) for i in range(1, 6)]
    combos = combine(first, second, currency="USD", limit=3)
    assert _flights(combos) == [("BA1", "AF1", 80200), ("BA1", "AF2", 80300), ("BA2", "AF1", 80300)]


@pytest.mark.parametrize("empty", ["first", "second"])
def test_an_empty_board_pairs_nothing(empty: str) -> None:
    out, back = _days()
    board: dict[str, list[Any]] = {
        "first": [_row("BA1", "JFK", "LHR", out, 295.0)],
        "second": [_row("AF1", "CDG", "JFK", back, 566.0)],
    }
    board[empty] = []
    assert combine(board["first"], board["second"], currency="USD", limit=5) == []


def test_a_row_in_another_currency_is_never_summed() -> None:
    """The EUR fare is the cheapest number on its board and is passed over; a
    row that names no currency is in the one Google was asked for."""
    out, back = _days()
    first = [
        _row("BA1", "JFK", "LHR", out, 100.0, currency="EUR"),
        _row("BA2", "JFK", "LHR", out, 295.0),
    ]
    second = [_row("AF1", "CDG", "JFK", back, 566.0, currency="")]
    combos = combine(first, second, currency="USD", limit=5)
    assert _flights(combos) == [("BA2", "AF1", 86100)]
    assert {c.currency for c in combos} == {"USD"}
