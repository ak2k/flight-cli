# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Tests for `_should_run_pp`'s decision logic.

The function decides whether PointsPath augmentation runs on a given query.
Before work-qmx1, this was gated to the matrix backend. Now both backends
support PP overlay, so the gating is purely token-presence + flag-coherence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from flight_cli.cli import _should_run_pp

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _tokens_path(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Redirect the on-disk token path so tests can flip between
    has-tokens and no-tokens without touching the real ~/.config file."""
    token_file = tmp_path / "pp.json"
    monkeypatch.setattr("flight_cli.pp.auth.TOKENS_PATH", token_file)
    # The cli imports load_tokens at module level, so patch the imported name too.
    monkeypatch.setattr("flight_cli.cli.load_tokens", _make_loader(token_file))
    return token_file


def _make_loader(path: Path) -> Callable[[], object | None]:
    """Return a function that reads `path` and returns a truthy Tokens-like
    sentinel when present, None when absent. The function only cares about
    truthiness, so we don't need real Tokens objects."""

    def _load() -> object | None:
        return object() if path.exists() else None

    return _load


def test_no_tokens_returns_false(_tokens_path: Path) -> None:
    assert _should_run_pp(no_pp=False, pp_only=False) is False


def test_no_tokens_with_pp_only_errors(_tokens_path: Path) -> None:
    with pytest.raises(typer.Exit) as ei:
        _should_run_pp(no_pp=False, pp_only=True)
    assert ei.value.exit_code == 2


def test_tokens_present_returns_true(_tokens_path: Path) -> None:
    _tokens_path.write_text("{}")  # presence is enough for the test loader
    assert _should_run_pp(no_pp=False, pp_only=False) is True


def test_no_pp_returns_false_even_with_tokens(_tokens_path: Path) -> None:
    _tokens_path.write_text("{}")
    assert _should_run_pp(no_pp=True, pp_only=False) is False


def test_no_pp_with_pp_only_is_mutually_exclusive(_tokens_path: Path) -> None:
    with pytest.raises(typer.Exit) as ei:
        _should_run_pp(no_pp=True, pp_only=True)
    assert ei.value.exit_code == 2
