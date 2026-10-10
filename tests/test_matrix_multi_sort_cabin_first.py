"""The itinerary Matrix multi-cabin hands the award matcher is the sort cabin's.

Matrix answers each cabin on its own task, so the fan-out's dict is in the order
the answers arrived. A row several cabins price takes its itinerary from the
first cabin in that dict, and the award matcher writes that itinerary's price as
each match's `cash_price`. The search puts the sort cabin first, as the Google
Flights path does, so `cash_price` is the fare of the cabin the table is sorted
on whichever answer arrived first.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Cabin
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pytest

_DEP = date.today() + timedelta(days=45)


def _answer(price: str) -> SearchResult:
    """One DL1788 solution at `price`, the same flight in every cabin."""
    solution: dict[str, Any] = {
        "id": "s1",
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
    return SearchResult.from_api(
        {
            "solutionCount": 1,
            "solutionList": {"solutions": [solution]},
            "currencyNotice": {"ext": {"price": price}},
        }
    )


def _matched_price(
    monkeypatch: pytest.MonkeyPatch, *, arrived: tuple[Cabin, ...], extra: tuple[str, ...]
) -> str | None:
    """The price on the one itinerary the award matcher is handed, with Matrix's
    answers arriving in the order `arrived`."""
    prices = {Cabin.COACH: "USD103.00", Cabin.BUSINESS: "USD303.00"}
    seen: list[SearchResult] = []

    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {cab: _answer(prices[cab]) for cab in arrived}

    def _capture(sr: SearchResult, **_kw: object) -> None:
        seen.append(sr)

    def _awards_on(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)
    monkeypatch.setattr(cli, "run_pp_for_search", _capture)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--no-matrix-url"),
            *("--no-google-url", "--backend", "matrix", "--cabin", "economy,business", *extra),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert len(seen[0].solutions) == 1
    return seen[0].solutions[0].price


def test_the_matcher_is_handed_the_first_cabins_fare_when_business_answers_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price = _matched_price(monkeypatch, arrived=(Cabin.BUSINESS, Cabin.COACH), extra=())
    assert price == "USD103.00"


def test_the_matcher_is_handed_the_sort_cabins_fare_when_economy_answers_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price = _matched_price(
        monkeypatch,
        arrived=(Cabin.COACH, Cabin.BUSINESS),
        extra=("--sort", "business"),
    )
    assert price == "USD303.00"


def test_the_json_document_keeps_the_cabins_in_the_order_they_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrived = (Cabin.BUSINESS, Cabin.COACH)

    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {cab: _answer("USD103.00") for cab in arrived}

    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cash-only"),
            *("--backend", "matrix", "--cabin", "economy,business", "--format", "json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert list(json.loads(result.stdout)) == [cab.value for cab in arrived]
