# pyright: reportPrivateUsage=false, reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/test_enrich.py.
"""The price insight beside separate-ticket fares the Cheapest tab prices.

`ds1_jfk_lax_tfu.json` answers JFK-LAX at USD204, inside Google's usual
USD85-225. Its Cheapest twin here sells some rows as self transfers, each at
its own price in its own currency.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin, Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter

if TYPE_CHECKING:
    import pytest

_DEP = date.today() + timedelta(days=45)
_LAX = "ds1_jfk_lax_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"

type _Row = gfid.GFlightWithId | tuple[gfid.GFlightWithId, ...]


def _parsed(page: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(page, _URL, 200))


def _cheapest_tab(*marked: tuple[int, int, str]) -> gfid.Board[gfid.GFlightWithId]:
    """The LAX capture with each (index, price, currency) row sold as a self
    transfer at that price in that currency. The prices are distinct."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    rows = gfid._rows_from_ds1(payload).rows
    for index, price, _ in marked:
        rows[index][7] = [1]
        rows[index][1][0][1] = price
    board = _parsed(
        _page(_answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat()))
    )
    currencies = {float(price): currency for _, price, currency in marked}

    def priced(r: gfid.GFlightWithId) -> gfid.GFlightWithId:
        if r.ticketing is None or r.flight.price is None:
            return r
        update = {"currency": currencies[r.flight.price]}
        return replace(r, flight=r.flight.model_copy(update=update))

    return gfid.Board(map(priced, board), insight=board.insight)


def _shown(monkeypatch: pytest.MonkeyPatch, *marked: tuple[int, int, str]) -> gfid.Board[_Row]:
    tab = _cheapest_tab(*marked)

    def laddered(*_: Any, **__: Any) -> gfid.Board[gfid.GFlightWithId]:
        return tab

    monkeypatch.setattr(gfid, "_one_call_laddered", laddered)
    filters = to_fli_filter(
        SpecificDateSearch(
            legs=(Leg.of("JFK", "LAX", _DEP),), options=SearchOptions(cabin=Cabin.COACH)
        )
    )
    parsed = _parsed(_page(_ds1(_LAX)))
    base: gfid.Board[_Row] = gfid.Board(parsed, insight=parsed.insight)
    assert base.insight == gfid.PriceInsight(204.0, 85.0, 225.0, "USD")
    return gfid._with_separate_tickets(
        filters, base, mode="show", transport=gfid.GfTransport(), currency="USD", keep=None
    )


def test_a_fare_in_another_currency_leaves_the_usd_insight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _shown(monkeypatch, (5, 80, "GBP"))
    assert shown.insight is not None
    assert shown.insight.cheapest == 204.0
    assert shown.insight.level == "typical"


def test_a_fare_in_the_insights_currency_still_lowers_its_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _shown(monkeypatch, (5, 80, "USD"))
    assert shown.insight is not None
    assert shown.insight.cheapest == 80.0
    assert shown.insight.level == "low"


def test_only_the_fares_in_the_insights_currency_count_and_every_one_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _shown(monkeypatch, (5, 80, "GBP"), (6, 90, "USD"))
    assert shown.insight is not None
    assert shown.insight.cheapest == 90.0
    added = [r for r in shown if not isinstance(r, tuple) and r.ticketing is not None]
    assert sorted((r.flight.price, r.flight.currency) for r in added) == [
        (80.0, "GBP"),
        (90.0, "USD"),
    ]
