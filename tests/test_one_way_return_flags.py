# pyright: reportPrivateUsage=false
"""A trip with no return refuses the flags that filter the return.

`--return-times`, `--routing-ret` and `--ext-ret` filter a return slice. A
`calendar --one-way` and a `search` with no `--return` have none, so a call
that took them would answer without the filter the user asked for. Each stops
with exit 2 and names them, as `detail` does. The backends are replaced by
recorders, so nothing reaches the network."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from click.testing import Result

_ONE_WAY_CALENDAR = ["calendar", "JFK", "LHR", "--start", "2026-10-20", "--one-way"]
_ONE_WAY_SEARCH = ["search", "JFK", "LHR", "--dep", "2026-10-20"]
_RETURN_ONLY = [
    ("--return-times", "evening"),
    ("--routing-ret", "LH UA"),
    ("--ext-ret", "MAXSTOPS 0"),
]


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The backends a command got as far as asking, in order."""
    asked: list[str] = []

    def _record(name: str) -> Any:
        def _stop(*_a: object, **_kw: object) -> None:
            asked.append(name)
            raise typer.Exit(0)

        return _stop

    monkeypatch.setattr(cli, "_calendar_without_fast", _record("calendar"))
    monkeypatch.setattr(cli, "_pick_backend", _record("search"))
    return asked


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _flat(text: str) -> str:
    """`text` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


@pytest.mark.parametrize(("flag", "value"), _RETURN_ONLY)
def test_a_one_way_calendar_refuses_a_return_only_flag(
    reached: list[str], flag: str, value: str
) -> None:
    """RED at base (exit 0, the flag dropped)."""
    result = _invoke(*_ONE_WAY_CALENDAR, flag, value)
    assert result.exit_code == 2, result.output
    stderr = _flat(result.stderr)
    assert flag in stderr
    assert "need a round trip. Drop them, or drop --one-way." in stderr
    assert result.stdout == ""
    assert reached == []


def test_a_search_with_no_return_refuses_return_times(reached: list[str]) -> None:
    """RED at base (exit 0, the flag dropped)."""
    result = _invoke(*_ONE_WAY_SEARCH, "--return-times", "evening")
    assert result.exit_code == 2, result.output
    stderr = _flat(result.stderr)
    assert "--return-times" in stderr
    assert "need a --return or --return-arrive. Drop them, or add one." in stderr
    assert result.stdout == ""
    assert reached == []


def test_a_one_way_calendar_names_every_return_only_flag_it_was_given(
    reached: list[str],
) -> None:
    result = _invoke(*_ONE_WAY_CALENDAR, "--return-times", "evening", "--ext-ret", "")
    assert result.exit_code == 2, result.output
    stderr = _flat(result.stderr)
    assert "--return-times, --ext-ret set the return's filters" in stderr
    assert "--routing-ret" not in stderr
    assert reached == []


@pytest.mark.parametrize("command", [_ONE_WAY_CALENDAR, _ONE_WAY_SEARCH])
def test_an_empty_return_times_filters_nothing_and_is_not_refused(
    reached: list[str], command: list[str]
) -> None:
    result = _invoke(*command, "--return-times", "")
    assert result.exit_code == 0, result.output
    assert len(reached) == 1


@pytest.mark.parametrize("command", [_ONE_WAY_CALENDAR, _ONE_WAY_SEARCH])
def test_the_outbound_filters_still_reach_a_trip_with_no_return(
    reached: list[str], command: list[str]
) -> None:
    result = _invoke(*command, "--depart-times", "morning", "--routing", "UA LH")
    assert result.exit_code == 0, result.output
    assert len(reached) == 1


def test_a_round_trip_calendar_keeps_its_return_flags(reached: list[str]) -> None:
    result = _invoke(
        "calendar", "JFK", "LHR", "--start", "2026-10-20", "--return-times", "evening",
        "--routing-ret", "LH UA", "--ext-ret", "MAXSTOPS 0",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert reached == ["calendar"]


def test_a_search_with_a_return_keeps_its_return_times(reached: list[str]) -> None:
    result = _invoke(*_ONE_WAY_SEARCH, "--return", "2026-10-27", "--return-times", "evening")
    assert result.exit_code == 0, result.output
    assert reached == ["search"]


def test_a_one_way_detail_passes_an_empty_return_times(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED at base (exit 2, the empty value counted as given)."""
    sent: list[object] = []

    def _run(search: object, *_a: object) -> None:
        sent.append(search)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_run", _run)
    result = _invoke(
        "detail", "JFK", "LHR", "--dep", "2026-10-20", "--no-matrix-url", "--no-google-url",
        "--return-times", "",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
