# pyright: reportPrivateUsage=false
"""`--enrich --format envelope` over an empty Google board: an infant's empty
board narrows the answer, an adult's stays complete."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import cli
from test_cross_check import _SEARCH, _run, _weave
from test_envelope import _envelope_of, _notes, _rows

if TYPE_CHECKING:
    from collections.abc import Callable

_INFANT = (
    "Google Flights served no rows for a party with an infant, as it has on routes with flights"
)


def _empty_board(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    *,
    matrix_fails: bool = False,
) -> None:
    """The weave, with Google answering an empty board it did not filter."""
    _weave(monkeypatch, gf_rows, matrix_fails=matrix_fails)

    def _none(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _none)


@pytest.mark.parametrize("flag", ["--inf-lap", "--inf-seat"])
def test_an_empty_infant_board_narrows_the_enriched_envelope(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]], flag: str
) -> None:
    _empty_board(monkeypatch, gf_rows)
    env = _envelope_of(_run([*_SEARCH, flag, "1", "-n", "5", "--enrich", "--format", "envelope"]))
    assert (env["backend"], env["complete"], _rows(env)) == ("gflight", False, [])
    assert env["cross_check"]["rows"]
    assert _notes(env, "results") == [f"results: {_INFANT}, and cross_check holds Matrix's rows"]


def test_an_empty_adult_board_stays_a_complete_enriched_answer(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _empty_board(monkeypatch, gf_rows)
    env = _envelope_of(_run([*_SEARCH, "-n", "5", "--enrich", "--format", "envelope"]))
    assert (env["backend"], env["complete"], _rows(env)) == ("gflight", True, [])
    assert env["cross_check"]["rows"]


def test_an_empty_infant_board_with_no_matrix_answer_names_the_infant(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    _empty_board(monkeypatch, gf_rows, matrix_fails=True)
    env = _envelope_of(
        _run([*_SEARCH, "--inf-lap", "1", "-n", "5", "--enrich", "--format", "envelope"])
    )
    assert (env["complete"], env["cross_check"]) == (False, None)
    assert _notes(env, "results") == [f"results: {_INFANT}"]
