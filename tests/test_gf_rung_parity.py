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
import re
from typing import TYPE_CHECKING, Any

import pytest

from conftest import GFLIGHT_PAGE_DIR, _FakeRateLimiter, _FakeResponse, _NullCookies, _page
from flight_cli import _gf_browser
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from test_gf_full_board import _URL, _one_way_filters

if TYPE_CHECKING:
    from collections.abc import Callable

_DIR = GFLIGHT_PAGE_DIR / "rung_parity"
_HEADLESS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36"
)
_FLI_HEADERS = {"content-type": "application/x-www-form-urlencoded;charset=UTF-8"}


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
    User-Agent a request carries."""

    def __init__(self, *, curated: str, headless: str) -> None:
        self._curated, self._headless = curated, headless
        self.headers = dict(_FLI_HEADERS)
        self.cookies = _NullCookies()
        self.uas: list[str] = []

    def get(self, _url: str, **kw: Any) -> _FakeResponse:
        sent: dict[str, str] = {**self.headers, **(kw.get("headers") or {})}
        ua = next((v for k, v in sent.items() if k.lower() == "user-agent"), "")
        self.uas.append(ua)
        return _FakeResponse(text=self._headless if "HeadlessChrome/" in ua else self._curated)


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


def _serve(monkeypatch: pytest.MonkeyPatch, *, curated: str, headless: str) -> _UaSession:
    session = _UaSession(curated=_html(curated), headless=_html(headless))
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
