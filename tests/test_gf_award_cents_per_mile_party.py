# pyright: reportPrivateUsage=false
"""The award table's cents per mile weighs one traveler's miles against one
traveler's cash. Google prices the whole party, so a Google row hands the
award matcher its total divided by the party for an adults-only party, and no
cash at all for a party with a child or an infant, whose share of the total
Google does not give.

Google is faked below `search_with_ids`, at `_one_call_laddered`, with the
JFK-LAX capture; the award overlay is stubbed at `run_pp_for_search`. No test
reaches Google, Matrix, PointsPath or Chrome."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin, Leg, Pax, SearchOptions

if TYPE_CHECKING:
    from flight_cli.models import SearchResult

_LAX = "ds1_jfk_lax_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"


def _dep() -> date:
    return date.today() + timedelta(days=45)


def _google(
    _filters: Any, _transport: Any, *, currency: str = "USD", cheapest: bool = False
) -> gfid.Board[gfid.GFlightWithId]:
    _ = currency, cheapest
    html = _page(_answering(_ds1(_LAX), origin=None, destination=None, date=_dep().isoformat()))
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


def _overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[SearchResult, dict[int, dict[str, float]]]]:
    """Fake Google and the award overlay; each search appends what the overlay
    is handed."""
    seen: list[tuple[SearchResult, dict[int, dict[str, float]]]] = []

    def _awards(res: SearchResult, **kw: Any) -> None:
        seen.append((res, kw["cash_per_cabin"]))

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(gfid, "_one_call_laddered", _google)
    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    return seen


def _one_cabin_cash(
    monkeypatch: pytest.MonkeyPatch, pax: Pax
) -> tuple[SearchResult, dict[int, dict[str, float]]]:
    """The result and the cash map a one-cabin Google search hands the award
    overlay."""
    seen = _overlay(monkeypatch)
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LAX", _dep()),),
        opts=SearchOptions(cabin=Cabin.COACH, pax=pax),
        top_n=5,
        json_out=False,
        run_pp=True,
        sel=cli._resolve_providers(
            providers=None, cash_only=False, awards_only=False, provider_opt=()
        ),
    )
    ((res, cash),) = seen
    assert res.solutions
    return res, cash


def _awards_cash(
    monkeypatch: pytest.MonkeyPatch, cabin: str, *party: str
) -> tuple[SearchResult, dict[int, dict[str, float]]]:
    """The result and the cash map one multi-cabin search hands the award
    overlay."""
    seen = _overlay(monkeypatch)
    args = [
        "search",
        "--no-google-url",
        "--no-matrix-url",
        "JFK",
        "LAX",
        "--dep",
        _dep().isoformat(),
        "--cabin",
        cabin,
        "--backend",
        "gflight",
        "-n",
        "5",
        *party,
    ]
    result = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})
    assert result.exit_code == 0, result.output
    ((res, cash),) = seen
    assert res.solutions
    return res, cash


def test_a_two_adult_google_search_values_awards_on_one_travelers_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alone_res, alone = _one_cabin_cash(monkeypatch, Pax())
    pair_res, pair = _one_cabin_cash(monkeypatch, Pax(adults=2))
    one = [alone[id(it)] for it in alone_res.solutions if id(it) in alone]
    assert one
    halved = [{k: v / 2 for k, v in m.items()} for m in one]
    assert [pair[id(it)] for it in pair_res.solutions if id(it) in pair] == halved


def test_a_two_adult_google_multi_cabin_search_values_awards_on_one_travelers_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alone_res, alone = _awards_cash(monkeypatch, "economy,business")
    pair_res, pair = _awards_cash(monkeypatch, "economy,business", "--adults", "2")
    one = [alone[id(it)] for it in alone_res.solutions if id(it) in alone]
    assert one
    # The fake prices the same board for any party, so a pair's per-traveler
    # cash is half of what one traveler's search is handed.
    halved = [{k: v / 2 for k, v in m.items()} for m in one]
    assert [pair[id(it)] for it in pair_res.solutions if id(it) in pair] == halved


def test_a_google_party_with_a_child_gets_no_award_cents_per_mile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, single = _one_cabin_cash(monkeypatch, Pax(adults=1, children=1))
    _, multi = _awards_cash(monkeypatch, "economy,business", "--children", "1")
    assert single == {}
    assert multi == {}


@pytest.mark.parametrize(
    ("price", "party", "cash"),
    [
        ("$100", None, 100.0),
        ("$100", Pax(), 100.0),
        ("USD 100", Pax(adults=2), 50.0),
        ("$100", Pax(adults=0), None),
        ("$100", Pax(adults=2, seniors=1), None),
        ("$100", Pax(adults=2, youth=1), None),
        ("$100", Pax(adults=2, infants_in_lap=1), None),
        ("GBP 100", Pax(adults=2), None),
        (None, Pax(adults=2), None),
    ],
)
def test_one_travelers_cash_is_a_pairs_total_split_only_for_adults(
    price: str | None, party: Pax | None, cash: float | None
) -> None:
    assert cli._traveler_cash(price, party) == cash
