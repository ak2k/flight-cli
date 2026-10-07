# pyright: reportPrivateUsage=false
"""After a whole search goes to Matrix, Google's read failures are notes, not a narrower answer.

Each narrowing is recorded through the function that records it in a real run,
from inside a stand-in for `cli._gflight_results`; each arm is a way
`search` hands Google's answer to Matrix. Matrix and the award providers are
in process (`test_envelope._hermetic`)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from flight_cli import _envelope, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfThrottledError
from test_envelope import (
    _envelope_of,
    _hermetic,  # noqa: F401 # pyright: ignore[reportUnusedImport] — the autouse fixture
    _search,
)
from test_gf_full_board import _DEP, _LAX, _URL, _served
from test_gf_rung_parity import _board

if TYPE_CHECKING:
    from collections.abc import Callable


def _pin_refused() -> None:
    gfid._report_pin_outcome(
        served=True, pins=3, refused=[GfThrottledError("rate-limited")], stopped=None, skipped=0
    )


def _pin_stopped() -> None:
    gfid._report_pin_outcome(
        served=True, pins=3, refused=[], stopped=GfThrottledError("rate-limited"), skipped=2
    )


def _tab_unread() -> None:
    cli._note_separate_tickets(
        gfid.Board(separate_failed=GfThrottledError("rate-limited")), gf_mode="http", bags=False
    )


# Each narrowing, and the stderr line it is said in.
_NARROWINGS: dict[str, tuple[Callable[[], None], str]] = {
    "return-board-refused": (_pin_refused, "1 of 3 return boards unavailable: rate-limited"),
    "pins-stopped": (_pin_stopped, "stopped pinning: 2 of 3 return boards skipped"),
    "cheapest-tab-unread": (
        _tab_unread,
        "Itineraries on separate tickets not read: Google Flights rate-limited.",
    ),
}


def _typed() -> list[Any]:
    raise GfThrottledError("rate-limited")


def _untyped() -> list[Any]:
    raise ValueError("boom")


def _no_rows() -> list[Any]:
    return gfid.Board()


# Each hand-off: the arguments that reach it, and what Google answers.
_ARMS: dict[str, tuple[list[str], Callable[[], list[Any]]]] = {
    "typed-failure": (["--cash-only"], _typed),
    "untyped-failure": (["--cash-only"], _untyped),
    "board-emptied": (["--cash-only"], lambda: gfid.Board(dropped=5)),
    "infant": (["--cash-only", "--inf-lap", "1"], _no_rows),
    "awards-separate-only": (
        [],
        lambda: gfid.Board([SimpleNamespace(ticketing="separate")], dropped=1),
    ),
    "multi-cabin": (["--cash-only", "--cabin", "economy,business"], lambda: gfid.Board(dropped=5)),
}


def _google(
    monkeypatch: pytest.MonkeyPatch, record: Callable[[], None], answer: Callable[[], list[Any]]
) -> None:
    def results(*_a: object, **_kw: object) -> list[Any]:
        record()
        return answer()

    monkeypatch.setattr(cli, "_gflight_results", results)


def _auto(*extra: str) -> dict[str, Any]:
    return _envelope_of(_search("JFK", "LAX", "--dep", _DEP.isoformat(), *extra))


def _said(env: dict[str, Any]) -> str:
    return " ".join(" ".join(cast("list[str]", env["notes"])).split())


@pytest.mark.parametrize("arm", list(_ARMS))
@pytest.mark.parametrize("narrowing", list(_NARROWINGS))
def test_a_google_narrowing_before_a_hand_off_is_a_note_on_matrixs_answer(
    monkeypatch: pytest.MonkeyPatch, narrowing: str, arm: str
) -> None:
    """Red at the base: the narrowing stood, so Matrix's whole answer read as incomplete."""
    record, line = _NARROWINGS[narrowing]
    extra, answer = _ARMS[arm]
    _google(monkeypatch, record, answer)
    env = _auto(*extra)
    said = _said(env)
    assert "Using Matrix:" in said, said
    assert line in said, said
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]


@pytest.mark.parametrize("narrowing", list(_NARROWINGS))
def test_a_google_narrowing_on_googles_own_answer_still_narrows_it(
    monkeypatch: pytest.MonkeyPatch, narrowing: str
) -> None:
    """Green at the base: no hand-off, so Google's answer is the narrower one."""
    record, line = _NARROWINGS[narrowing]
    board = gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))
    _google(monkeypatch, record, lambda: board)
    env = _auto("--cash-only", "--backend", "gflight", "--fast")
    assert line in _said(env)
    assert (env["backend"], env["complete"]) == ("gflight", False), env["notes"]


def test_a_narrowing_of_no_one_backend_before_a_hand_off_still_narrows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base: a narrowing not scoped to Google holds whoever answers."""
    _google(monkeypatch, _envelope.narrow, lambda: gfid.Board(dropped=5))
    env = _auto("--cash-only")
    assert "Using Matrix:" in _said(env)
    assert (env["backend"], env["complete"]) == ("matrix", False), env["notes"]


@pytest.mark.parametrize(
    ("extra", "answer"),
    [
        (["--cash-only"], lambda: gfid.Board(dropped=5, unread=3)),
        (["--cash-only", "--inf-lap", "1"], lambda: gfid.Board(unread=3)),
        ([], lambda: gfid.Board([SimpleNamespace(ticketing="separate")], dropped=1, unread=3)),
        (["--cash-only", "--cabin", "economy,business"], lambda: gfid.Board(dropped=5, unread=3)),
    ],
    ids=["board-emptied", "infant", "awards-separate-only", "multi-cabin"],
)
def test_rows_google_could_not_read_on_a_handed_off_board_are_a_note(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], answer: Callable[[], list[Any]]
) -> None:
    """Red at the base: the unread rows of a board handed to Matrix were said nowhere."""

    def nothing() -> None:
        return None

    _google(monkeypatch, nothing, answer)
    env = _auto(*extra)
    said = _said(env)
    assert "Using Matrix:" in said, said
    assert "3 economy rows its pages served could not be read" in said, said
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]


def test_every_narrowing_in_the_google_search_module_names_google() -> None:
    """A narrowing recorded there with no backend would make Matrix's answer
    incomplete after a hand-off."""
    source = Path(gfid.__file__)
    unscoped = [
        node.lineno
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source)))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "narrow")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "narrow")
        )
        and not any(k.arg == "of" for k in node.keywords)
    ]
    assert unscoped == []


def test_a_multi_cabin_hand_off_says_each_cabins_cheapest_tab_went_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red before: the multi-cabin path said its Cheapest-tab notes only below
    its hand-off, so a search handed to Matrix never said a cabin's tab went
    unread, where the one-cabin path says it ahead of the hand-off line."""

    def nothing() -> None:
        return None

    _google(
        monkeypatch,
        nothing,
        lambda: gfid.Board(dropped=5, separate_failed=GfThrottledError("rate-limited")),
    )
    env = _auto("--cash-only", "--cabin", "economy,business")
    said = _said(env)
    for cab in ("COACH", "BUSINESS"):
        line = (
            f"Google Flights {cab}: itineraries on separate tickets not read: "
            "Google Flights rate-limited."
        )
        assert line in said, said
        assert said.index(line) < said.index("Using Matrix:"), said
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]


# The line `cli._note_row_cap` prints for a board shown at Google's row cap,
# after "Google Flights" and the cabin of a multi-cabin search.
_ROW_CAP_LINE = "stops at 300 rows for this search"


def _nothing() -> None:
    return None


def _at_the_row_cap(answer: Callable[[], list[Any]]) -> Callable[[], list[Any]]:
    """`answer`, its board stopped at Google's row cap as the token page's did."""

    def capped() -> list[Any]:
        board = cast("gfid.Board[Any]", answer())
        board.capped_at = _board("ds1_nyc_lon_token").capped_at
        return board

    return capped


@pytest.mark.parametrize("arm", list(_ARMS))
def test_a_row_cap_on_a_board_handed_to_matrix_is_not_said(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    """The cap bounds the board Matrix replaced, so it says nothing about
    Matrix's answer: no line, and `complete` stays true."""
    extra, answer = _ARMS[arm]
    _google(monkeypatch, _nothing, _at_the_row_cap(answer))
    env = _auto(*extra)
    said = _said(env)
    assert "Using Matrix:" in said, said
    assert _ROW_CAP_LINE not in said, said
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]


def test_a_row_cap_on_googles_own_answer_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    """No hand-off, so the line describes the board shown. It is a note, so
    `complete` stays true."""
    board = gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))
    _google(monkeypatch, _nothing, _at_the_row_cap(lambda: board))
    env = _auto("--cash-only", "--backend", "gflight", "--fast")
    line = f"Google Flights {_ROW_CAP_LINE}: fares above USD1006.00 may be missing."
    assert line in _said(env)
    assert (env["backend"], env["complete"]) == ("gflight", True), env["notes"]
