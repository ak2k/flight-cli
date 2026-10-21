"""A table of a three-slice (multi-city) search shows every slice."""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pytest
from rich.console import Console

from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin, Leg
from flight_cli.models import Itinerary, SearchResult


def _slice(origin: str, destination: str, flight: str) -> dict[str, Any]:
    return {
        "flights": flight.split("/"),
        "origin": {"code": origin},
        "destination": {"code": destination},
        "departure": "2026-11-01T09:00",
        "arrival": "2026-11-01T12:00",
        "duration": 180,
    }


def _itinerary(
    *flights: str, price: str = "USD500.00", carriers: tuple[str, ...] = ("UA",)
) -> Itinerary:
    """A priced itinerary of one slice per flight (or `/`-joined connecting
    flights), each its own city pair."""
    cities = ["JFK", "LAX", "BOS", "SFO", "SEA", "ORD", "MIA", "DEN", "ATL"]
    pairs = zip(cities, cities[1:], flights, strict=False)
    return Itinerary.model_validate(
        {
            "ext": {"price": price},
            "itinerary": {
                "slices": [_slice(o, d, f) for o, d, f in pairs],
                "carriers": [{"code": c} for c in carriers],
            },
        }
    )


def _result(*itineraries: Itinerary) -> SearchResult:
    return SearchResult.model_validate(
        {"solutionCount": len(itineraries), "solutions": list(itineraries)}
    )


@pytest.fixture
def out(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """The table's console, wide enough that no cell wraps."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=300, color_system=None))
    return buf


def test_a_three_slice_table_shows_the_third_slice(out: io.StringIO) -> None:
    res = _result(_itinerary("UA1", "UA2", "UA1023"), _itinerary("UA1", "UA2", "UA582"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
    text = out.getvalue()
    assert "UA1023" in text
    assert "UA582" in text
    assert "slice 3" in text


def test_a_three_slice_multi_cabin_table_shows_the_third_slice(out: io.StringIO) -> None:
    rows: list[MultiCabinRow] = []
    for third in ("UA1023", "UA582"):
        row = MultiCabinRow(itinerary=_itinerary("UA1", "UA2", third))
        row.prices[Cabin.COACH] = "USD500.00"
        rows.append(row)
    cli._render_multi_cabin_search(  # pyright: ignore[reportPrivateUsage]
        rows, cabins=(Cabin.COACH,), sort_by=Cabin.COACH
    )
    text = out.getvalue()
    assert "UA1023" in text
    assert "UA582" in text
    assert "slice 3" in text


def test_a_round_trip_table_keeps_outbound_and_return(out: io.StringIO) -> None:
    res = _result(_itinerary("UA1", "UA2"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage]
    header = out.getvalue()
    assert "outbound" in header
    assert "return" in header
    assert "slice" not in header


def test_a_short_row_on_a_three_slice_table_dashes_its_missing_slices(
    out: io.StringIO,
) -> None:
    res = _result(_itinerary("UA1", "UA2", "UA1023"), _itinerary("UA77"))
    cli._render_search(res)  # pyright: ignore[reportPrivateUsage]
    text = out.getvalue()
    assert "slice 3" in text
    (short_line,) = [line for line in text.splitlines() if "UA77" in line]
    assert short_line.count("—") == 2


_DEP = dt.date(2026, 11, 1)


class _MergedRow:
    """A row of the Google + Matrix table that both sources priced alike."""

    def __init__(self, itinerary: Itinerary) -> None:
        self.itinerary = itinerary
        self.source = "both"
        self.gf_price = self.matrix_price = itinerary.price


def _render(
    table: str,
    itineraries: list[Itinerary],
    *,
    cabins: tuple[Cabin, ...] = (Cabin.COACH,),
    passengers: int = 1,
) -> None:
    if table == "single-cabin":
        cli._render_search(  # pyright: ignore[reportPrivateUsage]
            _result(*itineraries), passengers=passengers
        )
        return
    if table == "merged":
        cli._render_merged(  # pyright: ignore[reportPrivateUsage]
            [_MergedRow(itn) for itn in itineraries],
            legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _DEP)),
            top_n=len(itineraries),
        )
        return
    rows: list[MultiCabinRow] = []
    for itn in itineraries:
        row = MultiCabinRow(itinerary=itn)
        for cabin in cabins:
            row.prices[cabin] = itn.price or ""
        rows.append(row)
    cli._render_multi_cabin_search(  # pyright: ignore[reportPrivateUsage]
        rows, cabins=cabins, sort_by=cabins[0]
    )


def _rows(text: str) -> list[list[str]]:
    """Each table row's cells, a cell's lines joined with spaces dropped, so a
    value folded over several lines reads whole. A row starts where `#` is set."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        cells = line.split("│")[1:-1]
        if cells and cells[0].strip():
            rows.append([""] * len(cells))
        if cells and rows:
            rows[-1] = [a + b.replace(" ", "") for a, b in zip(rows[-1], cells, strict=True)]
    return rows


@pytest.mark.parametrize("width", [40, 60, 80])
@pytest.mark.parametrize("slices", [3, 4, 6, 8])
@pytest.mark.parametrize(
    ("table", "cabins", "passengers", "carriers"),
    [
        ("single-cabin", (Cabin.COACH,), 1, ("UA",)),
        (
            "single-cabin",
            (Cabin.COACH,),
            3,
            ("UA", "LH", "AC", "NH", "OS", "LX", "SN", "TP", "A3", "OU"),
        ),
        ("multi-cabin", (Cabin.COACH,), 1, ("UA",)),
        ("multi-cabin", tuple(Cabin), 1, ("UA",)),
    ],
    ids=["one-price", "party-price-and-ten-carriers", "one-cabin", "four-cabin-prices"],
)
def test_a_narrow_multi_city_table_loses_no_cell_a_wide_one_prints(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    slices: int,
    table: str,
    cabins: tuple[Cabin, ...],
    passengers: int,
    carriers: tuple[str, ...],
) -> None:
    itineraries = [
        _itinerary(*["UA100/UA200"] * (slices - 1), last, price="USD12345.00", carriers=carriers)
        for last in ("UA300/UA1023", "UA300/UA1028")
    ]
    printed: dict[int, str] = {}
    for console_width in (400, width):
        buf = io.StringIO()
        monkeypatch.setattr(
            cli, "console", Console(file=buf, width=console_width, color_system=None)
        )
        _render(table, itineraries, cabins=cabins, passengers=passengers)
        printed[console_width] = buf.getvalue()
    assert "…" not in printed[width]
    assert _rows(printed[width]) == _rows(printed[400])


def test_a_folded_slice_cell_keeps_each_flight_number_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connecting list breaks between flight numbers, never inside one: cut
    mid-number, `UA300/UA10` over `23 Nov01` reads the date as part of the number."""
    itineraries = [
        _itinerary("UA100/UA200", "UA100/UA200", "UA100/UA200", last, price="USD12345.00")
        for last in ("UA300/UA1023", "UA300/UA1028")
    ]
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=80, color_system=None))
    _render("single-cabin", itineraries)
    printed = buf.getvalue()
    assert "UA1023" in printed
    assert "UA1028" in printed


@pytest.mark.parametrize(
    ("table", "cabins", "slice_columns"),
    [
        ("single-cabin", (Cabin.COACH,), slice(3, 5)),
        ("multi-cabin", (Cabin.COACH, Cabin.BUSINESS), slice(2, 4)),
        ("merged", (Cabin.COACH,), slice(6, 8)),
    ],
    ids=["single-cabin", "two-cabin-prices", "google-and-matrix"],
)
def test_a_narrow_round_trip_table_cuts_no_slice_cell(
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    cabins: tuple[Cabin, ...],
    slice_columns: slice,
) -> None:
    """Rich shares a short console between the outbound and return columns by
    their cells' lengths, so any slice cell can be the one narrowed past its
    longest word: cut there, `09:00→12:…` hides the arrival."""
    itineraries = [
        _itinerary("UA100/UA200", last, price="USD12345.00")
        for last in ("UA300/UA1023", "UA300/UA1028")
    ]
    printed: dict[int, list[list[str]]] = {}
    # From 33, a border, padding and one character for each of up to eight columns.
    narrow = range(33, 121)
    for width in (400, *narrow):
        buf = io.StringIO()
        monkeypatch.setattr(cli, "console", Console(file=buf, width=width, color_system=None))
        _render(table, itineraries, cabins=cabins)
        printed[width] = [row[slice_columns] for row in _rows(buf.getvalue())]
    assert [width for width in narrow if printed[width] != printed[400]] == []
