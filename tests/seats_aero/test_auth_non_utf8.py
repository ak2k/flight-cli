"""A seats.aero key file that is not UTF-8 reads as no key, not a crash.

`load_key` returns None for a missing or unparseable file; a file whose bytes
do not decode must do the same instead of raising UnicodeDecodeError into every
search and command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.providers.seats_aero import auth

if TYPE_CHECKING:
    import pytest


def _write_key_file(body: bytes) -> None:
    # Before the write, which replaces whatever file this names.
    assert auth.KEY_PATH.is_relative_to(auth.CONFIG_DIR), auth.KEY_PATH
    auth.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    auth.KEY_PATH.write_bytes(body)


def test_a_non_utf8_key_file_loads_as_none(tmp_path_factory: pytest.TempPathFactory) -> None:
    assert auth.KEY_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_key_file(b"\xff")
    assert auth.load_key() is None
    assert auth.is_configured() is False


def test_whoami_on_a_non_utf8_key_file_says_no_key_configured(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert auth.KEY_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_key_file(b"\xff")
    result = CliRunner().invoke(cli.app, ["auth", "seats-aero", "whoami"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert result.exit_code == 1
    assert "No Seats.aero key configured" in result.output
