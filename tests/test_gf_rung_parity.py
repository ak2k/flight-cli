# pyright: reportPrivateUsage=false
"""Both Google rungs read one board for one query.

For a multi-airport search Google serves a different board depending on the
User-Agent's token: under `HeadlessChrome` the 300 cheapest rows across every
airport pair, under `Chrome` a curated ~75. Rung 2's headless Chrome sends the
token, so rung 1 sends it too.

The `rung_parity/` fixtures are the gzip'd `ds:1` of live pages, one search
each read three ways: curl_cffi's own UA ("curated"), curl_cffi with the token
("token"), and rung 2's Chrome ("chrome"). NYC-LON 2026-11-04 is the
multi-airport query; BOS-LHR, EWR-LGW and JFK-LAX are single airport pairs,
whose board is the same under either UA.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import (
    FIXTURE_DIR,
    GFLIGHT_PAGE_DIR,
    _answering,
    _FakeRateLimiter,
    _FakeResponse,
    _NullCookies,
    _page,
    _unreadable,
)
from flight_cli import _gf_browser, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfPageShapeError
from flight_cli.domain import Leg, SearchOptions
from flight_cli.models import SearchResult
from test_gf_full_board import _DEP, _RET, _SEARCH, _URL, _no_matrix, _one_way_filters

if TYPE_CHECKING:
    from collections.abc import Callable

_DIR = GFLIGHT_PAGE_DIR / "rung_parity"
_MATRIX = FIXTURE_DIR / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
_HEADLESS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36"
)
_FLI_HEADERS = {"content-type": "application/x-www-form-urlencoded;charset=UTF-8"}
# What a token read is sometimes served live, on either rung: a page with no
# `ds:1` on it, where the next read of the same URL carries the board.
_NO_BOARD = "<!doctype html><html><body></body></html>"


def _ds1(name: str) -> str:
    return gzip.decompress((_DIR / f"{name}.json.gz").read_bytes()).decode()


def _html(name: str) -> str:
    return _page(_ds1(name))


def _board(name: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_html(name), _URL, 200))


def _shape(board: list[gfid.GFlightWithId]) -> tuple[int, float]:
    """A board's row count and its cheapest fare."""
    return len(board), min(r.flight.price for r in board if r.flight.price is not None)


class _UaSession:
    """fli's per-thread curl_cffi session, serving the board Google serves the
    User-Agent a request carries. The first `boardless` token reads are served a
    page with no board on it."""

    def __init__(self, *, curated: str, headless: str, boardless: int = 0) -> None:
        self._curated, self._headless, self._boardless = curated, headless, boardless
        self.headers = dict(_FLI_HEADERS)
        self.cookies = _NullCookies()
        self.uas: list[str] = []

    def get(self, _url: str, **kw: Any) -> _FakeResponse:
        sent: dict[str, str] = {**self.headers, **(kw.get("headers") or {})}
        ua = next((v for k, v in sent.items() if k.lower() == "user-agent"), "")
        self.uas.append(ua)
        if "HeadlessChrome/" not in ua:
            return _FakeResponse(text=self._curated)
        if self._boardless:
            self._boardless -= 1
            return _FakeResponse(text=_NO_BOARD)
        return _FakeResponse(text=self._headless)


class _UaClient:
    def __init__(self, session: _UaSession) -> None:
        self._rate_limiter = _FakeRateLimiter()
        self._session_obj = session

    def _session(self) -> _UaSession:
        return self._session_obj


class _Chrome:
    """Rung 2's session, serving what its navigations read in turn, the last
    page again once the others are spent."""

    def __init__(self, *pages: str) -> None:
        self._pages = list(pages)
        self.navigations = 0

    def get_html(self, _url: str) -> PageFetch:
        self.navigations += 1
        html = self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]
        return PageFetch(html, _URL, 200)


def _chrome_serving(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def session(*, headed: bool) -> _Chrome:
        return _Chrome(_html(name))

    monkeypatch.setattr(_gf_browser, "session", session)


def _chrome(monkeypatch: pytest.MonkeyPatch, *pages: str) -> _Chrome:
    chrome = _Chrome(*pages)

    def session(*, headed: bool) -> _Chrome:
        return chrome

    monkeypatch.setattr(_gf_browser, "session", session)
    return chrome


def _serve(
    monkeypatch: pytest.MonkeyPatch, *, curated: str, headless: str, boardless: int = 0
) -> _UaSession:
    session = _UaSession(curated=_html(curated), headless=_html(headless), boardless=boardless)
    monkeypatch.setattr(gfid, "get_client", lambda: _UaClient(session))
    return session


_SINGLE_PAIRS = [
    ("ds1_bos_lhr_curated", "ds1_bos_lhr_chrome"),
    ("ds1_ewr_lgw_curated", "ds1_ewr_lgw_token"),
    ("ds1_jfk_lax_curated", "ds1_jfk_lax_token"),
]


@pytest.mark.parametrize(
    ("curated", "headless", "chrome"),
    [
        ("ds1_nyc_lon_curated", "ds1_nyc_lon_token", "ds1_nyc_lon_chrome"),
        *((curated, headless, headless) for curated, headless in _SINGLE_PAIRS),
    ],
    ids=["nyc-lon", "bos-lhr", "ewr-lgw", "jfk-lax"],
)
def test_both_rungs_read_one_board(
    curated: str,
    headless: str,
    chrome: str,
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base for nyc-lon alone: rung 1 sent curl_cffi's `Chrome/` UA
    and read the curated 72 rows from USD679, where rung 2 read 300 from USD488.
    A single airport pair is one board under either UA, so its case is green at
    the base too."""
    gf_session()
    _serve(monkeypatch, curated=curated, headless=headless)
    _chrome_serving(monkeypatch, chrome)
    filters = _one_way_filters()
    http = gfid._one_call_laddered(filters, gfid.HTTP_TRANSPORT)
    browser = gfid._one_call_laddered(filters, gfid.GfTransport(mode="browser"))
    assert _shape(http) == _shape(browser)


@pytest.mark.parametrize(
    ("curated", "headless"), _SINGLE_PAIRS, ids=["bos-lhr", "ewr-lgw", "jfk-lax"]
)
def test_a_single_airport_pair_is_one_board_under_either_ua(curated: str, headless: str) -> None:
    """Green at the base and the tip: the fixtures' own fact, which is what keeps
    a single-airport search's answer unchanged by the token."""
    assert _shape(_board(curated)) == _shape(_board(headless))


def test_the_multi_airport_boards_differ_by_ua() -> None:
    """Green at the base and the tip: the fixtures discriminate the two UAs."""
    assert (_shape(_board("ds1_nyc_lon_curated")), _shape(_board("ds1_nyc_lon_token"))) == (
        (72, 679.0),
        (300, 488.0),
    )


@pytest.mark.parametrize("cheapest", [False, True], ids=["board", "cheapest-tab"])
def test_a_search_page_get_carries_the_token_and_the_session_gains_none(
    cheapest: bool, gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base, which sent no User-Agent of its own and so curl_cffi's
    `Chrome/146`. The session's headers are what fli set, before and after: a
    header set there would ride every other request fli makes on this thread."""
    gf_session()
    session = _serve(monkeypatch, curated="ds1_jfk_lax_curated", headless="ds1_jfk_lax_token")
    gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT, cheapest=cheapest)
    assert (session.uas, session.headers) == ([_HEADLESS_UA], _FLI_HEADERS)


def test_the_token_ua_is_curl_cffis_default_chrome() -> None:
    """Red at the base, which had no such UA. A curl_cffi upgrade that moves
    the `chrome` alias fails here: the UA would then claim one Chrome while the
    TLS fingerprint is another's."""
    from curl_cffi.requests.impersonate import DEFAULT_CHROME

    version = re.fullmatch(r".* HeadlessChrome/(\d+)\.0\.0\.0 Safari/537\.36", gfid._SEARCH_PAGE_UA)
    assert version is not None
    assert f"chrome{version.group(1)}" == DEFAULT_CHROME


# ───────────────────────── a token page with no board ─────────────────────────


def test_a_token_page_with_no_board_is_read_again(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red while one boardless page refused the leg: the next read of the same
    URL carries the full board."""
    gf_session()
    session = _serve(
        monkeypatch, curated="ds1_nyc_lon_curated", headless="ds1_nyc_lon_token", boardless=1
    )
    board = gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)
    assert (_shape(board), session.uas) == ((300, 488.0), [_HEADLESS_UA, _HEADLESS_UA])


def test_a_token_page_twice_with_no_board_is_read_a_third_time_with_the_token(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at ef32ded, whose third read dropped the token and answered the
    curated 72 rows from USD679 where rung 2 read 300 from USD488."""
    gf_session()
    session = _serve(
        monkeypatch, curated="ds1_nyc_lon_curated", headless="ds1_nyc_lon_token", boardless=2
    )
    board = gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)
    assert (_shape(board), session.uas) == ((300, 488.0), [_HEADLESS_UA] * 3)


def test_a_page_with_no_board_is_refused_after_three_token_reads(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at ef32ded, whose third read dropped the token. Three bound what a
    page whose layout really changed costs before it is refused."""
    gf_session()
    session = _UaSession(curated=_NO_BOARD, headless=_NO_BOARD)
    monkeypatch.setattr(gfid, "get_client", lambda: _UaClient(session))
    with pytest.raises(GfPageShapeError, match="no readable ds:1 payload"):
        gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)
    assert session.uas == [_HEADLESS_UA] * 3


def test_a_search_whose_token_pages_carry_no_board_is_refused(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at ef32ded, which answered from the curated board at USD679 and
    called it complete. That board can leave out the fare rung 2 lists, so the
    search says it read no board rather than answering from it."""
    gf_session()
    session = _UaSession(curated=_answered("ds1_nyc_lon_curated"), headless=_NO_BOARD)
    monkeypatch.setattr(gfid, "get_client", lambda: _UaClient(session))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--fast", "--format", "json"]
    result = CliRunner().invoke(cli.app, argv)
    assert (result.exit_code, result.stdout, session.uas) == (1, "", [_HEADLESS_UA] * 3)
    assert "no readable ds:1 payload" in " ".join(result.stderr.split())


@pytest.mark.parametrize("headed", [False, True], ids=["headless", "headed"])
def test_a_chrome_page_with_no_board_is_navigated_again(
    headed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at ef32ded, which refused the leg as a page-shape change at one
    navigation where rung 1 reads the same page again and lists its board."""
    chrome = _chrome(monkeypatch, _NO_BOARD, _html("ds1_nyc_lon_chrome"))
    transport = gfid.GfTransport(mode="browser", headed=headed)
    board = gfid._one_call_laddered(_one_way_filters(), transport)
    assert (_shape(board), chrome.navigations) == ((300, 488.0), 2)


def test_a_chrome_page_twice_with_no_board_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at ef32ded, which refused at one navigation. A navigation costs
    seconds where a rung-1 read costs one request, so two bound it."""
    chrome = _chrome(monkeypatch, _NO_BOARD)
    with pytest.raises(GfPageShapeError, match="no readable ds:1 payload"):
        gfid._one_call_laddered(_one_way_filters(), gfid.GfTransport(mode="browser"))
    assert chrome.navigations == 2


# ───────────────────────── the board's row cap ─────────────────────────

_CAP_LINE = (
    "Google Flights stops at 300 rows for this search: fares above USD1006.00 may be missing."
)


def _capped(rows: int, *, unreadable: int | None = None) -> gfid.Board[gfid.GFlightWithId]:
    """The token page cut to its first `rows` raw rows, one of them unreadable
    at `unreadable`."""
    payload: list[Any] = json.loads(_ds1("ds1_nyc_lon_token"))
    payload[3][0] = payload[3][0][: rows - len(payload[2][0])]
    ds1 = json.dumps(payload)
    if unreadable is not None:
        ds1 = _unreadable(ds1, index=unreadable)
    return gfid._rows_from_page_html(PageFetch(_page(ds1), _URL, 200))


def test_a_board_at_the_row_cap_names_its_highest_fare() -> None:
    """Red at the base, whose board recorded no cap. The raw count decides, an
    unread row included: no field of the page states the cap."""
    assert _board("ds1_nyc_lon_token").capped_at == {"USD": 1006.0}
    one_unread = _capped(300, unreadable=7)
    assert (len(one_unread), one_unread.unread, one_unread.capped_at) == (299, 1, {"USD": 1006.0})
    assert _capped(299).capped_at == {}
    for name in ("ds1_nyc_lon_curated", *(n for pair in _SINGLE_PAIRS for n in pair)):
        assert _board(name).capped_at == {}, name


def test_the_cap_survives_the_boards_currency_fill() -> None:
    """Red at the base: no cap to carry. The cap is in its page's currency,
    whichever one the search asked for, and one no row named takes the asked."""
    board = _board("ds1_nyc_lon_token")
    assert gfid._with_board_currency(board, "EUR").capped_at == {"USD": 1006.0}
    eur = gfid.Board(
        [replace(r, flight=r.flight.model_copy(update={"currency": "EUR"})) for r in board],
        capped_at=board.capped_at,
    )
    assert gfid._with_board_currency(eur, "USD").capped_at == {"EUR": 1006.0}
    undecoded = gfid.Board(
        [replace(r, flight=r.flight.model_copy(update={"currency": None})) for r in board],
        capped_at={"": 1006.0},
    )
    assert gfid._with_board_currency(undecoded, "EUR").capped_at == {"EUR": 1006.0}


@pytest.mark.parametrize(
    ("caps", "merged"),
    [
        (({"USD": 900.0}, {"USD": 700.0}), {"USD": 700.0}),
        ((dict[str, float](), {"USD": 800.0}), {"USD": 800.0}),
        ((dict[str, float](), dict[str, float]()), dict[str, float]()),
    ],
    ids=["both", "one", "neither"],
)
def test_a_board_merged_from_pages_takes_the_lowest_cap(
    caps: tuple[dict[str, float], dict[str, float]],
    merged: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, whose boards took no cap. 12 origins to LAX is two
    pages; a fare above the lower cap may be missing from that page."""
    from test_gf_chunked_search import _EAST, _row

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        del currency, cheapest
        origins = [a[0].name for a in filters.flight_segments[0].departure_airport]
        page = 0 if origins[0] in _EAST[:6] else 1
        rows = [_row(i, _DEP, o, "LAX", 100.0 + i) for i, o in enumerate(origins)]
        return gfid.Board(rows, capped_at=caps[page])

    monkeypatch.setattr(gfid, "_one_call", one_call)
    board = cli._gflight_results((Leg.of(_EAST, "LAX", _DEP),), SearchOptions(), 5)
    assert isinstance(board, gfid.Board)
    assert board.capped_at == merged


@pytest.mark.parametrize(
    ("usd_capped", "bound"),
    [(False, "EUR1006.00"), (True, "EUR1006.00 and USD1006.00")],
    ids=["one-page-capped", "both-pages-capped"],
)
def test_the_cap_line_names_the_currency_of_the_page_that_stopped(
    usd_capped: bool, bound: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red before, which read the line's currency off the rows the price cap
    left. 12 origins to LAX is two pages: the first answers in EUR and stops at
    the row cap, the second in USD. Every EUR fare is over the price cap, so
    only USD rows are shown, and the EUR cap still bounds the EUR fares."""
    from test_gf_chunked_search import _EAST, _Google

    day = _DEP.isoformat()
    raw = _page(_answering(_ds1("ds1_nyc_lon_token"), origin="JFK", destination="LAX", date=day))
    token = gfid._rows_from_page_html(PageFetch(raw, _URL, 200))
    eur = gfid.Board(
        [replace(r, flight=r.flight.model_copy(update={"currency": "EUR"})) for r in token],
        capped_at=token.capped_at,
    )
    google = _Google([(o, "LAX") for o in _EAST])

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        if filters.flight_segments[0].departure_airport[0][0].name in _EAST[:6]:
            return eur
        board = google(filters, currency=currency, cheapest=cheapest)
        return gfid.Board(board, capped_at=token.capped_at) if usd_capped else board

    monkeypatch.setattr(gfid, "_one_call", one_call)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = [*_SEARCH, ",".join(_EAST), "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight"]
    result = CliRunner().invoke(cli.app, [*argv, "--fast", "--max-price", "200"])
    assert result.exit_code == 0, result.output
    stderr = " ".join(result.stderr.split())
    line = f"Google Flights stops at 300 rows for this search: fares above {bound} may be missing."
    assert stderr.count("rows for this search") == 1, stderr
    assert line in stderr, stderr


@pytest.mark.parametrize("capped", [0, 1], ids=["unpinned-page", "pinned-page"])
def test_a_round_trip_page_names_its_cap_pinned_or_not(
    capped: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at 01c8265 for the unpinned page. 12 origins to LAX is two pages,
    and `-n 1` pins only the second page's outbounds, the cheapest. The first
    page's were read all the same, and a trip through its airports priced above
    its cap may be missing from the board."""
    from test_gf_chunked_search import _EAST, _Google

    google = _Google([(o, "LAX") for o in _EAST], fare=lambda i: 200.0 - i)

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        board = google(filters, currency=currency, cheapest=cheapest)
        out = filters.flight_segments[0]
        page = 0 if out.departure_airport[0][0].name in _EAST[:6] else 1
        if page != capped or out.selected_flight is not None or cheapest:
            return board
        return gfid.Board(board, unread=board.unread, capped_at={"USD": 900.0})

    monkeypatch.setattr(gfid, "_one_call", one_call)
    legs = (Leg.of(_EAST, "LAX", _DEP), Leg.of("LAX", _EAST, _RET))
    board = cli._gflight_results(legs, SearchOptions(), 1)
    assert isinstance(board, gfid.Board)
    assert {p for p, n in google.calls if n is not None} == {(_EAST[6:], ("LAX",))}
    assert board.capped_at == {"USD": 900.0}


def _keeps_none(_leg: int, _row: gfid.GFlightWithId) -> bool:
    return False


@pytest.mark.parametrize(
    ("mode", "base_cap", "keep", "merged"),
    [
        ("show", {}, None, {"USD": 1006.0}),
        ("show", {"USD": 1200.0}, None, {"USD": 1006.0}),
        ("show", {"USD": 900.0}, None, {"USD": 900.0}),
        ("show", {}, _keeps_none, {"USD": 1006.0}),
        ("hide", {}, None, {}),
    ],
    ids=["uncapped-base", "higher-base-cap", "lower-base-cap", "marked-rows-filtered", "hidden"],
)
def test_the_cheapest_tabs_cap_rides_the_rows_it_adds(
    mode: gfid.SeparateTickets,
    base_cap: dict[str, float],
    keep: Callable[[int, gfid.GFlightWithId], bool] | None,
    merged: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at 01c8265 for the uncapped base, the higher base cap and the
    filtered marked rows, where the board kept the base page's cap alone. Shown,
    the tab's marked rows join the board, and one priced above the tab's cap may
    be missing from them whether or not the routing left any. Hidden, they are
    only counted, so the board shown keeps its own cap."""
    token = _board("ds1_nyc_lon_token")
    tab = gfid.Board(
        [replace(r, ticketing="self_transfer") for r in token], capped_at=token.capped_at
    )

    def one_call_laddered(*_a: object, **_kw: object) -> gfid.Board[gfid.GFlightWithId]:
        return tab

    monkeypatch.setattr(gfid, "_one_call_laddered", one_call_laddered)
    answer = gfid._with_separate_tickets(
        _one_way_filters(),
        gfid.Board([token[0]], capped_at=base_cap),
        mode=mode,
        transport=gfid.HTTP_TRANSPORT,
        currency="USD",
        keep=keep,
    )
    assert answer.capped_at == merged


def _search(*extra: str) -> list[str]:
    return [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--backend", "gflight", *extra]


def _answered(name: str, *, origin: str | None = None, destination: str | None = None) -> str:
    day = (_DEP if origin is None else _RET).isoformat()
    return _page(_answering(_ds1(name), origin=origin, destination=destination, date=day))


def _cap_lines(text: str) -> list[str]:
    return re.findall(r"Google Flights[A-Z ]* stops at 300 rows[^.]*\.\d\d may be missing\.", text)


@pytest.mark.parametrize(
    "fmt", [(), ("--format", "json"), ("--format", "envelope")], ids=["table", "json", "envelope"]
)
def test_a_capped_board_says_where_it_stops_once(
    fmt: tuple[str, ...], gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base, which printed no line. A note in the envelope, not a
    narrowing: every fare at or below the cap is on the board."""
    gf_session(_answered("ds1_nyc_lon_token"))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search("--fast", *fmt), env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    assert _cap_lines(" ".join(result.stderr.split())) == [_CAP_LINE]
    if fmt[-1:] == ("envelope",):
        env = json.loads(result.stdout)
        assert (env["backend"], env["complete"]) == ("gflight", True)
        assert _cap_lines(" ".join(env["notes"])) == [_CAP_LINE]


def test_a_board_under_the_cap_prints_no_line(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Green at the base and the tip."""
    gf_session(_answered("ds1_nyc_lon_curated"))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search("--fast", "--format", "json"))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)
    assert "rows for this search" not in result.stderr


def test_a_round_trip_names_its_outbound_boards_cap(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base. The outbound page stopped at the cap, so a trip priced
    above its highest fare may be missing; the return pages are under it."""
    gf_session(
        _answered("ds1_nyc_lon_token"),
        _answered("ds1_nyc_lon_curated", origin="LHR", destination="JFK"),
    )
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = _search("--return", _RET.isoformat(), "--fast", "--format", "json")
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)
    assert _cap_lines(" ".join(result.stderr.split())) == [_CAP_LINE]


def test_a_search_handed_to_matrix_prints_no_cap_line(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Green at the base and the tip: the line describes Google's board, and
    a hand-off shows Matrix's."""
    gf_session(_answered("ds1_nyc_lon_token"))
    ran: list[bool] = []

    def matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", matrix)
    argv = [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--routing", "O:NZ+", "--fast"]
    result = CliRunner().invoke(cli.app, [*argv, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert ran == [True]
    stderr = " ".join(result.stderr.split())
    assert "Using Matrix: no Google Flights itinerary matched" in stderr
    assert "rows for this search" not in stderr


def test_a_multi_cabin_search_names_each_capped_cabin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base. Economy's board stopped at the cap and business's did
    not, so the one line names economy."""
    from fli.models import SeatType  # pyright: ignore[reportMissingTypeStubs]

    pages = {
        SeatType.ECONOMY: _answered("ds1_nyc_lon_token"),
        SeatType.BUSINESS: _answered("ds1_nyc_lon_curated"),
    }

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        del currency, cheapest
        return gfid._rows_from_page_html(PageFetch(pages[filters.seat_type], _URL, 200))

    monkeypatch.setattr(gfid, "_one_call", one_call)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search("--cabin", "economy,business", "--fast"))
    assert result.exit_code == 0, result.output
    assert _cap_lines(" ".join(result.stderr.split())) == [
        _CAP_LINE.replace("Google Flights", "Google Flights COACH")
    ]


def test_a_capped_board_the_routing_emptied_still_names_its_cap(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red while the line needed a row beside it. No row on the token board
    flies SN, and one priced above the cap may be missing from it: the line is
    what says the empty answer stops there. The currency is the capped page's,
    which no row is left to carry."""
    gf_session(_answered("ds1_nyc_lon_token"))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = _search("--routing", "O:SN+", "--fast", "--format", "envelope")
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    env = json.loads(result.stdout)
    assert (env["backend"], env["complete"], env["results"][0]["rows"]) == ("gflight", True, [])
    assert _cap_lines(" ".join(result.stderr.split())) == [_CAP_LINE]
    assert _cap_lines(" ".join(env["notes"])) == [_CAP_LINE]


def test_a_multi_cabin_search_names_the_cap_of_a_cabin_the_routing_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red while the line needed a row beside it: the emptied arm, with no
    hand-off asked for."""
    from fli.models import SeatType  # pyright: ignore[reportMissingTypeStubs]

    pages = {
        SeatType.ECONOMY: _answered("ds1_nyc_lon_token"),
        SeatType.BUSINESS: _answered("ds1_nyc_lon_curated"),
    }

    def one_call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        del currency, cheapest
        return gfid._rows_from_page_html(PageFetch(pages[filters.seat_type], _URL, 200))

    monkeypatch.setattr(gfid, "_one_call", one_call)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = _search("--cabin", "economy,business", "--routing", "O:SN+", "--fast")
    result = CliRunner().invoke(cli.app, argv)
    stderr = " ".join(result.stderr.split())
    assert "Google Flights COACH: no itinerary matched" in stderr, result.output
    assert _cap_lines(stderr) == [_CAP_LINE.replace("Google Flights", "Google Flights COACH")]


def test_the_merged_table_names_no_cap_for_a_board_the_routing_emptied(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Green before and after: the default search reads Google's board beside
    Matrix's and, with Google's emptied, answers from Matrix, so the line would
    describe a board nobody is shown."""
    gf_session(_answered("ds1_nyc_lon_token"))
    weave: list[object] = []
    handed: list[object] = []

    async def matrix_into(state: dict[str, Any], *args: object) -> None:
        weave.append(args)
        state["matrix"] = SearchResult.from_api(json.loads(_MATRIX.read_text()))

    def matrix(**kw: object) -> None:
        handed.append(kw)

    monkeypatch.setattr(cli, "_matrix_into", matrix_into)
    monkeypatch.setattr(cli, "_run_matrix_path", matrix)
    argv = [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--routing", "O:SN+"]
    result = CliRunner().invoke(cli.app, argv, env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    assert (len(weave), len(handed)) == (1, 0)
    stderr = " ".join(result.stderr.split())
    assert "rows for this search" not in stderr, stderr


@pytest.mark.parametrize("fmt", ["json", "envelope"])
def test_the_cross_check_document_names_the_cap_of_a_board_the_routing_emptied(
    fmt: str, gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at ef32ded, which printed the line beside rows only. The document's
    `search` half is Google's board, emptied or not, so the line describes a
    board it shows."""
    gf_session(_answered("ds1_nyc_lon_token"))

    async def matrix_into(state: dict[str, Any], *_args: object) -> None:
        state["matrix"] = SearchResult.from_api(json.loads(_MATRIX.read_text()))

    monkeypatch.setattr(cli, "_matrix_into", matrix_into)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--routing", "O:SN+"]
    result = CliRunner().invoke(cli.app, [*argv, "--enrich", "--format", fmt])
    assert result.exit_code == 0, result.output
    assert _cap_lines(" ".join(result.stderr.split())) == [_CAP_LINE]
    if fmt == "envelope":
        env = json.loads(result.stdout)
        assert (env["backend"], env["complete"], env["results"][0]["rows"]) == ("gflight", True, [])
        assert _cap_lines(" ".join(env["notes"])) == [_CAP_LINE]
