# pyright: reportPrivateUsage=false
"""A fare priced at a non-finite number is no fare: `--max-price` never admits
one, and the calendar's price graph never reads one as a cell."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from flight_cli import _gf_calgraph as cg
from flight_cli import cli
from flight_cli.domain import within_price_cap
from flight_cli.models import SearchResult
from test_cap_and_bags import _gbp_body, _matrix_cli
from test_gf_calgraph import _body, _cells

_CAP = 690
_NON_FINITE = [float("-inf"), float("inf"), float("nan")]


@pytest.mark.parametrize("amount", _NON_FINITE, ids=["-inf", "inf", "nan"])
def test_a_non_finite_amount_is_within_no_cap(amount: float) -> None:
    assert not within_price_cap(amount, "USD", cap=500, cap_currency="USD")
    assert within_price_cap(499.0, "USD", cap=500, cap_currency="USD")


@pytest.mark.parametrize("token", ["-inf", "inf", "nan"])
def test_a_capped_matrix_answer_drops_a_fare_priced_at_a_non_finite_number(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    body = _gbp_body()
    solutions = body["solutionList"]["solutions"]
    solutions[0]["ext"]["price"] = f"GBP{token}"

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return SearchResult.from_api(body)

    monkeypatch.setattr(cli, "_run", _run)
    result = _matrix_cli("--max-price", str(_CAP), "--format", "json")
    assert result.exit_code == 0, result.output
    prices = [s["ext"]["price"] for s in json.loads(result.stdout)["solutionList"]["solutions"]]
    assert prices
    assert f"GBP{token}" not in prices


@pytest.mark.parametrize("price", [*_NON_FINITE, 10**400], ids=["-inf", "inf", "nan", "huge"])
def test_the_price_graph_reads_no_cell_priced_at_a_non_finite_number(price: float) -> None:
    first = date.today() + timedelta(days=60)
    cells = _cells(first, 2)
    cells[0][2][0][1] = price
    page = cg.parse_graph(_body(cells), trip_length=None)
    assert [c.departure for c in page.cells] == [first + timedelta(days=1)]
    assert page.last == first + timedelta(days=1)
