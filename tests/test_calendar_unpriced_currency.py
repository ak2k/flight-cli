# pyright: reportPrivateUsage=false
"""A calendar answer that priced no day carries no currency, whatever its
currency notice says, so a fan-out never leaves it out as off-currency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flight_cli._calendar_split import price_currencies
from flight_cli.models import CalendarResult
from test_calendar_split import _cal, _flat, _pair_client, _priced, _run

if TYPE_CHECKING:
    import pytest


def _unpriced_with_notice() -> CalendarResult:
    return CalendarResult.from_api(
        {"solutionCount": 0, "currencyNotice": {"ext": {"price": "GBP400.00"}}}
    )


def test_an_empty_pair_is_not_left_out_for_its_currency_notice(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """EWR priced nothing, so its GBP notice neither drops it from a USD grid
    nor stands as the merged grid's cheapest."""
    _pair_client(
        monkeypatch,
        {("JFK", "LHR"): _priced("USD600.00"), ("EWR", "LHR"): _unpriced_with_notice()},
    )
    res, n = _run(_cal(["LHR"], origins=("JFK", "EWR"), one_way=True))
    assert n == 2
    assert [d.min_price for d in res.priced_days] == ["USD600.00"]
    assert res.cheapest_price == "USD600.00"
    line = _flat(capsys.readouterr().err)
    assert "came back priced" not in line
    assert "sub-queries failed" not in line


def test_an_unpriced_answer_with_a_currency_notice_has_no_currency() -> None:
    assert price_currencies(_unpriced_with_notice()) == ()
