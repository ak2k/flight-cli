# pyright: reportPrivateUsage=false
"""`flight watch run` saves each priced watch's lowest fare in `watches-seen.json`
beside `watches.json` and names the change since the last run on that watch's line."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import _watch, cli
from test_watch_run import _one_day

if TYPE_CHECKING:
    from collections.abc import Callable

_WINDOW = ("--from", "2026-10-20", "--to", "2026-11-16")
_HEAD = "1  JFK-LAX  2026-10-20..2026-11-16  economy"


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    assert _watch.watches_path().is_relative_to(tmp_path)


def _lines(*args: str) -> list[str]:
    ran = CliRunner().invoke(cli.app, ["watch", *args])
    assert ran.exit_code == 0, ran.output
    return ran.stdout.splitlines()


def test_a_second_run_names_the_fare_the_last_run_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    assert runner.invoke(cli.app, ["watch", "add", "JFK", "LAX", *_WINDOW]).exit_code == 0
    _one_day(monkeypatch, 450.0, "USD")
    first = runner.invoke(cli.app, ["watch", "run"])
    assert first.exit_code == 0, first.output
    assert first.stdout.splitlines() == [f"{_HEAD}: lowest USD 450 on 2026-10-31"]
    _one_day(monkeypatch, 400.0, "USD")
    second = runner.invoke(cli.app, ["watch", "run"])
    assert second.exit_code == 0, second.output
    assert second.stdout.splitlines() == [
        f"{_HEAD}: lowest USD 400 on 2026-10-31, down from USD 450 at the last run"
    ]


@pytest.mark.parametrize(
    ("price", "tail"),
    [
        (400.0, "under the ceiling, down from USD 450 at the last run"),
        (500.0, "over the ceiling, up from USD 450 at the last run"),
        (450.0, "under the ceiling, same as the last run"),
    ],
)
def test_the_change_follows_the_ceiling_verdict(
    monkeypatch: pytest.MonkeyPatch, price: float, tail: str
) -> None:
    assert _lines("add", "JFK", "LAX", *_WINDOW, "--below", "475") == [
        f"Added 1: {_HEAD[3:]}  below 475"
    ]
    _one_day(monkeypatch, 450.0, "USD")
    _ = _lines("run")
    _one_day(monkeypatch, price, "USD")
    assert _lines("run") == [f"{_HEAD}  below 475: lowest USD {price:g} on 2026-10-31, {tail}"]


def _none(*_a: object, **_kw: object) -> list[object]:
    return []


def test_a_skipped_failed_or_unpriced_line_keeps_the_saved_fare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = _lines("add", "JFK", "LAX", *_WINDOW)
    _ = _lines("add", "JFK", "LAX", *_WINDOW, "--award")
    _ = _lines("add", "JFK", "LAX")
    _one_day(monkeypatch, 450.0, "USD")
    _ = _lines("run")
    seen = tmp_path / "watches-seen.json"
    assert seen.exists()
    assert list(json.loads(seen.read_text())) == ["1"]
    assert stat.S_IMODE(seen.stat().st_mode) == 0o600

    def _down(*_a: object, **_kw: object) -> list[object]:
        raise RuntimeError("down")

    monkeypatch.setattr(cli, "lowest_fare_rows", _down)
    failed = _lines("run")
    assert failed[0] == f"{_HEAD}: lookup failed, RuntimeError"
    monkeypatch.setattr(cli, "lowest_fare_rows", _none)
    assert _lines("run")[0] == f"{_HEAD}: no priced date"
    _one_day(monkeypatch, float("nan"), "USD")
    assert _lines("run")[0] == f"{_HEAD}: lowest USD nan on 2026-10-31"
    assert [line.split(": ", 1)[1] for line in failed[1:]] == [
        "skipped, award watches are not polled yet",
        "skipped, a watch with no dates is not polled yet",
    ]
    _one_day(monkeypatch, 420.0, "USD")
    assert _lines("run")[0] == (
        f"{_HEAD}: lowest USD 420 on 2026-10-31, down from USD 450 at the last run"
    )


def test_a_currency_change_adds_nothing_and_the_new_currency_is_then_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = _lines("add", "LHR", "JFK", *_WINDOW)
    head = "1  LHR-JFK  2026-10-20..2026-11-16  economy"
    _one_day(monkeypatch, 450.0, "USD")
    _ = _lines("run")
    _one_day(monkeypatch, 300.0, "GBP")
    assert _lines("run") == [f"{head}: lowest GBP 300 on 2026-10-31"]
    _one_day(monkeypatch, 250.5, "GBP")
    assert _lines("run") == [
        f"{head}: lowest GBP 250.50 on 2026-10-31, down from GBP 300 at the last run"
    ]


def test_a_reused_id_is_not_compared_with_the_old_watch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = _lines("add", "JFK", "LAX", *_WINDOW)
    _one_day(monkeypatch, 450.0, "USD")
    _ = _lines("run")
    assert (tmp_path / "watches-seen.json").exists()
    _ = _lines("rm", "1")
    _ = _lines("add", "JFK", "SFO", *_WINDOW)
    _one_day(monkeypatch, 400.0, "USD")
    assert _lines("run") == [
        "1  JFK-SFO  2026-10-20..2026-11-16  economy: lowest USD 400 on 2026-10-31"
    ]


def test_an_unusable_seen_file_is_ignored_and_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = _lines("add", "JFK", "LAX", *_WINDOW)
    seen = tmp_path / "watches-seen.json"
    seen.write_text("{not json")
    _one_day(monkeypatch, 450.0, "USD")
    assert _lines("run") == [f"{_HEAD}: lowest USD 450 on 2026-10-31"]
    _one_day(monkeypatch, 460.0, "USD")
    assert _lines("run") == [
        f"{_HEAD}: lowest USD 460 on 2026-10-31, up from USD 450 at the last run"
    ]


def test_a_seen_file_that_cannot_be_written_is_named_on_stderr_and_run_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = _lines("add", "JFK", "LAX", *_WINDOW)
    seen = tmp_path / "watches-seen.json"
    seen.mkdir()
    _one_day(monkeypatch, 450.0, "USD")
    ran = CliRunner().invoke(cli.app, ["watch", "run"])
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [f"{_HEAD}: lowest USD 450 on 2026-10-31"]
    assert ran.stderr.startswith(f"{seen} was not saved: "), ran.stderr
    assert seen.is_dir()
    assert list(tmp_path.glob(".watches-seen.json.*")) == []


def _unreadable(directory: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    directory.chmod(0o300)


def _no_flock(_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl  # the module does not exist on Windows

    def _refuse(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(fcntl, "flock", _refuse)


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is a no-op on Windows")
@pytest.mark.parametrize(
    "refuse",
    [
        pytest.param(
            _unreadable,
            id="unreadable",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root opens any directory"),
        ),
        pytest.param(_no_flock, id="no-flock"),
    ],
)
def test_a_config_dir_that_cannot_be_locked_costs_the_save_and_run_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    refuse: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    _ = _lines("add", "JFK", "LAX", *_WINDOW)
    _one_day(monkeypatch, 450.0, "USD")
    refuse(tmp_path, monkeypatch)
    try:
        ran = CliRunner().invoke(cli.app, ["watch", "run"])
    finally:
        tmp_path.chmod(0o700)
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [f"{_HEAD}: lowest USD 450 on 2026-10-31"]
    assert f"{tmp_path / 'watches-seen.json'} was not saved" in ran.stderr, ran.stderr
    assert not (tmp_path / "watches-seen.json").exists()


def test_the_help_the_docstrings_and_the_readme_say_run_saves_the_last_fare() -> None:
    root = Path(__file__).resolve().parent.parent
    helped = CliRunner().invoke(cli.app, ["watch", "--help"], env={"COLUMNS": "200"}).stdout
    readme = (root / "README.md").read_text()
    texts = [
        " ".join(helped.split()),
        cli.__doc__ or "",
        _watch.__doc__ or "",
        _watch.watch_run.__doc__ or "",
        readme,
    ]
    assert all("nothing is sent or saved" not in t for t in texts)
    assert "sends and saves nothing" not in readme
    assert "last run" in texts[0]
    assert "`watches-seen.json`" in readme
    assert "`watches-seen.json`" in texts[2]
