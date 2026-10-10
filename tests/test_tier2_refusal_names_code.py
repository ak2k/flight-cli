"""A Tier-2 calendar refusal quotes the extension directives that declined, so
a reader can tell `+CABIN 2` from `-CODESHARE`. The price graph and the grid
gate both take the phrase from `grid_routing_blocker`."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from flight_cli import _gf_calgraph as cg
from flight_cli._gf_dategrid import grid_routing_blocker
from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    SearchOptions,
)


def _search(
    *,
    routing: str | None = None,
    extension: str | None = None,
    extension_ret: str | None = None,
    round_trip: bool = False,
    options: SearchOptions | None = None,
) -> CalendarSearch:
    legs = (Leg.of("JFK", "LAX", route_language=routing, extension=extension),)
    nights = 7 if round_trip else 0
    if round_trip:
        back = Leg.of("LAX", "JFK", route_language=routing, extension=extension_ret or extension)
        legs += (back,)
    start = date(2026, 12, 1)
    window = CalendarWindow(
        start=start, end=start + timedelta(days=13), duration_min=nights, duration_max=nights
    )
    return CalendarSearch(legs=legs, options=options or SearchOptions(), window=window)


def test_the_price_graph_names_the_cabin_directive_it_refused() -> None:
    search = _search(extension="+CABIN 2", options=SearchOptions(cabin=Cabin.BUSINESS))
    assert cg.graph_blocker(search) == "a Tier-2 extension code ('+CABIN 2')"


@pytest.mark.parametrize(
    ("routing", "extension", "expected"),
    [
        (None, "-CODESHARE", "a Tier-2 extension code ('-CODESHARE')"),
        (None, "MINCONNECT 1:00", "a Tier-2 extension code ('MINCONNECT 1:00')"),
        (
            None,
            "-CODESHARE;MINCONNECT 1:00",
            "Tier-2 extension codes ('-CODESHARE', 'MINCONNECT 1:00')",
        ),
        # A Tier-1 directive beside them is not what declined.
        (
            None,
            "MAXCONNECT 2:00; -REDEYES ;MAXDUR 9:00",
            "a Tier-2 extension code ('-REDEYES')",
        ),
        ("O:LH+", None, "Tier-2 routing"),
        ("O:LH+", "-CODESHARE", "both Tier-2 routing and a Tier-2 extension code ('-CODESHARE')"),
        (
            "O:LH+",
            "-CODESHARE;-REDEYES",
            "both Tier-2 routing and Tier-2 extension codes ('-CODESHARE', '-REDEYES')",
        ),
    ],
)
def test_the_grid_gate_quotes_each_declining_directive_as_typed(
    routing: str | None, extension: str | None, expected: str
) -> None:
    assert grid_routing_blocker(_search(routing=routing, extension=extension)) == expected


def test_the_return_leg_quotes_its_own_directive() -> None:
    search = _search(round_trip=True, extension="MAXDUR 9:00", extension_ret="-CODESHARE")
    expected = "a Tier-2 extension code ('-CODESHARE') on the return leg"
    assert grid_routing_blocker(search) == expected


def test_a_control_character_in_a_quoted_directive_is_escaped() -> None:
    blocker = grid_routing_blocker(_search(extension="-CITIES a\x1b[31m"))
    assert blocker == "a Tier-2 extension code ('-CITIES a\\x1b[31m')"
