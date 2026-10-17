# pyright: reportPrivateUsage=false
"""A narrow marked multi-cabin table of three or more slices never cuts a price.

Such a table folds every column, a price column that shows a `†`, `‡` or bag
mark included, so no flight number and no digit of a price is lost. A mark can
fold onto the line below its amount, as any folded cell's last characters do."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from rich.console import Console

from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin
from flight_cli.models import Itinerary, ItineraryDetails, Slice, SliceEndpoint

if TYPE_CHECKING:
    import pytest

_CABINS = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
_LEGS = (("JFK", "LHR"), ("LHR", "CDG"), ("CDG", "JFK"))


def _row() -> MultiCabinRow:
    """One three-slice itinerary on separate tickets, 14703.25 in every cabin."""
    itn = Itinerary(
        id="sol-1",
        displayTotal="USD14703.25",
        ticketing="separate_tickets",
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=[f"BA{i}"],
                    departure=f"2026-12-0{i}T06:00",
                    origin=SliceEndpoint(code=o),
                    destination=SliceEndpoint(code=d),
                )
                for i, (o, d) in enumerate(_LEGS, 1)
            ],
            carriers=[],
        ),
    )
    row = MultiCabinRow(itinerary=itn)
    for cabin in _CABINS:
        row.prices[cabin] = "USD14703.25"
    return row


def _price_cells(monkeypatch: pytest.MonkeyPatch, width: int) -> list[str]:
    """The row's four price cells at `width` columns, each rejoined across its
    lines with its spaces dropped."""
    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=width, no_color=True))
    cli._render_multi_cabin_search([_row()], cabins=_CABINS, sort_by=Cabin.COACH, slices=3)
    body = [line.split("│") for line in buf.getvalue().splitlines() if line.startswith("│")]
    return ["".join(parts[col].replace(" ", "") for parts in body) for col in range(-5, -1)]


def test_a_narrow_marked_three_slice_table_never_cuts_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for width in (34, 40, 50):
        assert _price_cells(monkeypatch, width) == ["14703.25†"] * 4, width
