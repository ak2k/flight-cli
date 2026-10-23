"""`flight watch add` names a config path that carries a newline on one stderr line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_a_config_path_with_a_newline_is_named_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "h" / "line1\nline2"
    assert directory.is_relative_to(tmp_path)
    directory.parent.mkdir()
    _ = directory.write_text("not a directory")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(directory))
    result = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    assert (result.exit_code, type(result.exception).__name__) == (1, "SystemExit"), result.output
    named = str(directory).replace("\n", " ")
    assert result.output.splitlines() == [f"{named} cannot be created: File exists"]
