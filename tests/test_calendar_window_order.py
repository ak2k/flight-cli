"""`calendar` refuses a window whose --end is before its --start, before any backend."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    import pytest

_SKILL = Path(__file__).parents[1] / ".claude" / "skills" / "flight-search" / "SKILL.md"


def test_an_end_before_the_start_is_refused_before_any_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_load(*_a: object, **_k: object) -> object:
        raise AssertionError("a refusal loads nothing")

    monkeypatch.setattr(cli, "_run_fast_calendar_grid", _no_load)
    monkeypatch.setattr(cli, "_run_fast_browser_grid", _no_load)
    start = date.today() + timedelta(days=60)
    end = start - timedelta(days=5)
    args = ["calendar", "JFK", "LAX", "--start", start.isoformat(), "--end", end.isoformat()]
    result = CliRunner().invoke(
        cli.app, [*args, "--no-cache", "--fast", "--gf-transport", "http", "-d", "5-7"]
    )
    err = " ".join(result.stderr.split())
    assert (result.exit_code, result.stdout) == (2, "")
    assert f"--end '{end.isoformat()}' is before --start '{start.isoformat()}'" in err
    assert "An `--end` before `--start` exits 2 before any backend is asked." in _SKILL.read_text()
