# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_verify.py follows; the constructor has no transport
# injection point.
"""`search --verify` for a party: Matrix's price beside Google's is the
party's, as Google's is.

Matrix lists one passenger's price as USD1.00 beside a party total at Google's
price, so no share of the total can pass for the party's price."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from flight_cli import cli
from flight_cli.client import MatrixClient
from test_verify import _as_row, _chain, _details_of, _Matrix, _row_solution, _run, _served

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()

    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: val for k, val in kw.items() if k == "impersonate"},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)
    return fake


def _party(matrix: _Matrix, *, stated: bool = True) -> tuple[int, str]:
    """Matrix answering the AS row at USD1.00 a passenger and, when `stated`,
    a party total at Google's price; the row's number and that price."""
    n, row = _as_row()
    total = f"USD{row.flight.price:.2f}"
    solution = _row_solution("AS-1", total, row)
    solution["ext"]["price"] = "USD1.00"
    if not stated:
        solution.pop("displayTotal")
    matrix.chain = _chain(solution)
    matrix.details = {"AS-1": _details_of(row)}
    return n, total


def test_verify_compares_a_partys_total_with_googles(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, total = _party(matrix)
    gf_session(_served())
    result = _run("-n", "40", "--adults", "2", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    assert f"Matrix {total} · Google {total} · same price" in result.stdout


def test_verify_json_prices_the_party_and_keeps_one_passengers_fare(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, total = _party(matrix)
    gf_session(_served())
    result = _run("-n", "40", "--adults", "2", "--format", "json", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    verdict = json.loads(result.stdout)["verify"]
    assert verdict["matrix"]["price"] == total
    assert verdict["matrix"]["per_traveler"] == "USD1.00"
    assert verdict["matrix"]["total"] == "USD213.20"
    assert verdict["delta"] == 0.0


def test_a_party_without_matrixs_total_shows_one_passengers_fare_ungapped(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, total = _party(matrix, stated=False)
    gf_session(_served(), _served())
    table = _run("-n", "40", "--adults", "2", "--verify", "--pick", str(n))
    assert table.exit_code == 0, table.output
    assert f"Matrix USD1.00 per traveler · Google {total}" in table.stdout
    for gap in ("same price", "cheaper", "dearer"):
        assert gap not in table.stdout
    doc = _run("-n", "40", "--adults", "2", "--format", "json", "--verify", "--pick", str(n))
    assert doc.exit_code == 0, doc.output
    verdict = json.loads(doc.stdout)["verify"]
    assert verdict["matrix"]["price"] is None
    assert verdict["matrix"]["per_traveler"] == "USD1.00"
    assert verdict["delta"] is None
