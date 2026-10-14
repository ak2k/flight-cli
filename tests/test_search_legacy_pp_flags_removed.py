"""`search` no longer takes the hidden `--no-pp`, `--pp-only`, `--pp-airlines` and
`--pp-cabin` flags: each is a Click usage error. `--cash-only`, `--awards-only` and
`--provider-opt pp.airlines=...` / `pp.cabins=...` replace them."""

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


def _stub_backends(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reached: list[str] = []

    def _stub(name: str):
        def _run(*_a: object, **_k: object) -> None:
            reached.append(name)
            raise typer.Exit(0)

        return _run

    for name in _BACKENDS:
        monkeypatch.setattr(cli, name, _stub(name))
    return reached


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--no-pp"], id="no-pp"),
        pytest.param(["--pp-only"], id="pp-only"),
        pytest.param(["--pp-airlines", "UA"], id="pp-airlines"),
        pytest.param(["--pp-cabin", "Economy"], id="pp-cabin"),
    ],
)
def test_search_refuses_a_legacy_pp_flag_as_an_unknown_option(argv: list[str]) -> None:
    # QQ7 is refused before any request, so the case needs no network and no stub.
    result = CliRunner().invoke(cli.app, ["search", "QQ7", "LAX", "--dep", _DAY, *argv])

    assert result.exit_code == 2, result.output
    assert f"No such option: {argv[0]}" in result.output
    assert "deprecated" not in result.output


def test_search_with_a_current_flag_runs_without_a_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached = _stub_backends(monkeypatch)

    result = CliRunner().invoke(cli.app, ["search", "JFK", "LHR", "--dep", _DAY, "--cash-only"])

    assert result.exit_code == 0, result.output
    assert len(reached) == 1
    assert "deprecated" not in result.output
