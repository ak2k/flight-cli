"""What the deprecated `fare` asks of a round trip's return slice.

A slice's routing reads from its own origin, so `fare` holds the return to the
rule `search` does: `--routing-ret`/`--ext-ret` when given (`''` for none), else
the outbound's codes only when they read the same both ways. The Matrix runner
is replaced by a recorder, so nothing reaches the network."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from click.testing import Result

    from flight_cli.domain import Leg

D, R = "2026-10-20", "2026-10-27"
_ONE_WAY = ["fare", "JFK", "LHR", "--dep", D, "--no-pp", "--no-matrix-url", "--no-google-url"]
_ROUND_TRIP = [*_ONE_WAY, "--return", R]
_NEED_RETURN = "--routing-ret and --ext-ret set the return's codes, and need a --return."


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Leg, ...]]:
    """The legs of every Matrix run `fare` started."""
    runs: list[tuple[Leg, ...]] = []

    def _record(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        runs.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path", _record)
    return runs


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _flat(text: str) -> str:
    """`text` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _codes(legs: tuple[Leg, ...]) -> list[tuple[str | None, str | None]]:
    return [(leg.route_language, leg.extension) for leg in legs]


def test_fare_refuses_an_ordered_chain_on_the_return(seen: list[tuple[Leg, ...]]) -> None:
    """RED at base: `fare` copied 'UA LH' onto the return, asking for UA then LH
    from London."""
    result = _invoke(*_ROUND_TRIP, "--routing", "UA LH")
    assert result.exit_code == 2, result.output
    assert "--routing-ret" in _flat(result.output)
    assert seen == []


@pytest.mark.parametrize("routing", ["UA LH", "DL747", "AA882-882"])
def test_a_direction_dependent_routing_is_refused_without_routing_ret(
    seen: list[tuple[Leg, ...]], routing: str
) -> None:
    result = _invoke(*_ROUND_TRIP, "--routing", routing)
    assert result.exit_code == 2, result.output
    err = _flat(result.stderr)
    assert "--routing-ret" in err
    assert f"--routing {routing!r}" in err
    assert result.stdout == ""
    assert seen == []


@pytest.mark.parametrize(
    ("args", "codes"),
    [
        (["--routing", "UA LH", "--routing-ret", "LH UA"], [("UA LH", None), ("LH UA", None)]),
        (["--routing", "UA LH", "--routing-ret", ""], [("UA LH", None), (None, None)]),
        (["--routing", "AA+"], [("AA+", None), ("AA+", None)]),
        (["--routing", "F* X:LHR F*"], [("F* X:LHR F*", None), ("F* X:LHR F*", None)]),
        (["--ext-ret", "MAXSTOPS 1"], [(None, None), (None, "MAXSTOPS 1")]),
        (
            ["--ext", "MAXCONNECT 2:00", "--ext-ret", ""],
            [(None, "MAXCONNECT 2:00"), (None, None)],
        ),
    ],
    ids=["routing-ret", "routing-ret-empty", "AA+", "via-LHR", "ext-ret", "ext-ret-empty"],
)
def test_the_return_leg_carries_its_own_codes(
    seen: list[tuple[Leg, ...]], args: list[str], codes: list[tuple[str | None, str | None]]
) -> None:
    result = _invoke(*_ROUND_TRIP, *args)
    assert result.exit_code == 0, result.output
    [legs] = seen
    assert _codes(legs) == codes


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        ([*_ONE_WAY, "--routing-ret", "LH UA"], "Drop them, or add --return."),
        ([*_ONE_WAY, "--ext-ret", "MAXSTOPS 1"], "Drop them, or add --return."),
        (
            ["fare", "--no-pp", "--slice", f"JFK-LHR:{D}", "--routing-ret", "LH UA"],
            "A --slice takes its own in its r= and e= fields.",
        ),
    ],
    ids=["one-way-routing-ret", "one-way-ext-ret", "slice"],
)
def test_return_codes_without_a_return_are_refused(
    seen: list[tuple[Leg, ...]], args: list[str], hint: str
) -> None:
    result = _invoke(*args)
    assert result.exit_code == 2, result.output
    assert f"{_NEED_RETURN} {hint}" in _flat(result.stderr)
    assert seen == []


def test_a_one_way_chain_is_sent_as_before(seen: list[tuple[Leg, ...]]) -> None:
    """Green at base and tip: one leg has no return to misread."""
    result = _invoke(*_ONE_WAY, "--routing", "UA LH")
    assert result.exit_code == 0, result.output
    [legs] = seen
    assert _codes(legs) == [("UA LH", None)]
