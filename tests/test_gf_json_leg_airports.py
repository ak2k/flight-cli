# pyright: reportPrivateUsage=false
"""A Google row's legs in `--format json` and the envelope name each airport by
its IATA code, with fli's name for it beside, so a leg compares with a Matrix
leg and with the airports the search asked for."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from test_envelope import _envelope_of, _rows, _search
from test_gf_full_board import _DEP, _LAX, _SEARCH, _served

if TYPE_CHECKING:
    from collections.abc import Callable

_OUTBOUND = "ds1_metadata_blocks_kept.json"  # HNL-SEA-MIA
_RETURN = "ds1_return_leg_pinned.json"  # MIA-LAX-HNL, its return board
_HNL = "Daniel K Inouye International Airport"
_SEA = "Seattle-Tacoma International Airport"
_MIA = "Miami International Airport"
_LAX_NAME = "Los Angeles International Airport"


def _codes(row: dict[str, Any]) -> list[tuple[str, str]]:
    return [(leg["departure_airport"], leg["arrival_airport"]) for leg in row["legs"]]


def _names(row: dict[str, Any]) -> list[tuple[str, str]]:
    return [(leg["departure_airport_name"], leg["arrival_airport_name"]) for leg in row["legs"]]


def _coded(leg: dict[str, Any]) -> bool:
    return all(
        re.fullmatch(r"[A-Z]{3}", leg[f"{end}_airport"]) and leg[f"{end}_airport_name"]
        for end in ("departure", "arrival")
    )


def test_every_google_json_leg_carries_iata_codes_and_airport_names(
    gf_rows: Callable[..., list[Any]],
) -> None:
    row = cli._gflight_json_row(gf_rows(_OUTBOUND)[0])
    assert _codes(row) == [("HNL", "SEA"), ("SEA", "MIA")]
    assert _names(row) == [(_HNL, _SEA), (_SEA, _MIA)]


def test_each_member_of_a_round_trip_carries_its_own_codes_and_names(
    gf_rows: Callable[..., list[Any]],
) -> None:
    ((outbound, back),) = cli._gflight_json_document([(gf_rows(_OUTBOUND)[0], gf_rows(_RETURN)[0])])
    assert _codes(outbound) == [("HNL", "SEA"), ("SEA", "MIA")]
    assert _names(outbound) == [(_HNL, _SEA), (_SEA, _MIA)]
    assert _codes(back) == [("MIA", "LAX"), ("LAX", "HNL")]
    assert _names(back) == [(_MIA, _LAX_NAME), (_LAX_NAME, _HNL)]


def test_every_envelope_leg_of_a_google_board_is_coded(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "--cash-only",
            *("JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--backend", "gflight", "--fast", "-n", "95"),
        )
    )
    rows = _rows(env)
    assert len(rows) == 95
    assert all(_coded(leg) for r in rows for leg in r["row"]["legs"])
    assert rows[0]["row"]["legs"][0]["departure_airport"] == "JFK"


def test_every_json_leg_of_a_google_board_is_coded(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *(*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--backend", "gflight", "--fast", "--format", "json", "-n", "95"),
        ],
    )
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    assert len(rows) == 95
    assert all(_coded(leg) for r in rows for leg in r["legs"])
    assert rows[0]["legs"][-1]["arrival_airport"] == "LAX"
