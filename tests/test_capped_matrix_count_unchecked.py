# pyright: reportPrivateUsage=false
"""A capped Matrix table whose count outruns the fares Matrix sent says the count
was not checked against the cap; a count cut to the fares kept, or no cap, says
nothing."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import SearchResult
from test_capped_unreadable_matrix import _matrix_answers, _solution

if TYPE_CHECKING:
    from click.testing import Result

_DEP = date.today() + timedelta(days=45)
_FIXTURE = (
    Path(__file__).parent / "fixtures" / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
)
_NOTE = "not checked against the cap of GBP 5000"


@pytest.fixture
def matrix_gbp(monkeypatch: pytest.MonkeyPatch) -> None:
    body: dict[str, Any] = json.loads(_FIXTURE.read_text())

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return SearchResult.from_api(body)

    monkeypatch.setattr(cli, "_run", _run)


pytestmark = pytest.mark.usefixtures("matrix_gbp")


def _search(*args: str) -> str:
    result: Result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LHR", "--dep", _DEP.isoformat(), "--cash-only"),
            *("--no-matrix-url", "--no-google-url", "--backend", "matrix"),
            *("--currency", "GBP", "-n", "25", *args),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    return " ".join(result.stdout.split())


def test_a_cap_that_drops_no_fare_says_matrix_s_count_was_not_checked_against_it() -> None:
    out = _search("--max-price", "5000")
    assert "88 solutions" in out
    assert _NOTE in out
    assert "raise -n to check more fares" in out


def test_a_cap_that_cut_the_count_to_the_fares_kept_prints_no_such_line() -> None:
    out = _search("--max-price", "690")
    assert "14 solutions" in out
    assert "not checked against the cap" not in out


def test_a_search_without_a_cap_prints_no_such_line() -> None:
    out = _search()
    assert "88 solutions" in out
    assert "not checked against the cap" not in out


def test_a_cap_that_drops_unreadable_fares_from_a_whole_answer_prints_no_such_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(
        monkeypatch,
        _solution("s1", "USD750.00", total="USD1500.00"),
        _solution("s2", "USD700.00", total=None),
        _solution("s3", "USD650.00", total=None),
    )
    result: Result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cash-only"),
            *("--no-matrix-url", "--no-google-url", "--backend", "matrix"),
            *("--cabin", "business", "--adults", "2", "-n", "25", "--max-price", "2000"),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    out = " ".join(result.stdout.split())
    assert "3 solutions" in out
    assert "not checked against the cap" not in out
