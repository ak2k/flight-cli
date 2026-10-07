# pyright: reportPrivateUsage=false
"""The low check compares Google's price with Matrix's in the same cabin.

A row's Google price is its cheapest listing's, and a listing can book a leg in
a cabin other than the one searched, while Matrix is asked in the searched
cabin. Such a row is passed over for the next one under Matrix's low. Google
serves the captured JFK-LAX board re-dated to `_DEP`; Matrix's default answer
is one B6999 trip at USD999, and the board's rows tie at USD204."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import _verify
from test_low_check import _LINE, _b6, _chain_text, _install, _price, _routed, _run, _under
from test_verify import _DEP, _booked, _chain, _details_of, _Matrix, _row_solution, _served

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

_FIRST = 4
_BUSINESS = 3


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()
    _install(monkeypatch, tmp_path, fake, fake.handler)
    return fake


def _board_with_cabin(cabin: int | None, *, only_row_one: bool) -> str:
    """The served board with leg 1 of row 1's raw listing, or of every raw
    listing, booked in `cabin` (a `_gflight_ids._CABIN` code, None for none)."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_tfu.json"))
    rows = gfid._rows_from_ds1(payload).rows
    row_one = _booked(_two_cheapest()[0])
    for raw in rows:
        if not only_row_one or _booked(gfid._parse_flight_with_id(raw)) == row_one:
            raw[0][2][0][gfid._LEG_CABIN_IDX] = cabin
    return _page(_answering(json.dumps(payload), origin=None, destination=None, date=str(_DEP)))


def _two_cheapest() -> tuple[Any, Any]:
    """Rows 1 and 2 of the merged table: the board's two lowest prices, in
    board order among equals."""
    from flight_cli._gf_common import PageFetch

    listed = list(gfid._rows_from_page_html(PageFetch(_served(), "https://x.test/", 200)))
    first, second, *_ = sorted(
        (r for r in listed if r.flight.price is not None), key=lambda r: r.flight.price or 0.0
    )
    return first, second


def _answer_row(matrix: _Matrix, row: Any) -> None:
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-1", _price(row), row))
    matrix.details = {"DL-1": _details_of(row)}


def test_a_row_whose_cheapest_listing_has_a_first_class_leg_is_not_the_checked_row(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Red at the base: the line names row 1's flights and Matrix is asked for
    them in economy. Row 1's listing books a leg in first; the check takes row
    2, which is all economy."""
    one, two = _two_cheapest()
    assert _booked(one) != _booked(two)
    _answer_row(matrix, two)
    gf_session(_board_with_cabin(_FIRST, only_row_one=True))
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    line = f"{_LINE}2's flights ({_chain_text(two)}): Matrix {_price(two)} · Google {_price(two)}"
    assert line in under, under
    assert f"{_LINE}1's" not in under
    (chain,) = _routed(matrix)
    assert chain["inputs"]["slices"][0]["routeLanguage"] == _booked(two).replace("+", " ")

    gf_session(_board_with_cabin(_FIRST, only_row_one=True))
    result = _run("-n", "10", "--enrich", "--format", "json")
    assert result.exit_code == 0, result.output
    low_check = json.loads(result.stdout)["cross_check"]["low_check"]
    assert (low_check["row"], low_check["outcome"]) == (2, "match")
    assert low_check["routing"] == _verify.routings(_verify.google_row(two))


def test_a_board_booked_wholly_outside_the_searched_cabin_asks_matrix_nothing_more(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Red at the base: row 1 is asked. Every listing books a leg in first, so
    no row is comparable: no line, no `low_check`, and Matrix's one search."""
    one, _ = _two_cheapest()
    _answer_row(matrix, one)
    gf_session(_board_with_cabin(_FIRST, only_row_one=False))
    result = _run("-n", "10", "--enrich", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["cross_check"]["low_check"] is None
    assert len(matrix.searches()) == 1


@pytest.mark.parametrize("cabin", [None, 1], ids=["none-stated", "economy"])
def test_a_leg_that_states_no_other_cabin_leaves_the_row_checked(
    cabin: int | None, gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Guard: green at the base. A leg Google states no cabin for, or states
    as economy, does not show the row priced in another cabin."""
    one, _ = _two_cheapest()
    _answer_row(matrix, one)
    gf_session(_board_with_cabin(cabin, only_row_one=True))
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    assert f"{_LINE}1's flights ({_chain_text(one)})" in _under(result.stdout)


def test_the_searched_cabin_is_the_one_a_listing_is_held_to(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Guard: green at the base. Searched in business, row 1 booked in business
    is checked, and row 2, booked in economy, is the one passed over."""
    one, _ = _two_cheapest()
    _answer_row(matrix, one)
    gf_session(_board_with_cabin(_BUSINESS, only_row_one=True))
    result = _run("-n", "10", "--cabin", "business")
    assert result.exit_code == 0, result.output
    assert f"{_LINE}1's flights ({_chain_text(one)})" in _under(result.stdout)
