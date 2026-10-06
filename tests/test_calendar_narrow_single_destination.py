# pyright: reportPrivateUsage=false
"""`--max-per-query > 1` narrows a calendar only when a query asks several destinations.

Matrix may under-report a request that names more than one destination. A split
whose every query names one is as complete as the default of one per query, so
its answer is not narrowed and its stderr carries no warning about it."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from test_calendar_split import _pair_client, _result

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_START = date.today() + timedelta(days=30)
_WARNING = "--max-per-query > 1"
_CALENDAR = [
    "calendar",
    "--start",
    _START.isoformat(),
    "--end",
    (_START + timedelta(days=13)).isoformat(),
    "--one-way",
    "--no-cache",
    "--no-matrix-url",
    "--no-google-url",
    "--max-per-query",
    "2",
]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))


def _grid() -> Any:
    return _result({9: {7: ("USD204.00", 3, {})}}, cheapest="USD204.00")


def _calendar(origins: str, destinations: str, fmt: str) -> Result:
    return CliRunner().invoke(
        cli.app, [*_CALENDAR[:1], origins, destinations, *_CALENDAR[1:], "--format", fmt]
    )


def test_single_destination_queries_leave_the_envelope_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _pair_client(monkeypatch, {("JFK", "LAX"): _grid(), ("EWR", "LAX"): _grid()})
    r = _calendar("JFK,EWR", "LAX", "envelope")
    assert r.exit_code == 0, r.output
    assert sorted(",".join(q.legs[0].destinations) for q in client.asked) == ["LAX", "LAX"]
    assert '"complete": true' in r.stdout.replace('":true', '": true'), r.stdout
    assert _WARNING not in r.stdout
    assert _WARNING not in r.stderr


def test_single_destination_queries_print_no_warning_beside_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pair_client(monkeypatch, {("JFK", "LAX"): _grid(), ("EWR", "LAX"): _grid()})
    r = _calendar("JFK,EWR", "LAX", "json")
    assert r.exit_code == 0, r.output
    assert _WARNING not in r.stderr


def test_a_query_asking_several_destinations_still_narrows_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _pair_client(monkeypatch, {("JFK", "LAX,SFO"): _grid(), ("EWR", "LAX,SFO"): _grid()})
    r = _calendar("JFK,EWR", "LAX,SFO", "envelope")
    assert r.exit_code == 0, r.output
    assert sorted(",".join(q.legs[0].destinations) for q in client.asked) == ["LAX,SFO", "LAX,SFO"]
    assert '"complete": false' in r.stdout.replace('":false', '": false'), r.stdout
    assert "may under-report" in r.stdout
    assert _WARNING in r.stderr


def test_several_destinations_asked_one_per_query_leave_the_envelope_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = [("LHR", "LGW"), ("LHR", "CDG"), ("LGW", "LHR"), ("LGW", "CDG")]
    client = _pair_client(monkeypatch, {pair: _grid() for pair in pairs})
    r = _calendar("LHR,LGW", "LHR,LGW,CDG", "envelope")
    assert r.exit_code == 0, r.output
    asked = sorted(",".join(q.legs[0].destinations) for q in client.asked)
    assert asked == ["CDG", "CDG", "LGW", "LHR"]
    assert '"complete": true' in r.stdout.replace('":true', '": true'), r.stdout
    assert _WARNING not in r.stdout
    assert _WARNING not in r.stderr
