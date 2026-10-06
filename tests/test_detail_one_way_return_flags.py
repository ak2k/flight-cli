# pyright: reportPrivateUsage=false
"""A one-way `detail` refuses the flags that set the return.

`--return-times`, `--routing-ret` and `--ext-ret` filter a return slice, and a
one-way has none, so a one-way call given any of them would answer without the
filter the user asked for. It stops with exit 2 and names them, as `search`
does for the codes. Matrix's runner is replaced by a recorder, so nothing
reaches the network."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from click.testing import Result

    from flight_cli.domain import CalendarFollowup

_ONE_WAY = ["detail", "JFK", "LHR", "--dep", "2026-10-20", "--no-matrix-url", "--no-google-url"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[CalendarFollowup]:
    searches: list[CalendarFollowup] = []

    def _run(search: CalendarFollowup, *_a: Any) -> None:
        searches.append(search)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_run", _run)
    return searches


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _flat(text: str) -> str:
    """`text` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--return-times", "evening"), ("--routing-ret", "LH UA"), ("--ext-ret", "MAXSTOPS 0")],
)
def test_a_one_way_detail_refuses_a_return_only_flag(
    sent: list[CalendarFollowup], flag: str, value: str
) -> None:
    """RED at base (exit 0, the flag dropped)."""
    result = _invoke(*_ONE_WAY, flag, value)
    assert result.exit_code == 2, result.output
    stderr = _flat(result.stderr)
    assert flag in stderr
    assert "need a --return" in stderr
    assert sent == []


def test_a_one_way_detail_names_every_return_only_flag_it_was_given(
    sent: list[CalendarFollowup],
) -> None:
    result = _invoke(*_ONE_WAY, "--return-times", "evening", "--ext-ret", "")
    assert result.exit_code == 2, result.output
    stderr = _flat(result.stderr)
    assert "--return-times" in stderr
    assert "--ext-ret" in stderr
    assert "--routing-ret" not in stderr
    assert sent == []


def test_a_one_way_detail_keeps_the_outbound_filters(sent: list[CalendarFollowup]) -> None:
    result = _invoke(*_ONE_WAY, "--depart-times", "morning", "--routing", "UA LH")
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
