# pyright: reportPrivateUsage=false
"""`flight watch run` names the seen file on one stderr line when it cannot save it,
even when the config path carries a newline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flight_cli import _watch

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_an_unlockable_config_dir_names_the_seen_file_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "h" / "line1\nline2"
    assert directory.is_relative_to(tmp_path)
    directory.parent.mkdir()
    _ = directory.write_text("not a directory")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(directory))
    _watch._remember({})
    named = str(directory).replace("\n", " ")
    assert capsys.readouterr().err.splitlines() == [
        f"{named} cannot be created: File exists",
        f"{named}/watches-seen.json was not saved",
    ]


def test_an_unwritable_seen_file_is_named_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "line1\nline2"
    assert directory.is_relative_to(tmp_path)
    (directory / "watches-seen.json").mkdir(parents=True)
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(directory))
    _watch._remember({})
    named = str(directory).replace("\n", " ")
    assert capsys.readouterr().err.splitlines() == [
        f"{named}/watches-seen.json was not saved: Is a directory"
    ]
