"""A mistyped airport code is a typed exit 2 on every command that builds a leg.

`Leg.of` validates the code with pydantic, and a `ValidationError` that nothing
catches reaches the terminal as a rich traceback and exit 1.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flight_cli import cli

_DAY = (date.today() + timedelta(days=45)).isoformat()
_END = (date.today() + timedelta(days=50)).isoformat()

_BACKENDS = (
    "_run_gflight_path",
    "_run_matrix_path",
    "_run_gflight_path_multi",
    "_run_matrix_path_multi",
    "_run_enriched_path",
    "_run_calendar",
    "_run_calendar_enriched",
    "_http_date_grid",
)

_COMMANDS = {
    "search": ["search", "{o}", "LAX", "--dep", _DAY],
    "search-return": ["search", "JFK", "{o}", "--dep", _DAY, "--return", _END],
    "search-slice": ["search", "--slice", "{o}-LAX:" + _DAY],
    "fare": ["fare", "{o}", "LAX", "--dep", _DAY],
    "gflight": ["gflight", "{o}", "LAX", "--dep", _DAY],
    "calendar": ["calendar", "{o}", "ATL", "--start", _DAY, "--end", _END],
    "detail": ["detail", "{o}", "LAX", "--dep", _DAY],
}


def _invoke(monkeypatch: pytest.MonkeyPatch, command: str, code: str):
    def _unreached(*_a: object, **_k: object) -> None:
        raise AssertionError("a backend ran on a query with an invalid airport")

    for name in _BACKENDS:
        monkeypatch.setattr(cli, name, _unreached)
    argv = [a.format(o=code) if "{o}" in a else a for a in _COMMANDS[command]]
    return CliRunner().invoke(cli.app, argv)


@pytest.mark.parametrize("command", list(_COMMANDS))
def test_an_invalid_airport_code_exits_2_with_a_typed_last_line(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    result = _invoke(monkeypatch, command, "QQ7")

    assert result.exit_code == 2, result.output
    assert result.stderr.strip().splitlines()[-1] == "Not a 3-letter IATA code: 'QQ7'"
    assert "Traceback" not in result.stderr
    assert "ValidationError" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("command", ["search", "calendar", "detail"])
def test_the_refusal_prints_the_typed_code_as_text_not_markup_or_control(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    result = _invoke(monkeypatch, command, "[bold]\x1b[2J")

    assert result.exit_code == 2, result.output
    assert "'[BOLD]" in result.stderr
    assert "\x1b" not in result.stderr


def test_detail_refuses_a_blank_airport_list(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "detail", ",")

    assert result.exit_code == 2, result.output
    assert "origin and destination are required" in result.stderr


def test_no_command_builds_a_leg_except_through_the_refusing_helper() -> None:
    """A return leg repeats the airports its outbound leg has just
    validated, so no run can tell a bare `Leg.of` there from the helper; the
    source can."""
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "of"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Leg"
    ]
    assert not bare, f"Leg.of called directly in cli.py at lines {bare}"
