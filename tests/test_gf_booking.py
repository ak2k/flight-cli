# pyright: reportPrivateUsage=false
"""`--sellers`: the booking options Google's booking page lists for one row.

The envelopes under `fixtures/gf_booking/` are `GetBookingResults` responses the
page received in a real Chrome, trimmed to the fields the parser reads, price
tokens dropped. The two `*_full` ones keep each seller's redirect link (`[5]`)
and bag fees (`[18]`), Google's signed `u` token replaced by `TOKEN-<the
option's index>`; the others drop both. No test here loads a page: the browser
session is replaced wherever the path would reach one.
"""

from __future__ import annotations

import base64
import json
import pathlib
import signal
import urllib.parse
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import (  # pyright: ignore[reportMissingTypeStubs] — fli ships no stubs
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
)
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gf_booking as gb
from flight_cli import _gf_browser as gfb
from flight_cli import cli
from flight_cli._gf_browser import CapturedResponse
from flight_cli._gf_errors import GfBrowserUnavailableError
from flight_cli._gf_rpc_shared import GfPageRpcError, refuse_a_wall, url_currency
from flight_cli._gflight_ids import GFlightWithId
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.links import google_flights_booking_url, google_flights_pinned_url
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PROBE_DATE = "2026-11-04"
_RPC_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetBookingResults?f.sid=1&rt=c"
)
# fli's validator rejects a past travel date, so this is derived.
_DEP = date.today() + timedelta(days=45)


def _fixture(name: str) -> str:
    return (FIXTURES / "gf_booking" / name).read_text()


# ───────────────────────────── the parser ────────────────────────────────────


def test_a_one_way_lists_every_fare_family_cheapest_first() -> None:
    sellers = gb.parse_sellers(_fixture("ow_jfk_lax_dl1788.body"), flights=[("DL", "1788")])
    assert [(s.name, s.price, s.fare) for s in sellers] == [
        ("Delta", 204, "Delta Main Basic"),
        ("Delta", 269, "Delta Main Classic"),
        ("Delta", 324, "Delta Comfort Basic"),
        ("Delta", 364, "Delta Main Extra"),
        ("Delta", 389, "Delta Comfort Classic"),
    ]
    assert all(s.airline for s in sellers)


def test_agencies_and_codeshare_sellers_are_listed_with_no_fare_name() -> None:
    """35 sellers on BA178, four of them selling it under their own numbers.
    A missing fare name is a blank, not a failure: agencies never carry one,
    and on this route neither do the airlines."""
    sellers = gb.parse_sellers(_fixture("ow_jfk_lhr_ba178.body"), flights=[("BA", "178")])
    assert len(sellers) == 35
    assert (sellers[0].name, sellers[0].price, sellers[0].airline) == ("Booking.com", 282, False)
    assert {s.name for s in sellers if s.airline} >= {"British Airways", "American", "Iberia"}
    assert all(s.fare is None for s in sellers)
    prices = [s.price for s in sellers]
    assert prices == sorted(p for p in prices if p is not None)


def test_a_round_trip_is_matched_on_both_legs() -> None:
    sellers = gb.parse_sellers(
        _fixture("rt_jfk_lhr_ba178_ba115.body"), flights=[("BA", "178"), ("BA", "115")]
    )
    assert len(sellers) == 14
    assert (sellers[0].name, sellers[0].price) == ("American", 817)


def test_a_flight_number_matches_however_it_is_zero_padded() -> None:
    sellers = gb.parse_sellers(_fixture("ow_jfk_lax_dl1788.body"), flights=[("DL", "01788")])
    assert len(sellers) == 5


@pytest.mark.parametrize(
    "flights",
    [
        pytest.param([("DL", "1789")], id="another-flight"),
        pytest.param([("BA", "178")], id="only-the-outbound-of-a-round-trip"),
    ],
)
def test_a_page_answering_for_other_flights_is_refused(flights: list[tuple[str, str]]) -> None:
    body = (
        _fixture("rt_jfk_lhr_ba178_ba115.body")
        if flights == [("BA", "178")]
        else _fixture("ow_jfk_lax_dl1788.body")
    )
    with pytest.raises(GfPageRpcError, match="other flights"):
        gb.parse_sellers(body, flights=flights)


def _option(
    name: str,
    price: float | None,
    *,
    fare: str | None = None,
    airline: bool = False,
    flights: list[list[str]] | None = None,
    five: Any = None,
    bags: Any = None,
) -> list[Any]:
    """One seller in the captured option shape: `[1][0]` the seller, `[3]` the
    flights, `[5]` the link, `[7][0][1]` the price, `[18]` the bags, `[21][3]`
    the fare name."""
    return [
        0,
        [[name.upper(), name, None, airline]],
        None,
        flights if flights is not None else [["B6", "1523"]],
        None,
        five,
        None,
        None if price is None else [[None, price], None],
        *([None] * 10),
        bags,
        None,
        None,
        [None, None, None, fare],
    ]


def _five(base: Any, pairs: Any) -> list[Any]:
    """`option[5]` as captured: the display domain, then the redirect's base
    URL and the pairs the page posts to it."""
    return ["www.example.com/...", None, [base, pairs]]


def _booking_body(*options: list[Any]) -> str:
    """A `GetBookingResults` answer in the captured envelope's shape: an
    itinerary chunk, then the option list."""
    head = json.dumps([["wrb.fr", None, json.dumps([None, [None] * 23])]])
    listed = json.dumps([["wrb.fr", None, json.dumps([None, [list(options)]])]])
    return f")]}}'\n\n{len(head) + 1}\n{head}\n{len(listed) + 1}\n{listed}\n"


def test_no_sellers_is_a_refusal_not_an_empty_list() -> None:
    with pytest.raises(GfPageRpcError, match="no sellers"):
        gb.parse_sellers(_booking_body(), flights=[("B6", "1523")])


def test_sellers_with_no_price_at_all_are_a_refusal() -> None:
    body = _booking_body(_option("JetBlue", None, airline=True))
    with pytest.raises(GfPageRpcError, match="priced none"):
        gb.parse_sellers(body, flights=[("B6", "1523")])


def test_an_unpriced_seller_goes_last_rather_than_being_dropped() -> None:
    body = _booking_body(_option("Kiwi", None), _option("JetBlue", 179, airline=True))
    sellers = gb.parse_sellers(body, flights=[("B6", "1523")])
    assert [(s.name, s.price) for s in sellers] == [("JetBlue", 179), ("Kiwi", None)]


def test_the_error_13_body_is_a_typed_refusal() -> None:
    body = (FIXTURES / "gf_rpc" / "error13.body").read_text()
    with pytest.raises(GfPageRpcError) as caught:
        gb.parse_sellers(body, flights=[("DL", "1788")])
    assert caught.value.code == 13


# ───────────────────────────── links and bags ────────────────────────────────

_CLK = "https://www.google.com/travel/clk/f"


def test_each_fare_carries_its_own_link_and_bag_fees() -> None:
    """AA171's four fares as Google's page states them: Main Plus alone has
    its first checked bag free."""
    sellers = gb.parse_sellers(_fixture("ow_jfk_lax_aa171_full.body"), flights=[("AA", "171")])
    free_carry_on = gb.BagFee("carry-on", 1, 0)
    assert [(s.fare, s.link, s.bags) for s in sellers] == [
        (
            "Basic Economy",
            f"{_CLK}?u=TOKEN-0",
            (gb.BagFee("checked", 1, 55), gb.BagFee("checked", 2, 65), free_carry_on),
        ),
        (
            "Main Cabin",
            f"{_CLK}?u=TOKEN-1",
            (gb.BagFee("checked", 1, 50), gb.BagFee("checked", 2, 60), free_carry_on),
        ),
        (
            "Main Plus",
            f"{_CLK}?u=TOKEN-2",
            (gb.BagFee("checked", 1, 0), gb.BagFee("checked", 2, 60), free_carry_on),
        ),
        (
            "Main Select",
            f"{_CLK}?u=TOKEN-3",
            (gb.BagFee("checked", 1, 50), gb.BagFee("checked", 2, 60), free_carry_on),
        ),
    ]


def test_a_bag_code_that_is_neither_a_fee_nor_free_adds_nothing() -> None:
    """BA178's agencies: CheapOair's `[[3], [0], [3]]` says nothing of the
    second bag, and Booking.com's `[null, null, [3]]` speaks of the carry-on
    alone."""
    sellers = gb.parse_sellers(_fixture("ow_jfk_lhr_ba178_full.body"), flights=[("BA", "178")])
    by_name = {s.name: s for s in sellers}
    assert by_name["CheapOair"].bags == (gb.BagFee("checked", 1, 0), gb.BagFee("carry-on", 1, 0))
    assert by_name["Globehunters"].bags == (gb.BagFee("checked", 1, 0), gb.BagFee("carry-on", 1, 0))
    assert by_name["Booking.com"].bags == (gb.BagFee("carry-on", 1, 0),)
    assert by_name["Justfly.com"].bags[:2] == (
        gb.BagFee("checked", 1, 90),
        gb.BagFee("checked", 2, 110),
    )
    links = [s.link or "" for s in sellers]
    assert len(set(links)) == len(sellers) == 35
    assert all(link.startswith(f"{_CLK}?u=TOKEN-") for link in links)


@pytest.mark.parametrize(
    ("name", "flights"),
    [
        pytest.param("ow_jfk_lax_dl1788.body", [("DL", "1788")], id="dl1788"),
        pytest.param("ow_jfk_lhr_ba178.body", [("BA", "178")], id="ba178"),
        pytest.param("rt_jfk_lhr_ba178_ba115.body", [("BA", "178"), ("BA", "115")], id="rt"),
    ],
)
def test_an_entry_with_no_link_or_bags_gives_none(
    name: str, flights: list[tuple[str, str]]
) -> None:
    sellers = gb.parse_sellers(_fixture(name), flights=flights)
    assert {(s.link, s.bags) for s in sellers} == {(None, ())}


@pytest.mark.parametrize(
    ("five", "link"),
    [
        pytest.param(_five(_CLK, [["u", "T-1"]]), f"{_CLK}?u=T-1", id="captured-shape"),
        pytest.param(
            _five("https://g.example/c", [["u", "a b"], ["v", "x&y=z"]]),
            "https://g.example/c?u=a+b&v=x%26y%3Dz",
            id="pairs-become-the-query",
        ),
        pytest.param(_five("https://g.example/c", []), "https://g.example/c", id="no-pairs"),
        pytest.param(
            ["d", None, ["https://g.example/c"]], "https://g.example/c", id="pairs-absent"
        ),
        pytest.param(_five("http://g.example/c", [["u", "T"]]), None, id="http"),
        pytest.param(_five("javascript:alert(1)", [["u", "T"]]), None, id="javascript"),
        pytest.param(_five("https://g.example/\x1b[2J", [["u", "T"]]), None, id="esc"),
        pytest.param(_five("https://g.example/a b", [["u", "T"]]), None, id="space"),
        pytest.param(_five("https://g.example/c?a=1", [["u", "T"]]), None, id="base-query"),
        pytest.param(_five("https://g.example/c#top", [["u", "T"]]), None, id="base-fragment"),
        pytest.param(_five("https:///c", [["u", "T"]]), None, id="no-host"),
        pytest.param(_five("https://[red]/x", [["u", "T"]]), None, id="bracketed-name"),
        pytest.param(_five("https://[::1/x", [["u", "T"]]), None, id="unclosed-bracket"),
        pytest.param(_five(5, [["u", "T"]]), None, id="base-not-a-string"),
        pytest.param(_five("https://g.example/c", [["u", 5]]), None, id="value-not-a-string"),
        pytest.param(_five("https://g.example/c", [["u", "\ud800"]]), None, id="lone-surrogate"),
        pytest.param(_five("https://g.example/c", [["u", "T", "x"]]), None, id="three-items"),
        pytest.param(_five("https://g.example/c", ["u=T"]), None, id="pair-not-a-list"),
        pytest.param(_five("https://g.example/c", "u=T"), None, id="pairs-not-a-list"),
        pytest.param(None, None, id="null"),
    ],
)
def test_a_link_is_read_only_from_a_well_formed_https_entry(five: Any, link: str | None) -> None:
    body = _booking_body(_option("Kiwi.com", 179, fare="Basic", five=five))
    (seller,) = gb.parse_sellers(body, flights=[("B6", "1523")])
    assert seller.link == link
    assert (seller.name, seller.price, seller.fare) == ("Kiwi.com", 179, "Basic")


@pytest.mark.parametrize(
    ("bags", "fees"),
    [
        pytest.param(
            [[2, [[None, 45]], 1], [2, [[None, 55.5]], 1], [3]],
            (("checked", 1, 45), ("checked", 2, 55.5), ("carry-on", 1, 0)),
            id="fees-and-free",
        ),
        pytest.param([[3]], (("checked", 1, 0),), id="first-slot-only"),
        pytest.param([[0], [1], [7]], (), id="unknown-codes"),
        pytest.param([None, None, None], (), id="nulls"),
        pytest.param([[2, [[None, 0]], 1]], (), id="zero-fee"),
        pytest.param([[2, [[None, -5]], 1]], (), id="negative-fee"),
        pytest.param([[2, [[None, True]], 1]], (), id="bool-fee"),
        pytest.param([[2, [[None, "45"]], 1]], (), id="string-fee"),
        pytest.param([[2, [[None, float("inf")]], 1]], (), id="infinite-fee"),
        pytest.param([[2, [[None, 10**400]], 1]], (), id="fee-past-a-float"),
        pytest.param([[2]], (), id="fee-missing"),
        pytest.param([3, "3", {"0": 2}], (), id="slots-not-lists"),
        pytest.param("3", (), id="not-a-list"),
        pytest.param(None, (), id="null"),
    ],
)
def test_a_bag_fee_is_read_only_from_a_known_code(
    bags: Any, fees: tuple[tuple[str, int, float], ...]
) -> None:
    body = _booking_body(_option("Kiwi.com", 179, fare="Basic", bags=bags))
    (seller,) = gb.parse_sellers(body, flights=[("B6", "1523")])
    assert [tuple(b) for b in seller.bags] == list(fees)
    assert (seller.name, seller.price, seller.fare) == ("Kiwi.com", 179, "Basic")


def test_a_price_that_is_not_finite_is_no_price() -> None:
    """`json.loads` reads `Infinity`; written back out it is no JSON at all."""
    body = _booking_body(_option("Inf", float("inf")), _option("JetBlue", 179, airline=True))
    sellers = gb.parse_sellers(body, flights=[("B6", "1523")])
    assert [(s.name, s.price) for s in sellers] == [("JetBlue", 179), ("Inf", None)]
    doc = gb.document(gb.BookingOptions("USD", sellers))
    assert json.loads(json.dumps(doc, allow_nan=False))[1]["price"] is None


def test_the_document_gives_each_seller_its_link_and_bags() -> None:
    sellers = gb.parse_sellers(_fixture("ow_jfk_lax_aa171_full.body"), flights=[("AA", "171")])
    doc = gb.document(gb.BookingOptions("USD", sellers))
    assert doc[2] == {
        "seller": "American",
        "price": 415,
        "currency": "USD",
        "fare": "Main Plus",
        "airline": True,
        "booking_url": f"{_CLK}?u=TOKEN-2",
        "bags": [
            {"bag": "checked", "nth": 1, "fee": 0, "currency": "USD"},
            {"bag": "checked", "nth": 2, "fee": 60, "currency": "USD"},
            {"bag": "carry-on", "nth": 1, "fee": 0, "currency": "USD"},
        ],
    }


def test_the_currency_is_the_one_the_url_asks_for() -> None:
    url = "https://www.google.com/travel/flights/booking?tfs=x&curr=EUR"
    assert url_currency(url) == "EUR"


# ───────────────────────────── the URL ───────────────────────────────────────


# The `tfs=` of booking URLs Google Flights' own page wrote (B1, B3) and of URLs
# built by the probe and served by the page (B4a-c).
_PROBE_BOOKING_TFS = {
    ("B1", 0): (
        "CBwQAhpAEgoyMDI2LTExLTA0IiAKA0pGSxIKMjAyNi0xMS0wNBoDTEFYKgJETDIEMTc4OGoHCAES"
        "A0pGS3IHCAESA0xBWEABSAFwAYIBCwj___________8BmAEC"
    ),
    ("B4", 0): (
        "CBwQAhpAEgoyMDI2LTExLTA0IiAKA0pGSxIKMjAyNi0xMS0wNBoDTEFYKgJCNjIEMTAyM2oHCAES"
        "A0pGS3IHCAESA0xBWEABSAFwAYIBCwj___________8BmAEC"
    ),
    ("B4", 2): (
        "CBwQAhpgEgoyMDI2LTExLTA0Ih8KA0pGSxIKMjAyNi0xMS0wNBoDU0ZPKgJBUzIDMjI3Ih8KA1NG"
        "TxIKMjAyNi0xMS0wNBoDTEFYKgJBUzIDNTk1agcIARIDSkZLcgcIARIDTEFYQAFIAXABggELCP__"
        "_________wGYAQI"
    ),
    ("B3", 1): (
        "CBwQAho_EgoyMDI2LTExLTA0Ih8KA0pGSxIKMjAyNi0xMS0wNBoDTEhSKgJCQTIDMTc4agcIARID"
        "SkZLcgcIARIDTEhSGj8SCjIwMjYtMTEtMTEiHwoDTEhSEgoyMDI2LTExLTExGgNKRksqAkJBMgMx"
        "MTVqBwgBEgNMSFJyBwgBEgNKRktAAUgBcAGCAQsI____________AZgBAQ"
    ),
}


def _seg(o: str, d: str, carrier: str, flight: str, day: str = PROBE_DATE) -> dict[str, str]:
    return {"origin": o, "date": day, "destination": d, "carrier": carrier, "flight": flight}


def _search(o: str, d: str, *, ret: str | None = None) -> SpecificDateSearch:
    legs = (Leg.of((o,), (d,), date.fromisoformat(PROBE_DATE)),)
    if ret is not None:
        legs += (Leg.of((d,), (o,), date.fromisoformat(ret)),)
    return SpecificDateSearch(legs=legs, options=SearchOptions())


@pytest.mark.parametrize(
    ("key", "search", "outbound", "returning"),
    [
        pytest.param(
            ("B1", 0),
            _search("JFK", "LAX"),
            [_seg("JFK", "LAX", "DL", "1788")],
            None,
            id="B1-DL1788",
        ),
        pytest.param(
            ("B4", 0),
            _search("JFK", "LAX"),
            [_seg("JFK", "LAX", "B6", "1023")],
            None,
            id="B4a-B6-1023",
        ),
        pytest.param(
            ("B4", 2),
            _search("JFK", "LAX"),
            [_seg("JFK", "SFO", "AS", "227"), _seg("SFO", "LAX", "AS", "595")],
            None,
            id="B4c-connection",
        ),
        pytest.param(
            ("B3", 1),
            _search("JFK", "LHR", ret="2026-11-11"),
            [_seg("JFK", "LHR", "BA", "178")],
            [_seg("LHR", "JFK", "BA", "115", "2026-11-11")],
            id="B3-round-trip",
        ),
    ],
)
def test_the_booking_url_carries_the_tfs_googles_own_page_writes(
    key: tuple[str, int],
    search: SpecificDateSearch,
    outbound: list[dict[str, str]],
    returning: list[dict[str, str]] | None,
) -> None:
    url = google_flights_booking_url(search, outbound_segments=outbound, return_segments=returning)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert parts.path == "/travel/flights/booking"
    assert query["tfs"] == [_PROBE_BOOKING_TFS[key]]
    assert query["gl"] == ["US"]
    assert query["curr"] == ["USD"]
    # The same itinerary the pinned link opens, on the other page.
    pinned = google_flights_pinned_url(
        search, outbound_segments=outbound, return_segments=returning
    )
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(pinned).query)["tfs"] == query["tfs"]


# ───────────────────────────── the Chrome step ───────────────────────────────


class _FakeSession:
    """Answers each capture with the next body, recording what was asked and
    the guard and scope the caller held."""

    def __init__(self, answers: list[tuple[int, str] | BaseException]) -> None:
        self._answers = answers
        self.urls: list[str] = []
        self.checks: list[object] = []
        self.held: list[tuple[int, object]] = []
        self.finished = False

    def capture(
        self,
        url: str,
        wanted: Callable[[str], bool],
        *,
        click: object = None,
        check_page: Callable[..., object] | None = None,
    ) -> CapturedResponse:
        assert click is None
        assert wanted(_RPC_URL)
        assert not wanted(_RPC_URL.replace("GetBookingResults", "GetShoppingResults"))
        self.urls.append(url)
        self.checks.append(check_page)
        self.held.append((getattr(gfb._scope_depth, "n", 0), signal.getsignal(signal.SIGINT)))
        answer = self._answers[len(self.urls) - 1]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return CapturedResponse(url=_RPC_URL, status=status, body=body)


def _serve(
    monkeypatch: pytest.MonkeyPatch, *answers: str | tuple[int, str] | BaseException
) -> _FakeSession:
    fake = _FakeSession([a if not isinstance(a, str) else (200, a) for a in answers])

    def _session(*, headed: bool) -> _FakeSession:
        del headed
        return fake

    monkeypatch.setattr(gfb, "session", _session)
    return fake


def _no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*, headed: bool) -> object:
        raise AssertionError("this run must not open Chrome")

    monkeypatch.setattr(gfb, "session", _forbidden)


def test_booking_options_open_the_url_and_read_the_pages_own_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _serve(monkeypatch, _fixture("ow_jfk_lax_dl1788.body"))
    url = "https://www.google.com/travel/flights/booking?tfs=abc&hl=en&gl=US&curr=USD"
    options = gb.booking_options(url, flights=[("DL", "1788")], headed=False)
    assert fake.urls == [url]
    assert fake.checks == [refuse_a_wall]
    assert options.currency == "USD"
    assert options.sellers[0].price == 204


def test_a_non_2xx_rpc_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, (500, "oops"))
    with pytest.raises(GfPageRpcError, match="HTTP 500"):
        gb.booking_options("https://x/booking?curr=USD", flights=[("DL", "1")], headed=False)


# ───────────────────────────── the command ───────────────────────────────────


def _gf_rows() -> list[Any]:
    """Real rows parsed from the committed capture: B6 1523, B6 123, B6 523,
    each USD179.00."""
    from flight_cli import _gflight_ids as gfid

    capture = FIXTURES / "gflight_page" / "ds1_jfk_lax_3rows.json"
    rows = gfid._rows_from_ds1(json.loads(capture.read_text())).rows
    return [gfid._parse_flight_with_id(r) for r in rows]


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The Google half answers with three rows, and awards stay off."""
    rows = _gf_rows()

    def _gf(*_a: object, **_kw: object) -> list[Any]:
        return list(rows)

    monkeypatch.setattr(cli, "_gflight_results", _gf)

    def _awards(sel: cli.ProviderSelection) -> bool:
        return not sel.cash_only

    monkeypatch.setattr(cli, "_should_run_awards", _awards)
    return rows


def _run(*extra: str) -> Any:
    return CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "-n", "3", "--cash-only", *extra],
    )


_B6_1523 = [["B6", "1523"]]


def test_fast_sellers_print_a_block_under_the_table(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("JetBlue", 179, fare="Blue Basic", airline=True, flights=_B6_1523),
            _option("Kiwi.com", 170, flights=_B6_1523),
        ),
    )
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.index("Google Flights") < out.index("Booking options for #1")
    block = out.split("Booking options for #1", 1)[1]
    assert block.index("Kiwi.com") < block.index("JetBlue")
    assert "USD170.00" in block
    assert "Blue Basic" in block
    assert "Kiwi.com at USD170.00 beats the table price, USD179.00." in block
    # Row 1's legs, on the booking page, in the table's currency.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
    assert urllib.parse.urlsplit(fake.urls[0]).path == "/travel/flights/booking"
    assert query["curr"] == ["USD"]
    raw = base64.urlsafe_b64decode(query["tfs"][0] + "==")
    assert b"B6" in raw
    assert b"1523" in raw


def test_the_pick_names_the_row_opened(monkeypatch: pytest.MonkeyPatch, board: list[Any]) -> None:
    """Row 2 of the table is B6 123; the page must be asked for that one."""
    fake = _serve(
        monkeypatch, _booking_body(_option("JetBlue", 179, airline=True, flights=[["B6", "123"]]))
    )
    result = _run("--fast", "--sellers", "--pick", "2", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "Booking options for #2" in result.stdout
    assert len(fake.urls) == 1


def test_a_seller_at_the_table_price_does_not_beat_it(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    _serve(monkeypatch, _booking_body(_option("JetBlue", 179, airline=True, flights=_B6_1523)))
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "beats" not in result.stdout


def _wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """A console wide enough that no seven-column table folds a cell, and
    still narrower than any link."""
    monkeypatch.setattr(cli, "console", Console(width=250, no_color=True))


def _cells(line: str, bar: str) -> list[str]:
    return [c.strip() for c in line.strip().strip(bar).split(bar)]


# Wider than any console, as each of Google's tokens is.
_TOKEN = "T" * 3000


def test_fast_sellers_print_each_sellers_bags_and_whole_link(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    _wide(monkeypatch)
    body = _booking_body(
        _option(
            "JetBlue",
            179,
            fare="Blue Basic",
            airline=True,
            flights=_B6_1523,
            five=_five(_CLK, [["u", f"J{_TOKEN}"]]),
            bags=[[2, [[None, 35]], 1], [2, [[None, 45]], 1], [3]],
        ),
        _option(
            "Kiwi.com",
            170,
            flights=_B6_1523,
            five=_five(_CLK, [["u", f"K{_TOKEN}"]]),
            bags=[None, None, [3]],
        ),
        _option("Agency", 185, flights=_B6_1523),
    )
    kiwi, jetblue, _ = gb.parse_sellers(body, flights=[("B6", "1523")])
    _serve(monkeypatch, body)
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    block = result.stdout.split("Booking options for #1", 1)[1]
    lines = block.splitlines()
    header = next(line for line in lines if "seller" in line)
    assert _cells(header, "┃") == [
        "#",
        "seller",
        "price",
        "fare",
        "carry-on",
        "1st checked",
        "2nd checked",
    ]
    rows = [_cells(line, "│") for line in lines if line.startswith("│")]
    assert rows == [
        ["1", "Kiwi.com", "USD170.00", "", "free", "", ""],
        ["2", "JetBlue", "USD179.00", "Blue Basic", "free", "USD35.00", "USD45.00"],
        ["3", "Agency", "USD185.00", "", "", "", ""],
    ]
    assert "Each link goes through Google to that seller's own page for this fare." in block
    assert "whole trip" not in block
    assert [line for line in lines if "https://" in line] == [
        f"1 Kiwi.com {kiwi.link}",
        f"2 JetBlue {jetblue.link}",
    ]
    # The verdict is the last thing said.
    assert lines[-1] == "Kiwi.com at USD170.00 beats the table price, USD179.00."


def test_sellers_with_no_link_print_no_link_lines(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    _serve(
        monkeypatch,
        _booking_body(_option("JetBlue", 179, airline=True, flights=_B6_1523, bags=[[3]])),
    )
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    block = result.stdout.split("Booking options for #1", 1)[1]
    assert "free" in block
    assert "link" not in block
    assert "https://" not in block


def _priced_in(monkeypatch: pytest.MonkeyPatch, rows: list[Any], currency: str) -> None:
    """Google answers with `rows` priced in `currency`, whatever was asked of it."""
    relabeled = [
        GFlightWithId(
            flight=r.flight.model_copy(update={"currency": currency}),
            flight_id=r.flight_id,
            amenities=r.amenities,
        )
        for r in rows
    ]

    def _gf(*_a: object, **_kw: object) -> list[Any]:
        return list(relabeled)

    monkeypatch.setattr(cli, "_gflight_results", _gf)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--fast"], id="fast"),
        pytest.param(["--fast", "--format", "json"], id="fast-json"),
        pytest.param([], id="enriched-matrix-silent"),
    ],
)
def test_the_booking_page_is_asked_in_the_currency_google_priced_the_row_in(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], args: list[str]
) -> None:
    """Google priced the rows in GBP under `--currency EUR`, and the sellers
    are compared with the row, so they are asked for in GBP."""
    _priced_in(monkeypatch, board, "GBP")
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    fake = _serve(monkeypatch, _booking_body(_option("Kiwi.com", 170, flights=_B6_1523)))
    result = _run(*args, "--sellers", "--currency", "EUR", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
    assert query["curr"] == ["GBP"]
    if "json" in args:
        assert json.loads(result.stdout)["booking_options"][0]["currency"] == "GBP"
    else:
        assert "Kiwi.com at GBP170.00 beats the table price, GBP179.00." in result.stdout


def _opts(*prices: int | None, currency: str = "USD") -> Any:
    return gb.BookingOptions(
        currency, tuple(gb.Seller(f"s{i}", p, None, False) for i, p in enumerate(prices))
    )


@pytest.mark.parametrize(
    ("sellers", "table", "beaten"),
    [
        pytest.param(_opts(282), ["USD295.00"], 295.0, id="lower"),
        pytest.param(_opts(295), ["USD295.00"], None, id="equal"),
        pytest.param(_opts(294), ["USD294.49"], None, id="whole-unit-could-be-above"),
        pytest.param(_opts(294), ["USD294.50"], 294.5, id="whole-unit-surely-below"),
        pytest.param(_opts(282), ["EUR295.00"], None, id="other-currency"),
        pytest.param(
            _opts(200, currency="EUR"),
            ["USD179.00", "EUR900.00"],
            None,
            id="below-the-row-price-in-its-currency-only",
        ),
        pytest.param(
            _opts(170), ["USD179.00", "EUR100.00"], None, id="row-price-in-another-currency"
        ),
        pytest.param(_opts(282), ["USDn/a", "USD300.00"], None, id="row-price-that-does-not-parse"),
        pytest.param(_opts(282), ["USD290.00", "USD280.00"], None, id="lower-of-two-row-prices"),
        pytest.param(_opts(282), [None, "USD300.00"], 300.0, id="one-side-unpriced"),
        pytest.param(_opts(282), [None], None, id="no-table-price"),
    ],
)
def test_the_table_price_is_beaten_only_in_the_same_currency_and_surely_below(
    sellers: Any, table: list[str | None], beaten: float | None
) -> None:
    assert cli._undercut(sellers, table) == beaten


def test_json_wraps_the_unchanged_search_document(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    plain = _run("--fast", "--format", "json")
    assert plain.exit_code == 0, plain.output
    _serve(
        monkeypatch,
        _booking_body(_option("JetBlue", 179, fare="Blue", airline=True, flights=_B6_1523)),
    )
    wrapped = _run("--fast", "--format", "json", "--sellers")
    assert wrapped.exit_code == 0, wrapped.output
    doc = json.loads(wrapped.stdout)
    assert set(doc) == {"search", "booking_options"}
    assert doc["search"] == json.loads(plain.stdout)
    assert doc["booking_options"] == [
        {
            "seller": "JetBlue",
            "price": 179,
            "currency": "USD",
            "fare": "Blue",
            "airline": True,
            "booking_url": None,
            "bags": [],
        }
    ]


def test_without_the_flag_json_is_the_bare_list(board: list[Any]) -> None:
    result = _run("--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), list)


def test_the_booking_page_runs_guarded_and_scoped(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """Ctrl-C reaches Chrome and one scope closes it, as on search's fast arm."""
    fake = _serve(
        monkeypatch, _booking_body(_option("JetBlue", 179, airline=True, flights=_B6_1523))
    )
    closed: list[bool] = []
    monkeypatch.setattr(gfb, "close_thread_session", lambda: closed.append(True))
    before = signal.getsignal(signal.SIGINT)
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    depth, handler = fake.held[0]
    assert depth == 1
    assert handler is not before
    assert closed == [True]
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.parametrize(
    ("args", "said"),
    [
        pytest.param(["--fast", "--cabin", "y,j"], "--cabin", id="multi-cabin"),
        pytest.param(["--backend", "matrix"], "runs on Matrix", id="matrix"),
        pytest.param(["--fast", "--pick", "4"], "--pick 4", id="pick-past-n"),
        pytest.param(["--fast", "--pick", "0"], "--pick 0", id="pick-zero"),
    ],
)
def test_a_search_that_cannot_open_a_row_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, args: list[str], said: str
) -> None:
    _no_chrome(monkeypatch)

    def _no_search(*_a: object, **_kw: object) -> object:
        raise AssertionError("refused before any request")

    for name in ("_gflight_results", "_run", "_run_gflight_multi", "_run_matrix_multi"):
        monkeypatch.setattr(cli, name, _no_search)
    result = _run("--sellers", *args)
    assert result.exit_code == 2, result.output
    assert said in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("args", "said"),
    [
        pytest.param(["--awards-only"], "--awards-only", id="awards-only"),
        pytest.param(["--format", "json"], "--cash-only", id="awards-json"),
    ],
)
def test_award_runs_that_leave_no_room_for_sellers_are_refused(
    monkeypatch: pytest.MonkeyPatch, args: list[str], said: str
) -> None:
    _no_chrome(monkeypatch)

    def _awards_on(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--fast", "--sellers", *args],
    )
    assert result.exit_code == 2, result.output
    assert said in result.stderr


def test_a_pick_past_a_short_board_exits_2_without_opening_chrome(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """`-n 10` admits `--pick 5`, and the board has three rows. No fallback to
    row one: the block would be headed with a number it does not describe."""
    _no_chrome(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--cash-only",
            "--fast",
            "--sellers",
            "--pick",
            "5",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--pick 5 is out of range (1-3)" in result.stderr
    assert result.stdout == ""


def test_an_empty_board_has_no_row_to_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_chrome(monkeypatch)

    def _empty(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _empty)
    result = _run("--fast", "--sellers", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "no itinerary to open" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_error_13_fails_the_command_and_is_not_called_a_throttle(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], fmt: str
) -> None:
    _serve(monkeypatch, (FIXTURES / "gf_rpc" / "error13.body").read_text())
    result = _run("--fast", "--sellers", "--format", fmt, "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 1, result.output
    assert "No booking options for #1" in result.stderr
    assert "error 13" in result.stderr
    assert "rate" not in result.stderr.lower()
    assert "Booking options" not in result.stdout
    if fmt == "json":
        assert result.stdout == ""


def test_a_missing_chrome_keeps_its_remedy_and_drops_the_transports_that_cannot_help(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    missing = GfBrowserUnavailableError(
        "Chrome failed to launch for Google Flights: no such file.",
        remedy="Install Chrome (`x`). Retry, or use `--gf-transport http` (or `--backend matrix`).",
    )
    _serve(monkeypatch, missing)
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 1, result.output
    assert "Chrome failed to launch" in result.stderr
    assert "Install Chrome" in result.stderr
    assert "--gf-transport" not in result.stderr
    assert "--backend matrix" not in result.stderr


def test_remote_seller_text_is_escaped(monkeypatch: pytest.MonkeyPatch, board: list[Any]) -> None:
    _wide(monkeypatch)
    _serve(
        monkeypatch,
        _booking_body(
            _option(
                "[bold]Evil\x1b[2J[/x]",
                179,
                fare="[red]Fare",
                flights=_B6_1523,
                five=_five("https://g.example/[red]/:smile:/c", [["u", "[bold]T"]]),
            )
        ),
    )
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "[bold]Evil" in result.stdout
    assert "\x1b" not in result.stdout
    assert "[red]Fare" in result.stdout
    assert (
        "1 [bold]Evil[2J[/x] https://g.example/[red]/:smile:/c?u=%5Bbold%5DT"
        in result.stdout.splitlines()
    )


# ───────────────────────────── the enriched path ─────────────────────────────


def _matrix_answers(solutions: list[dict[str, Any]]) -> Any:
    async def _answer(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = SearchResult.model_validate(
            {"solutions": solutions, "solutionCount": len(solutions)}
        )

    return _answer


def _matrix_solution(flight: str, price: str, flies: date = _DEP) -> dict[str, Any]:
    day = flies.isoformat()
    return {
        "displayTotal": price,
        "itinerary": {
            "slices": [
                {
                    "flights": [flight],
                    "departure": f"{day}T08:00",
                    "arrival": f"{day}T11:00",
                    "origin": {"code": "JFK"},
                    "destination": {"code": "LAX"},
                }
            ]
        },
    }


def test_enriched_sellers_open_the_merged_tables_row(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """A cheaper Matrix-only itinerary sorts first in the merged table, so its
    #2 is Google's #1 (B6 1523) — the row the Google link pins."""
    monkeypatch.setattr(
        cli, "_matrix_into", _matrix_answers([_matrix_solution("DL100", "USD150.00")])
    )
    fake = _serve(
        monkeypatch, _booking_body(_option("JetBlue", 179, airline=True, flights=_B6_1523))
    )
    result = _run("--sellers", "--pick", "2", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "Google Flights + Matrix" in result.stdout
    assert "Booking options for #2" in result.stdout
    assert len(fake.urls) == 1


def _merged_with(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], flight: str, price: str
) -> None:
    """Google answers with B6 1523 alone at USD179.00, and Matrix with `flight`
    at `price` on the day B6 1523 flies: B6 1523 is one merged row showing both
    prices, and any other flight a row of its own."""
    _priced_in(monkeypatch, board[:1], "USD")
    flies = board[0].flight.legs[0].departure_datetime.date()
    monkeypatch.setattr(
        cli, "_matrix_into", _matrix_answers([_matrix_solution(flight, price, flies)])
    )


@pytest.mark.parametrize(
    ("flight", "sold", "asked"),
    [
        pytest.param("B61523", _B6_1523, "USD", id="google-priced-the-row"),
        pytest.param("DL100", [["DL", "100"]], "EUR", id="google-did-not-show-the-row"),
    ],
)
def test_enriched_sellers_are_asked_in_the_currency_of_the_rows_google_price(
    monkeypatch: pytest.MonkeyPatch,
    board: list[Any],
    flight: str,
    sold: list[list[str]],
    asked: str,
) -> None:
    """Under `--currency EUR`, Google priced B6 1523 in USD and Matrix priced
    row 1 in EUR. The page is asked in the currency of the row's Google price,
    and for a row Google did not show, in the requested one."""
    _merged_with(monkeypatch, board, flight, "EUR150.00")
    fake = _serve(monkeypatch, _booking_body(_option("Kiwi.com", 170, flights=sold)))
    result = _run("--sellers", "--currency", "EUR", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "Booking options for #1" in result.stdout
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
    assert query["curr"] == [asked]


@pytest.mark.parametrize(
    ("matrix_price", "seller"),
    [
        pytest.param("EUR900.00", 200, id="between-the-two-prices"),
        pytest.param("EUR100.00", 170, id="below-the-google-price-only"),
    ],
)
def test_no_seller_beats_a_row_priced_in_two_currencies(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], matrix_price: str, seller: int
) -> None:
    """The merged row shows Google's USD179.00 beside Matrix's price in EUR
    under `--currency EUR`. A seller is compared with the row in one currency
    only, so none is said to beat it, and the sellers still print."""
    _merged_with(monkeypatch, board, "B61523", matrix_price)
    _serve(monkeypatch, _booking_body(_option("Kiwi.com", seller, flights=_B6_1523)))
    result = _run("--sellers", "--currency", "EUR", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "Kiwi.com" in result.stdout.split("Booking options for #1", 1)[1]
    assert "beats" not in result.stdout


def _matrix_connection(arrival: str) -> dict[str, Any]:
    """AA100 JFK-ORD then AA200 ORD-LAX, leaving in the evening. Matrix dates
    the slice's two ends and neither flight."""
    return {
        "displayTotal": "USD150.00",
        "itinerary": {
            "slices": [
                {
                    "flights": ["AA100", "AA200"],
                    "departure": f"{_DEP.isoformat()}T17:00",
                    "arrival": arrival,
                    "origin": {"code": "JFK"},
                    "destination": {"code": "LAX"},
                    "stops": [{"code": "ORD"}],
                }
            ]
        },
    }


@pytest.mark.parametrize(
    "arrival",
    [
        pytest.param(f"{(_DEP + timedelta(days=1)).isoformat()}T00:30", id="lands-next-day"),
        # The same two stamps a connection crossing the date line carries: it
        # lands on the day it left while its second flight leaves the next day.
        pytest.param(f"{_DEP.isoformat()}T22:30", id="lands-the-day-it-leaves"),
    ],
)
def test_enriched_sellers_open_a_matrix_connection_only_when_its_flights_dates_are_known(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], arrival: str
) -> None:
    """Matrix dates a connection's two ends and none of its flights, and the
    ends do not date the flights between them, whatever day it lands. A
    booking page asked for the wrong day prices another trip under this row's
    number."""
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers([_matrix_connection(arrival)]))
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("American", 150, airline=True, flights=[["AA", "100"], ["AA", "200"]])
        ),
    )
    result = _run("--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 1, result.output
    assert "No booking options for #1" in result.stderr
    assert "--fast" in result.stderr
    assert fake.urls == []


def _booking_tfs(url: str) -> bytes:
    return base64.urlsafe_b64decode(
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0] + "=="
    )


def test_enriched_sellers_open_a_matrix_nonstop_on_the_day_it_leaves(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """A nonstop's one flight leaves on the slice's departure day, which Matrix
    states, also when it lands the next day."""
    day, next_day = _DEP.isoformat(), (_DEP + timedelta(days=1)).isoformat()
    red_eye = _matrix_solution("DL100", "USD150.00")
    red_eye["itinerary"]["slices"][0].update(departure=f"{day}T23:00", arrival=f"{next_day}T07:30")
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers([red_eye]))
    fake = _serve(
        monkeypatch, _booking_body(_option("Delta", 150, airline=True, flights=[["DL", "100"]]))
    )
    result = _run("--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "Booking options for #1" in result.stdout
    raw = _booking_tfs(fake.urls[0])
    # Once for the searched slice, once for the flight.
    assert raw.count(day.encode()) == 2
    assert next_day.encode() not in raw


def _nz_over_the_date_line(day: date) -> GFlightWithId:
    """NZ104 Sydney-Auckland, then NZ10 Auckland-Honolulu, which leaves after
    midnight in Auckland and lands in Honolulu on the calendar day the trip
    began. Local times, as Google gives them."""
    next_day = day + timedelta(days=1)

    def leg(
        number: str, frm: str, to: str, leaves: datetime, lands: datetime, minutes: int
    ) -> FlightLeg:
        return FlightLeg(
            airline=Airline["NZ"],
            flight_number=number,
            departure_airport=Airport[frm],
            arrival_airport=Airport[to],
            departure_datetime=leaves,
            arrival_datetime=lands,
            duration=minutes,
        )

    flight = FlightResult(
        price=900.0,
        currency="USD",
        duration=780,
        stops=1,
        legs=[
            leg(
                "104",
                "SYD",
                "AKL",
                datetime.combine(day, time(18, 0)),
                datetime.combine(day, time(23, 0)),
                180,
            ),
            leg(
                "10",
                "AKL",
                "HNL",
                datetime.combine(next_day, time(0, 30)),
                datetime.combine(day, time(10, 0)),
                510,
            ),
        ],
    )
    return GFlightWithId(flight=flight, flight_id="", amenities=[])


def test_fast_sellers_open_a_google_connection_on_the_days_google_gives_its_flights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slice leaves and lands on the same day, and its second flight is
    asked for on the next, the day Google gives it."""
    row = _nz_over_the_date_line(_DEP)

    def _one_row(*_a: object, **_kw: object) -> list[Any]:
        return [row]

    monkeypatch.setattr(cli, "_gflight_results", _one_row)
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("Air New Zealand", 900, airline=True, flights=[["NZ", "104"], ["NZ", "10"]])
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "SYD",
            "HNL",
            "--dep",
            _DEP.isoformat(),
            "--cash-only",
            "--fast",
            "--sellers",
            "--no-matrix-url",
            "--no-google-url",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Booking options for #1" in result.stdout
    raw = _booking_tfs(fake.urls[0])
    # The searched slice and NZ104 on the day the trip began, NZ10 on the next.
    assert raw.count(_DEP.isoformat().encode()) == 2
    assert (_DEP + timedelta(days=1)).isoformat().encode() in raw.split(b"104", 1)[1]


def test_enriched_sellers_open_a_matrix_connection_on_the_days_its_google_match_gives(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """Matrix lists the same two NZ flights as Google, dating only the slice's
    ends. The merged row takes Google's date for each flight, so it is opened,
    with NZ10 asked for on the day after the trip began."""
    row = _nz_over_the_date_line(_DEP)

    def _one_row(*_a: object, **_kw: object) -> list[Any]:
        return [row]

    monkeypatch.setattr(cli, "_gflight_results", _one_row)
    day = _DEP.isoformat()
    connection = {
        "displayTotal": "USD880.00",
        "itinerary": {
            "slices": [
                {
                    "flights": ["NZ104", "NZ10"],
                    "departure": f"{day}T18:00+11:00",
                    "arrival": f"{day}T10:00-10:00",
                    "origin": {"code": "SYD"},
                    "destination": {"code": "HNL"},
                    "stops": [{"code": "AKL"}],
                }
            ]
        },
    }
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers([connection]))
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("Air New Zealand", 880, airline=True, flights=[["NZ", "104"], ["NZ", "10"]])
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "SYD",
            "HNL",
            "--dep",
            day,
            "--cash-only",
            "--sellers",
            "--no-matrix-url",
            "--no-google-url",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Google Flights + Matrix" in result.stdout
    assert "Booking options for #1" in result.stdout
    raw = _booking_tfs(fake.urls[0])
    assert raw.count(day.encode()) == 2
    assert (_DEP + timedelta(days=1)).isoformat().encode() in raw.split(b"104", 1)[1]


async def _no_matrix(*_a: object, **_kw: object) -> None:
    return None


def test_enriched_sellers_with_no_matrix_answer_open_the_google_tables_row(
    monkeypatch: pytest.MonkeyPatch, board: list[Any]
) -> None:
    """With Matrix silent, the Google table painted first is the only numbered
    table on screen, so `--pick 2` names its row 2 (B6 123)."""
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    fake = _serve(
        monkeypatch, _booking_body(_option("JetBlue", 170, airline=True, flights=[["B6", "123"]]))
    )
    result = _run("--sellers", "--pick", "2", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.index("Google Flights · JFK→LAX") < out.index("Booking options for #2")
    assert "JetBlue at USD170.00 beats the table price, USD179.00." in out
    raw = base64.urlsafe_b64decode(
        urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)["tfs"][0] + "=="
    )
    assert b"123" in raw
    assert b"1523" not in raw


def test_enriched_sellers_with_no_table_at_all_fail_rather_than_go_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _empty(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _empty)
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    _no_chrome(monkeypatch)
    result = _run("--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 1, result.output
    assert "No booking options" in result.stderr
    assert result.stdout == ""


def _round_trip_sellers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`--sellers` on a round trip whose row 1 is B6 1523 out, B6 123 back."""
    outbound, returning, _ = _gf_rows()

    def _pairs(*_a: object, **_kw: object) -> list[Any]:
        return [(outbound, returning)]

    monkeypatch.setattr(cli, "_gflight_results", _pairs)
    return CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--return",
            (_DEP + timedelta(days=7)).isoformat(),
            "--cash-only",
            "--fast",
            "--sellers",
            "--no-matrix-url",
            "--no-google-url",
        ],
    )


def test_a_round_trip_opens_the_booking_page_for_both_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A round-trip row is an outbound and a return; the page is asked for the
    pair and must list sellers of the pair, not of the outbound alone."""
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("JetBlue", 350, airline=True, flights=[["B6", "1523"], ["B6", "123"]])
        ),
    )
    result = _round_trip_sellers(monkeypatch)
    assert result.exit_code == 0, result.output
    block = result.stdout.split("Booking options for #1", 1)[1]
    assert "USD350.00" in block
    assert "whole trip" not in block
    raw = base64.urlsafe_b64decode(
        urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)["tfs"][0] + "=="
    )
    assert b"1523" in raw
    assert b"123" in raw.split(b"1523", 1)[1]


@pytest.mark.parametrize(
    ("five", "caption"),
    [
        pytest.param(
            _five(_CLK, [["u", "T"]]),
            "Each link goes through Google to that seller's own page for this fare; "
            "bag fees cover the whole trip.",
            id="with-links",
        ),
        pytest.param(None, "Bag fees cover the whole trip.", id="no-links"),
    ],
)
def test_a_round_trips_bag_fees_are_said_to_cover_the_whole_trip(
    monkeypatch: pytest.MonkeyPatch, five: Any, caption: str
) -> None:
    """AA's fees on BA178 are 85/100 one way and 170/200 round trip."""
    _wide(monkeypatch)
    _serve(
        monkeypatch,
        _booking_body(
            _option(
                "American",
                817,
                airline=True,
                flights=[["B6", "1523"], ["B6", "123"]],
                five=five,
                bags=[[2, [[None, 170]], 1], [2, [[None, 200]], 1], [3]],
            )
        ),
    )
    result = _round_trip_sellers(monkeypatch)
    assert result.exit_code == 0, result.output
    block = result.stdout.split("Booking options for #1", 1)[1]
    assert "USD170.00" in block
    assert caption in block.splitlines()


# ───────────────────────────── under a price cap ─────────────────────────────

_CAP = "190"

# Each path that opens a booking page: Google's own table, its document, the
# table painted while Matrix stays silent, and the merged table.
_CAPPED_PATHS = [
    pytest.param(["--fast"], None, id="fast"),
    pytest.param(["--fast", "--format", "json"], None, id="fast-json"),
    pytest.param([], _no_matrix, id="enriched-matrix-silent"),
    pytest.param([], "USD175.00", id="enriched-merged"),
]


def _capped_run(
    monkeypatch: pytest.MonkeyPatch,
    board: list[Any],
    args: list[str],
    matrix: object,
    currency: str = "USD",
) -> Any:
    """Row 1 is B6 1523, which Google priced at 179 in `currency`; `matrix` is
    what Matrix answers, or on the merged table B6 1523's Matrix price."""
    if isinstance(matrix, str):
        _merged_with(monkeypatch, board, "B61523", matrix)
        board = board[:1]
    elif matrix is not None:
        monkeypatch.setattr(cli, "_matrix_into", matrix)
    _priced_in(monkeypatch, board, currency)
    return _run(*args, "--sellers", "--max-price", _CAP, "--no-matrix-url", "--no-google-url")


@pytest.mark.parametrize(("args", "matrix"), _CAPPED_PATHS)
def test_under_a_cap_no_booking_offer_over_it_is_printed_or_listed(
    monkeypatch: pytest.MonkeyPatch, board: list[Any], args: list[str], matrix: object
) -> None:
    """The row is under the cap and one of its sellers is not; an unpriced
    seller cannot be shown to be under it, so it goes as well."""
    _serve(
        monkeypatch,
        _booking_body(
            _option("Agency", 200, flights=_B6_1523),
            _option("JetBlue", 179, fare="Blue", airline=True, flights=_B6_1523),
            _option("Kiwi.com", 170, flights=_B6_1523),
            _option("Unpriced", None, flights=_B6_1523),
        ),
    )
    result = _capped_run(monkeypatch, board, args, matrix)
    assert result.exit_code == 0, result.output
    if "json" in args:
        offers = json.loads(result.stdout)["booking_options"]
        assert [(o["seller"], o["price"]) for o in offers] == [("Kiwi.com", 170), ("JetBlue", 179)]
        return
    block = result.stdout.split("Booking options for #1", 1)[1]
    assert "Kiwi.com" in block
    assert "JetBlue" in block
    assert "Agency" not in block
    assert "USD200.00" not in block
    assert "Unpriced" not in block


@pytest.mark.parametrize(("args", "matrix"), _CAPPED_PATHS)
@pytest.mark.parametrize(
    ("currency", "prices"),
    [
        pytest.param("USD", (200, 210), id="over"),
        pytest.param("GBP", (170, 179), id="other-currency"),
    ],
)
def test_under_a_cap_with_no_booking_offer_under_it_one_line_says_so(
    monkeypatch: pytest.MonkeyPatch,
    board: list[Any],
    args: list[str],
    matrix: object,
    currency: str,
    prices: tuple[int, int],
) -> None:
    """Google priced the row in `currency`, so its booking page answers in it:
    GBP offers are not under a USD cap, whatever their amount."""
    fake = _serve(
        monkeypatch,
        _booking_body(*(_option(f"s{p}", p, flights=_B6_1523) for p in prices)),
    )
    result = _capped_run(monkeypatch, board, args, matrix, currency)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
    assert query["curr"] == [currency]
    assert result.exit_code == 1, result.output
    said = [line for line in result.stderr.splitlines() if "booking" in line.lower()]
    assert said == [f"No booking options for #1: no booking offer is at or under USD {_CAP}."]
    assert "Booking options" not in result.stdout
    if "json" in args:
        assert result.stdout == ""
