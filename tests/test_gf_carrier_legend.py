# pyright: reportPrivateUsage=false
"""The line under the Google table that names the carrier codes its legs column shows."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import FlightLeg, FlightResult  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]
from rich.console import Console
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import cli
from flight_cli._gflight_ids import GFlightWithId, LegAmenities
from flight_cli.domain import Leg

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_DAY = datetime(2026, 11, 4, 8, 0)
_LEGEND = "Carriers: MX Breeze Airways · F9 Frontier Airlines"


def _row(
    *legs: tuple[str, str],
    amenities: dict[int, LegAmenities] | None = None,
    price: float = 100.0,
) -> GFlightWithId:
    """One itinerary flying `legs`, each a (booking carrier, flight number)."""
    fli_legs = [
        FlightLeg(
            airline=Airline[carrier],
            flight_number=number,
            departure_airport=Airport["JFK"],
            arrival_airport=Airport["LAX"],
            departure_datetime=_DAY + timedelta(hours=i),
            arrival_datetime=_DAY + timedelta(hours=i, minutes=90),
            duration=90,
        )
        for i, (carrier, number) in enumerate(legs)
    ]
    flight = FlightResult(
        price=price, currency="USD", duration=90 * len(legs), stops=len(legs) - 1, legs=fli_legs
    )
    given = amenities or {}
    return GFlightWithId(
        flight=flight,
        flight_id="",
        amenities=[given.get(i, LegAmenities()) for i in range(len(legs))],
    )


def _printed(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[GFlightWithId],
    *,
    width: int = 200,
    match: frozenset[str] = frozenset(),
) -> list[str]:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=width, no_color=True))
    cli._render_gflight_table(  # pyright: ignore[reportPrivateUsage] — the render site IS the unit
        rows,
        legs=(Leg.of("JFK", "LAX", _DEP),),
        top_n=len(rows),
        match_carriers=match,
        currency="USD",
    )
    return buffer.getvalue().splitlines()


def _legend(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("Carriers:")]


def test_the_table_names_each_carrier_code_its_legs_column_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _printed(monkeypatch, [_row(("MX", "100")), _row(("F9", "200"))])
    assert any("MX 100" in ln for ln in lines)
    assert _legend(lines) == [_LEGEND]


def test_the_legend_lists_a_code_once_in_the_order_the_rows_first_show_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_row(("F9", "1")), _row(("MX", "2"), ("F9", "3")), _row(("MX", "4"))]
    assert _legend(_printed(monkeypatch, rows)) == [
        "Carriers: F9 Frontier Airlines · MX Breeze Airways"
    ]


def test_a_code_named_only_on_a_later_row_keeps_its_first_shown_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EN shows first with no name; a later row's Google name fills it in place."""
    metal = LegAmenities(operating_carrier="EN", operating_carrier_name="Air Dolomiti")
    rows = [
        _row(("EN", "1"), ("MX", "2"), price=100.0),
        _row(("EN", "3"), amenities={0: metal}, price=200.0),
    ]
    assert _legend(_printed(monkeypatch, rows)) == ["Carriers: EN Air Dolomiti · MX Breeze Airways"]


def test_the_legend_sits_directly_under_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _printed(monkeypatch, [_row(("MX", "100"))])
    below = lines[next(i for i, ln in enumerate(lines) if ln.startswith("└")) + 1]
    assert below == "Carriers: MX Breeze Airways"


def test_the_legend_sits_between_the_table_and_the_legroom_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(("MX", "100"), amenities={0: LegAmenities(pitch_inches=31)})
    lines = _printed(monkeypatch, [row])
    border = next(i for i, ln in enumerate(lines) if ln.startswith("└"))
    assert lines[border + 1] == "Carriers: MX Breeze Airways"
    assert lines[border + 2].startswith("Legroom glyphs:")


def test_a_code_the_map_lacks_takes_the_name_google_sent_for_that_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metal = LegAmenities(operating_carrier="EN", operating_carrier_name="Air Dolomiti")
    row = _row(("EN", "8858"), amenities={0: metal})
    assert _legend(_printed(monkeypatch, [row])) == ["Carriers: EN Air Dolomiti"]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_name_google_sent_leaves_the_code_out(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    metal = LegAmenities(operating_carrier="EN", operating_carrier_name=blank)
    row = _row(("EN", "8858"), amenities={0: metal})
    lines = _printed(monkeypatch, [row])
    assert any("EN 8858" in ln for ln in lines)
    assert _legend(lines) == []


@pytest.mark.parametrize(
    "unseen",
    ["\x1b\x00", "\u200b\u2066", " \x7f\t\u00ad "],
    ids=["control", "invisible", "mixed"],
)
def test_a_name_google_sent_that_prints_as_nothing_leaves_the_code_out(
    monkeypatch: pytest.MonkeyPatch, unseen: str
) -> None:
    metal = LegAmenities(operating_carrier="EN", operating_carrier_name=unseen)
    row = _row(("EN", "8858"), amenities={0: metal})
    lines = _printed(monkeypatch, [row])
    assert any("EN 8858" in ln for ln in lines)
    assert _legend(lines) == []


def test_a_code_sold_for_another_operator_is_not_named_for_the_metal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS is booked and EN flies it: Google's EN name is not OS's."""
    metal = LegAmenities(operating_carrier="EN", operating_carrier_name="Air Dolomiti")
    row = _row(("OS", "36"), amenities={0: metal})
    lines = _printed(monkeypatch, [row])
    assert any("OS 36" in ln for ln in lines)
    assert _legend(lines) == []


def test_a_codeshare_relabel_names_the_matched_code_and_the_booking_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--routing LH+` shows UA58 as `LH9407 (op UA58)`: both codes are on screen."""
    sold = LegAmenities(marketing_carriers=("LH",), marketing_flights=("LH9407",))
    row = _row(("UA", "58"), amenities={0: sold})
    lines = _printed(monkeypatch, [row], match=frozenset({"LH"}))
    assert any("LH9407 (op UA58)" in ln for ln in lines)
    assert _legend(lines) == ["Carriers: LH Lufthansa · UA United Airlines"]


def test_a_name_from_google_prints_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = LegAmenities(operating_carrier="EN", operating_carrier_name="[red]Air[/] \x1b[31mD")
    row = _row(("EN", "1"), amenities={0: hostile})
    legend = _legend(_printed(monkeypatch, [row]))
    assert legend == ["Carriers: EN [red]Air[/] [31mD"]


def test_a_narrow_console_wraps_the_legend_within_its_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _printed(monkeypatch, [_row(("MX", "100")), _row(("F9", "200"))], width=40)
    assert max(len(ln) for ln in lines) <= 40
    assert _LEGEND in " ".join(" ".join(lines).split())


def test_a_search_prints_the_legend_under_its_table(gf_session: Callable[..., Any]) -> None:
    body = _answering(
        _ds1("ds1_jfk_lhr_tfu.json"), origin="JFK", destination="LHR", date=_DEP.isoformat()
    )
    gf_session(_page(body))
    argv = ["search", "--cash-only", "--no-google-url", "--no-matrix-url", "JFK", "LHR"]
    argv += ["--dep", _DEP.isoformat(), "--backend", "gflight", "--fast", "-n", "10"]
    result = CliRunner().invoke(cli.app, argv, env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    legend = _legend(result.stdout.splitlines())
    assert len(legend) == 1
    assert "DL Delta Air Lines" in legend[0]
