# pyright: reportPrivateUsage=false
"""A search price Matrix sends as no readable number is the no-price dash in the
`cheapest:` headline, with no currency tag.

`USD1e999` is `float` infinity: `_price_amount` reads it, and `USDabc`, as None.
The calendar headline already follows this rule; the search headline printed the
unreadable text itself, tagged `(USD)`.
"""

from __future__ import annotations

import pytest

from flight_cli import cli
from flight_cli.models import SearchResult


def _result(price: str) -> SearchResult:
    return SearchResult.from_api({"solutionCount": 1, "currencyNotice": {"ext": {"price": price}}})


def _headline(capsys: pytest.CaptureFixture[str], res: SearchResult, passengers: int = 1) -> str:
    cli._render_search(res, passengers=passengers)
    (line,) = (ln for ln in capsys.readouterr().out.split("\n") if "cheapest" in ln)
    return line


@pytest.mark.parametrize("price", ["USD1e999", "USDabc"])
def test_the_headline_prints_the_no_price_dash_for_an_unreadable_price(
    price: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _headline(capsys, _result(price)) == "1 solutions  · cheapest: —"


def test_the_headline_dash_keeps_the_per_traveler_wording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    line = _headline(capsys, _result("USD1e999"), passengers=2)
    assert line == "1 solutions  · cheapest per traveler: —"


def test_the_headline_still_prints_a_readable_price_with_its_currency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _headline(capsys, _result("USD421.00")) == "1 solutions  · cheapest: 421.00 (USD)"


@pytest.mark.parametrize(
    ("price", "value"), [("USD421.00", 421.0), ("USD1e999", None), ("USDabc", None)]
)
def test_cheapest_value_is_the_readable_amount_or_none(price: str, value: float | None) -> None:
    assert _result(price).cheapest_value == value
