"""A watch save creates its config directory 0700 and leaves an existing one alone.

The directory also holds `pp.json` and `seats.json`; at the default mode it lists
them to every local user. `~/.config/flight-cli` can hold files that are not
credentials, so a directory that already exists keeps its mode.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import _watch, cli

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    yield
    _ = os.umask(old)


def _save() -> None:
    _watch._save([])  # pyright: ignore[reportPrivateUsage] - the save under test


def _locked_cycle() -> None:
    with _watch._locked():  # pyright: ignore[reportPrivateUsage] - the lock under test
        pass


def _add() -> None:
    result = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    assert result.exit_code == 0, result.output


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.usefixtures("umask_022")
@pytest.mark.parametrize("run", [_save, _locked_cycle, _add], ids=["save", "locked", "add"])
def test_a_watch_save_creates_the_config_dir_0700_and_leaves_an_existing_one(
    run: Callable[[], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "outer" / "flight-cli"
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(fresh))
    # Before any write: the save creates whatever directory this names.
    assert _watch.watches_path().parent.is_relative_to(tmp_path), _watch.watches_path()
    run()
    assert _mode(fresh) == 0o700
    # `parents=True`: only the leaf is private; a missing parent keeps the default.
    assert _mode(fresh.parent) == 0o755

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(existing))
    assert _watch.watches_path().parent.is_relative_to(tmp_path), _watch.watches_path()
    run()
    assert _mode(existing) == 0o755
