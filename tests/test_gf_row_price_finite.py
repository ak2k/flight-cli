# pyright: reportPrivateUsage=false
"""A flight row whose price is not a finite number.

Google's JSON can hold an integer too large for a float, `1e400` (read as
infinity), `Infinity` or `NaN` at a row's price. Such a row states no fare the
search can show: it is left out and counted in the board's `unread`, like a row
whose layout does not parse, and no output carries a non-JSON `Infinity` or a
`USDinf` cell.
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
    *("--backend", "gflight", "--fast", "-n", "200"),
]
_VALUES = {
    "beyond_a_float": 10**400,
    "infinite": math.inf,
    "negative_infinite": -math.inf,
    "not_a_number": math.nan,
}


def _payload_with_first_row_priced(value: float) -> str:
    """The captured JFK-LAX payload with the first row's price at `value`:
    `row[1]` is the price block, `row[1][0]` its head and `[-1]` the amount."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    gfid._rows_from_ds1(payload).rows[0][1][0][-1] = value
    return json.dumps(payload)


def _board(payload: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(payload), _URL, 200))


def _strict(text: str) -> Any:
    def refuse(token: str) -> Any:
        raise AssertionError(f"non-JSON number {token} in the document")

    return json.loads(text, parse_constant=refuse)


@pytest.mark.parametrize("value", _VALUES.values(), ids=list(_VALUES))
def test_a_row_priced_beyond_a_finite_number_is_left_out_and_counted(value: float) -> None:
    whole = _board(_ds1(_LAX))
    board = _board(_payload_with_first_row_priced(value))
    assert whole.unread == 0
    assert (len(board), board.unread) == (len(whole) - 1, 1)
    assert all(row.flight.price is None or math.isfinite(row.flight.price) for row in board)


@pytest.mark.parametrize("value", _VALUES.values(), ids=list(_VALUES))
def test_the_search_answers_without_the_row_and_says_so(
    gf_session: Callable[..., Any], value: float
) -> None:
    """Exit 0, every other row as served, the unread note, and a document a
    strict JSON reader accepts."""
    gf_session(_served(_LAX))
    whole = _envelope_of(_run(*_SEARCH, *_ONE_WAY, "--format", "envelope"))
    gf_session(
        _page(
            _answering(
                _payload_with_first_row_priced(value),
                origin=None,
                destination=None,
                date=_DEP.isoformat(),
            )
        )
    )
    result = _run(*_SEARCH, *_ONE_WAY, "--format", "envelope")
    env = _envelope_of(result)
    _strict(result.stdout)
    assert len(_rows(env)) == len(_rows(whole)) - 1
    assert any("could not be read" in n for n in env["notes"]), env["notes"]


def test_the_json_and_table_formats_show_no_infinite_price(gf_session: Callable[..., Any]) -> None:
    gf_session(
        _page(
            _answering(
                _payload_with_first_row_priced(math.inf),
                origin=None,
                destination=None,
                date=_DEP.isoformat(),
            )
        )
    )
    as_json = _run(*_SEARCH, *_ONE_WAY, "--format", "json")
    assert as_json.exit_code == 0, as_json.output
    _strict(as_json.stdout)
    as_table = _run(*_SEARCH, *_ONE_WAY, "--format", "table")
    assert as_table.exit_code == 0, as_table.output
    assert "USDinf" not in as_table.stdout
