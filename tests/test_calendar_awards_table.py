# pyright: reportPrivateUsage=false
"""`calendar --awards K` under the table calendar prints award space beneath it.

Matrix serves the live JFK-LAX fixture of `test_calendar_formats`; the award
providers are the stub of `test_calendar_awards`, with space on October 31 only.
"""

from __future__ import annotations

from typing import Any

import click
import pytest
import typer.main

from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from flight_cli.providers import registry
from flight_cli.providers.base import AwardFlight, CabinAward, LegQuery
from test_calendar_awards import _providers
from test_calendar_formats import _BODY, _ROUTE, _run, _serve

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)


def test_a_table_calendar_prints_award_space_on_the_cheapest_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2")
    assert result.exit_code == 0, result.output
    assert asked == ["JFK-LAX 2026-10-31", "JFK-LAX 2026-11-04"]
    lines = result.stdout.splitlines()
    assert [ln for ln in lines if "2026-10-31" in ln and "American Airlines" in ln]
    assert [ln for ln in lines if "2026-11-04" in ln and "no award space" in ln]


def test_a_table_calendar_prints_the_return_date_and_the_grid_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    _providers(monkeypatch)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "1")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert [ln for ln in lines if "2026-10-31" in ln and "2026-11-07" in ln and "American" in ln]
    assert [ln for ln in lines if "lowest fare per departure day" in ln]
    assert result.stdout.index("lowest fare per departure day") < result.stdout.index("2026-11-07")


def test_a_table_calendar_without_a_provider_says_so_and_prints_the_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch, configured=False)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2")
    assert result.exit_code == 0, result.output
    assert asked == []
    assert "--awards: no award provider is configured" in result.stderr
    assert "lowest fare per departure day" in result.stdout
    assert "no award space" not in result.stdout


def test_a_failed_award_query_keeps_the_table_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)
    _providers(monkeypatch)

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("seats down")

    monkeypatch.setattr(registry, "gather_awards", _boom)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2")
    assert result.exit_code == 0, result.output
    assert "--awards: award query failed: seats down" in result.stderr
    assert "lowest fare per departure day" in result.stdout
    assert "no award space" not in result.stdout


def test_a_program_name_is_printed_as_text_not_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)

    async def _gather(
        legs: list[LegQuery], **_kw: Any
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        found = AwardFlight(
            origin="JFK",
            destination="LAX",
            departure="2026-10-31T08:00:00",
            arrival="2026-10-31T11:00:00",
            flight_number="AA1",
            program="Air[/x] [bold]Club",
            cabins=[CabinAward("Economy", 12500, 5.6, "USD")],
        )
        return ([[found] for _ in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "1")
    assert result.exit_code == 0, result.output
    assert "Air[/x] [bold]Club" in result.stdout


def test_a_metro_table_calendar_asks_at_most_eight_airport_pairs_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch)
    route = ["calendar", "NYC", "LON", "--start", "2026-10-20", "--end", "2026-11-16", "-d", "7"]
    result = _run(*route, "--gf-transport", "http", "--awards", "1")
    assert result.exit_code == 0, result.output
    assert len(asked) == 8
    assert "--awards: asked the first 8 airport pairs" in result.stderr


def test_the_awards_help_names_the_table_calendar() -> None:
    group = typer.main.get_command(cli.app)
    assert isinstance(group, click.Group)
    (awards,) = [p for p in group.commands["calendar"].params if "--awards" in p.opts]
    help_text = getattr(awards, "help", None) or ""
    assert "the table calendar prints a line a date under the grid" in help_text
