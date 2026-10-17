# pyright: reportPrivateUsage=false
"""A narrow marked multi-cabin table of three or more slices loses no cell.

A table that shows a `†` or `‡` holds its price columns to one line. In a table
of three or more slices that hold made Rich take its width from the slice
columns, which printed blank below 73 columns, so the flight numbers and routes
were gone. Every column folds there, as the unmarked table already does."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Literal

from rich.console import Console

from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin
from flight_cli.models import Itinerary, ItineraryDetails, Slice, SliceEndpoint

if TYPE_CHECKING:
    import pytest

_CABINS = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
_LEGS = (("JFK", "LHR"), ("LHR", "CDG"), ("CDG", "FRA"), ("FRA", "JFK"))
_WIDE = 400
_TICKETING: tuple[Literal["separate_tickets", "self_transfer"], ...] = (
    "separate_tickets",
    "self_transfer",
)
_Ticketing = Literal["separate_tickets", "self_transfer"]


def _row(count: int, ticketing: _Ticketing) -> MultiCabinRow:
    """One itinerary of `count` slices, flights BA1.., 14703.25 in every cabin."""
    itn = Itinerary(
        id="sol-1",
        displayTotal="USD14703.25",
        ticketing=ticketing,
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=[f"BA{i}"],
                    departure=f"2026-12-0{i}T06:00",
                    origin=SliceEndpoint(code=o),
                    destination=SliceEndpoint(code=d),
                )
                for i, (o, d) in enumerate(_LEGS[:count], 1)
            ],
            carriers=[],
        ),
    )
    row = MultiCabinRow(itinerary=itn)
    for cabin in _CABINS:
        row.prices[cabin] = "USD14703.25"
    return row


def _cells(
    monkeypatch: pytest.MonkeyPatch, width: int, count: int, ticketing: _Ticketing
) -> list[str]:
    """Every cell of the table at `width` columns, each rejoined across its lines
    with its spaces dropped."""
    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=width, no_color=True))
    cli._render_multi_cabin_search(
        [_row(count, ticketing)], cabins=_CABINS, sort_by=Cabin.COACH, slices=count
    )
    body = [line.split("│")[1:-1] for line in buf.getvalue().splitlines() if line.startswith("│")]
    return ["".join(parts[col].replace(" ", "") for parts in body) for col in range(len(body[0]))]


def test_a_narrow_marked_multi_slice_table_loses_no_cell_a_wide_one_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[str] = []
    for count in (3, 4):
        for ticketing in _TICKETING:
            wide = _cells(monkeypatch, _WIDE, count, ticketing)
            for width in (34, 50, 60, 70):
                cells = _cells(monkeypatch, width, count, ticketing)
                has_flights = [f"BA{i}" in cells[1 + i] for i in range(1, count + 1)]
                if has_flights != [True] * count:
                    failures.append(f"{count} slices {ticketing} at {width}: flights {has_flights}")
                if any("…" in c for c in cells):
                    failures.append(f"{count} slices {ticketing} at {width}: a cell is cut")
                if cells != wide:
                    failures.append(f"{count} slices {ticketing} at {width}: {cells} != wide")
    assert failures == []
