"""`flight auth seats-aero key` that cannot write the key says so in one line and exits 1.

`flight auth pp login` does the same for a failed token save; an OSError from the
key save must not reach the user as a traceback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    import pytest

_ARGS = ["auth", "seats-aero", "key", "NEWKEY"]


def _fail_save(_key: str) -> None:
    raise OSError("disk full")


def test_a_failed_save_prints_a_line_and_exits_1(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert seats_auth.KEY_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    monkeypatch.setattr(seats_auth, "save_key", _fail_save)
    result = CliRunner().invoke(cli.app, _ARGS)
    assert (result.exit_code, type(result.exception).__name__) == (1, "SystemExit"), result.output
    assert "Could not save the key (OSError): disk full" in result.output
    assert "Saved" not in result.output
    assert "NEWKEY" not in result.output


def test_the_error_text_reaches_the_terminal_without_control_characters(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert seats_auth.KEY_PATH.is_relative_to(tmp_path_factory.getbasetemp())

    def hostile(_key: str) -> None:
        raise OSError("\x1b[2Jfull [bold]disk")

    monkeypatch.setattr(seats_auth, "save_key", hostile)
    result = CliRunner().invoke(cli.app, _ARGS)
    assert result.exit_code == 1, result.output
    assert "\x1b" not in result.output
    assert "[bold]disk" in result.output


def test_a_save_that_fails_inside_save_key_leaves_no_key_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert seats_auth.CONFIG_DIR.is_relative_to(tmp_path_factory.getbasetemp())
    blocker = seats_auth.CONFIG_DIR
    blocker.parent.mkdir(parents=True, exist_ok=True)
    _ = blocker.write_text("not a directory")
    result = CliRunner().invoke(cli.app, _ARGS)
    assert result.exit_code == 1, result.output
    assert "Could not save the key (" in result.output
    assert not seats_auth.KEY_PATH.exists()
    assert "Saved" not in result.output


def test_an_error_other_than_oserror_is_not_reported_as_a_failed_save(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert seats_auth.KEY_PATH.is_relative_to(tmp_path_factory.getbasetemp())

    def broken(_key: str) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(seats_auth, "save_key", broken)
    result = CliRunner().invoke(cli.app, _ARGS)
    assert type(result.exception) is RuntimeError, result.output
    assert "Could not save" not in result.output
