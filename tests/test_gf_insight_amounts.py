# pyright: reportPrivateUsage=false
"""A price insight or history amount that is not a finite number.

Google's JSON can hold an integer too large for a float, `1e400` (read as
infinity) or `NaN`. Such a figure is one the page cannot state: the block that
carries it reads as absent, and the rows, the other block and the exit status
are as the page gives them without it.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import pytest

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from test_envelope import (
    _SEARCH,
    _envelope_of,
    _hermetic,  # noqa: F401 # pyright: ignore[reportUnusedImport] — Matrix in process
    _rows,
    _run,
)
from test_gf_full_board import _DEP, _LAX, _served

if TYPE_CHECKING:
    from collections.abc import Callable

_URL = "https://www.google.com/travel/flights?tfs=abc"
_ONE_WAY = [
    *("--cash-only", "JFK", "LAX", "--dep", _DEP.isoformat()),
    *("--backend", "gflight", "--fast", "--format", "envelope"),
]
# Where the page states each amount, and the `Board` attribute it feeds beside
# the one it does not: `ds:1[5][1]`, `[4]` and `[5]` are the insight's pairs,
# `[5][10][0]` the history's `[epoch_ms, price]` points.
_AMOUNTS = {
    "cheapest": ((5, 1, 1), "insight", "history"),
    "typical_low": ((5, 4, 1), "insight", "history"),
    "typical_high": ((5, 5, 1), "insight", "history"),
    "first_point": ((5, 10, 0, 0, 1), "history", "insight"),
    "last_point": ((5, 10, 0, -1, 1), "history", "insight"),
}
_VALUES = {"beyond_a_float": 10**400, "infinite": math.inf, "not_a_number": math.nan}


def _payload_with(path: tuple[int, ...], value: float) -> list[Any]:
    payload: list[Any] = json.loads(_ds1(_LAX))
    holder: Any = payload
    for index in path[:-1]:
        holder = holder[index]
    holder[path[-1]] = value
    return payload


def _board(payload: list[Any]) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(json.dumps(payload)), _URL, 200))


@pytest.mark.parametrize("value", _VALUES.values(), ids=list(_VALUES))
@pytest.mark.parametrize(("path", "field", "other"), _AMOUNTS.values(), ids=list(_AMOUNTS))
def test_an_amount_that_is_not_finite_states_none_of_its_block(
    path: tuple[int, ...], field: str, other: str, value: float
) -> None:
    whole = _board(json.loads(_ds1(_LAX)))
    board = _board(_payload_with(path, value))
    assert getattr(whole, field) is not None
    assert getattr(board, field) is None
    assert getattr(board, other) == getattr(whole, other)
    assert list(board) == list(whole)


@pytest.mark.parametrize(
    ("path", "key", "other"),
    [
        (_AMOUNTS["cheapest"][0], "insight", "price_history"),
        (_AMOUNTS["first_point"][0], "price_history", "insight"),
    ],
    ids=["insight", "history"],
)
@pytest.mark.parametrize("value", _VALUES.values(), ids=list(_VALUES))
def test_the_search_answers_whole_without_the_amount_it_cannot_read(
    gf_session: Callable[..., Any], path: tuple[int, ...], key: str, other: str, value: float
) -> None:
    """Exit 0 with every row, the other block as served, and the amount's own
    key empty: a search does not fail on a figure."""
    gf_session(_served(_LAX))
    whole = _envelope_of(_run(*_SEARCH, *_ONE_WAY))
    served = _page(
        _answering(
            json.dumps(_payload_with(path, value)),
            origin=None,
            destination=None,
            date=_DEP.isoformat(),
        )
    )
    gf_session(served)
    env = _envelope_of(_run(*_SEARCH, *_ONE_WAY))
    assert whole[key] != []
    assert env[key] == []
    assert env[other] == whole[other]
    assert _rows(env) == _rows(whole)
    assert env["complete"] == whole["complete"]
