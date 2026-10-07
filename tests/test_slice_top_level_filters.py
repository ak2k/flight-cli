# pyright: reportPrivateUsage=false
"""A top-level `--routing`/`--extension` beside `--slice` reaches every slice
Matrix is sent, unless the slice names its own `r=`/`e=`. Every backend call is
replaced by a recorder, so nothing reaches the network."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import SearchResult
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    import pytest

    from flight_cli.domain import Leg, SpecificDateSearch

_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url", "--format", "json"]


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=45 + offset)).isoformat()


def _sent(monkeypatch: pytest.MonkeyPatch, *args: str) -> list[dict[str, Any]]:
    """The slices of the one body Matrix is sent for `flight search <args>`."""
    asked: list[SpecificDateSearch] = []

    def _matrix(search: SpecificDateSearch, *_a: object) -> SearchResult:
        asked.append(search)
        return SearchResult.from_api({})

    monkeypatch.setattr(cli, "_run", _matrix)
    result = CliRunner().invoke(cli.app, [*_SEARCH, *args], env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    (search,) = asked
    return to_wire(search).as_json()["inputs"]["slices"]


def _two_slices(*, first: str = "", second: str = "") -> list[str]:
    return [
        "--slice",
        f"EWR-FRA:{_day(0)}{first}",
        "--slice",
        f"FRA-SIN:{_day(7)}{second}",
    ]


def test_top_level_routing_and_extension_reach_every_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _sent(monkeypatch, *_two_slices(), "--routing", "LH+", "--extension", "MAXCONNECT 2:00")
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in out] == [
        ("LH+", "MAXCONNECT 2:00"),
        ("LH+", "MAXCONNECT 2:00"),
    ]


def test_a_slice_with_its_own_codes_keeps_them_and_the_other_takes_the_top_level_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _sent(
        monkeypatch,
        *_two_slices(first=":r=BA+:e=MAXSTOPS 1"),
        "--routing",
        "LH+",
        "--extension",
        "MAXCONNECT 2:00",
    )
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in out] == [
        ("BA+", "MAXSTOPS 1"),
        ("LH+", "MAXCONNECT 2:00"),
    ]


def test_an_empty_r_or_e_keeps_the_slice_free_of_the_top_level_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _sent(
        monkeypatch,
        *_two_slices(second=":r=:e="),
        "--routing",
        "LH+",
        "--extension",
        "MAXCONNECT 2:00",
    )
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in out] == [
        ("LH+", "MAXCONNECT 2:00"),
        ("", ""),
    ]


def test_slices_with_no_top_level_codes_send_what_they_were_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _sent(monkeypatch, *_two_slices(second=":r=LH+"))
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in out] == [
        (None, None),
        ("LH+", None),
    ]


def test_fare_gives_the_top_level_codes_to_every_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Leg, ...]] = []

    def _matrix(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        seen.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app, ["fare", "--no-pp", *_two_slices(second=":r=BA+"), "--routing", "LH+"]
    )
    assert result.exit_code == 0, result.output
    ((first, second),) = seen
    assert (first.route_language, second.route_language) == ("LH+", "BA+")


def test_slice_help_says_the_top_level_codes_are_the_default() -> None:
    result = CliRunner().invoke(cli.app, ["search", "--help"], env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    assert "--routing/--extension is the default for a slice with no r=/e=." in " ".join(
        result.output.replace("│", " ").split()
    )
