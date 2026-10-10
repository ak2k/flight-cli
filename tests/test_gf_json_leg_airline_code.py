# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A Google row's legs in `--format json` and the envelope carry the carrier's
IATA code in `airline_code`, beside `airline`, which keeps the name, so a leg
builds its flight number and matches a Matrix leg by carrier."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from fli.models import Airline
from typer.testing import CliRunner

from flight_cli import cli
from test_envelope import _envelope_of, _rows, _search
from test_gf_full_board import _DEP, _LAX, _SEARCH, _served

if TYPE_CHECKING:
    from collections.abc import Callable

_OUTBOUND = "ds1_metadata_blocks_kept.json"  # HNL-SEA-MIA, Alaska both legs
_RETURN = "ds1_return_leg_pinned.json"  # MIA-LAX-HNL, American both legs


def _carriers(row: dict[str, Any]) -> list[tuple[str, str]]:
    return [(leg.get("airline_code", ""), leg["airline"]) for leg in row["legs"]]


def _coded(leg: dict[str, Any]) -> bool:
    code = leg.get("airline_code", "")
    return bool(re.fullmatch(r"[A-Z0-9]{2}", code)) and leg["airline"] != code


def test_every_google_json_leg_carries_the_carrier_code_beside_its_name(
    gf_rows: Callable[..., list[Any]],
) -> None:
    row = cli._gflight_json_row(gf_rows(_OUTBOUND)[0])
    assert _carriers(row) == [("AS", "Alaska Airlines")] * 2


def test_each_member_of_a_round_trip_carries_its_own_carrier_code(
    gf_rows: Callable[..., list[Any]],
) -> None:
    ((outbound, back),) = cli._gflight_json_document([(gf_rows(_OUTBOUND)[0], gf_rows(_RETURN)[0])])
    assert _carriers(outbound) == [("AS", "Alaska Airlines")] * 2
    assert _carriers(back) == [("AA", "American Airlines")] * 2


def test_a_digit_leading_carrier_code_drops_fli_s_underscore(
    gf_rows: Callable[..., list[Any]],
) -> None:
    g = gf_rows(_OUTBOUND)[0]
    first = g.flight.legs[0].model_copy(update={"airline": Airline["_9W"]})
    g.flight = g.flight.model_copy(update={"legs": [first, *g.flight.legs[1:]]})
    row = cli._gflight_json_row(g)
    assert _carriers(row)[0] == ("9W", Airline["_9W"].value)
    assert _carriers(row)[1] == ("AS", "Alaska Airlines")


def test_every_envelope_leg_of_a_google_board_carries_a_carrier_code(
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


def test_every_json_leg_of_a_google_board_carries_a_carrier_code(
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
