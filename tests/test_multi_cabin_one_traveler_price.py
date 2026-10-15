# pyright: reportPrivateUsage=false
"""A narrow one-traveler multi-cabin table prints every digit of each price."""

from __future__ import annotations

import json
from datetime import date, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Any

from rich.console import Console
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin
from flight_cli.models import SearchResult
from test_multi_cabin_marks import _LAX, _MARKED, _URL

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


def _dear_board(
    _filters: Any, _transport: Any, *, currency: str = "USD", cheapest: bool = False
) -> gfid.Board[gfid.GFlightWithId]:
    """Google's answer to every cabin: the LAX capture's rows at USD14703 and up,
    and on the Cheapest tab row `_MARKED` a self transfer at USD14700."""
    _ = currency
    payload: list[Any] = json.loads(_ds1(_LAX))
    rows = gfid._rows_from_ds1(payload).rows
    for i, row in enumerate(rows):
        row[1][0][1] = 14703 + i
    if cheapest:
        rows[_MARKED][7] = [1]
        rows[_MARKED][1][0][1] = 14700
    html = _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


def _marked_cells(monkeypatch: pytest.MonkeyPatch, width: int, *extra: str) -> list[str]:
    """The four price cells of the one marked row, at `width` columns."""
    monkeypatch.setattr(gfid, "_one_call_laddered", _dear_board)
    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=width, no_color=True))
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), *_LINKLESS, "-n", "1"),
            *("--backend", "gflight", "--cabin", "economy,premium,business,first", *extra),
        ],
    )
    assert result.exit_code == 0, result.output
    body = [line.split("│") for line in buf.getvalue().splitlines() if line.startswith("│")]
    return ["".join(parts[col].strip() for parts in body) for col in range(-5, -1)]


def test_a_narrow_marked_one_traveler_table_never_cuts_a_price_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shown mark keeps the price column on one line, so a cell too narrow for
    its price is cut: at 33 columns to its first digits, at 50 to its amount
    without the mark. The cut must show, or 1470 reads as a fare of 1,470."""
    for width in (33, 50):
        cells = _marked_cells(monkeypatch, width)
        assert [c for c in cells if c != "14700.00 ‡" and not c.endswith("…")] == [], width


def test_a_narrow_marked_party_table_never_cuts_a_price_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = _marked_cells(monkeypatch, 33, "--adults", "2")
    assert [c for c in cells if c != "14700.00 ‡" and not c.endswith("…")] == []
