# pyright: reportPrivateUsage=false
"""A Google board that holds fares in two currencies is ranked by value in the
requested currency, never by the bare number: a fare in the currency asked for
comes before any in another, and a board in one currency keeps its order."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import Cabin, Leg

if TYPE_CHECKING:
    from collections.abc import Callable

# fli's validator rejects a past travel date, so this is derived.
_DEP = date.today() + timedelta(days=45)
_Fare = tuple[float | None, str | None]


def _rows(gf_rows: Callable[..., list[Any]], *fares: _Fare) -> list[Any]:
    base = gf_rows("ds1_jfk_lax_3rows.json")
    return [
        gfid.GFlightWithId(
            flight=base[i % len(base)].flight.model_copy(
                update={"price": price, "currency": currency}
            ),
            flight_id=base[i % len(base)].flight_id,
            amenities=base[i % len(base)].amenities,
        )
        for i, (price, currency) in enumerate(fares)
    ]


def _fares(rows: list[Any]) -> list[_Fare]:
    return [(r.flight.price, r.flight.currency) for r in rows]


def test_a_fare_in_the_requested_currency_ranks_before_a_lower_number_in_another(
    gf_rows: Callable[..., list[Any]],
) -> None:
    board = _rows(gf_rows, (1004.0, "GBP"), (1043.0, "USD"))
    assert _fares(cli._price_ordered(board, currency="USD")) == [(1043.0, "USD"), (1004.0, "GBP")]
    assert _fares(cli._price_ordered(board, currency="GBP")) == [(1004.0, "GBP"), (1043.0, "USD")]


def test_unpriced_rows_rank_last_and_an_undecoded_currency_is_the_requested_one(
    gf_rows: Callable[..., list[Any]],
) -> None:
    board = _rows(gf_rows, (None, "USD"), (500.0, None), (400.0, "EUR"), (600.0, "USD"))
    assert _fares(cli._price_ordered(board, currency="USD")) == [
        (500.0, None),
        (600.0, "USD"),
        (400.0, "EUR"),
        (None, "USD"),
    ]


def test_a_board_in_one_currency_keeps_its_order_and_ties_keep_their_arrival(
    gf_rows: Callable[..., list[Any]],
) -> None:
    board = _rows(gf_rows, (300.0, "GBP"), (100.0, "GBP"), (100.0, "GBP"), (None, "GBP"))
    ordered = cli._price_ordered(board, currency="USD")
    assert ordered == [board[1], board[2], board[0], board[3]]
    assert [r is b for r, b in zip(ordered[:2], board[1:3], strict=True)] == [True, True]


def test_a_combination_ranks_on_its_terminal_members_fare(
    gf_rows: Callable[..., list[Any]],
) -> None:
    out, back = (
        _rows(gf_rows, (1.0, "GBP"), (1043.0, "USD")),
        _rows(gf_rows, (2.0, "USD"), (1004.0, "GBP")),
    )
    pairs = [(out[0], back[1]), (out[1], back[0])]
    ordered = cli._price_ordered(pairs, currency="USD")
    assert [_fares([p[-1]])[0] for p in ordered] == [(2.0, "USD"), (1004.0, "GBP")]


def test_the_same_itinerary_on_two_pages_keeps_the_fare_in_the_requested_currency(
    gf_rows: Callable[..., list[Any]],
) -> None:
    gbp, usd = _rows(gf_rows, (1004.0, "GBP")), _rows(gf_rows, (1043.0, "USD"))
    boards = [gfid.Board(gbp), gfid.Board(usd)]
    assert _fares(cli._merged_boards(boards, currency="USD")) == [(1043.0, "USD")]
    assert _fares(cli._merged_boards(boards, currency="GBP")) == [(1004.0, "GBP")]


def test_an_outbound_two_pages_list_is_pinned_on_the_page_with_the_requested_fare(
    gf_rows: Callable[..., list[Any]],
) -> None:
    kept = {0: _rows(gf_rows, (1004.0, "GBP")), 1: _rows(gf_rows, (1043.0, "USD"))}
    assert list(cli._union_pins(kept, 1, currency="USD")) == [1]
    assert list(cli._union_pins(kept, 1, currency="GBP")) == [0]


def test_a_cabins_document_leads_with_the_fare_in_the_requested_currency(
    gf_rows: Callable[..., list[Any]],
) -> None:
    board = _rows(gf_rows, (900.0, "USD"), (1004.0, "GBP"))
    shown = cli._cabin_document_rows(board, [], Cabin.COACH, 1, own=None, currency="GBP")
    assert _fares(shown) == [(1004.0, "GBP")]


def test_the_table_trim_keeps_the_fare_in_the_requested_currency(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=200, no_color=True))
    board = _rows(gf_rows, (900.0, "USD"), (1004.0, "GBP"))
    legs = (Leg.of("JFK", "LAX", _DEP),)
    cli._render_gflight_table(board, legs=legs, top_n=1, currency="GBP")
    assert "GBP1004.00" in buffer.getvalue()
    assert "USD900.00" not in buffer.getvalue()


@pytest.mark.parametrize(("currency", "kept"), [("GBP", (1004.0, "GBP")), ("USD", (900.0, "USD"))])
def test_a_google_search_trims_to_the_fare_in_the_requested_currency(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    currency: str,
    kept: _Fare,
) -> None:
    board = gfid.Board(_rows(gf_rows, (900.0, "USD"), (1004.0, "GBP")))

    def _gf(*_a: Any, **_kw: Any) -> gfid.Board[Any]:
        return board

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--currency",
            currency,
            "-n",
            "1",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [(row["price"], row["currency"]) for row in json.loads(result.stdout)] == [kept]


def test_the_table_lists_a_fare_in_the_requested_currency_first(
    monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    board = gfid.Board(_rows(gf_rows, (900.0, "USD"), (1004.0, "GBP")))

    def _gf(*_a: Any, **_kw: Any) -> gfid.Board[Any]:
        return board

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight"),
            *("--currency", "GBP", "-n", "2", "--fast", "--no-google-url"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.index("GBP1004.00") < result.stdout.index("USD900.00")
