"""A credential save that fails partway keeps the previous file.

An error before the new bytes land must leave the old token or key in place:
otherwise the user is logged out by a save that never finished.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from flight_cli.pp import auth as pp_auth
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from pathlib import Path


def _fail_fchmod(*_args: object) -> None:
    raise OSError("disk full")


def test_a_failed_pp_save_keeps_the_previous_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    monkeypatch.setattr(pp_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(pp_auth, "TOKENS_PATH", config / "pp.json")
    # Before any write: the save overwrites whatever file these name.
    assert pp_auth.CONFIG_DIR.is_relative_to(tmp_path), pp_auth.CONFIG_DIR
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path), pp_auth.TOKENS_PATH
    pp_auth.save_tokens(pp_auth.Tokens("old", "r", 4102444800))
    with monkeypatch.context() as patch:
        patch.setattr(os, "fchmod", _fail_fchmod)
        with pytest.raises(OSError, match="disk full"):
            pp_auth.save_tokens(pp_auth.Tokens("new", "r", 4102444800))
    kept = pp_auth.load_tokens()
    assert kept is not None, "pp.json was emptied by the failed save"
    assert kept.access_token == "old"  # noqa: S105 - asserting a dummy value
    assert [p.name for p in config.iterdir()] == ["pp.json"]


def test_a_failed_seats_save_keeps_the_previous_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    monkeypatch.setattr(seats_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(seats_auth, "KEY_PATH", config / "seats.json")
    assert seats_auth.CONFIG_DIR.is_relative_to(tmp_path), seats_auth.CONFIG_DIR
    assert seats_auth.KEY_PATH.is_relative_to(tmp_path), seats_auth.KEY_PATH
    seats_auth.save_key("old")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fchmod", _fail_fchmod)
        with pytest.raises(OSError, match="disk full"):
            seats_auth.save_key("new")
    assert seats_auth.load_key() is not None, "seats.json was emptied by the failed save"
    assert seats_auth.load_key() == "old"
    assert [p.name for p in config.iterdir()] == ["seats.json"]
