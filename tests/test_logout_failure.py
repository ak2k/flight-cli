"""A `logout` that cannot delete the Seats.aero key or the PointsPath tokens says so and exits 1.

`auth seats-aero key` does the same for a failed save; an OSError from the delete
must not reach the user as a traceback.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.pp import auth as pp_auth
from flight_cli.pp import cli as pp_cli
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(sys.platform == "win32", reason="a directory mode does not block unlink")
@pytest.mark.skipif(os.geteuid() == 0, reason="root unlinks in any directory")
@pytest.mark.parametrize(
    ("module", "attr", "command"),
    [
        (seats_auth, "KEY_PATH", ["auth", "seats-aero", "logout"]),
        (pp_auth, "TOKENS_PATH", ["auth", "pp", "logout"]),
    ],
)
def test_a_logout_that_cannot_unlink_exits_1_with_a_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    attr: str,
    command: list[str],
) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = store_dir / "credentials.json"
    assert store.is_relative_to(tmp_path)
    monkeypatch.setattr(module, attr, store)
    _ = store.write_text("{}")
    store_dir.chmod(0o500)
    try:
        result = CliRunner().invoke(cli.app, command)
        kept = store.exists()
    finally:
        store_dir.chmod(0o700)
    assert (result.exit_code, type(result.exception).__name__) == (1, "SystemExit"), result.output
    assert "(PermissionError)" in result.output
    assert "Deleted" not in result.output
    assert kept
    assert "Could not delete" in result.stderr
    assert "Could not delete" not in result.stdout


@pytest.mark.parametrize(
    ("module", "attr", "command"),
    [
        (seats_auth, "clear_key", ["auth", "seats-aero", "logout"]),
        (pp_cli, "clear_tokens", ["auth", "pp", "logout"]),
    ],
)
def test_a_logout_that_fails_with_a_non_os_error_still_raises(
    monkeypatch: pytest.MonkeyPatch, module: object, attr: str, command: list[str]
) -> None:
    def broken() -> bool:
        raise ValueError(attr)

    monkeypatch.setattr(module, attr, broken)
    result = CliRunner().invoke(cli.app, command)
    assert (result.exit_code, type(result.exception).__name__) == (1, "ValueError"), result.output
