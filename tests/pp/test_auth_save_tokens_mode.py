# pyright: reportUnusedFunction=false
"""`save_tokens` holds the new token at 0600 for the whole write, also over a loose file.

`O_CREAT` applies its mode only to a file it creates, so a file already at 0644
or 0666 holds the new bearer token at that mode unless it is tightened before
the write.
"""

from __future__ import annotations

import os
import stat
from typing import IO, TYPE_CHECKING, Any, Self

import pytest

from flight_cli.pp import auth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType


@pytest.fixture
def _token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    config = tmp_path / "config"
    monkeypatch.setattr(auth, "CONFIG_DIR", config)
    monkeypatch.setattr(auth, "TOKENS_PATH", config / "pp.json")
    # Before any write: the save overwrites whatever file these name.
    assert auth.TOKENS_PATH.is_relative_to(tmp_path), auth.TOKENS_PATH
    assert auth.CONFIG_DIR.is_relative_to(tmp_path), auth.CONFIG_DIR
    old = os.umask(0o022)
    yield config
    _ = os.umask(old)


class _WriteSpy:
    """Wraps the file `save_tokens` writes and records the file's mode at each write."""

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
def test_an_existing_loose_token_file_is_0600_when_the_new_token_is_written(
    _token_dir: Path, monkeypatch: pytest.MonkeyPatch, loose: int
) -> None:
    _token_dir.mkdir()
    _ = auth.TOKENS_PATH.write_text("{}")
    auth.TOKENS_PATH.chmod(loose)
    at_write = _modes_at_write(monkeypatch)
    auth.save_tokens(auth.Tokens("a", "r", 4102444800))
    assert [oct(m) for m in at_write] == [oct(0o600)]
    assert stat.S_IMODE(auth.TOKENS_PATH.stat().st_mode) == 0o600


def test_a_new_token_file_is_0600_when_the_token_is_written(
    _token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_write = _modes_at_write(monkeypatch)
    auth.save_tokens(auth.Tokens("a", "r", 4102444800))
    assert [oct(m) for m in at_write] == [oct(0o600)]
    assert stat.S_IMODE(auth.TOKENS_PATH.stat().st_mode) == 0o600
    loaded = auth.load_tokens()
    assert loaded is not None
    assert loaded.access_token == "a"  # noqa: S105 — asserting a dummy round-trip


def test_the_mode_is_set_through_the_open_file_not_the_path_after_the_write(
    _token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _token_dir.mkdir()
    _ = auth.TOKENS_PATH.write_text("{}")
    auth.TOKENS_PATH.chmod(0o644)

    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("save_tokens tightened the mode by path")

    monkeypatch.setattr(os, "chmod", refuse)
    auth.save_tokens(auth.Tokens("a", "r", 4102444800))
    assert stat.S_IMODE(auth.TOKENS_PATH.stat().st_mode) == 0o600
