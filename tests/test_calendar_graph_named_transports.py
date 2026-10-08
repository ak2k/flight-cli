# pyright: reportPrivateUsage=false
"""A calendar's price graph is read under `--gf-transport browser` and `auto`: the
note, the option help, the README and the skill say both."""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer.main

from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from test_calendar_formats import _ROUTE, _envelope_of, _fully_priced, _notes, _run, _serve

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    text = (_ROOT / rel).read_text().replace("\n# ", "\n")
    return " ".join(text.split())


@pytest.mark.parametrize("fmt", ["json", "envelope"])
def test_the_note_for_an_unasked_graph_names_browser_and_auto(
    fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _fully_priced())
    result = _run(*_ROUTE, "--format", fmt)
    assert result.exit_code == 0, result.output
    why = (
        f"--format {fmt} reads it only when --gf-transport names browser or auto, "
        "which opens Chrome"
    )
    assert result.stderr == f"Google Flights price graph not asked: {why}.\n"
    if fmt == "envelope":
        assert _notes(_envelope_of(result), "price_graph") == [f"price_graph: not asked: {why}"]


def test_the_fast_help_says_browser_and_auto_carry_the_graph() -> None:
    group = typer.main.get_command(cli.app)
    assert isinstance(group, click.Group)
    (fast,) = [p for p in group.commands["calendar"].params if "--fast" in p.opts]
    help_text = re.sub(r"\[/?(?:bold)?\]", "", getattr(fast, "help", None) or "")
    assert "carries it when --gf-transport browser or auto is given" in help_text


def test_the_readme_and_the_skill_say_browser_and_auto_carry_the_graph() -> None:
    readme = _flat("README.md")
    skill = _flat(".claude/skills/flight-search/SKILL.md")
    assert "carries it when given --gf-transport browser or auto." in readme
    assert "beside it under `--gf-transport browser` or `auto`)" in readme
    assert "a calendar under `--gf-transport browser` or `auto` adds `google_price_graph`" in skill
    assert "read under `--gf-transport browser` or `auto`. Schema" in readme
    assert (
        "Add `--gf-transport browser` or `auto` to a calendar to read Google's price graph" in skill
    )
