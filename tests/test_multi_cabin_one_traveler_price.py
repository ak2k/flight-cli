"""A narrow one-traveler multi-cabin table prints every digit of each price."""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Any

from rich.console import Console
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Cabin
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pytest

_DEP = date.today() + timedelta(days=45)
_LINKLESS = ("--cash-only", "--no-matrix-url", "--no-google-url")


def _body(*, price: str) -> dict[str, Any]:
    """One DL1788 solution at `price` a passenger."""
    return {
        "solutionCount": 1,
        "solutionList": {
            "solutions": [
                {
                    "id": "s1",
                    "ext": {"price": price},
                    "itinerary": {
                        "slices": [
                            {
                                "flights": ["DL1788"],
                                "departure": f"{_DEP.isoformat()}T09:00:00",
                                "arrival": f"{_DEP.isoformat()}T12:00:00",
                                "origin": {"code": "JFK"},
                                "destination": {"code": "LAX"},
                            }
                        ],
                        "carriers": [{"code": "DL"}],
                    },
                }
            ]
        },
        "currencyNotice": {"ext": {"price": price}},
    }


def test_a_narrow_four_cabin_one_traveler_table_keeps_every_digit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {
            Cabin.COACH: SearchResult.from_api(_body(price="USD103.00")),
            Cabin.PREMIUM_COACH: SearchResult.from_api(_body(price="USD203.00")),
            Cabin.BUSINESS: SearchResult.from_api(_body(price="USD6303.20")),
            Cabin.FIRST: SearchResult.from_api(_body(price="USD14703.25")),
        }

    buf = StringIO()
    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)
    monkeypatch.setattr(cli, "console", Console(file=buf, width=66, no_color=True))
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), *_LINKLESS),
            *("--backend", "matrix", "--cabin", "economy,premium,business,first"),
        ],
    )
    assert result.exit_code == 0, result.output
    # One itinerary, so every body line belongs to it; a cell wrapped onto
    # several lines is rejoined column by column.
    body = [line.split("│") for line in buf.getvalue().splitlines() if line.startswith("│")]
    cells = ["".join(parts[col].strip() for parts in body) for col in range(-5, -1)]
    assert cells == ["103.00", "203.00", "6303.20", "14703.25"]
