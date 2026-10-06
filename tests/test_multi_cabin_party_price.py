"""A party's Matrix multi-cabin table prints each cabin's party total.

Google prices the whole party, and Matrix lists one passenger's price beside the
total it states for the party. The multi-cabin table prints the total under a
header naming the party, as the single-cabin table does, so a Matrix price and
a Google price compare directly. A cabin Matrix states no total for prints one
passenger's price, starred and footnoted as per traveler.
"""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli._multi_cabin import merge
from flight_cli.domain import Cabin, Leg, Pax, SearchOptions
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli.models import Itinerary

_DEP = date.today() + timedelta(days=45)
_LINKLESS = ("--cash-only", "--no-matrix-url", "--no-google-url")


def _body(*, price: str, total: str | None) -> dict[str, Any]:
    """One DL1788 solution at `price` a passenger, `total` for the party."""
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
    if total is not None:
        solution["displayTotal"] = total
    return {
        "solutionCount": 1,
        "solutionList": {"solutions": [solution]},
        "currencyNotice": {"ext": {"price": price}},
    }


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, *, economy_total: str | None) -> None:
    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {
            Cabin.COACH: SearchResult.from_api(_body(price="USD103.00", total=economy_total)),
            Cabin.BUSINESS: SearchResult.from_api(_body(price="USD303.00", total="USD603.20")),
        }

    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)


def _search(*extra: str) -> str:
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), *_LINKLESS),
            *("--backend", "matrix", "--cabin", "economy,business", *extra),
        ],
    )
    assert result.exit_code == 0, result.output
    return " ".join(result.stdout.split())


def test_a_partys_multi_cabin_table_prints_each_cabins_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(monkeypatch, economy_total="USD203.60")
    out = _search("--adults", "2")
    assert "203.60" in out
    assert "603.20" in out
    assert "103.00" not in out
    assert "303.00" not in out
    assert "total for 2 travelers" in out
    assert "per traveler" not in out


def test_a_cabin_without_a_total_is_marked_per_traveler(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(monkeypatch, economy_total=None)
    out = _search("--adults", "2")
    assert "103.00*" in out
    assert "603.20" in out
    assert "603.20*" not in out
    assert "* per traveler: Matrix states no total for the party" in out


def test_a_narrow_four_cabin_party_table_keeps_every_digit_and_star(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {
            Cabin.COACH: SearchResult.from_api(_body(price="USD103.00", total="USD206.00")),
            Cabin.PREMIUM_COACH: SearchResult.from_api(_body(price="USD203.00", total="USD406.20")),
            Cabin.BUSINESS: SearchResult.from_api(_body(price="USD6303.20", total="USD12606.40")),
            Cabin.FIRST: SearchResult.from_api(_body(price="USD14703.25", total=None)),
        }

    buf = StringIO()
    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)
    monkeypatch.setattr(cli, "console", Console(file=buf, width=80, no_color=True))
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), *_LINKLESS, "--adults", "2"),
            *("--backend", "matrix", "--cabin", "economy,premium,business,first"),
        ],
    )
    assert result.exit_code == 0, result.output
    # One itinerary, so every body line belongs to it; a cell wrapped onto
    # several lines is rejoined column by column.
    body = [line.split("│") for line in buf.getvalue().splitlines() if line.startswith("│")]
    cells = ["".join(parts[col].strip() for parts in body) for col in range(-5, -1)]
    assert cells == ["206.00", "406.20", "12606.40", "14703.25*"]
    assert "* per traveler: Matrix states no total for the party" in buf.getvalue()


def test_one_traveler_prints_the_listed_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(monkeypatch, economy_total="USD203.60")
    out = _search()
    assert "103.00" in out
    assert "303.00" in out
    assert "*" not in out
    assert "travelers" not in out


def _answer(*listings: tuple[str, str]) -> SearchResult:
    """One solution per (flight, price), each a JFK-LHR flight on one day."""
    solutions = [
        {
            "ext": {"price": price},
            "itinerary": {
                "slices": [
                    {
                        "flights": [flight],
                        "departure": "2026-08-15T09:00",
                        "origin": {"code": "JFK"},
                        "destination": {"code": "LHR"},
                    }
                ],
                "carriers": [],
            },
        }
        for flight, price in listings
    ]
    return SearchResult.from_api(
        {"solutionCount": len(solutions), "solutionList": {"solutions": solutions}}
    )


def test_merge_reads_each_cabins_total_off_the_listing_it_prices() -> None:
    by_cabin = {
        Cabin.COACH: _answer(("AA100", "USD600.00"), ("BA200", "USD500.00")),
        Cabin.BUSINESS: _answer(("AA100", "USD3000.00")),
    }
    known = {"USD600.00": "USD1200.00", "USD500.00": "USD1000.00"}

    def total_of(it: Itinerary) -> str | None:
        return known.get(it.price or "")

    plain = merge(by_cabin, sort_by=Cabin.COACH, top_n=10, currency="USD")
    rows = merge(by_cabin, sort_by=Cabin.COACH, top_n=10, currency="USD", total_of=total_of)
    assert (
        [r.prices for r in rows]
        == [r.prices for r in plain]
        == [
            {Cabin.COACH: "USD500.00"},
            {Cabin.COACH: "USD600.00", Cabin.BUSINESS: "USD3000.00"},
        ]
    )
    assert [r.totals for r in rows] == [
        {Cabin.COACH: "USD1000.00"},
        {Cabin.COACH: "USD1200.00"},
    ]
    assert all(r.totals == {} for r in plain)


@pytest.mark.parametrize("adults", [1, 2])
def test_googles_multi_cabin_table_names_the_party(
    adults: int, gf_rows: Callable[[str], list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = gf_rows("ds1_jfk_lax_3rows.json")

    def _fan_out(**_kw: Any) -> dict[Cabin, list[Any]]:
        return {Cabin.COACH: board, Cabin.BUSINESS: board}

    buf = StringIO()
    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, no_color=True))
    cli._run_gflight_path_multi(  # pyright: ignore[reportPrivateUsage] — the Google path IS the unit
        legs=(Leg.of("JFK", "LAX", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=adults)),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        sort_by=Cabin.COACH,
        top_n=5,
        json_out=False,
        run_pp=False,
        sel=cli._resolve_providers(  # pyright: ignore[reportPrivateUsage] — builds the cash-only selection
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    out = " ".join(buf.getvalue().split())
    assert "Google Flights" in out
    assert ("total for 2 travelers" in out) is (adults == 2)
    assert "*" not in out
