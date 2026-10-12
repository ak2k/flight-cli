"""`search` refuses an invalid airport before it announces a backend.

The picker reads a mistyped code as a city code and prints "Using Matrix: ..."
for a query the leg build then refuses, so the refusal has to come first.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli

_DAY = (date.today() + timedelta(days=45)).isoformat()

_BACKENDS = (
    "_run_gflight_path",
    "_run_matrix_path",
    "_run_gflight_path_multi",
    "_run_matrix_path_multi",
    "_run_enriched_path",
)


def _search(monkeypatch: pytest.MonkeyPatch, *argv: str):
    def _unreached(*_a: object, **_k: object) -> None:
        raise AssertionError("a backend ran on a query that never validated")

    for name in _BACKENDS:
        monkeypatch.setattr(cli, name, _unreached)
    return CliRunner().invoke(cli.app, ["search", *argv])


def test_search_refuses_an_invalid_airport_before_announcing_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _search(monkeypatch, "QQ7", "LAX", "--dep", _DAY)

    assert result.exit_code == 2, result.output
    assert "Using " not in result.stderr
    assert result.stderr.strip().splitlines() == ["Not a 3-letter IATA code: 'QQ7'"]


@pytest.mark.parametrize(
    ("origin", "destination", "refusal"),
    [
        ("NYC", "QQ7", "Not a 3-letter IATA code: 'QQ7'"),
        ("JFK,XXXX", "MIA", "Not a 3-letter IATA code: 'XXXX'"),
        (",", "LAX", "origin and destination are required."),
    ],
)
def test_search_refuses_before_the_picker_whatever_the_bad_argument(
    monkeypatch: pytest.MonkeyPatch, origin: str, destination: str, refusal: str
) -> None:
    result = _search(monkeypatch, origin, destination, "--dep", _DAY)

    assert result.exit_code == 2, result.output
    assert result.stderr.strip().splitlines() == [refusal]


def test_search_beside_slice_leaves_the_positional_codes_to_the_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beside --slice the positional codes build no leg, so the early check skips them too."""
    reached: list[str] = []

    def _stop(*_a: object, **_k: object) -> None:
        reached.append("backend")
        raise typer.Exit(0)

    for name in _BACKENDS:
        monkeypatch.setattr(cli, name, _stop)
    result = CliRunner().invoke(
        cli.app, ["search", "QQ7", "LAX", "--dep", _DAY, "--slice", f"JFK-LAX:{_DAY}"]
    )

    assert result.exit_code == 0, result.output
    assert reached == ["backend"]
    assert "Not a 3-letter IATA code" not in result.stderr
