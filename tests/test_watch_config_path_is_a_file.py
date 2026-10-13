"""`flight watch add` when the config directory cannot be created exits 1 naming the path."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from pathlib import Path


def test_a_config_path_that_is_a_file_exits_1_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    directory = root / "flight-cli"
    assert directory.is_relative_to(tmp_path)
    root.mkdir()
    _ = directory.write_text("not a directory")
    result = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    assert (result.exit_code, type(result.exception).__name__) == (1, "SystemExit"), result.output
    assert f"{directory} cannot be created: File exists" in result.output
    assert directory.read_text() == "not a directory"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory")
def test_an_unwritable_parent_exits_1_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    directory = root / "flight-cli"
    assert directory.is_relative_to(tmp_path)
    root.mkdir()
    root.chmod(0o500)
    try:
        result = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    finally:
        root.chmod(0o700)
    assert (result.exit_code, type(result.exception).__name__) == (1, "SystemExit"), result.output
    assert f"{directory} cannot be created: Permission denied" in result.output
    assert not directory.exists()
