"""`flight watch add|list|rm`: saved watches, with no network and no polling."""

from __future__ import annotations

import json
import resource
import stat
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result


def _watch(*args: str) -> Result:
    return CliRunner().invoke(cli.app, ["watch", *args])


def _store(root: Path) -> Path:
    return root / "flight-cli" / "watches.json"


@pytest.fixture
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    return root


def test_add_then_list_shows_the_watch_and_stores_it_0600(
    config_root: Path, tmp_path: Path
) -> None:
    store = _store(config_root)
    assert store.is_relative_to(tmp_path)
    added = _watch("add", "jfk", "LHR", "--below", "400")
    assert added.exit_code == 0, added.output
    assert "Added 1: JFK-LHR  any date  economy  below 400" in added.stdout
    listed = _watch("list")
    assert listed.exit_code == 0, listed.output
    assert listed.stdout.strip() == "1  JFK-LHR  any date  economy  below 400"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    saved = json.loads(store.read_text())
    assert [(w["origin"], w["destination"], w["below"], w["award"]) for w in saved] == [
        ("JFK", "LHR", 400.0, False)
    ]
    assert sorted(p.name for p in store.parent.iterdir()) == ["watches.json"]


def test_flight_cli_config_dir_moves_the_store(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = tmp_path / "moved"
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(moved))
    assert _watch("add", "SFO", "NRT").exit_code == 0
    assert (moved / "watches.json").is_file()
    assert not _store(config_root).exists()


def test_date_window_cabin_and_award_are_stored_and_listed(config_root: Path) -> None:
    window = _watch(
        "add", "JFK", "LHR", "--from", "2026-11-01", "--to", "2026-11-09", "--cabin", "Business"
    )
    assert window.exit_code == 0, window.output
    one_day = _watch("add", "LAX", "SYD", "--dep", "2026-12-24", "--award")
    assert one_day.exit_code == 0, one_day.output
    assert _watch("list").stdout.splitlines() == [
        "1  JFK-LHR  2026-11-01..2026-11-09  business",
        "2  LAX-SYD  2026-12-24  economy  award",
    ]
    saved = json.loads(_store(config_root).read_text())
    assert [(w["dep_from"], w["dep_to"], w["cabin"]) for w in saved] == [
        ("2026-11-01", "2026-11-09", "business"),
        ("2026-12-24", "2026-12-24", "economy"),
    ]


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["LONDON", "JFK"], id="origin-not-iata"),
        pytest.param(["JFK", "LONDON"], id="destination-not-iata"),
        pytest.param(["JFK", "LHR", "--dep", "2026-02-30"], id="no-such-day"),
        pytest.param(
            ["JFK", "LHR", "--dep", "2026-11-01", "--from", "2026-11-02"], id="dep-and-window"
        ),
        pytest.param(["JFK", "LHR", "--dep", ""], id="dep-empty"),
        pytest.param(
            ["JFK", "LHR", "--dep", "", "--from", "2026-11-01", "--to", "2026-11-09"],
            id="dep-empty-and-window",
        ),
        pytest.param(["JFK", "LHR", "--from", "2026-11-01"], id="window-without-end"),
        pytest.param(
            ["JFK", "LHR", "--from", "2026-11-09", "--to", "2026-11-01"], id="window-backwards"
        ),
        pytest.param(["JFK", "LHR", "--below", "0"], id="ceiling-zero"),
        pytest.param(["JFK", "LHR", "--below", "inf"], id="ceiling-infinite"),
        pytest.param(["JFK", "LHR", "--below", "1e400"], id="ceiling-overflows-to-infinite"),
        pytest.param(["JFK", "LHR", "--cabin", "steerage"], id="unknown-cabin"),
    ],
)
def test_a_refused_add_exits_2_and_writes_nothing(config_root: Path, args: list[str]) -> None:
    result = _watch("add", *args)
    assert result.exit_code == 2, result.output
    assert not config_root.exists()


def test_refusal_message_does_not_echo_the_raw_value(config_root: Path) -> None:
    result = _watch("add", "JFK", "\x07\u202ex")
    assert result.exit_code == 2
    assert "\x07" not in result.output
    assert "\u202e" not in result.output
    assert not config_root.exists()


def test_rm_deletes_one_watch_and_ids_are_not_reused(
    config_root: Path,
) -> None:
    for dest in ("LHR", "CDG", "FCO"):
        assert _watch("add", "JFK", dest).exit_code == 0
    removed = _watch("rm", "2")
    assert removed.exit_code == 0
    assert "Removed 2: JFK-CDG" in removed.stdout
    assert _watch("list").stdout.splitlines() == [
        "1  JFK-LHR  any date  economy",
        "3  JFK-FCO  any date  economy",
    ]
    assert _watch("add", "JFK", "AMS").stdout.startswith("Added 4:")
    before = _store(config_root).read_bytes()
    unknown = _watch("rm", "9")
    assert unknown.exit_code == 1
    assert "no watch with id 9" in unknown.output
    assert _store(config_root).read_bytes() == before


def test_list_with_no_store_says_so_and_creates_nothing(config_root: Path) -> None:
    result = _watch("list")
    assert result.exit_code == 0
    assert result.stdout.strip() == "No watches saved."
    assert not config_root.exists()


@pytest.mark.parametrize("command", [["list"], ["add", "JFK", "LHR"], ["rm", "1"]])
def test_an_unreadable_store_exits_1_and_is_never_overwritten(
    config_root: Path, command: list[str]
) -> None:
    store = _store(config_root)
    store.parent.mkdir(parents=True)
    store.write_text('[{"id": 1, "origin": "jfk"}]')
    result = _watch(*command)
    assert result.exit_code == 1, result.output
    assert "left untouched" in result.output
    assert store.read_text() == '[{"id": 1, "origin": "jfk"}]'


_JFK_LHR = {
    "id": 1,
    "origin": "JFK",
    "destination": "LHR",
    "dep_from": None,
    "dep_to": None,
    "below": None,
    "cabin": "economy",
    "award": False,
}
_TERMINAL_DRIVERS = ("\x07", "\x1b", "\u202e")


def test_unusable_store_message_drops_control_characters_from_an_unknown_key(
    config_root: Path,
) -> None:
    store = _store(config_root)
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps([{**_JFK_LHR, "\x07\x1b]0;pwn\x07\u202ek": 1}]))
    result = _watch("list")
    assert result.exit_code == 1, result.output
    assert "left untouched" in result.output
    assert not [c for c in _TERMINAL_DRIVERS if c in result.output]


def test_unusable_store_message_drops_control_characters_from_the_store_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "a\x07\u202eb"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("FLIGHT_CLI_CONFIG_DIR", raising=False)
    _store(root).parent.mkdir(parents=True)
    _store(root).write_text("not json")
    result = _watch("list")
    assert result.exit_code == 1, result.output
    assert "left untouched" in result.output
    assert not [c for c in _TERMINAL_DRIVERS if c in result.output]


@pytest.mark.parametrize("saved", [0, 1], ids=["first-add", "over-a-saved-store"])
def test_a_write_that_fails_part_way_keeps_the_old_store_and_no_temp_file(
    config_root: Path, saved: int
) -> None:
    for _ in range(saved):
        assert _watch("add", "JFK", "LHR").exit_code == 0
    store = _store(config_root)
    before = store.read_bytes() if saved else None
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    # A full disk, short of filling one: any write past 16 bytes fails with EFBIG.
    resource.setrlimit(resource.RLIMIT_FSIZE, (16, hard))
    try:
        failed = _watch("add", "SFO", "NRT")
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
    assert isinstance(failed.exception, OSError), failed.output
    assert (store.read_bytes() if store.exists() else None) == before
    assert sorted(p.name for p in store.parent.iterdir()) == ["watches.json"][:saved]
