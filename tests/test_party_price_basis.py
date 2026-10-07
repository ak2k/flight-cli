# pyright: reportPrivateUsage=false
"""A party's itinerary price is its total on Matrix as on Google.

Google prices the whole party, and Matrix lists one passenger's price beside
the total it states for the party. Each itinerary table prints the total under
a header naming the party, so the two backends compare directly and
`--max-price` reads the number shown. Matrix's carrier x stops grid and its
cheapest line have no party total, so they say they are per traveler.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

# fli's validator rejects a past travel date, so the date is derived.
_DEP = date.today() + timedelta(days=45)
_LINKLESS = ("--cash-only", "--no-matrix-url", "--no-google-url")


def _body(*, total: str | None) -> dict[str, Any]:
    """One DL1788 solution at USD103 a passenger, `total` for the party."""
    solution: dict[str, Any] = {
        "id": "s1",
        "ext": {"price": "USD103.00"},
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
        "currencyNotice": {"ext": {"price": "USD103.00"}},
        "carrierStopMatrix": {
            "columns": [{"label": {"code": "DL", "shortName": "Delta"}}],
            "rows": [{"label": 0, "cells": [{"minPrice": "USD103.00", "minPriceInGrid": True}]}],
        },
    }


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, *, total: str | None = "USD203.60") -> None:
    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return SearchResult.from_api(_body(total=total))

    monkeypatch.setattr(cli, "_run", _run)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _search(*args: str) -> Result:
    return _invoke("search", "JFK", "LAX", "--dep", _DEP.isoformat(), *_LINKLESS, *args)


def _table(out: str, title: str) -> tuple[str, list[list[str]]]:
    """The price column's header, wrapped lines joined, and the numbered rows'
    cells of the table titled `title`."""
    _, after = out.split(title, 1)
    lines = after.splitlines()
    end = next(i for i, ln in enumerate(lines) if ln.strip().startswith("└"))
    lines = lines[:end]
    header = [
        [c.strip() for c in ln.strip().strip("┃").split("┃")]
        for ln in lines
        if ln.strip().startswith("┃")
    ]
    rows = [
        cells
        for ln in lines
        if ln.strip().startswith("│")
        and (cells := [c.strip() for c in ln.strip().strip("│").split("│")])[0]
        .removeprefix("★")
        .isdigit()
    ]
    return " ".join(" ".join(h[1] for h in header).split()), rows


def _flat(out: str) -> str:
    return " ".join(out.split())


def test_matrix_table_prints_the_party_total(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(monkeypatch)
    result = _search("--backend", "matrix", "--adults", "2")
    assert result.exit_code == 0, result.output
    header, rows = _table(result.stdout, "Itineraries")
    assert rows[0][1] == "203.60"
    assert re.search(r"\b2 travelers\b", header), header


def test_matrixs_grid_and_cheapest_line_say_they_are_per_traveler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(monkeypatch)
    out = _flat(_search("--backend", "matrix", "--adults", "2").stdout)
    assert "cheapest per traveler: 103.00 (USD)" in out
    assert "Carrier x stops grid (USD) per traveler" in out


def test_one_traveler_prints_as_before_on_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(monkeypatch)
    result = _search("--backend", "matrix")
    assert result.exit_code == 0, result.output
    header, rows = _table(result.stdout, "Itineraries")
    assert (header, rows[0][1]) == ("price", "103.00")
    assert "per traveler" not in result.stdout
    assert "1 solutions · cheapest: 103.00 (USD)" in _flat(result.stdout)


def test_a_capped_party_keeps_and_prints_the_same_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap reads the total the row prints: 203.60 is over 200 though the
    listed 103.00 is under it, and under 210."""
    _matrix_answers(monkeypatch)
    kept = _search("--backend", "matrix", "--adults", "2", "--max-price", "210")
    assert kept.exit_code == 0, kept.output
    assert _table(kept.stdout, "Itineraries")[1][0][1] == "203.60"
    cut = _search("--backend", "matrix", "--adults", "2", "--max-price", "200")
    assert cut.exit_code == 0, cut.output
    assert "No solutions at or under" in cut.stdout
    assert "Itineraries" not in cut.stdout


def test_a_party_solution_without_a_total_is_marked_per_traveler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(monkeypatch, total=None)
    result = _search("--backend", "matrix", "--adults", "2")
    assert result.exit_code == 0, result.output
    assert _table(result.stdout, "Itineraries")[1][0][1] == "103.00 per traveler"


def test_a_party_without_a_total_is_cut_by_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap cannot read a party's total Matrix does not state, so the row
    goes, as it does on the cross-check."""
    _matrix_answers(monkeypatch, total=None)
    result = _search("--backend", "matrix", "--adults", "2", "--max-price", "500")
    assert result.exit_code == 0, result.output
    assert _table(result.stdout, "Itineraries")[1] == []


def test_detail_prints_a_partys_total(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(monkeypatch)
    result = _invoke(
        "detail",
        "JFK",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--adults",
        "2",
        "--no-matrix-url",
        "--no-google-url",
    )
    assert result.exit_code == 0, result.output
    header, rows = _table(result.stdout, "Itineraries")
    assert rows[0][1] == "203.60"
    assert re.search(r"\b2 travelers\b", header), header


@pytest.mark.parametrize(("adults", "header"), [("1", "price"), ("2", "total (2 travelers)")])
def test_googles_header_names_the_party(
    gf_session: Callable[..., Any], gf_board: Callable[..., str], adults: str, header: str
) -> None:
    gf_session(gf_board(5))
    result = _invoke(
        "search",
        "HNL",
        "MIA",
        "--dep",
        _DEP.isoformat(),
        *_LINKLESS,
        "--backend",
        "gflight",
        "--fast",
        "--adults",
        adults,
        "-n",
        "3",
    )
    assert result.exit_code == 0, result.output
    shown, rows = _table(result.stdout, "Google Flights")
    assert shown == header
    assert len(rows) == 3
