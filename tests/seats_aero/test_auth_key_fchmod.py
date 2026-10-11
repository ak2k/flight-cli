# pyright: reportUnusedFunction=false
"""`save_key` holds the new key at 0600 for the whole write, also over a loose file.

`O_CREAT` applies its mode only to a file it creates, so a file already at 0644
or 0666 held the new key at that mode until the chmod that followed the write.
"""

from __future__ import annotations

import os
import stat
from typing import IO, TYPE_CHECKING, Any, Self

import pytest

from flight_cli.providers import seats_aero
from flight_cli.providers.seats_aero import auth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType


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


class _WriteSpy:
    """Wraps the file `save_key` writes and records the file's mode at each write."""

    def __init__(self, fh: IO[Any], fd: int, seen: list[int]) -> None:
        self._fh = fh
        self._fd = fd
        self._seen = seen

    def write(self, text: str) -> int:
        self._seen.append(stat.S_IMODE(os.fstat(self._fd).st_mode))
        return self._fh.write(text)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._fh.close()


def _modes_at_write(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = os.fdopen

    def spy(fd: int, mode: str = "r") -> _WriteSpy:
        return _WriteSpy(real(fd, mode), fd, seen)

    monkeypatch.setattr(os, "fdopen", spy)
    return seen


@pytest.mark.parametrize("loose", [0o644, 0o666])
def test_an_existing_loose_key_file_is_0600_when_the_new_key_is_written(
    _key_dir: Path, monkeypatch: pytest.MonkeyPatch, loose: int
) -> None:
    _key_dir.mkdir()
    _ = auth.KEY_PATH.write_text("{}")
    auth.KEY_PATH.chmod(loose)
    at_write = _modes_at_write(monkeypatch)
    auth.save_key("pro_new")
    assert [oct(m) for m in at_write] == [oct(0o600)]
    assert stat.S_IMODE(auth.KEY_PATH.stat().st_mode) == 0o600
    assert auth.load_key() == "pro_new"


def test_a_new_key_file_is_0600_when_the_key_is_written(
    _key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_write = _modes_at_write(monkeypatch)
    auth.save_key("pro_secret")
    assert [oct(m) for m in at_write] == [oct(0o600)]
    assert stat.S_IMODE(auth.KEY_PATH.stat().st_mode) == 0o600
    assert auth.load_key() == "pro_secret"


def test_a_shorter_key_replaces_a_longer_one_whole(_key_dir: Path) -> None:
    auth.save_key("pro_" + "a" * 40)
    auth.save_key("pro_short")
    assert auth.load_key() == "pro_short"


def test_the_package_docstring_names_the_real_auth_command() -> None:
    doc = seats_aero.__doc__ or ""
    assert "`flight auth seats-aero key`" in doc
    assert "flight auth seats key" not in doc
