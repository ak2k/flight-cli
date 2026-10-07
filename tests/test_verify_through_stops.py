# pyright: reportPrivateUsage=false
"""A stop inside one through flight is not a connection.

Google writes a through flight as one leg or as a leg per stop, and Matrix's
summary lists the stop or not. A solution is a candidate when its connections
(the stops between two different flight numbers) are the row's; a stop inside
one flight is left to the booking details, which `same_flights` compares at the
flight's ends."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from flight_cli import _verify as v
from test_answer_read_whole import (
    _Answer,
    _booked,
    _low_check_path,
    _slice,
    _summary,
    _through,
    _verify_path,
)
from test_verify import _answer, _chain, _flight, _row, _solution

if TYPE_CHECKING:
    import pathlib

_WHOLE = _row((_flight("XX1", "JFK", "LAX", "2026-10-20T08:00", "2026-10-20T13:00"),))
_SPLIT = _row(
    (
        _flight("XX1", "JFK", "DEN", "2026-10-20T08:00", "2026-10-20T10:00"),
        _flight("XX1", "DEN", "LAX", "2026-10-20T11:00", "2026-10-20T13:00"),
    )
)
_THEN_THROUGH = _row(
    (
        _flight("YY2", "JFK", "ORD", "2026-10-20T08:00", "2026-10-20T09:30"),
        _flight("XX1", "ORD", "LAX", "2026-10-20T11:00", "2026-10-20T13:00"),
    )
)


def _candidates(row: v.Row, flights: list[str], stops: list[str]) -> list[int]:
    sol = _solution(
        "S-1", "USD1.00", "2026-10-20T08:00-04:00", "2026-10-20T13:00-07:00", flights, stops
    )
    return v.candidates(row, _answer(sol))


@pytest.mark.parametrize(
    ("row", "flights", "stops", "want"),
    [
        pytest.param(
            _WHOLE, ["XX1", "XX1"], ["DEN"], [0], id="row one leg, summary lists the stop"
        ),
        pytest.param(_SPLIT, ["XX1"], [], [0], id="row a leg per stop, summary lists none"),
        pytest.param(_WHOLE, ["XX1", "XX1"], [], [0], id="summary lists a flight it gives no stop"),
        pytest.param(
            _SPLIT, ["XX1"], ["DEN"], [0], id="summary writes the flight once and lists its stop"
        ),
        pytest.param(
            _THEN_THROUGH, ["YY2", "XX1", "XX1"], ["ORD", "DEN"], [0], id="connection then through"
        ),
        pytest.param(
            _THEN_THROUGH, ["YY2", "XX1", "XX1"], ["PDX", "DEN"], [], id="another connection"
        ),
        pytest.param(_THEN_THROUGH, ["YY2", "XX1", "XX1"], ["DEN"], [], id="connection left out"),
    ],
)
def test_only_the_stops_between_two_flight_numbers_must_match_the_rows(
    row: v.Row, flights: list[str], stops: list[str], want: list[int]
) -> None:
    assert _candidates(row, flights, stops) == want


def _through_answer() -> _Answer:
    """The row Google writes as one leg, Matrix's summary listing its stop and
    its booking details flying it as two legs."""
    return _Answer(
        _WHOLE,
        _chain(
            _summary(
                "XX-1",
                _slice(
                    "JFK",
                    "LAX",
                    "2026-10-20T08:00-04:00",
                    "2026-10-20T13:00-07:00",
                    ["XX1", "XX1"],
                    ["DEN"],
                ),
            )
        ),
        {
            "XX-1": _booked(
                [
                    _through(
                        "XX1",
                        ("JFK", "DEN", "2026-10-20T08:00-04:00", "2026-10-20T10:00-06:00"),
                        ("DEN", "LAX", "2026-10-20T11:00-06:00", "2026-10-20T13:00-07:00"),
                    )
                ]
            )
        },
        "XX-1",
    )


def test_a_through_flight_written_as_one_leg_is_the_row_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    answer = _through_answer()
    assert _verify_path(answer, monkeypatch, tmp_path) == ("match", None)
    assert _low_check_path(answer, tmp_path) == ("match", None)
