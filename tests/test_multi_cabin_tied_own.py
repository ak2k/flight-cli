# pyright: reportPrivateUsage=false
"""A multi-cabin Google search's document fills a cabin's ten with the cheapest
fares in the requested currency: a fare in another currency ranks after every
one in it, whatever its number."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import Cabin
from test_envelope import _envelope_of
from test_multi_cabin_pins import _JFK, _LAX, _RET, _cells, _in_euros, _reversed, _row, _search

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

# Business's returns on 103-109 are priced in EUR from 900 up, below every USD
# fare by number. Its returns on 101 list 904 first, then 900, both at USD1000,
# the cheapest USD fares.
_EUROS = {n: 900.0 + 5 * i for i, n in enumerate(range(103, 110))}
_BACK_ON_101 = {904: 1000.0, 900: 1000.0, 901: 1010.0, 902: 1020.0, 903: 1030.0}


def _tied(monkeypatch: pytest.MonkeyPatch) -> Callable[..., gfid.Board[Any]]:
    google = _in_euros(_reversed(monkeypatch), _EUROS)

    def call(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        picked = filters.flight_segments[0].selected_flight
        flight = None if picked is None else int(picked.legs[0].flight_number)
        if filters.seat_type.name != "BUSINESS" or flight != 101:
            return board
        return gfid.Board([_row(n, _RET, _LAX, _JFK, fare) for n, fare in _BACK_ON_101.items()])

    return call


def _listing(row: list[dict[str, Any]]) -> tuple[str, str, str | None, float | None]:
    """A round trip's document row as (outbound, return, currency, price)."""
    out, back = row[0]["legs"][0], row[-1]["legs"][0]
    return (out["flight_number"], back["flight_number"], row[-1]["currency"], row[-1]["price"])


def _business_listings(result: Result, fmt: str) -> list[tuple[str, str, str | None, float | None]]:
    """Business's rows in the document `--format` writes."""
    if fmt == "envelope":
        env = _envelope_of(result)
        business = next(g for g in env["results"] if g["cabin"] == "BUSINESS")
        return [_listing(r["row"]) for r in business["rows"]]
    assert result.exit_code == 0, result.output
    doc: dict[str, list[list[dict[str, Any]]]] = json.loads(result.stdout)
    return [_listing(row) for row in doc["BUSINESS"]]


@pytest.mark.parametrize("fmt", ["envelope", "json"])
def test_euro_returns_do_not_fill_a_cabins_ten_ahead_of_its_dollar_ones(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Red at the base, whose business document opens with ten EUR rows and holds
    21."""
    table = _search(monkeypatch, _tied(monkeypatch))
    assert table.exit_code == 0, table.output
    assert "own cheapest" not in table.stdout
    assert 1000.0 in _cells(table.stdout)["BUSINESS"]
    listed = _business_listings(_search(monkeypatch, _tied(monkeypatch), "--format", fmt), fmt)
    assert {currency for _, _, currency, _ in listed} == {"USD"}
    assert listed[:2] == [("101", "904", "USD", 1000.0), ("101", "900", "USD", 1000.0)]
    assert len(listed) == 15


@pytest.mark.parametrize(
    ("cabin", "one_way", "label"),
    [
        (None, None, "Google Flights"),
        (Cabin.BUSINESS, None, "Google Flights BUSINESS"),
        (None, "outbound", "Google Flights outbound one-way"),
    ],
)
def test_a_boards_label_names_its_cabin_or_its_one_way(
    cabin: Cabin | None, one_way: str | None, label: str
) -> None:
    assert cli._google_board_label(cabin, one_way) == label
