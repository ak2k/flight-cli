"""`flight gflight` is gone: `flight search --backend gflight` is its replacement."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

from flight_cli import cli

_DAY = (date.today() + timedelta(days=45)).isoformat()
_ROOT = Path(__file__).resolve().parents[1]


def test_gflight_is_not_a_command() -> None:
    group = typer.main.get_command(cli.app)

    assert isinstance(group, click.Group)
    assert "gflight" not in group.commands


def test_running_gflight_is_an_unknown_command() -> None:
    # QQ7 is refused before any request would go out, so the test needs no network.
    result = CliRunner().invoke(cli.app, ["gflight", "QQ7", "LAX", "--dep", _DAY])

    assert result.exit_code == 2, result.output
    assert "No such command 'gflight'" in result.output


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        ".claude/skills/flight-search/SKILL.md",
        "src/flight_cli/cli.py",
        "src/flight_cli/fli_bridge.py",
        "tests/test_gf_browser.py",
    ],
)
def test_no_user_text_names_the_removed_command(path: str) -> None:
    text = (_ROOT / path).read_text()

    assert "flight gflight" not in text
    assert "deprecated `gflight`" not in text
    assert "the deprecated command" not in text


def test_no_other_test_runs_the_removed_command() -> None:
    """Against the unknown command a test that only checks an exit of 2 or a
    sleep total still passes, so a leftover argv would go unnoticed."""
    argv_head = re.compile(r'\[\s*"gflight"\s*,')
    left = [
        path.name
        for path in sorted((_ROOT / "tests").rglob("test_*.py"))
        if path != Path(__file__).resolve() and argv_head.search(path.read_text())
    ]

    assert left == []
