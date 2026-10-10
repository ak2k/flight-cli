# pyright: reportPrivateUsage=false
"""A leg and a typed airport list name each airport once, in typed order.

`search JFK,JFK LHR` is the one airport JFK: its `Leg` holds `JFK` once, so the
Google Flights table title reads `JFK→LHR`, what `search JFK LHR` prints, the
date grid serves it as one airport, and a refusal that names a typed code names
it once. A metro code typed beside a member stays as typed."""

from __future__ import annotations

import re
from datetime import date

import pytest

from flight_cli import cli
from flight_cli._gf_dategrid import grid_can_serve
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, SearchOptions

_DEP = date(2026, 11, 4)


@pytest.mark.parametrize(
    ("origins", "destinations", "title"),
    [
        (("JFK",), ("LHR",), "JFK→LHR"),
        (("JFK", "JFK"), ("LHR",), "JFK→LHR"),
        (("JFK", "EWR", "jfk"), ("LHR",), "JFK,EWR→LHR"),
        (("JFK",), ("LHR", "LGW", "LHR"), "JFK→LHR,LGW"),
        (("NYC", "JFK"), ("LHR",), "NYC,JFK→LHR"),
    ],
    ids=[
        "single",
        "repeated-origin",
        "repeat-among-two",
        "repeated-destination",
        "metro-and-member",
    ],
)
def test_the_google_table_title_names_a_repeated_airport_once(
    origins: tuple[str, ...],
    destinations: tuple[str, ...],
    title: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Red at the base, whose title comma-joined every token of the leg: `JFK,JFK→LHR`."""
    legs = (Leg.of(origins, destinations, _DEP),)

    cli._render_gflight_table([], legs=legs, top_n=1, currency="USD")
    cli._render_merged([], legs=legs, top_n=1)

    out = capsys.readouterr().out
    for table in ("Google Flights", "Google Flights + Matrix"):
        assert re.search(rf"{re.escape(table)} · {re.escape(title)}\s", out), out


def test_a_leg_holds_each_airport_once_in_typed_order() -> None:
    leg = Leg.of(("JFK", "EWR", "JFK"), ("LHR", "lhr", "LGW"), _DEP)

    assert (leg.origins, leg.destinations) == (("JFK", "EWR"), ("LHR", "LGW"))


def test_a_calendar_asking_one_airport_twice_is_served_as_one_airport() -> None:
    """Red at the base, where `JFK,JFK` was two tokens and the date grid refused it."""
    search = CalendarSearch(
        legs=(Leg.of(("JFK", "JFK"), ("LAX",)),),
        window=CalendarWindow(
            start=date(2026, 8, 10), end=date(2026, 8, 25), duration_min=5, duration_max=7
        ),
        options=SearchOptions(),
    )

    assert grid_can_serve(search)


def test_a_typed_airport_list_names_each_airport_once() -> None:
    assert cli._parse_iata_list("jfk, EWR,JFK,,ewr") == ("JFK", "EWR")


def test_a_refusal_names_a_repeated_city_code_once() -> None:
    """Red at the base: `a city code rather than an airport (YTO, YTO)`."""
    reasons = cli._gf_unserveable_reasons(cli.BACKEND_GFLIGHT, "YTO,YTO", "LHR")

    assert reasons == ["a city code rather than an airport (YTO)"]
