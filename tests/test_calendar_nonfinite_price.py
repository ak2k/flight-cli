# pyright: reportPrivateUsage=false
"""A calendar day Matrix prices at a number that is not finite reads as no price.

`USD1e999` is `float` infinity: the table printed its cell as `1e999` and the
envelope read the row as 1.0 (the first run of digits), the cheapest of the grid.
The fixture is the live JFK-LAX answer `test_calendar_formats` serves.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from conftest import LITERAL_DATES_NOW
from flight_cli.models import CalendarDay
from test_calendar_formats import _BODY, _FORMATS, _ROUTE, _envelope_of, _run, _serve

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_NON_FINITE = "USD1e999"
_STUB = ["--gf-transport", "http"]


def _first_priced_day_non_finite() -> tuple[dict[str, Any], int]:
    """The fixture with its first enabled priced day set to `_NON_FINITE`, and
    that day's date."""
    body = copy.deepcopy(_BODY)
    for week in body["calendar"]["months"][0]["weeks"]:
        for day in week["days"]:
            if day.get("minPrice") and not day.get("disabled"):
                day["minPrice"] = _NON_FINITE
                return body, day["date"]
    raise AssertionError("the fixture has no priced day")


@pytest.mark.parametrize(
    ("price", "value"),
    [(_NON_FINITE, None), ("USD-1e999", None), ("USD595.00", 595.0), ("USD1_000", 1000.0)],
)
def test_a_day_priced_at_a_non_finite_number_has_no_price_value(
    price: str, value: float | None
) -> None:
    assert CalendarDay.model_validate({"date": 1, "minPrice": price}).price_value == value


def test_the_table_prints_the_no_price_cell_for_a_non_finite_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, date_of_day = _first_priced_day_non_finite()
    _serve(monkeypatch, body)
    result = _run(*_ROUTE, *_STUB)
    assert result.exit_code == 0, result.output
    assert "1e999" not in result.stdout
    assert "overall cheapest: 407.00 (USD)" in result.stdout
    rows = [
        [cell.strip() for cell in line.strip("│").split("│")]
        for line in result.stdout.split("\n")
        if line.startswith("│")
    ]
    (row,) = (cells for cells in rows if cells[0] == str(date_of_day))
    assert row[1] == "—"
    assert row[2] == "467.00"


def test_the_envelope_prices_a_non_finite_day_at_none_and_keeps_the_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, date_of_day = _first_priced_day_non_finite()
    _serve(monkeypatch, body)
    result = _run(*_ROUTE, *_STUB, *_FORMATS["envelope"])
    assert result.exit_code == 0, result.output
    rows = _envelope_of(result)["results"]
    (day,) = (r for r in rows if r["departure"] == f"2026-10-{date_of_day:02d}")
    assert day["price"] is None
    assert min(r["price"] for r in rows if r["price"] is not None) == 407.0


def test_json_still_prints_matrixs_string_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    body, _ = _first_priced_day_non_finite()
    _serve(monkeypatch, body)
    result = _run(*_ROUTE, *_STUB, *_FORMATS["json"])
    assert result.exit_code == 0, result.output
    assert f'"minPrice": "{_NON_FINITE}"' in result.stdout
