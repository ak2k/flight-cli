# pyright: reportUnusedFunction=false
"""`save_key` creates the key file 0600 from the first byte, not by a chmod afterward.

`write_text` makes the file at the process umask, so the old write-then-chmod
left the Pro API key readable by other local users until the chmod ran.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from flight_cli.providers.seats_aero import auth

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def _modes_at_chmod(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = Path.chmod

    def spy(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == auth.KEY_PATH:
            seen.append(stat.S_IMODE(self.stat().st_mode))
        real(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", spy)
    return seen


def test_a_new_key_file_is_0600_before_the_chmod_runs(
    _key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_chmod = _modes_at_chmod(monkeypatch)
    auth.save_key("pro_secret")
    assert [oct(m) for m in before_chmod] == [oct(0o600)]
    assert stat.S_IMODE(auth.KEY_PATH.stat().st_mode) == 0o600
    assert auth.load_key() == "pro_secret"


def test_an_existing_loose_key_file_is_tightened_to_0600(_key_dir: Path) -> None:
    _key_dir.mkdir()
    auth.KEY_PATH.write_text("{}")
    auth.KEY_PATH.chmod(0o644)
    auth.save_key("pro_new")
    assert stat.S_IMODE(auth.KEY_PATH.stat().st_mode) == 0o600
    assert auth.load_key() == "pro_new"
