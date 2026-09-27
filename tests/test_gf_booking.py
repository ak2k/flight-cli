# pyright: reportPrivateUsage=false
"""`--sellers`: the booking options Google's booking page lists for one row.

The envelopes under `fixtures/gf_booking/` are `GetBookingResults` responses the
page received in a real Chrome, trimmed to the fields the parser reads (the
redirect links, price tokens and bag details are dropped). No test here loads a
page: the browser session is replaced wherever the path would reach one.
"""

from __future__ import annotations

import base64
import json
import pathlib
import signal
import urllib.parse
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import _gf_booking as gb
from flight_cli import _gf_browser as gfb
from flight_cli import cli
from flight_cli._gf_browser import CapturedResponse
from flight_cli._gf_errors import GfBrowserUnavailableError
from flight_cli._gf_rpc_shared import GfPageRpcError, refuse_a_wall, url_currency
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
    price: int | None,
    *,
    fare: str | None = None,
    airline: bool = False,
    flights: list[list[str]] | None = None,
) -> list[Any]:
    """One seller in the captured option shape: `[1][0]` the seller, `[3]` the
    flights, `[7][0][1]` the price, `[21][3]` the fare name."""
    return [
        0,
        [[name.upper(), name, None, airline]],
        None,
        flights if flights is not None else [["B6", "1523"]],
        None,
        None,
        None,
        None if price is None else [[None, price], None],
        *([None] * 13),
        [None, None, None, fare],
    ]


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
        {"seller": "JetBlue", "price": 179, "currency": "USD", "fare": "Blue", "airline": True}
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
    _serve(
        monkeypatch,
        _booking_body(_option("[bold]Evil\x1b[2J[/x]", 179, fare="[red]Fare", flights=_B6_1523)),
    )
    result = _run("--fast", "--sellers", "--no-matrix-url", "--no-google-url")
    assert result.exit_code == 0, result.output
    assert "[bold]Evil" in result.stdout
    assert "\x1b" not in result.stdout
    assert "[red]Fare" in result.stdout


# ───────────────────────────── the enriched path ─────────────────────────────


def _matrix_answers(solutions: list[dict[str, Any]]) -> Any:
    async def _answer(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = SearchResult.model_validate(
            {"solutions": solutions, "solutionCount": len(solutions)}
        )

    return _answer


def _matrix_solution(flight: str, price: str) -> dict[str, Any]:
    day = (_DEP).isoformat()
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


def test_a_round_trip_opens_the_booking_page_for_both_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A round-trip row is an outbound and a return; the page is asked for the
    pair and must list sellers of the pair, not of the outbound alone."""
    outbound, returning, _ = _gf_rows()

    def _pairs(*_a: object, **_kw: object) -> list[Any]:
        return [(outbound, returning)]

    monkeypatch.setattr(cli, "_gflight_results", _pairs)
    fake = _serve(
        monkeypatch,
        _booking_body(
            _option("JetBlue", 350, airline=True, flights=[["B6", "1523"], ["B6", "123"]])
        ),
    )
    result = CliRunner().invoke(
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
    assert result.exit_code == 0, result.output
    assert "USD350.00" in result.stdout.split("Booking options for #1", 1)[1]
    raw = base64.urlsafe_b64decode(
        urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)["tfs"][0] + "=="
    )
    assert b"1523" in raw
    assert b"123" in raw.split(b"1523", 1)[1]
