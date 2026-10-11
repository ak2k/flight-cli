"""`flight watch add|rm` under concurrency: one writer's update is not lost to another's."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import _watch, cli

if TYPE_CHECKING:
    from collections.abc import Callable

_PAUSE_SECS = 1.0


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    path = _watch.watches_path()
    assert path.is_relative_to(tmp_path)
    return path


def _add(destination: str) -> Callable[[], None]:
    def run() -> None:
        _watch.watch_add(
            origin="JFK",
            destination=destination,
            dep=None,
            dep_from=None,
            dep_to=None,
            below=None,
            cabin="economy",
            award=False,
        )

    return run


def _rm(watch_id: int) -> Callable[[], None]:
    def run() -> None:
        _watch.watch_rm(watch_id)

    return run


def _race(
    monkeypatch: pytest.MonkeyPatch,
    store: Path,
    first: Callable[[], None],
    second: Callable[[], None],
) -> None:
    """Run `first` and, once it has read the store, `second` to completion; `first`
    waits for `second` to finish (or `_PAUSE_SECS`) before it goes on to write."""
    real_read = Path.read_bytes
    first_has_read = threading.Event()
    second_is_done = threading.Event()
    errors: list[BaseException] = []

    def read_bytes(self: Path) -> bytes:
        data = real_read(self)
        if self == store and threading.current_thread().name == "first":
            first_has_read.set()
            _ = second_is_done.wait(_PAUSE_SECS)
        return data

    def guarded(job: Callable[[], None], done: threading.Event | None) -> Callable[[], None]:
        def run() -> None:
            try:
                job()
            except BaseException as e:  # re-asserted on the main thread
                errors.append(e)
            finally:
                if done:
                    done.set()

        return run

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    t1 = threading.Thread(target=guarded(first, None), name="first", daemon=True)
    t2 = threading.Thread(target=guarded(second, second_is_done), name="second", daemon=True)
    t1.start()
    assert first_has_read.wait(10)
    t2.start()
    t1.join(10)
    t2.join(10)
    assert not (t1.is_alive() or t2.is_alive())
    assert errors == []


def _destinations(store: Path) -> list[str]:
    return [w["destination"] for w in json.loads(store.read_text())]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (_add("CDG"), _add("AMS"), ["LHR", "CDG", "AMS"]),
        (_rm(1), _add("AMS"), ["AMS"]),
    ],
    ids=["add-then-add", "rm-then-add"],
)
def test_a_second_writer_starting_mid_update_does_not_lose_either_update(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: Callable[[], None],
    second: Callable[[], None],
    expected: list[str],
) -> None:
    saved = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    assert saved.exit_code == 0, saved.output
    _race(monkeypatch, store, first, second)
    assert _destinations(store) == expected
    assert sorted(p.name for p in store.parent.iterdir()) == ["watches.json"]


def test_rm_with_no_config_directory_exits_1_and_creates_none(store: Path) -> None:
    assert not store.parent.exists()
    result = CliRunner().invoke(cli.app, ["watch", "rm", "1"])
    assert result.exit_code == 1, result.output
    assert "no watch with id 1" in result.output
    assert not store.parent.exists()


def test_a_command_that_fails_inside_the_lock_releases_it(store: Path) -> None:
    store.parent.mkdir(parents=True)
    store.write_text("not json")
    failed = CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    assert failed.exit_code == 1, failed.output
    store.unlink()
    outcome: list[int] = []

    def again() -> None:
        outcome.append(CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"]).exit_code)

    t = threading.Thread(target=again, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the second add is still waiting for the lock"
    assert outcome == [0]
    assert _destinations(store) == ["LHR"]


@pytest.mark.parametrize("lock_wait_secs", [0.2])
def test_a_command_waiting_on_a_store_lock_left_held_fails_instead_of_hanging(
    store: Path, lock_wait_secs: float
) -> None:
    store.parent.mkdir(parents=True)
    held = os.open(store.parent, os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX)
    # Unbounded, the add would wait forever; letting go later makes that a failure here.
    release = threading.Timer(lock_wait_secs * 25, fcntl.flock, (held, fcntl.LOCK_UN))
    release.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="lock"):
            CliRunner().invoke(cli.app, ["watch", "add", "JFK", "LHR"])
    finally:
        release.cancel()
        release.join()
        os.close(held)
    assert not store.exists()
