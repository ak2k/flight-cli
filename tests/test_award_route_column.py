# pyright: reportPrivateUsage=false
"""The matched award table names the route of a row only when its rows differ."""

from __future__ import annotations

import io
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

import test_award_airport_pairs as pairs
from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from flight_cli.models import Itinerary, ItineraryDetails, Slice, SliceEndpoint
from flight_cli.pp import cli as pp_cli
from flight_cli.pp.match import MatchedFare
from test_gf_airport_sets import _board_from_jfk_and_ewr

if TYPE_CHECKING:
    from test_award_airport_pairs import _Arms

# The autouse fixture of that module, registered under its own name here.
arms = pairs.arms

# The searches built here carry literal travel dates; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_DEP = date(2026, 11, 4)


def _matched_lines(
    origin: str, rows: list[Any], arms: _Arms, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """The lines after the 'Cash + award' title of `search <origin> LAX`."""
    pairs._google(monkeypatch, rows)
    arms.answers = {
        "JFK-LAX": [pairs._award("B6123", "JFK", "2026-10-14T06:00:00", "eVt0r")],
        "EWR-LAX": [pairs._award("B6523", "EWR", "2026-10-14T07:00:00", "VV7jhf")],
    }
    r = CliRunner().invoke(cli.app, ["search", origin, "LAX", "--dep", _DEP.isoformat(), "--fast"])
    assert r.exit_code == 0, r.output
    return r.stdout.split("Cash + award", 1)[1].splitlines()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.replace("┃", "│").strip().strip("│").split("│")]


def _row(lines: list[str], flight: str) -> list[str]:
    return _cells(next(ln for ln in lines if flight in ln))


def _header(lines: list[str]) -> list[str]:
    return next(c for c in map(_cells, lines) if c[0] == "flight")


def test_the_matched_award_table_names_the_airport_of_each_row_of_a_set_search(
    arms: _Arms, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = _matched_lines("JFK,EWR", _board_from_jfk_and_ewr(), arms, monkeypatch)

    ewr_row = _row(lines, "B6523")
    jfk_row = _row(lines, "B6123")
    assert "EWR→LAX" in ewr_row, f"the EWR row names no airport: {ewr_row!r}"
    assert "JFK→LAX" in jfk_row, f"the JFK row names no airport: {jfk_row!r}"
    assert _header(lines)[:3] == ["flight", "route", "stops"]
    assert ewr_row[:2] == ["B6523", "EWR→LAX"]
    assert jfk_row[:2] == ["B6123", "JFK→LAX"]


def test_a_table_whose_rows_share_one_route_has_no_route_column(
    arms: _Arms, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = _matched_lines("JFK", _board_from_jfk_and_ewr()[:2], arms, monkeypatch)

    assert _header(lines)[:2] == ["flight", "stops"]
    assert not any("→" in ln for ln in lines), "\n".join(lines)


def _fare(flight: str, origin: str) -> MatchedFare:
    cash = Itinerary(
        displayTotal="USD900.00",
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=[flight],
                    departure="2026-11-04T08:00:00",
                    origin=SliceEndpoint(code=origin),
                    destination=SliceEndpoint(code="LAX"),
                )
            ]
        ),
    )
    return MatchedFare(itinerary=cash, awards=[])


def test_a_route_cell_prints_a_hostile_airport_code_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(
        pp_cli, "console", Console(file=buf, width=1000, force_terminal=False, no_color=True)
    )

    pp_cli._render_matches(
        [_fare("B6123", "JFK"), _fare("B6523", "[/x]\x1b[2J[bold]")], ("Economy",)
    )

    out = buf.getvalue()
    assert "[/X][2J[BOLD]→LAX" in out, out
    assert "\x1b" not in out, repr(out)
