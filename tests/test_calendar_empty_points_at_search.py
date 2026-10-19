"""An empty Matrix calendar points a one-date search at `flight search`.

`flight fare` is a deprecated alias that prints its own deprecation warning,
so the advice beside "Calendar empty." names the command a user should run."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from flight_cli.models import CalendarResult

if TYPE_CHECKING:
    from flight_cli.domain import CalendarSearch

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_ROUTE = ["calendar", "JFK", "LAX", "--start", "2026-10-20", "--end", "2026-11-16", "-d", "7"]


def _serve_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Matrix:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            del search, cache
            return CalendarResult.from_api({"solutionCount": 0, "calendar": {"months": []}})

    monkeypatch.setattr(cli, "MatrixClient", _Matrix)


def test_an_empty_calendar_names_flight_search_for_a_single_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_empty(monkeypatch)
    result = CliRunner().invoke(
        cli.app, [*_ROUTE, "--gf-transport", "http", "--no-cache"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    advice = " ".join(result.stdout.split())
    assert "Calendar empty." in advice
    assert "flight fare" not in advice
    assert "flight search" in advice
