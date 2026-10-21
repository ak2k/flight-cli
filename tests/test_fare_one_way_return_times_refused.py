# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--return-times` on a `fare` with no `--return`.

A one-way has no return leg to carry the window, so `fare` must refuse it with
exit 2 before any request, as `search` does (tests/test_one_way_return_flags.py),
rather than answer at every hour."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from test_open_jaw_search import _days, _matrix

if TYPE_CHECKING:
    import pytest

_MEMORY = Path(__file__).resolve().parent.parent / "docs" / "memories" / "calendar_two_phase.md"


def _fare(*extra: str) -> Any:
    out, back = _days()
    args = ["fare", "--no-pp", "JFK", "LHR", "--dep", out.isoformat()]
    if "--return" in extra:
        extra = (*extra, back.isoformat())
    args += ["--no-matrix-url", "--no-google-url"]
    return CliRunner().invoke(cli.app, [*args, *extra], env={"COLUMNS": "200", "NO_COLOR": "1"})


def test_fare_with_no_return_refuses_return_times_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix = _matrix(monkeypatch)
    result = _fare("--return-times", "evening")
    assert result.exit_code == 2, result.output
    printed = " ".join(result.stderr.split())
    assert "--return-times sets the return's filters, and needs a --return." in printed, printed
    assert "Drop it, or add one." in printed
    assert matrix.searches == []
    assert result.stdout == ""


def test_fare_with_no_return_still_takes_an_empty_return_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix = _matrix(monkeypatch)
    result = _fare("--return-times", "")
    assert result.exit_code == 0, result.output
    assert len(matrix.searches) == 1


def test_fare_with_a_return_still_takes_return_times(monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = _matrix(monkeypatch)
    result = _fare("--return-times", "evening", "--return")
    assert result.exit_code == 0, result.output
    assert len(matrix.searches) == 1
    assert len(matrix.searches[0].legs) == 2


def test_the_memory_says_fare_refuses_it_too() -> None:
    text = " ".join(_MEMORY.read_text().split())
    assert "a `fare` with no `--return`" in text
