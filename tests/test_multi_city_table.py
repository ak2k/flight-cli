"""A table of a three-slice (multi-city) search shows every slice."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from flight_cli import cli
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin
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
    cities = ["JFK", "LAX", "BOS", "SFO", "SEA", "ORD", "MIA"]
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


@pytest.fixture
def narrow(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """The table's console at 80 columns, the width Rich gives output that is
    not a terminal."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=80, color_system=None))
    return buf


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


@pytest.mark.parametrize("table", ["single-cabin", "multi-cabin"])
def test_a_narrow_four_slice_table_prints_each_flight_number_whole(
    narrow: io.StringIO, table: str
) -> None:
    connecting = ("UA100/UA200", "UA101/UA201", "UA102/UA202")
    _render(table, [_itinerary(*connecting, last) for last in ("UA300/UA1023", "UA300/UA1028")])
    text = narrow.getvalue()
    assert "…" not in text
    first, second = _rows(text)
    assert any("UA300/UA1023" in cell for cell in first)
    assert any("UA300/UA1028" in cell for cell in second)


@pytest.mark.parametrize("table", ["single-cabin", "multi-cabin"])
def test_a_narrow_six_slice_table_keeps_the_price_and_headers_whole(
    narrow: io.StringIO, table: str
) -> None:
    _render(table, [_itinerary(*["UA100/UA200"] * 6, price="USD12345.00")])
    text = narrow.getvalue()
    assert "…" not in text
    (row,) = _rows(text)
    assert "12345.00" in row


@pytest.mark.parametrize(
    ("table", "cabins", "passengers", "carriers"),
    [
        ("multi-cabin", tuple(Cabin), 1, ("UA",)),
        (
            "single-cabin",
            (Cabin.COACH,),
            3,
            ("UA", "LH", "AC", "NH", "OS", "LX", "SN", "TP", "A3", "OU"),
        ),
    ],
    ids=["four-cabin-prices", "party-price-and-ten-carriers"],
)
def test_a_narrow_six_slice_table_erases_no_cell_a_wide_one_prints(
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    cabins: tuple[Cabin, ...],
    passengers: int,
    carriers: tuple[str, ...],
) -> None:
    itineraries = [
        _itinerary(*["UA100/UA200"] * 5, last, price="USD12345.00", carriers=carriers)
        for last in ("UA300/UA1023", "UA300/UA1028")
    ]
    printed: dict[int, str] = {}
    for width in (400, 80):
        buf = io.StringIO()
        monkeypatch.setattr(cli, "console", Console(file=buf, width=width, color_system=None))
        _render(table, itineraries, cabins=cabins, passengers=passengers)
        printed[width] = buf.getvalue()
    assert "…" not in printed[80]
    assert _rows(printed[80]) == _rows(printed[400])
