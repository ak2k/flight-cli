# pyright: reportPrivateUsage=false
"""A negative `--stops` is no limit on the search page's gate, as it is on every row check.

`search_page_reasons` reads it as `_stop_ceiling` and `routing_keep` do, so a
`MAXSTOPS 3` beside it is still a ceiling the page cannot encode."""

from __future__ import annotations

import pytest

from flight_cli._gf_postfilter import search_page_reasons
from flight_cli.cli import BACKEND_GFLIGHT, BACKEND_MATRIX
from flight_cli.routing_predicates import classify
from test_backend_dispatch import _call

_ABOVE_TWO = "a stop ceiling above 2 (3)"


@pytest.mark.parametrize("stops", [-1, -5])
def test_a_negative_stops_leaves_the_extension_ceiling_to_the_page_gate(stops: int) -> None:
    predicates = classify(None, "MAXSTOPS 3").predicates
    assert search_page_reasons(predicates, stops) == [_ABOVE_TWO]


def test_a_negative_stops_alone_asks_the_page_nothing() -> None:
    assert search_page_reasons([], -1) == []
    assert search_page_reasons(classify(None, "MAXSTOPS 1").predicates, -1) == []


def test_zero_stops_still_lowers_the_ceiling_below_the_extension() -> None:
    predicates = classify(None, "MAXSTOPS 3").predicates
    assert search_page_reasons(predicates, 0) == []


def test_a_negative_stops_beside_a_high_ceiling_goes_to_matrix_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _call(extension="MAXSTOPS 3", stops=-1) == BACKEND_MATRIX
    assert _ABOVE_TWO in " ".join(capsys.readouterr().err.split())
    assert _call(extension="MAXSTOPS 3", stops=0) == BACKEND_GFLIGHT
