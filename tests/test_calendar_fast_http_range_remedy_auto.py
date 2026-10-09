"""The remedy for `calendar --fast -d 5-7 --gf-transport http` names `auto` beside
`browser`: under `--fast`, `auto` is the browser grid, so it serves the same range."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    import pytest

_START = date.today() + timedelta(days=60)
_REMEDY = "For the range on Google, run with --gf-transport browser or auto."
_NOTE = Path(__file__).resolve().parent.parent / "docs/memories/gf_multi_page_legs.md"


def _calendar(transport: str) -> tuple[int, str, str]:
    end = _START + timedelta(days=13)
    args = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", end.isoformat()]
    args += ["--no-cache", "--fast", "-d", "5-7", "--gf-transport", transport]
    result = CliRunner().invoke(cli.app, args)
    return result.exit_code, result.stdout, " ".join(result.stderr.split())


def test_the_http_refusal_names_auto_beside_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_load(*_a: object, **_k: object) -> object:
        raise AssertionError("a refusal loads nothing")

    monkeypatch.setattr(cli, "_run_fast_calendar_grid", _no_load)
    code, out, err = _calendar("http")
    assert (code, out) == (1, "")
    assert err.endswith(_REMEDY), err


def test_auto_serves_the_range_the_remedy_names(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    def _browser_grid(*_a: object, **_k: object) -> None:
        loads.append("browser grid")

    monkeypatch.setattr(cli, "_run_fast_browser_grid", _browser_grid)
    code, _, err = _calendar("auto")
    assert (code, loads) == (0, ["browser grid"]), err


def test_the_note_on_the_refusal_quotes_the_remedy() -> None:
    assert f"ends with `{_REMEDY}`" in " ".join(_NOTE.read_text().split())
