# pyright: reportPrivateUsage=false
"""An extension code's numbers (`MAXSTOPS 1`, `MAXDUR 9:00`) are plain ASCII
digits. A digit-like character that is not one (a superscript, an Arabic-Indic
digit) is a code Google cannot express, so it goes to Matrix with a reason,
instead of crashing the parser or being read as a number."""

from __future__ import annotations

import pytest

from flight_cli.cli import BACKEND_AUTO, BACKEND_GFLIGHT, BACKEND_MATRIX, _pick_backend
from flight_cli.routing_predicates import (
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
    UnsupportedPred,
    classify,
    page_can_encode,
    parse_extension,
)

# Escapes keep the non-ASCII digits visible: U+00B2 SUPERSCRIPT TWO is
# `str.isdigit()` but not `isdecimal()`, so `int()` raises on it; U+0661
# ARABIC-INDIC DIGIT ONE is both, and `int()` reads it as 1.
_SUPERSCRIPT_TWO = "\u00b2"
_ARABIC_INDIC_ONE = "\u0661"


def _backend(extension: str) -> str:
    return _pick_backend(
        backend=BACKEND_AUTO,
        routing=None,
        extension=extension,
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


@pytest.mark.parametrize(
    "digit", [_SUPERSCRIPT_TWO, _ARABIC_INDIC_ONE], ids=["superscript-two", "arabic-indic-one"]
)
def test_non_ascii_digit_is_unsupported_not_a_crash(digit: str) -> None:
    directive = f"MAXSTOPS {digit}"
    (pred,) = parse_extension(directive)
    assert pred == UnsupportedPred(
        token=directive, reason=f"extension {directive!r} not expressible on GF"
    )


@pytest.mark.parametrize(
    "digit", [_SUPERSCRIPT_TWO, _ARABIC_INDIC_ONE], ids=["superscript-two", "arabic-indic-one"]
)
def test_non_ascii_digit_goes_to_matrix_with_its_reason(digit: str) -> None:
    directive = f"MAXSTOPS {digit}"
    assert _backend(directive) == BACKEND_MATRIX
    encodable, reasons = page_can_encode(classify(None, directive).predicates)
    assert not encodable
    assert reasons == [f"extension {directive!r} not expressible on GF"]


def test_maxstops_past_the_int_digit_limit_is_unsupported_not_a_crash() -> None:
    # `int()` refuses a string of more than 4300 digits.
    directive = "MAXSTOPS " + "1" * 4301
    assert parse_extension(directive) == [
        UnsupportedPred(token=directive, reason=f"extension {directive!r} not expressible on GF")
    ]
    assert _backend(directive) == BACKEND_MATRIX


def test_ascii_digits_still_parse() -> None:
    assert parse_extension("MAXSTOPS 1") == [StopsPred(max_stops=1)]
    assert parse_extension("MAXSTOPS 12") == [StopsPred(max_stops=12)]
    assert _backend("MAXSTOPS 1") == BACKEND_GFLIGHT
    assert parse_extension("MAXDUR 9:00") == [MaxDurationPred(minutes=540)]
    assert parse_extension("MINCONNECT 1:30") == [ConnectTimePred(min_minutes=90, max_minutes=None)]


@pytest.mark.parametrize("code", ["MAXDUR", "MAXCONNECT", "MINCONNECT"])
@pytest.mark.parametrize("time", [f"{_ARABIC_INDIC_ONE}:00", f"1:0{_ARABIC_INDIC_ONE}"])
def test_non_ascii_digit_in_a_clock_time_is_unsupported(code: str, time: str) -> None:
    directive = f"{code} {time}"
    (pred,) = parse_extension(directive)
    assert pred == UnsupportedPred(
        token=directive, reason=f"extension {directive!r} not expressible on GF"
    )
