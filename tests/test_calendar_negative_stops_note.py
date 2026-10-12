# pyright: reportPrivateUsage=false
"""A negative `--stops` is no limit on Google's page, so the two-lows note keeps
its stop clause, as `_gf_calgraph.page_url` reads the same input."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from test_calendar_beside_graph import (
    _DIFFER,
    _JFK_LHR_DAY,
    _JFK_LHR_GRAPHS,
    _graphs_are,
    _matrix_prices,
    _note,
    _run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_CLAUSE = "Google allowed any number of stops"


def _note_for(monkeypatch: pytest.MonkeyPatch, limit: Sequence[str]) -> str:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)
    result = _run("JFK", "LHR", "-d", "5-7", *limit)
    assert result.exit_code == 0, result.output
    note = _note(result)
    assert note.startswith(_DIFFER)
    return note


def test_a_negative_stops_alone_leaves_the_stop_clause_in_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _note_for(monkeypatch, ("--stops", "-1"))
    assert "Matrix held each direction to one stop more than the fewest" in note
    assert _CLAUSE in note


@pytest.mark.parametrize(
    "limit",
    [("--stops", "0"), ("--stops", "-1", "--ext", "MAXSTOPS 1")],
    ids=["nonstop", "negative-with-a-stop-code"],
)
def test_a_stop_limit_google_was_asked_still_drops_the_stop_clause(
    limit: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _note_for(monkeypatch, limit)
    assert "between the same airports. Matrix's grid" in note
    assert _CLAUSE not in note
