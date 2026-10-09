"""Google's error 13 on the price graph is read again after a pause.

The graph answered "error 13" in 2 of 13 live runs and the next identical run
priced, so a refused graph is loaded once more after each of the search page's
pauses before it is given up. No test here loads a page: the browser session is
replaced as in `test_gf_calgraph`.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from datetime import date
from typing import Any

import pytest

from flight_cli import _gf_calgraph as cg
from flight_cli import _gflight_ids as gfid
from test_gf_calgraph import _fixture, _search, _serve

_FIRST, _LAST = date(2026, 10, 20), date(2026, 11, 2)


class _Clock:
    """`time`, recording each sleep rather than taking it."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    recorder = _Clock()
    monkeypatch.setattr(gfid, "time", recorder)
    return recorder


def test_a_refused_graph_is_loaded_again_after_a_pause(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Red at the base: the refusal was raised on the first body, with no pause."""
    refused = _fixture("error13.body")
    fake = _serve(monkeypatch, refused, _fixture("ow_jfk_lax.body"))
    graph = cg.price_graph(_search(start=_FIRST, end=_LAST), headed=False)
    assert [url for url, _ in fake.calls] == ["page:2026-10-20", "page:2026-10-20"]
    assert (len(graph.cells), graph.loads, clock.sleeps) == (14, 2, [2.0])


def test_a_graph_refused_on_every_read_is_refused_within_the_pause_bound(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Three reads, the search page's two pauses, then the refusal as it came."""
    refused = _fixture("error13.body")
    fake = _serve(monkeypatch, refused, refused, refused, _fixture("ow_jfk_lax.body"))
    with pytest.raises(cg.GfPriceGraphError) as e:
        cg.price_graph(_search(start=_FIRST, end=_LAST), headed=False)
    assert e.value.code == 13
    assert (len(fake.calls), clock.sleeps) == (3, [2.0, 6.0])


def test_a_refusal_with_no_load_left_to_read_again_is_raised_as_it_came(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    with pytest.raises(cg.GfPriceGraphError) as e:
        cg.price_graph(_search(start=_FIRST, end=_LAST), headed=False, pages=1)
    assert (e.value.code, len(fake.calls), clock.sleeps) == (13, 1, [])


def test_a_load_spent_on_a_refusal_is_named_when_the_window_runs_out(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    window = _search(start=date(2026, 10, 20), end=date(2026, 12, 10))
    with pytest.raises(cg.GfGraphBudgetError) as e:
        cg.price_graph(window, headed=False, pages=2)
    assert str(e.value) == (
        "no price-graph load of the 8 was left for the rest of the window (a page was loaded again)"
    )
    assert (e.value.loads, len(fake.calls), clock.sleeps) == (2, 2, [2.0])


def test_another_error_row_is_not_loaded_again(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    other = _fixture("error13.body").replace("[13,null,", "[14,null,")
    fake = _serve(monkeypatch, other, _fixture("ow_jfk_lax.body"))
    with pytest.raises(cg.GfPriceGraphError) as e:
        cg.price_graph(_search(start=_FIRST, end=_LAST), headed=False)
    assert (e.value.code, len(fake.calls), clock.sleeps) == (14, 1, [])


def test_the_pauses_come_out_of_the_running_searchs_budget(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A search that has paused for 7 of its 8 seconds has one left, whatever
    page it spent them on."""
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    with gfid.search_escalation():
        budget = gfid._search_escalation.get()
        assert budget is not None
        assert budget.pause(7.0) == 7.0
        graph = cg.price_graph(_search(start=_FIRST, end=_LAST), headed=False)
    assert (len(graph.cells), len(fake.calls), clock.sleeps) == (14, 2, [1.0])
