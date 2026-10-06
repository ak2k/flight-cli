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
import logging
import re
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import (
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
from test_gf_full_board import _DEP, _RET, _SEARCH, _URL, _no_matrix, _one_way_filters

if TYPE_CHECKING:
    from collections.abc import Callable

_DIR = GFLIGHT_PAGE_DIR / "rung_parity"
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
    """Rung 2's session, serving what its navigation read."""

    def __init__(self, html: str) -> None:
        self._html = html

    def get_html(self, _url: str) -> PageFetch:
        return PageFetch(self._html, _URL, 200)


def _chrome_serving(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def session(*, headed: bool) -> _Chrome:
        return _Chrome(_html(name))

    monkeypatch.setattr(_gf_browser, "session", session)


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


def test_a_token_page_twice_with_no_board_is_read_without_the_token(
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Red while one boardless page refused the leg. The board a plain `Chrome/`
    UA is served is shorter, and the warning says so, but it is Google's answer
    where the refusal sent the search to Matrix."""
    gf_session()
    session = _serve(
        monkeypatch, curated="ds1_nyc_lon_curated", headless="ds1_nyc_lon_token", boardless=2
    )
    with caplog.at_level(logging.WARNING, logger=gfid.__name__):
        board = gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)
    assert (_shape(board), session.uas) == ((72, 679.0), [_HEADLESS_UA, _HEADLESS_UA, ""])
    assert [r.getMessage() for r in caplog.records] == [
        "Google Flights' full board did not read twice (Google Flights' search page carried "
        "no readable ds:1 payload; the page shape changed); read the shorter board it serves "
        "a regular browser, which can leave cheaper fares out"
    ]


def test_a_page_with_no_board_under_either_ua_is_refused_after_three_reads(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red while the first refusal was final, at one read. Three bound what a
    page whose layout really changed costs before it is refused."""
    gf_session()
    session = _UaSession(curated=_NO_BOARD, headless=_NO_BOARD)
    monkeypatch.setattr(gfid, "get_client", lambda: _UaClient(session))
    with pytest.raises(GfPageShapeError, match="no readable ds:1 payload"):
        gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)
    assert session.uas == [_HEADLESS_UA, _HEADLESS_UA, ""]


def test_a_search_whose_token_pages_carry_no_board_answers_from_google(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red while a boardless token page refused the search as a page-shape
    change, which the default search hands to Matrix."""
    gf_session()
    session = _UaSession(curated=_answered("ds1_nyc_lon_curated"), headless=_NO_BOARD)
    monkeypatch.setattr(gfid, "get_client", lambda: _UaClient(session))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = [*_SEARCH, "NYC", "LON", "--dep", _DEP.isoformat(), "--fast", "--format", "envelope"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    env = json.loads(result.stdout)
    assert (env["backend"], env["complete"]) == ("gflight", True)
    assert min(r["price"] for r in env["results"][0]["rows"]) == 679.0
    assert [n for n in env["notes"] if "shorter board" in n]


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
    assert _board("ds1_nyc_lon_token").capped_at == 1006.0
    one_unread = _capped(300, unreadable=7)
    assert (len(one_unread), one_unread.unread, one_unread.capped_at) == (299, 1, 1006.0)
    assert _capped(299).capped_at is None
    for name in ("ds1_nyc_lon_curated", *(n for pair in _SINGLE_PAIRS for n in pair)):
        assert _board(name).capped_at is None, name


def test_the_cap_survives_the_boards_currency_fill() -> None:
    """Red at the base: no cap to carry."""
    board = _board("ds1_nyc_lon_token")
    assert gfid._with_board_currency(board, "USD").capped_at == 1006.0


@pytest.mark.parametrize(
    ("caps", "merged"),
    [((900.0, 700.0), 700.0), ((None, 800.0), 800.0), ((None, None), None)],
    ids=["both", "one", "neither"],
)
def test_a_board_merged_from_pages_takes_the_lowest_cap(
    caps: tuple[float | None, float | None],
    merged: float | None,
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
    """Red at the base. Each return page lists one pin's returns, so the
    outbound page's cap is the one that says which trips may be missing."""
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
