"""`flight watch add|rm` in a config directory the process cannot open for reading."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is a no-op on Windows")
@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any directory")
@pytest.mark.parametrize("command", [["add", "JFK", "LHR"], ["rm", "1"]])
def test_a_config_dir_without_read_permission_exits_1_naming_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    directory = root / "flight-cli"
    assert directory.is_relative_to(tmp_path)
    directory.mkdir(parents=True)
    directory.chmod(0o300)
    try:
        result = CliRunner().invoke(cli.app, ["watch", *command])
    finally:
        directory.chmod(0o700)
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, OSError), repr(result.exception)
    assert str(directory) in result.output
    assert "Permission denied" in result.output
