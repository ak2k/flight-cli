"""`flight explain --help`: a cheatsheet of the routing tokens and extension codes
`explain` reads, with two worked examples, so a first-time reader needs no other page."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flight_cli import cli

CHEATSHEET_LINES = (
    "F = any flight",
    "N = a nonstop flight",
    "X = any connection",
    "AA = a flight marketed by AA",
    "O:AA = a flight operated by AA",
    "LHR = a connection at LHR",
    "~ = not",
    "+ = one or more",
    "* = zero or more",
    "? = zero or one",
    "MAXSTOPS 1 = at most 1 stop",
    "MAXCONNECT 2:00 = no layover longer than 2:00",
    "-REDEYES = no red-eye flights",
    "flight explain 'F* X:LHR F*'",
    "flight explain 'O:LH+' --ext 'MAXCONNECT 2:00'",
)


@pytest.mark.parametrize("line", CHEATSHEET_LINES)
def test_the_help_lists_each_cheatsheet_line(line: str) -> None:
    result = CliRunner().invoke(cli.app, ["explain", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert line in " ".join(result.stdout.split())


@pytest.mark.parametrize("line", CHEATSHEET_LINES)
def test_an_80_column_help_prints_each_cheatsheet_line_on_one_row(line: str) -> None:
    # typer's rich markup mode prints the docstring's own line breaks.
    result = CliRunner().invoke(cli.app, ["explain", "--help"], env={"COLUMNS": "80"})
    assert result.exit_code == 0
    assert any(line in row for row in result.stdout.splitlines())
