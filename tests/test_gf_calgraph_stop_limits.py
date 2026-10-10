# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`calendar --fast` compares a round trip's legs by the stop limit each is held to.

`--stops 0 --routing-ret N` holds both legs to no stops, so the one filter set
Google writes on both slices is the question asked; the gates used to compare
the raw per-leg predicates and refused it as two different routings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import typer

from flight_cli import _gf_calgraph as cg
from flight_cli.domain import SearchOptions
from test_gf_airport_sets import _tfs
from test_gf_calgraph import _RT, _START, _calendar, _graph_is, _no_matrix, _search
from test_links_search_tfs import _slices

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli.domain import CalendarSearch

_GATES = [pytest.param(cg.graph_blocker, id="graph"), pytest.param(cg.page_blocker, id="page")]
_DIFFER = "different routing or extension codes on the outbound and the return"


@pytest.mark.parametrize("gate", _GATES)
def test_a_round_trip_held_to_no_stops_on_both_legs_is_admitted(
    gate: Callable[[CalendarSearch], str | None],
) -> None:
    search = _search(nights=7, routing_ret="N", options=SearchOptions(max_extra_stops=0))
    assert gate(search) is None


@pytest.mark.parametrize("gate", _GATES)
@pytest.mark.parametrize(
    "search",
    [
        _search(
            nights=7,
            routing="N",
            extension="MAXSTOPS 1",
            routing_ret="N",
            extension_ret="MAXSTOPS 2",
        ),
        _search(
            nights=7,
            extension="MAXSTOPS 2",
            extension_ret="MAXSTOPS 1",
            options=SearchOptions(max_extra_stops=1),
        ),
    ],
    ids=["own-ceilings-meet-at-nonstop", "stops-lowers-both-to-one"],
)
def test_two_ceilings_that_come_to_the_same_limit_are_admitted(
    gate: Callable[[CalendarSearch], str | None], search: CalendarSearch
) -> None:
    assert gate(search) is None


@pytest.mark.parametrize("gate", _GATES)
@pytest.mark.parametrize(
    "search",
    [
        _search(nights=7, routing_ret="N", options=SearchOptions(max_extra_stops=1)),
        _search(nights=7, routing_ret="N"),
    ],
    ids=["stops-1-return-nonstop", "no-stops-return-nonstop"],
)
def test_legs_held_to_different_limits_or_codes_are_still_refused(
    gate: Callable[[CalendarSearch], str | None], search: CalendarSearch
) -> None:
    assert gate(search) == _DIFFER


def test_a_stop_limit_does_not_hide_a_carrier_one_leg_alone_names() -> None:
    search = _search(
        nights=7, routing="AA+", routing_ret="N", options=SearchOptions(max_extra_stops=0)
    )
    assert cg.graph_blocker(search) == _DIFFER


def test_the_page_writes_no_stops_on_both_slices() -> None:
    search = _search(nights=7, routing_ret="N", options=SearchOptions(max_extra_stops=0))
    out, back = _slices(_tfs(cg.page_url(search, _START)))
    assert (out[5], back[5]) == ([0], [0])


def test_fast_asks_the_graph_for_a_round_trip_held_to_no_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT)
    _calendar(fmt="json", one_way=False, duration="7", stops=0, routing_return="N")
    cap = capsys.readouterr()
    assert len(json.loads(cap.out)["grid"]) == 2
    assert "Run without --fast" not in cap.err
    assert [s[0].options.max_extra_stops for s in seen] == [0]


def test_fast_refuses_a_round_trip_held_to_one_stop_and_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT)
    with pytest.raises(typer.Exit) as e:
        _calendar(one_way=False, duration="7", stops=1, routing_return="N")
    cap = capsys.readouterr()
    assert e.value.exit_code == 1
    assert _DIFFER in " ".join(cap.err.split())
    assert (seen, cap.out) == ([], "")
