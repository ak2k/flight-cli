# pyright: reportUnusedFunction=false
"""`save_key` leaves an existing loose key file at 0600.

The mode while the key is written is covered in test_auth_key_fchmod.py.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from flight_cli.providers.seats_aero import auth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def _key_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    config = tmp_path / "config"
    monkeypatch.setattr(auth, "CONFIG_DIR", config)
    monkeypatch.setattr(auth, "KEY_PATH", config / "seats.json")
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    # Before any write: the save overwrites whatever file these name.
    assert auth.KEY_PATH.is_relative_to(tmp_path), auth.KEY_PATH
    assert auth.CONFIG_DIR.is_relative_to(tmp_path), auth.CONFIG_DIR
    old = os.umask(0o022)
    yield config
    _ = os.umask(old)


def test_an_existing_loose_key_file_is_tightened_to_0600(_key_dir: Path) -> None:
    _key_dir.mkdir()
    auth.KEY_PATH.write_text("{}")
    auth.KEY_PATH.chmod(0o644)
    auth.save_key("pro_new")
    assert stat.S_IMODE(auth.KEY_PATH.stat().st_mode) == 0o600
    assert auth.load_key() == "pro_new"
