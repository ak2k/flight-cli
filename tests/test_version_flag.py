"""`flight --version` prints the installed version and exits 0."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flight_cli import cli

_ROOT = Path(__file__).resolve().parent.parent


def _declared_version() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_version_flag_prints_the_declared_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert (result.exit_code, result.output) == (0, f"flight-cli {_declared_version()}\n")


def test_version_flag_prints_the_version_text_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_version(_name: str) -> str:
        return "[bold]1.2.3[/]"

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    result = CliRunner().invoke(cli.app, ["--version"])
    assert (result.exit_code, result.output) == (0, "flight-cli [bold]1.2.3[/]\n")


def test_version_flag_before_help_prints_the_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version", "--help"])
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        f"flight-cli {_declared_version()}\n",
        "",
    )


def test_version_flag_after_help_prints_the_version() -> None:
    result = CliRunner().invoke(cli.app, ["--help", "--version"])
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        f"flight-cli {_declared_version()}\n",
        "",
    )


def test_version_flag_is_listed_in_root_help() -> None:
    result = CliRunner().invoke(cli.app, ["--help"], env={"COLUMNS": "400"})
    assert result.exit_code == 0
    assert "--version" in result.output
    assert "Print the installed version and exit." in result.output


def _bash_completions(line: str) -> list[str]:
    result = CliRunner().invoke(
        cli.app,
        [],
        prog_name="flight",
        env={
            "_FLIGHT_COMPLETE": "complete_bash",
            "COMP_WORDS": line,
            "COMP_CWORD": str(len(line.split(" ")) - 1),
        },
    )
    assert result.exit_code == 0, result.output
    return [ln for ln in result.stdout.splitlines() if ln]


@pytest.mark.parametrize("rest", ["", "search --cabin "])
def test_version_flag_leaves_shell_completion_unchanged(rest: str) -> None:
    assert _bash_completions(f"flight --version {rest}") == _bash_completions(f"flight {rest}")
