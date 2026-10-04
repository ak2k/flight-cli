# pyright: reportPrivateUsage=false
"""A round trip names each pinned outbound it loses, and why.

The six `*_oplh_*` captures are skill Example 7 on Google (2026-10-01): JFK-LHR
`--routing O:LH+ --ext 'MINCONNECT 1:30'`, the outbound board and the return
board of each of its five pins, in pin order. Two pins have no LH-operated
return: LH405/LH914 (2 returns served) and LH411/UA9440 (1)."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import cli

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from click.testing import Result

# fli's own validator rejects a past travel date, so these are derived.
_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_RETURNS = [f"ds1_lhr_jfk_oplh_ret{i}.json" for i in range(1, 6)]
_EXAMPLE_7 = [
    "search",
    "--cash-only",
    "--no-google-url",
    "--no-matrix-url",
    "JFK",
    "LHR",
    "--dep",
    _DEP.isoformat(),
    "--return",
    _RET.isoformat(),
    "--routing",
    "O:LH+",
    "--ext",
    "MINCONNECT 1:30",
    "--backend",
    "gflight",
    "--format",
    "json",
]
_LOGGER = "flight_cli._gflight_ids"


def _outbound() -> str:
    return _page(
        _answering(
            _ds1("ds1_jfk_lhr_oplh_out.json"), origin=None, destination=None, date=_DEP.isoformat()
        )
    )


def _return(name: str) -> str:
    return _page(_answering(_ds1(name), origin=None, destination=None, date=_RET.isoformat()))


def _replay(
    gf_session: Callable[..., Any], caplog: pytest.LogCaptureFixture, *returns: str
) -> tuple[Any, Result, list[str]]:
    fake = gf_session(_outbound(), *returns)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = CliRunner().invoke(cli.app, _EXAMPLE_7)
    assert result.exit_code == 0, result.output
    lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    return fake, result, lines


def _pairs(result: Result) -> list[list[dict[str, Any]]]:
    return json.loads(result.stdout)


def _booked(member: dict[str, Any]) -> tuple[str, ...]:
    return tuple(leg["flight_number"] for leg in member["legs"])


def test_the_replay_keeps_three_lh_operated_pairs_in_six_gets(
    gf_session: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Green at the base and the tip: what the replay answers is unchanged."""
    fake, result, lines = _replay(gf_session, caplog, *map(_return, _RETURNS))
    pairs = _pairs(result)
    assert len(fake.gets) == 6
    assert len(pairs) == 3
    assert all(
        leg["amenities"]["operating_carrier"] == "LH"
        for pair in pairs
        for member in pair
        for leg in member["legs"]
    )
    assert "2 of 5 pinned outbounds have no return flight matching the routing" in lines


def test_each_pin_the_routing_left_without_a_return_is_named(
    gf_session: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Red at the base, whose count line was the only account of them."""
    _, result, lines = _replay(gf_session, caplog, *map(_return, _RETURNS))
    count = "2 of 5 pinned outbounds have no return flight matching the routing"
    named = [
        "pinned outbound LH405/LH914 (USD943.00) lost: Google served 2 returns for it, "
        "none matching the routing",
        "pinned outbound LH411/UA9440 (USD1346.00) lost: Google served 1 return for it, "
        "none matching the routing",
    ]
    assert [ln for ln in lines if ln in (count, *named)] == [count, *named]
    served = {_booked(pair[0]) for pair in _pairs(result)}
    assert len(served) + len(named) == 5


def test_a_pin_google_served_no_return_for_is_counted_and_named(
    gf_session: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Red at the base, which dropped it without a word. Pin 2's return board
    is replaced by one with no rows."""
    returns = [_return(n) for n in _RETURNS]
    returns[1] = _page(_ds1("ds1_zero_rows.json"))
    fake, result, lines = _replay(gf_session, caplog, *returns)
    pairs = _pairs(result)
    assert len(fake.gets) == 6
    assert len(pairs) == 2
    count = "1 of 5 pinned outbounds have no return flight on Google"
    named = "pinned outbound LH401/LH900 (USD842.00) lost: Google served no return for it"
    assert count in lines
    assert named in lines
    assert lines.index(count) < lines.index(named)
    lost = [ln for ln in lines if ln.startswith("pinned outbound ")]
    assert len({_booked(pair[0]) for pair in pairs}) + len(lost) == 5
