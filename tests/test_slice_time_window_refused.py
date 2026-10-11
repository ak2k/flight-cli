# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--depart-times` and `--return-times` beside `--slice`.

A `--slice` takes no time window, on Matrix or on Google, so the flags would
reach no slice. The search is refused with exit 2 before any request, as a
`--flex` beside a slice is, rather than answered at every hour."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from flight_cli import cli
from test_open_jaw_search import _google, _matrix, _search

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md"
_MEMORY = _ROOT / "docs" / "memories" / "gf_separate_tickets.md"


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        pytest.param(
            ("--depart-times", "morning"),
            "--depart-times would reach no --slice: a slice takes no time window.",
            id="depart-times",
        ),
        pytest.param(
            ("--return-times", "9:30-13:45"),
            "--return-times would reach no --slice: a slice takes no time window.",
            id="return-times",
        ),
        pytest.param(
            ("--depart-times", "morning", "--return-times", "evening"),
            "--depart-times and --return-times would reach no --slice: a slice takes no time "
            "window.",
            id="both",
        ),
        pytest.param(
            ("--depart-times", "morning", "--backend", "gflight"),
            "--depart-times would reach no --slice: a slice takes no time window.",
            id="backend-gflight",
        ),
        pytest.param(
            ("--depart-times", "morning", "--backend", "matrix"),
            "--depart-times would reach no --slice: a slice takes no time window.",
            id="backend-matrix",
        ),
    ],
)
def test_a_time_window_beside_a_slice_is_exit_2_before_any_request(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], said: str
) -> None:
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--cash-only", *extra)
    assert result.exit_code == 2, result.output
    printed = " ".join(result.stderr.split())
    assert said in printed, printed
    assert "Drop the time flags, or give the trip as origin and destination." in printed
    assert "Using Matrix" not in printed
    assert google.calls == []
    assert matrix.searches == []
    assert result.stdout == ""


def test_the_slice_blocker_takes_no_top_level_time_window() -> None:
    assert "top_codes" not in inspect.signature(cli._open_jaw_blocker).parameters


@pytest.mark.parametrize("doc", [_SKILL, _MEMORY], ids=["skill", "memory"])
def test_the_docs_say_a_time_window_beside_a_slice_is_refused(doc: Path) -> None:
    text = " ".join(doc.read_text().split())
    assert "`--depart-times`/`--return-times` beside a `--slice` are refused" in text
    assert "reach no slice" not in text
    assert "neither reaches a `--slice`" not in text
