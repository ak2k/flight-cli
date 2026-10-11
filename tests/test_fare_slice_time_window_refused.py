# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--depart-times` and `--return-times` beside `--slice` on the deprecated `fare`.

`search` refuses them (tests/test_slice_time_window_refused.py); `fare` builds
its slice legs with no time window, so it must refuse them the same way, with
exit 2 before any request, rather than answer at every hour. The help of both
commands says so."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from test_open_jaw_search import _days, _matrix

_MEMORY = Path(__file__).resolve().parent.parent / "docs" / "memories" / "gf_separate_tickets.md"


def _fare(*extra: str) -> Any:
    out, back = _days()
    args = [
        "fare",
        "--no-pp",
        "--slice",
        f"JFK-LHR:{out}",
        "--slice",
        f"CDG-JFK:{back}",
        "--no-matrix-url",
        "--no-google-url",
    ]
    return CliRunner().invoke(cli.app, [*args, *extra], env={"COLUMNS": "200", "NO_COLOR": "1"})


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        pytest.param(
            ("--depart-times", "morning"),
            "--depart-times would reach no --slice: a slice takes no time window.",
            id="depart-times",
        ),
        pytest.param(
            ("--return-times", "evening"),
            "--return-times would reach no --slice: a slice takes no time window.",
            id="return-times",
        ),
        pytest.param(
            ("--depart-times", "morning", "--return-times", "evening"),
            "--depart-times and --return-times would reach no --slice: a slice takes no time "
            "window.",
            id="both",
        ),
    ],
)
def test_fare_refuses_a_time_window_beside_a_slice_before_any_request(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], said: str
) -> None:
    matrix = _matrix(monkeypatch)
    result = _fare(*extra)
    assert result.exit_code == 2, result.output
    printed = " ".join(result.stderr.split())
    assert said in printed, printed
    assert "Drop the time flags, or give the trip as origin and destination." in printed
    assert matrix.searches == []
    assert result.stdout == ""


def test_fare_still_takes_a_time_window_without_a_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = _matrix(monkeypatch)
    out, _ = _days()
    result = CliRunner().invoke(
        cli.app,
        [
            "fare",
            "--no-pp",
            "JFK",
            "LHR",
            "--dep",
            out.isoformat(),
            "--depart-times",
            "morning",
            "--no-matrix-url",
            "--no-google-url",
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert len(matrix.searches) == 1


@pytest.mark.parametrize(
    ("command", "said"),
    [
        pytest.param(
            "search", "give --arrive-times instead. Refused beside --slice.", id="search-depart"
        ),
        pytest.param(
            "search",
            "give --return-arrive-times instead. Refused beside --slice.",
            id="search-return",
        ),
        pytest.param(
            "fare", "(comma list: morning,evening). Refused beside --slice.", id="fare-depart"
        ),
        pytest.param("fare", "return times-of-day. Refused beside --slice.", id="fare-return"),
    ],
)
def test_the_help_of_a_time_flag_says_a_slice_refuses_it(command: str, said: str) -> None:
    result = CliRunner().invoke(cli.app, [command, "--help"], env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    flat = " ".join(result.stdout.replace("│", " ").split())
    assert said in flat


def test_the_memory_says_fare_refuses_it_too() -> None:
    text = " ".join(_MEMORY.read_text().split())
    assert "called by `search` and by the deprecated `fare`" in text
