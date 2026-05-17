# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Tests for seats_aero/auth.py key resolution + persistence.

Verifies the precedence env > file > None and the 0600 perms on saved files.
Env-var manipulation uses monkeypatch so the user's real key isn't disturbed.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from flight_cli.providers.seats_aero import auth

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture
def _isolated_key_path(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Redirect KEY_PATH + clear env var so tests don't see the user's real key."""
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "KEY_PATH", tmp_path / "seats.json")
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    return tmp_path / "seats.json"


def test_load_returns_none_when_unconfigured(_isolated_key_path: Path) -> None:
    assert auth.load_key() is None
    assert auth.is_configured() is False


def test_load_reads_env_var(_isolated_key_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(auth.API_KEY_ENV, "pro_envvalue")
    assert auth.load_key() == "pro_envvalue"
    assert auth.is_configured() is True


def test_env_var_wins_over_file(_isolated_key_path: Path, monkeypatch: MonkeyPatch) -> None:
    auth.save_key("pro_filevalue")
    monkeypatch.setenv(auth.API_KEY_ENV, "pro_envvalue")
    assert auth.load_key() == "pro_envvalue"


def test_file_used_when_env_unset(_isolated_key_path: Path) -> None:
    auth.save_key("pro_filevalue")
    assert auth.load_key() == "pro_filevalue"


def test_save_key_writes_0600_perms(_isolated_key_path: Path) -> None:
    auth.save_key("pro_secret")
    perms = stat.S_IMODE(_isolated_key_path.stat().st_mode)
    # On macOS, umask may already restrict but the explicit chmod 0o600 should win.
    assert perms == 0o600, f"expected 0o600, got {oct(perms)}"


def test_save_key_overwrites_existing(_isolated_key_path: Path) -> None:
    auth.save_key("pro_old")
    auth.save_key("pro_new")
    assert auth.load_key() == "pro_new"


def test_save_key_strips_whitespace(_isolated_key_path: Path) -> None:
    auth.save_key("  pro_padded  ")
    # Stored value is stripped
    stored = json.loads(_isolated_key_path.read_text())
    assert stored["api_key"] == "pro_padded"


def test_clear_key_removes_file(_isolated_key_path: Path) -> None:
    auth.save_key("pro_secret")
    assert auth.clear_key() is True
    assert not _isolated_key_path.exists()
    assert auth.clear_key() is False  # idempotent


def test_clear_key_doesnt_touch_env(_isolated_key_path: Path, monkeypatch: MonkeyPatch) -> None:
    """clear_key only deletes the file; env var still resolves."""
    monkeypatch.setenv(auth.API_KEY_ENV, "pro_envvalue")
    auth.save_key("pro_filevalue")
    auth.clear_key()
    assert auth.load_key() == "pro_envvalue"


def test_get_key_raises_when_unconfigured(_isolated_key_path: Path) -> None:
    with pytest.raises(auth.SeatsAuthError, match=r"No Seats\.aero API key"):
        auth.get_key()


def test_get_key_returns_value_when_configured(
    _isolated_key_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(auth.API_KEY_ENV, "pro_envvalue")
    assert auth.get_key() == "pro_envvalue"


def test_malformed_file_returns_none(_isolated_key_path: Path) -> None:
    """A corrupt file should not crash — just behave as if unconfigured."""
    _isolated_key_path.write_text("not json")
    assert auth.load_key() is None


def test_file_with_no_api_key_returns_none(_isolated_key_path: Path) -> None:
    """Valid JSON without the api_key field falls through to None."""
    _isolated_key_path.write_text(json.dumps({"other_field": "x"}))
    assert auth.load_key() is None
