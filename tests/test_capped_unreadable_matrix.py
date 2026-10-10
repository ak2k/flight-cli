"""A capped Matrix answer whose every fare the cap drops prints the cap sentence.

`_price_capped` keeps Matrix's own `solutionCount` when it drops a fare it cannot
read (no party total, another currency), so the table must not announce that
count over a table with no rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pytest

# fli's validator rejects a past travel date, so the date is derived.
_DEP = date.today() + timedelta(days=45)


def _solution(sid: str, price: str, *, total: str | None) -> dict[str, Any]:
    solution: dict[str, Any] = {
        "id": sid,
        "ext": {"price": price},
        "itinerary": {
            "slices": [
                {
                    "flights": ["DL1788"],
                    "departure": f"{_DEP.isoformat()}T09:00:00",
                    "arrival": f"{_DEP.isoformat()}T12:00:00",
                    "origin": {"code": "JFK"},
                    "destination": {"code": "LAX"},
                }
            ],
            "carriers": [{"code": "DL"}],
        },
    }
    if total is not None:
        solution["displayTotal"] = total
    return solution


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, *solutions: dict[str, Any]) -> None:
    body = {"solutionCount": len(solutions), "solutionList": {"solutions": list(solutions)}}

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return SearchResult.from_api(body)

    monkeypatch.setattr(cli, "_run", _run)


def _search(*args: str) -> str:
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--cash-only",
            "--no-matrix-url",
            "--no-google-url",
            "--backend",
            "matrix",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    return " ".join(result.stdout.split())


def test_a_party_cap_that_drops_only_unreadable_fares_names_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(monkeypatch, _solution("s1", "USD103.00", total=None))
    out = _search("--adults", "2", "--max-price", "500")
    assert "No solutions at or under USD 500." in out
    assert "solutions ·" not in out
    assert "Itineraries" not in out


def test_a_cap_that_drops_only_foreign_currency_fares_names_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(monkeypatch, _solution("s1", "GBP90.00", total=None))
    out = _search("--currency", "USD", "--max-price", "500")
    assert "No solutions at or under USD 500." in out
    assert "solutions ·" not in out


def test_a_cap_that_keeps_a_fare_still_prints_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(
        monkeypatch,
        _solution("s1", "USD103.00", total=None),
        _solution("s2", "USD110.00", total="USD220.00"),
    )
    out = _search("--adults", "2", "--max-price", "500")
    assert "No solutions at or under" not in out
    assert "2 solutions" in out
    assert "220.00" in out
