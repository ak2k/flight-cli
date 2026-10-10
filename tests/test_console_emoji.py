"""Remote text prints as received: a `:smile:` in it is not turned into an emoji."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.table import Table

from flight_cli import cli
from flight_cli.pp import cli as pp_cli

if TYPE_CHECKING:
    from rich.console import Console

_CONSOLES = [
    pytest.param(cli.console, False, id="cli.console"),
    pytest.param(cli.err, True, id="cli.err"),
    pytest.param(pp_cli.console, False, id="pp.console"),
    pytest.param(pp_cli.err, True, id="pp.err"),
]


def _read(capsys: pytest.CaptureFixture[str], *, stderr: bool) -> str:
    captured = capsys.readouterr()
    return captured.err if stderr else captured.out


@pytest.mark.parametrize(("console", "stderr"), _CONSOLES)
def test_a_line_with_an_emoji_code_prints_the_code(
    capsys: pytest.CaptureFixture[str], console: Console, stderr: bool
) -> None:
    console.print("Fly :smile: Co", highlight=False)
    assert _read(capsys, stderr=stderr) == "Fly :smile: Co\n"


@pytest.mark.parametrize(("console", "stderr"), _CONSOLES)
def test_a_table_cell_with_an_emoji_code_prints_the_code(
    capsys: pytest.CaptureFixture[str], console: Console, stderr: bool
) -> None:
    table = Table(show_header=False)
    table.add_row("Fly :smile: Co")
    console.print(table)
    out = _read(capsys, stderr=stderr)
    assert "Fly :smile: Co" in out
    assert "\N{SMILING FACE WITH OPEN MOUTH AND SMILING EYES}" not in out
