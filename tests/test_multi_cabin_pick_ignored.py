# pyright: reportPrivateUsage=false
"""A `--pick` on a multi-cabin search names no row, because no multi-cabin
surface numbers one, so the search says the pick is ignored on stderr and
otherwise answers as it does without it, on both multi-cabin paths."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from test_multi_cabin_pins import _DEP, _matrix_search

if TYPE_CHECKING:
    import pytest
    from click.testing import Result

_LINE = "--pick 2 is ignored: a multi-cabin search has no one row to name."


def _flat(text: str) -> str:
    return " ".join(text.split())


def _google_search(monkeypatch: pytest.MonkeyPatch, *extra: str) -> Result:
    """The Google multi-cabin path, stopped where it hands back (`None`: it answered)."""

    def _multi(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(cli, "_run_gflight_path_multi", _multi)
    return CliRunner().invoke(
        cli.app,
        [
            *("search", "--cash-only", "--no-google-url", "--no-matrix-url", "JFK", "LAX"),
            *("--dep", _DEP.isoformat(), "--cabin", "economy,business"),
            *("--backend", "gflight", *extra),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )


def test_a_pick_on_a_multi_cabin_search_is_said_to_be_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unpicked = _matrix_search(monkeypatch)
    picked = _matrix_search(monkeypatch, "--pick", "2")
    assert picked.exit_code == 0, picked.output
    assert _flat(picked.stderr) == _LINE, f"stderr holds no word on the pick: {picked.stderr!r}"
    assert picked.stdout == unpicked.stdout


def test_a_pick_on_a_google_multi_cabin_search_is_said_to_be_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _google_search(monkeypatch, "--pick", "2")
    assert result.exit_code == 0, result.output
    assert _flat(result.stderr) == _LINE, f"stderr holds no word on the pick: {result.stderr!r}"


def test_a_multi_cabin_search_with_no_pick_says_nothing_of_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix = _matrix_search(monkeypatch)
    assert matrix.exit_code == 0, matrix.output
    assert "--pick" not in matrix.stderr
    google = _google_search(monkeypatch)
    assert google.exit_code == 0, google.output
    assert "--pick" not in google.stderr
