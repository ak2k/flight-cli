# pyright: reportPrivateUsage=false
"""Under auto, a multi-cabin search whose every Google Flights cabin is refused
goes to Matrix, as a one-cabin auto search does, instead of exiting 1."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli._gf_errors import GfThrottledError
from flight_cli.domain import Cabin

_DEP = date.today() + timedelta(days=45)


def _search(*extra: str) -> list[str]:
    return [
        *("search", "--cash-only", "--no-google-url", "--no-matrix-url"),
        *("JFK", "LAX", "--dep", _DEP.isoformat()),
        *("--cabin", "economy,business", "--format", "json"),
        *extra,
    ]


@pytest.fixture
def matrix_ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Cabin, ...]]:
    """Every Google Flights cabin query is throttled; Matrix records the cabins
    it was asked for and writes a marker document."""
    ran: list[tuple[Cabin, ...]] = []

    def _refused(*_a: object, **_kw: object) -> list[object]:
        raise GfThrottledError("throttled")

    def _matrix(*, cabins: tuple[Cabin, ...], **_kw: object) -> None:
        ran.append(cabins)
        sys.stdout.write('{"answered_by": "matrix"}')

    monkeypatch.setattr(cli, "_gflight_results", _refused)
    monkeypatch.setattr(cli, "_run_matrix_path_multi", _matrix)
    return ran


def test_under_auto_every_refused_cabin_sends_the_search_to_matrix(
    matrix_ran: list[tuple[Cabin, ...]],
) -> None:
    result = CliRunner().invoke(cli.app, _search())
    assert result.exit_code == 0, result.output
    assert matrix_ran == [(Cabin.COACH, Cabin.BUSINESS)]
    assert json.loads(result.stdout) == {"answered_by": "matrix"}
    assert "All Google Flights cabin queries failed" not in result.stderr
    assert "Using Matrix: every Google Flights cabin query failed." in result.stderr


def test_an_explicit_google_backend_still_exits_1_when_every_cabin_is_refused(
    matrix_ran: list[tuple[Cabin, ...]],
) -> None:
    """Green at the base: --backend gflight never falls back."""
    result = CliRunner().invoke(cli.app, _search("--backend", "gflight"))
    assert result.exit_code == 1, result.output
    assert matrix_ran == []
    assert "All Google Flights cabin queries failed." in result.stderr


def test_fast_still_exits_1_when_every_cabin_is_refused(
    matrix_ran: list[tuple[Cabin, ...]],
) -> None:
    """`--fast` asks for Google Flights alone, as a one-cabin `--fast` does."""
    result = CliRunner().invoke(cli.app, _search("--fast"))
    assert result.exit_code == 1, result.output
    assert matrix_ran == []
    assert "All Google Flights cabin queries failed." in result.stderr
    assert "Using Matrix" not in result.stderr
