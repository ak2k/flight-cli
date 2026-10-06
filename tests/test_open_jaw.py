# pyright: reportPrivateUsage=false
"""`_open_jaw.combine`: a multi-city trip's one-way boards, one per slice,
combined into the cheapest runs flyable in order, each total in one currency."""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_cli import _open_jaw
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
    assert (combos[0].total, combos[0].currency) == (861.0, "USD")


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


# ─────────────────────────── three or more boards ───────────────────────────

_AIRPORTS = ("SFO", "ORD", "BOS", "MIA")


@st.composite
def _boards(draw: st.DrawFn) -> list[list[Any]]:
    """Two to four boards of up to six rows, unordered, whose fares tie often
    and whose airports and times make some runs flyable and some not."""
    day = dt.date.today() + dt.timedelta(days=30)
    boards: list[list[Any]] = []
    for i in range(draw(st.integers(2, 4))):
        frm, to = draw(st.sampled_from([(a, b) for a in _AIRPORTS for b in _AIRPORTS if a != b]))
        rows = [
            _row(
                f"UA{i:d}{j:d}",
                frm,
                to,
                day + dt.timedelta(days=draw(st.integers(0, 3))),
                draw(st.sampled_from([100.10, 150.15, 200.20, 250.25, 300.0])),
                currency=draw(st.sampled_from(["USD", "USD", "USD", "", "EUR"])),
                at=dt.time(draw(st.integers(0, 23)), 0),
                hours=draw(st.integers(1, 8)),
            )
            for j in range(draw(st.integers(0, 6)))
        ]
        boards.append(rows)
    return boards


def _product(boards: list[list[Any]], *, limit: int, cap: int | None) -> list[Combination]:
    """Every flyable combination in USD, ranked by total and then board
    position, cut to `limit`: what `combine` answers without building this."""
    found: list[tuple[int, tuple[int, ...], tuple[Any, ...]]] = []
    for at in itertools.product(*(range(len(b)) for b in boards)):
        picked = tuple(b[j] for b, j in zip(boards, at, strict=True))
        if any((r.flight.currency or "USD") != "USD" for r in picked):
            continue
        if not all(flyable(a, b) for a, b in itertools.pairwise(picked)):
            continue
        total = sum(round(r.flight.price * 100) for r in picked)
        if cap is None or total <= cap * 100:
            found.append((total, at, picked))
    found.sort(key=lambda f: (f[0], f[1]))
    return [Combination(picked, total, "USD") for total, _, picked in found[:limit]]


def _which(combos: list[Combination]) -> list[tuple[int, tuple[int, ...]]]:
    return [(c.total_cents, tuple(map(id, c.tickets))) for c in combos]


@settings(deadline=None, max_examples=300)
@given(
    boards=_boards(),
    limit=st.integers(1, 8),
    cap=st.sampled_from([None, None, 400, 600, 900]),
)
def test_n_boards_answer_as_their_whole_product_would(
    boards: list[list[Any]], limit: int, cap: int | None
) -> None:
    """Red at the base, whose `combine` took two boards."""
    got = combine(*boards, currency="USD", limit=limit, cap=cap)
    assert _which(got) == _which(_product(boards, limit=limit, cap=cap))
    assert all(len(c.tickets) == len(boards) for c in got)


def test_three_full_boards_cost_far_less_than_their_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three 300-row boards whose cheapest middle tickets all leave before the
    first ticket lands: 27,000,000 combinations, answered in under 200,000
    `flyable` calls. Red at the base, whose `combine` took two boards."""
    day = dt.date.today() + dt.timedelta(days=30)
    first = [_row(f"UA{i:d}", "SFO", "ORD", day, 100.0 + i, at=dt.time(10, 0)) for i in range(300)]
    middle = [
        _row(f"AA{i:d}", "ORD", "BOS", day, 50.0 + i, at=dt.time(8 if i < 200 else 15, 0))
        for i in range(300)
    ]
    last = [
        _row(f"B6{i:d}", "BOS", "SFO", day + dt.timedelta(days=4), 200.0 + i) for i in range(300)
    ]
    calls = 0

    def counted(a: Any, b: Any) -> bool:
        nonlocal calls
        calls += 1
        return flyable(a, b)

    monkeypatch.setattr(_open_jaw, "flyable", counted)
    combos = combine(first, middle, last, currency="USD", limit=10)
    assert calls < 200_000
    # The cheapest middle ticket that leaves after the first lands is AA200.
    assert [tuple(map(id, c.tickets)) for c in combos[:4]] == [
        (id(first[0]), id(middle[200]), id(last[0])),
        (id(first[0]), id(middle[200]), id(last[1])),
        (id(first[0]), id(middle[201]), id(last[0])),
        (id(first[1]), id(middle[200]), id(last[0])),
    ]
    assert [c.total_cents for c in combos[:4]] == [55000, 55100, 55100, 55100]


def test_three_boards_tie_in_the_first_boards_order_then_the_next() -> None:
    """Three combinations total 600.00, and the cheapest row of the first board
    is its second, so board order and price order disagree. Red at the base,
    whose `combine` took two boards."""
    out = dt.date.today() + dt.timedelta(days=30)
    first = [_row("UA2", "SFO", "ORD", out, 250.0), _row("UA1", "SFO", "ORD", out, 200.0)]
    middle = [
        _row("AA1", "ORD", "BOS", out + dt.timedelta(days=1), 150.0),
        _row("AA2", "ORD", "BOS", out + dt.timedelta(days=1), 200.0),
    ]
    last = [
        _row("B61", "BOS", "SFO", out + dt.timedelta(days=2), 250.0),
        _row("B62", "BOS", "SFO", out + dt.timedelta(days=2), 200.0),
    ]
    combos = combine(first, middle, last, currency="USD", limit=10)
    named = [
        "/".join(
            f"{r.flight.legs[0].airline.name}{r.flight.legs[0].flight_number}" for r in c.tickets
        )
        for c in combos
    ]
    assert [c.total_cents for c in combos] == [
        55000,
        60000,
        60000,
        60000,
        65000,
        65000,
        65000,
        70000,
    ]
    assert named[1:4] == ["UA2/AA1/B62", "UA1/AA1/B61", "UA1/AA2/B62"]
