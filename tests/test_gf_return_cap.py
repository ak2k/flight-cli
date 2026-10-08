# pyright: reportPrivateUsage=false
"""A round trip names the row cap of every page it read.

A return page lists one pin's returns, round-trip totals included, so a page
that stopped at Google's 300 rows may be missing a return priced above its
highest fare, whichever page the outbound is.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import Leg, SearchOptions
from flight_cli.fli_bridge import to_fli_filter
from test_gf_chunked_search import _Google
from test_gf_full_board import _DEP, _RET, _no_matrix
from test_gf_rung_parity import _CAP_LINE, _cap_lines, _ds1, _search

if TYPE_CHECKING:
    from collections.abc import Callable


def _outbound_under_the_cap() -> str:
    """The token page cut to 299 raw rows, one under Google's cap."""
    payload: list[Any] = json.loads(_ds1("ds1_nyc_lon_token"))
    payload[3][0] = payload[3][0][: 299 - len(payload[2][0])]
    ds1 = _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    return _page(ds1)


def _return_at_the_cap() -> str:
    """The 300-row token page re-dated LHR to JFK: every pin's return page."""
    ds1 = _answering(
        _ds1("ds1_nyc_lon_token"), origin="LHR", destination="JFK", date=_RET.isoformat()
    )
    return _page(ds1)


def test_a_round_trip_names_the_cap_of_a_return_page_it_read(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base, which carried the outbound page's cap alone: the
    outbound is 299 rows and every return page is 300, so no line printed."""
    gf_session(_outbound_under_the_cap(), _return_at_the_cap())
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = _search(
        "--return", _RET.isoformat(), "--no-separate-tickets", "--fast", "--format", "envelope"
    )
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert _cap_lines(" ".join(result.stderr.split())) == [_CAP_LINE]
    assert _cap_lines(" ".join(json.loads(result.stdout)["notes"])) == [_CAP_LINE]


def _keeps_the_outbound_only(leg: int, _row: gfid.GFlightWithId) -> bool:
    return leg == 0


def _keeps_every_row(_leg: int, _row: gfid.GFlightWithId) -> bool:
    return True


@pytest.mark.parametrize(
    ("outbound", "returned", "keep", "merged"),
    [
        ({}, {"USD": 900.0}, _keeps_every_row, {"USD": 900.0}),
        ({"USD": 1200.0}, {"USD": 900.0}, _keeps_every_row, {"USD": 900.0}),
        ({"USD": 800.0}, {"USD": 900.0}, _keeps_every_row, {"USD": 800.0}),
        ({"USD": 1200.0}, {}, _keeps_every_row, {"USD": 1200.0}),
        ({}, {"USD": 900.0}, _keeps_the_outbound_only, {"USD": 900.0}),
    ],
    ids=[
        "return-only",
        "return-lower",
        "outbound-lower",
        "outbound-only",
        "routing-emptied-the-returns",
    ],
)
def test_a_round_trip_board_holds_the_lowest_cap_of_the_pages_read(
    outbound: dict[str, float],
    returned: dict[str, float],
    keep: Callable[[int, gfid.GFlightWithId], bool],
    merged: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base for every case with a return cap, the routing's
    emptying of the returns included: the cap is read off the page, before the
    routing runs."""
    google = _Google([("JFK", "LAX")])

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        board = google(filters, currency=currency, cheapest=cheapest)
        capped_at = returned if filters.flight_segments[0].selected_flight else outbound
        return gfid.Board(board, capped_at=capped_at)

    monkeypatch.setattr(gfid, "_one_call", one_call)
    search = cli.SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET)), options=SearchOptions()
    )
    answer = gfid.search_with_ids(to_fli_filter(search), top_n=2, keep=keep)
    assert answer is not None
    assert answer.capped_at == merged


def test_a_return_page_that_ignored_its_pin_names_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the base. The cap is read after the pin check: the first pin's
    return page answers the outbound leg again, as a page that dropped the pin
    does, and is refused with a cap of its own; the second pin's is served."""
    google = _Google([("JFK", "LAX"), ("JFK", "LAX")])

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        picked = filters.flight_segments[0].selected_flight
        if picked is None:
            return google(filters, currency=currency, cheapest=cheapest)
        if picked.legs[0].flight_number == "0":
            unpinned = filters.model_copy(deep=True)
            unpinned.flight_segments[0].selected_flight = None
            return gfid.Board(google(unpinned, currency=currency), capped_at={"USD": 100.0})
        return gfid.Board(google(filters, currency=currency), capped_at={"USD": 900.0})

    monkeypatch.setattr(gfid, "_one_call", one_call)
    search = cli.SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET)), options=SearchOptions()
    )
    answer = gfid.search_with_ids(to_fli_filter(search), top_n=2)
    assert answer is not None
    assert (answer.pinned, answer.capped_at) == (2, {"USD": 900.0})
