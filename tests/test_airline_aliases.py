"""One fli airline member per code.

fli builds `Airline` as an enum over a code -> display-name table, so a code
whose display name repeats an earlier one is an alias of the earlier member:
`Airline.W9` (Wizz Air UK) is `Airline.W6` (Wizz Air Hungary), and a member's
name is the code every request and every decoded row carries.
`fli_bridge.fli_airlines` gives each aliased code a member of its own. These
tests pin that table and the places the member travels: fli's models, copies
of them, and the JSON dump."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest
from fli.models import FlightLeg  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]

from flight_cli.fli_bridge import fli_airline, fli_airlines

_DAY = date.today() + timedelta(days=45)

# fli's keys: a digit-leading code is written `_5C`.
_ALIASES = sorted(key for key, member in Airline.__members__.items() if member.name != key)


def test_fli_still_aliases_the_codes_this_table_exists_for() -> None:
    assert Airline["W9"] is Airline["W6"]
    assert _ALIASES == ["MT", "S0", "W9", "Z0", "_1W", "_5C"]


@pytest.mark.parametrize("key", _ALIASES)
def test_an_aliased_code_has_a_member_named_for_it(key: str) -> None:
    canonical = Airline.__members__[key]
    member = fli_airline(key.removeprefix("_"))
    assert member.name == key
    assert member is not canonical
    assert isinstance(member, Airline)
    assert member.value == canonical.value
    assert fli_airline(key.removeprefix("_")) is member
    assert fli_airlines()[key] is member


def test_every_other_key_is_flis_own_member() -> None:
    """Alliances included, so a request for any of them is the one fli built."""
    wrong = [
        key
        for key, member in Airline.__members__.items()
        if member.name == key and fli_airlines()[key] is not member
    ]
    assert wrong == []
    assert set(fli_airlines()) == set(Airline.__members__)
    assert fli_airline("ONEWORLD") is Airline["ONEWORLD"]


def test_a_digit_leading_code_is_keyed_as_fli_keys_it() -> None:
    assert fli_airline("5C").name == "_5C"
    assert fli_airline("2K") is Airline["_2K"]


@pytest.mark.parametrize("code", ["XX", ""])
def test_a_code_fli_has_no_entry_for_raises_attribute_error(code: str) -> None:
    with pytest.raises(AttributeError) as raised:
        fli_airline(code)
    assert raised.value.args == (code,)


def test_an_aliased_member_survives_the_models_that_carry_it() -> None:
    w9 = fli_airline("W9")
    leg = FlightLeg(
        airline=w9,
        flight_number="5008",
        departure_airport=Airport["LTN"],
        arrival_airport=Airport["TIA"],
        departure_datetime=datetime(_DAY.year, _DAY.month, _DAY.day, 6, 0),
        arrival_datetime=datetime(_DAY.year, _DAY.month, _DAY.day, 10, 5),
        duration=185,
    )
    assert leg.airline is w9
    assert deepcopy(leg).airline is w9
    assert leg.model_copy(deep=True).airline is w9
    assert leg.model_dump(mode="json")["airline"] == "Wizz Air"
