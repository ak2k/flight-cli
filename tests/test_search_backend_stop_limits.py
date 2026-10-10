# pyright: reportPrivateUsage=false
"""`flight search` compares a round trip's legs by the stop limit each is held to.

Google's page writes one filter set, under one stop limit, onto both slices, and
every returned row is held to its own leg's stop ceilings and `--stops`. A
`--stops 0` beside `--routing-ret N` therefore asks the same question on both
legs and stays on Google Flights; codes that come to different limits, or that
differ in anything else, are Matrix's."""

from __future__ import annotations

from typing import Any

import pytest
import typer

from flight_cli.cli import BACKEND_GFLIGHT, BACKEND_MATRIX
from test_backend_dispatch import _call

_DIFFER = "different routing or extension codes on the outbound and the return"


@pytest.mark.parametrize(
    ("overrides", "return_codes"),
    [
        ({"stops": 0}, ("N", None)),
        ({"stops": 1, "extension": "MAXSTOPS 2"}, (None, "MAXSTOPS 1")),
        ({"routing": "N", "extension": "MAXSTOPS 1"}, ("N", "MAXSTOPS 2")),
    ],
    ids=["stops-with-nonstop-return", "stops-lowers-both", "ceilings-meet-at-nonstop"],
)
def test_legs_that_come_to_the_same_stop_limit_stay_on_google_silently(
    overrides: dict[str, Any],
    return_codes: tuple[str | None, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _call(return_codes=return_codes, **overrides) == BACKEND_GFLIGHT
    assert _call(BACKEND_GFLIGHT, return_codes=return_codes, **overrides) == BACKEND_GFLIGHT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("overrides", "return_codes"),
    [
        ({"stops": 1}, ("N", None)),
        ({"stops": 0, "routing": "AA+"}, ("N", None)),
        ({"stops": -1}, ("N", None)),
        ({}, ("N", None)),
    ],
    ids=["stops-1-return-nonstop", "carrier-beside-stops", "negative-stops-is-none", "no-stops"],
)
def test_legs_held_to_different_limits_or_codes_are_still_matrix_naming_it(
    overrides: dict[str, Any],
    return_codes: tuple[str | None, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _call(return_codes=return_codes, **overrides) == BACKEND_MATRIX
    assert _DIFFER in " ".join(capsys.readouterr().err.split())
    with pytest.raises(typer.BadParameter, match=_DIFFER):
        _call(BACKEND_GFLIGHT, return_codes=return_codes, **overrides)
