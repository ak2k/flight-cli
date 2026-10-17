"""`flight search --dry-run` prints the Matrix query it would send, and sends nothing.

The Matrix runner is replaced by one that raises, so a request leaving the
process fails the test; nothing reaches the network."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from typer.testing import CliRunner

from flight_cli import _config, cli
from flight_cli.client import SEARCH_URL
from flight_cli.providers import registry
from flight_cli.providers.pointspath import provider as pp_provider
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from click.testing import Result

_DEP = (date.today() + timedelta(days=60)).isoformat()
_RET = (date.today() + timedelta(days=67)).isoformat()
_BASE = ["search", "JFK", "LHR", "--dep", _DEP]
_MATRIX = [*_BASE, "--backend", "matrix"]
_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_kw: object) -> NoReturn:
        raise AssertionError("--dry-run sent a Matrix request")

    monkeypatch.setattr(cli, "_run", _raise)


def _invoke(*args: str) -> Result:
    # At 80 columns a refusal line Rich wraps would show as two.
    return CliRunner().invoke(cli.app, list(args), env={"COLUMNS": "80"})


def _body(text: str) -> dict[str, Any]:
    """The JSON object that follows `Wire body:` in the table form."""
    _, _, rest = text.partition("Wire body:\n")
    body: dict[str, Any] = json.loads(rest)
    return body


def test_dry_run_prints_the_matrix_query_without_sending_it() -> None:
    result = _invoke(*_MATRIX, "--routing", "LH+", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Backend: matrix" in result.stdout
    assert "Endpoint: POST " + SEARCH_URL in result.stdout
    assert "LH+" in json.dumps(_body(result.stdout))


def test_dry_run_json_is_one_document() -> None:
    result = _invoke(*_MATRIX, "--format", "json", "--dry-run")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["backend"] == "matrix"
    assert doc["endpoint"] == SEARCH_URL
    assert doc["reason"] == "--backend matrix"
    assert isinstance(doc["wire_body"], dict)


def test_dry_run_names_a_matrix_only_flag_as_the_reason() -> None:
    result = _invoke(*_BASE, "--routing", "BA AA", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Backend: matrix (auto: this search uses a Matrix-only flag)" in result.stdout


def test_dry_run_body_carries_the_cap_currency() -> None:
    """A capped search is asked in USD by default, as the real search asks it."""
    result = _invoke(*_MATRIX, "--max-price", "900", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "USD" in json.dumps(_body(result.stdout))


@pytest.mark.parametrize(
    ("extra", "route"),
    [
        (["--backend", "gflight"], "Google Flights"),
        ([], "Google Flights"),
        (["--backend", "matrix", "--cabin", "economy,business"], "multi-cabin"),
        (["--backend", "matrix", "--return", _RET], "multi-leg or round-trip"),
        (["--backend", "matrix", "--format", "csv"], "--format csv"),
    ],
)
def test_dry_run_refuses_a_route_on_one_stderr_line(extra: list[str], route: str) -> None:
    result = _invoke(*_BASE, *extra, "--dry-run")
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1, result.stderr
    assert route in result.stderr


def test_dry_run_refuses_slice_searches_on_one_stderr_line() -> None:
    result = _invoke(
        "search",
        "--slice",
        "JFK-LHR:" + _DEP,
        "--slice",
        "LHR-CDG:" + _DEP,
        "--backend",
        "gflight",
        "--dry-run",
    )
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1, result.stderr
    assert "--slice" in result.stderr


@pytest.mark.parametrize(
    "extra", [[], ["--format", "json"], ["--fare-rules", "--format", "json"]], ids=str
)
def test_dry_run_reads_no_award_config_or_credentials(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    """Nothing is sent, so no award provider is asked: a config.toml that would
    not load and the providers' credential checks are never reached."""

    def _read(*_a: object, **_kw: object) -> NoReturn:
        raise AssertionError("--dry-run read award-provider credentials")

    _config.DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ = _config.DEFAULT_CONFIG_PATH.write_text("x = = 1\n")
    monkeypatch.setattr(registry, "has_any_configured", _read)
    monkeypatch.setattr(pp_provider, "is_configured", _read)
    monkeypatch.setattr(seats_auth, "is_configured", _read)
    result = _invoke(*_MATRIX, *extra, "--dry-run")
    assert result.exit_code == 0, result.output
    assert SEARCH_URL in result.stdout


def test_dry_run_refuses_sellers_as_the_matrix_search_does() -> None:
    result = _invoke(*_MATRIX, "--sellers", "--dry-run")
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "--sellers needs a Google Flights result" in result.stderr


def test_readme_names_dry_run() -> None:
    assert "--dry-run" in (_ROOT / "README.md").read_text()
