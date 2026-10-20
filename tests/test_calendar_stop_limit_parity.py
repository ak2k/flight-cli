# pyright: reportPrivateUsage=false
"""The two-lows note's stop clause asks the question `_gf_calgraph.page_url`
answers: whether Google's page was given a stop limit."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from flight_cli import _gf_calgraph, cli
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, SearchOptions

if TYPE_CHECKING:
    import pytest

_DEP = date.today() + timedelta(days=45)
_WINDOW = CalendarWindow(start=_DEP, end=_DEP + timedelta(days=6), duration_min=7, duration_max=7)


def _round_trip(
    out: str | None, back: str | None, max_extra_stops: int | None = None
) -> CalendarSearch:
    return CalendarSearch(
        legs=(
            Leg.of("JFK", "LHR", extension=out),
            Leg.of("LHR", "JFK", extension=back),
        ),
        window=_WINDOW,
        options=SearchOptions(max_extra_stops=max_extra_stops),
    )


def _page_set_a_limit(search: CalendarSearch, monkeypatch: pytest.MonkeyPatch) -> bool:
    asked: list[int] = []
    real = _gf_calgraph._fli_max_stops

    def spy(ceiling: int) -> Any:
        asked.append(ceiling)
        return real(ceiling)

    monkeypatch.setattr(_gf_calgraph, "_fli_max_stops", spy)
    _gf_calgraph.page_url(search, _DEP)
    return bool(asked)


def test_the_note_reads_a_stop_limit_where_the_page_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page writes the outbound leg's predicates onto every slice, so a stop
    code on the return leg alone sets no limit there and none in the note."""
    cases = [
        _round_trip(None, None),
        _round_trip(None, None, 0),
        _round_trip(None, None, -1),
        _round_trip("MAXSTOPS 1", "MAXSTOPS 1"),
        _round_trip("MAXSTOPS 1", "MAXSTOPS 1", -1),
        _round_trip("MAXSTOPS 1", None),
        _round_trip(None, "MAXSTOPS 1"),
    ]
    for search in cases:
        assert cli._stops_reach_the_page(search) == _page_set_a_limit(search, monkeypatch), search

    def nonstop(_search: CalendarSearch) -> int:
        return 0

    # Not raising: where the name is absent, the test fails on the asserts below.
    monkeypatch.setattr(_gf_calgraph, "page_stop_limit", nonstop, raising=False)
    plain = _round_trip(None, None)
    assert _page_set_a_limit(plain, monkeypatch)
    assert cli._stops_reach_the_page(plain)
