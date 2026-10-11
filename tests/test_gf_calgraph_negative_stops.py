# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A negative `--stops` is no limit on `calendar --fast`'s page, as it is on the search page.

The page URL used to take the lowest of every stop limit with a negative
`--stops` among them, so `--stops -1` beside `MAXSTOPS 1` wrote no limit at all,
and a round trip whose legs name different ceilings passed the gates it
should have failed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from flight_cli import _gf_calgraph as cg
from flight_cli.domain import SearchOptions
from test_gf_calgraph import _START, _search

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli.domain import CalendarSearch

_NEGATIVE = SearchOptions(max_extra_stops=-1)
_GATES = [pytest.param(cg.graph_blocker, id="graph"), pytest.param(cg.page_blocker, id="page")]
_DIFFER = "different routing or extension codes on the outbound and the return"


def test_a_negative_stops_leaves_a_legs_own_ceiling_on_the_page() -> None:
    ceiling = cg.page_url(_search(extension="MAXSTOPS 1"), _START)
    assert ceiling != cg.page_url(_search(), _START)
    assert cg.page_url(_search(extension="MAXSTOPS 1", options=_NEGATIVE), _START) == ceiling


def test_a_negative_stops_alone_asks_the_page_no_limit() -> None:
    assert cg.page_url(_search(options=_NEGATIVE), _START) == cg.page_url(_search(), _START)


@pytest.mark.parametrize("gate", _GATES)
def test_a_negative_stops_does_not_equalize_legs_with_different_ceilings(
    gate: Callable[[CalendarSearch], str | None],
) -> None:
    search = _search(
        nights=7, extension="MAXSTOPS 1", extension_ret="MAXSTOPS 2", options=_NEGATIVE
    )
    assert gate(search) == _DIFFER
