"""One fli airport member per code.

fli builds `Airport` as an enum over a code -> display-name table, so a code
whose display name repeats an earlier one is an alias of the earlier member:
`Airport.OKA` is `Airport.NAH`, and a member's name is the code every request
and every decoded row carries. `fli_bridge.fli_airports` gives each aliased
code a member of its own. These tests pin that table and the places the member
travels: fli's models, copies of them, and the JSON dump."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest
from fli.models import FlightLeg  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]
from fli.models.google_flights.base import FlightSegment  # pyright: ignore[reportMissingTypeStubs]

from flight_cli.fli_bridge import fli_airport, fli_airports

# fli's FlightSegment validator rejects a past travel date.
_DEP = date.today() + timedelta(days=45)

_ALIASES = sorted(code for code, member in Airport.__members__.items() if member.name != code)
# MLH and BSL are one airport, which Google serves only as BSL.
_OWN = [code for code in _ALIASES if code != "MLH"]


def test_fli_still_aliases_the_codes_this_table_exists_for() -> None:
    assert Airport["OKA"] is Airport["NAH"]
    assert {"OKA", "NTL", "TRI", "MLH"} <= set(_ALIASES)


@pytest.mark.parametrize("code", _OWN)
def test_an_aliased_code_has_a_member_named_for_it(code: str) -> None:
    canonical = Airport.__members__[code]
    member = fli_airport(code)
    assert member.name == code
    assert member is not canonical
    assert isinstance(member, Airport)
    assert member.value == canonical.value
    assert fli_airport(code) is member
    assert fli_airports()[code] is member


def test_mlh_resolves_to_bsl() -> None:
    assert fli_airport("MLH") is Airport["BSL"]


def test_every_other_code_is_flis_own_member() -> None:
    wrong = [
        code
        for code, member in Airport.__members__.items()
        if member.name == code and fli_airport(code) is not member
    ]
    assert wrong == []
    assert set(fli_airports()) == set(Airport.__members__)


@pytest.mark.parametrize("code", ["QQQ", "NYC"])
def test_a_code_fli_has_no_entry_for_raises_attribute_error(code: str) -> None:
    with pytest.raises(AttributeError, match=code):
        fli_airport(code)


def test_an_aliased_member_survives_the_models_that_carry_it() -> None:
    oka = fli_airport("OKA")
    segment = FlightSegment(
        departure_airport=[[Airport["LAX"], 0]],
        arrival_airport=[[oka, 0]],
        travel_date=_DEP.isoformat(),
    )
    leg = FlightLeg(
        airline=Airline["CI"],
        flight_number="122",
        departure_airport=Airport["TPE"],
        arrival_airport=oka,
        departure_datetime=datetime(_DEP.year, _DEP.month, _DEP.day, 9, 0),
        arrival_datetime=datetime(_DEP.year, _DEP.month, _DEP.day, 11, 30),
        duration=90,
    )
    assert segment.arrival_airport[0][0] is oka
    assert leg.arrival_airport is oka
    assert deepcopy(segment).arrival_airport[0][0] is oka
    assert segment.model_copy(deep=True).arrival_airport[0][0] is oka
    assert deepcopy(leg).arrival_airport is oka
    assert leg.model_copy(deep=True).arrival_airport is oka
    assert leg.model_dump(mode="json")["arrival_airport"] == "Naha Airport"


@pytest.mark.parametrize(("origin", "destination"), [("NTL", "NCL"), ("NTL", "NCS")])
def test_two_codes_fli_aliases_alike_are_two_airports(origin: str, destination: str) -> None:
    """fli's segment validator refuses one airport at both ends; NTL, NCS and
    NCL are three airports it files under one member."""
    segment = FlightSegment(
        departure_airport=[[fli_airport(origin), 0]],
        arrival_airport=[[fli_airport(destination), 0]],
        travel_date=_DEP.isoformat(),
    )
    assert segment.departure_airport[0][0] is fli_airport(origin)
    assert segment.arrival_airport[0][0] is fli_airport(destination)
