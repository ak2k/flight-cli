"""A credential save creates its config directory 0700 and leaves an existing one alone.

The directory names `pp.json` and `seats.json` to every local user at the
default mode, though neither file is readable. `~/.config/flight-cli` can hold
files that are not credentials, so a directory that already exists keeps its mode.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from flight_cli.pp import auth as pp_auth
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    yield
    _ = os.umask(old)


def _save_into(store: str, config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `store`'s module-level paths at `config`, then run its real save."""
    if store == "pp":
        monkeypatch.setattr(pp_auth, "CONFIG_DIR", config)
        monkeypatch.setattr(pp_auth, "TOKENS_PATH", config / "pp.json")
        # Before any write: the save overwrites whatever file these name.
        assert pp_auth.CONFIG_DIR.is_relative_to(tmp_path), pp_auth.CONFIG_DIR
        assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path), pp_auth.TOKENS_PATH
        pp_auth.save_tokens(pp_auth.Tokens("a", "r", 4102444800))
    else:
        monkeypatch.setattr(seats_auth, "CONFIG_DIR", config)
        monkeypatch.setattr(seats_auth, "KEY_PATH", config / "seats.json")
        assert seats_auth.CONFIG_DIR.is_relative_to(tmp_path), seats_auth.CONFIG_DIR
        assert seats_auth.KEY_PATH.is_relative_to(tmp_path), seats_auth.KEY_PATH
        seats_auth.save_key("k")


@pytest.mark.usefixtures("umask_022")
@pytest.mark.parametrize("store", ["pp", "seats"])
def test_a_save_creates_the_config_dir_0700_and_leaves_an_existing_one(
    store: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "outer" / "flight-cli"
    _save_into(store, fresh, tmp_path, monkeypatch)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
    # `parents=True`: only the leaf is private; a missing parent keeps the default.
    assert stat.S_IMODE(fresh.parent.stat().st_mode) == 0o755
    saved = fresh / ("pp.json" if store == "pp" else "seats.json")
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    _save_into(store, existing, tmp_path, monkeypatch)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
