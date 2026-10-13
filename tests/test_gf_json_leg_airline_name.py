# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A Google row's legs in `--format json` name a carrier the bundled map
`CARRIER_NAMES` lists by that map's name, not fli's: fli names LH its cargo arm.
A carrier the map does not list keeps fli's name, and `airline_code` is fli's."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fli.models import Airline

from flight_cli import cli

if TYPE_CHECKING:
    from collections.abc import Callable

_OUTBOUND = "ds1_metadata_blocks_kept.json"  # HNL-SEA-MIA, Alaska both legs


def _first_leg_as(gf_rows: Callable[..., list[Any]], member: str) -> dict[str, Any]:
    g = gf_rows(_OUTBOUND)[0]
    first = g.flight.legs[0].model_copy(update={"airline": Airline[member]})
    g.flight = g.flight.model_copy(update={"legs": [first, *g.flight.legs[1:]]})
    leg: dict[str, Any] = cli._gflight_json_row(g)["legs"][0]
    return leg


def test_a_lufthansa_leg_is_named_lufthansa_not_its_cargo_arm(
    gf_rows: Callable[..., list[Any]],
) -> None:
    leg = _first_leg_as(gf_rows, "LH")
    assert leg["airline"] == "Lufthansa", f"LH leg named {leg['airline']!r}"
    assert leg["airline_code"] == "LH"


@pytest.mark.parametrize(
    ("code", "name"),
    [
        pytest.param("SY", "Sun Country Airlines", id="sun-country"),
        pytest.param("YX", "Republic Airways", id="republic"),
    ],
)
def test_the_other_carriers_the_map_spells_differently_take_its_spelling(
    gf_rows: Callable[..., list[Any]], code: str, name: str
) -> None:
    leg = _first_leg_as(gf_rows, code)
    assert leg["airline"] == name
    assert leg["airline_code"] == code


def test_a_carrier_the_map_does_not_list_keeps_fli_s_name(
    gf_rows: Callable[..., list[Any]],
) -> None:
    leg = _first_leg_as(gf_rows, "_9W")
    assert leg["airline"] == Airline["_9W"].value
    assert leg["airline_code"] == "9W"
