# pyright: reportPrivateUsage=false
"""A flight number is plain ASCII digits, at most nine. A digit-like character
that is not one (an Arabic-Indic digit, a superscript) or a longer run is no
flight number Google can serve, so a routing carrying it goes to Matrix, and a
flight string from Google's own answer never crashes the parser or the
booking-page key."""

from __future__ import annotations

import pytest

from flight_cli._gf_booking import _flight_key
from flight_cli._gf_postfilter import _parse_flight
from flight_cli.cli import BACKEND_AUTO, BACKEND_GFLIGHT, BACKEND_MATRIX, _pick_backend
from flight_cli.routing_predicates import (
    SpecificFlightPred,
    UnsupportedPred,
    _is_one_flight,
    parse_routing,
)

# Escapes keep the non-ASCII digits visible: U+0661 ARABIC-INDIC DIGIT ONE is
# `isdecimal()`, so `\d` matches it and `int()` reads it as 1; U+00B2
# SUPERSCRIPT TWO is `isdigit()` only, so `int()` raises on it.
_ARABIC_INDIC_ONE = "\u0661"
_SUPERSCRIPT_TWO = "\u00b2"
# `int()` refuses a string of more than 4300 digits.
_PAST_INT_LIMIT = "1" * 5000


def _backend(routing: str) -> str:
    return _pick_backend(
        backend=BACKEND_AUTO,
        routing=routing,
        extension=None,
        slice_specs=None,
        depart_times=None,
        return_times=None,
        stops=None,
        children=0,
        seniors=0,
        youth=0,
        inf_seat=0,
        inf_lap=0,
        origin="JFK",
        destination="LHR",
        allow_airport_changes=True,
        show_only_available=True,
    )


def _unsupported(routing: str) -> UnsupportedPred:
    return UnsupportedPred(token=routing, reason=f"routing {routing!r} not GF-expressible")


def test_non_ascii_digit_in_a_routing_flight_number_goes_to_matrix() -> None:
    routing = "BA" + _ARABIC_INDIC_ONE
    assert parse_routing(routing) == [_unsupported(routing)]
    assert _backend(routing) == BACKEND_MATRIX


@pytest.mark.parametrize(
    "routing",
    ["BA1-" + _ARABIC_INDIC_ONE, "BA" + _ARABIC_INDIC_ONE + "-2"],
    ids=["high-end", "low-end"],
)
def test_non_ascii_digit_in_a_flight_number_range_goes_to_matrix(routing: str) -> None:
    assert parse_routing(routing) == [_unsupported(routing)]
    assert _backend(routing) == BACKEND_MATRIX


@pytest.mark.parametrize(
    "routing",
    ["BA" + _PAST_INT_LIMIT, "BA1-" + _PAST_INT_LIMIT, "BA1234567890"],
    ids=["past-int-limit", "range-past-int-limit", "ten-digits"],
)
def test_routing_flight_number_past_nine_digits_is_unsupported_not_a_crash(routing: str) -> None:
    assert parse_routing(routing) == [_unsupported(routing)]
    assert _backend(routing) == BACKEND_MATRIX


def test_ascii_routing_flight_numbers_still_parse() -> None:
    assert parse_routing("UA882") == [SpecificFlightPred("UA", 882, 882)]
    assert parse_routing("UA1000-2000+") == [SpecificFlightPred("UA", 1000, 2000, quantifier="+")]
    assert parse_routing("BA123456789") == [SpecificFlightPred("BA", 123456789, 123456789)]
    assert _backend("BA1") == BACKEND_GFLIGHT


def test_one_flight_ignores_non_ascii_digits_and_reads_long_ranges() -> None:
    assert not _is_one_flight("BA" + _ARABIC_INDIC_ONE)
    assert not _is_one_flight("BA1-" + _ARABIC_INDIC_ONE)
    assert _is_one_flight("BA" + _PAST_INT_LIMIT + "-" + _PAST_INT_LIMIT)
    assert not _is_one_flight("BA1-" + _PAST_INT_LIMIT)
    assert _is_one_flight("BA7")
    assert _is_one_flight("BA007-7")
    assert not _is_one_flight("BA7-8")


def test_a_flight_string_with_a_non_ascii_or_overlong_number_is_not_parsed() -> None:
    assert _parse_flight("UA" + _ARABIC_INDIC_ONE) is None
    assert _parse_flight("UA" + _SUPERSCRIPT_TWO) is None
    assert _parse_flight("UA" + _PAST_INT_LIMIT) is None
    assert _parse_flight("UA882") == ("UA", 882)
    assert _parse_flight("UA0882") == ("UA", 882)


def test_booking_flight_key_keeps_a_number_it_cannot_read_as_ascii_digits() -> None:
    assert _flight_key("DL", _SUPERSCRIPT_TWO) == "DL" + _SUPERSCRIPT_TWO
    assert _flight_key("DL", _ARABIC_INDIC_ONE) == "DL" + _ARABIC_INDIC_ONE
    assert _flight_key("DL", _PAST_INT_LIMIT) == "DL" + _PAST_INT_LIMIT
    assert _flight_key("DL", "0178") == "DL178"
    assert _flight_key("DL", 178) == "DL178"
