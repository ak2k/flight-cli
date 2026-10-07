# pyright: reportPrivateUsage=false
"""Google's server error on a search page is named, not read as a re-shaped page.

Google sometimes answers a search page with HTTP 200 and a `ds:1` that holds an
RPC status, `[13, null, [ErrorResponse]]`, ending `errorHasStatus: true` where a
board's blob ends on `sideChannel`. The two fixtures are live NYC-LON pages of
that shape, trimmed to their scripts: in one the `ds:1` blob is the page's last,
in the other it comes before `ds:4`.
"""

from __future__ import annotations

import gzip
import json
import re
import time
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import GFLIGHT_PAGE_DIR, _ds1, _page
from flight_cli import _doctor, _gf_browser, _gf_calgraph, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import (
    GfBackendError,
    GfPageShapeError,
    GfSearchServerError,
    GfThrottledError,
)
from test_gf_full_board import _RET, _URL, _no_matrix, _one_way_filters
from test_gf_rung_parity import (
    _NO_BOARD,
    _answered,
    _chrome,
    _html,
    _search,
    _shape,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_ERROR_PAGES = [
    "page_ds1_error_status13.html.gz",
    "page_ds1_error_status13_before_ds4.html.gz",
]
_BOARDLESS = "Google Flights' search page carried no readable ds:1 payload; the page shape changed"
_TOO_SHORT = (
    "ds:1 decoded to 2 top-level entries, too few to hold a board at [2, 3]; "
    "the payload layout changed"
)


def _error_page(name: str = _ERROR_PAGES[0]) -> str:
    return gzip.decompress((GFLIGHT_PAGE_DIR / name).read_bytes()).decode()


def _read(html: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


def _outcome(html: str) -> object:
    """A page's rows and what the board says beside them, or its refusal."""
    try:
        board = _read(html)
    except GfBackendError as e:
        return type(e), str(e)
    return list(board), vars(board)


def _committed_pages() -> list[Any]:
    plain = sorted(GFLIGHT_PAGE_DIR.glob("*.json"))
    zipped = sorted((GFLIGHT_PAGE_DIR / "rung_parity").glob("*.json.gz"))
    return [
        *(pytest.param(_page(p.read_text()), id=p.name) for p in plain),
        *(pytest.param(_page(gzip.decompress(p.read_bytes()).decode()), id=p.name) for p in zipped),
    ]


def _no_status(_html: str) -> None:
    return None


def _none_parsed() -> str:
    """The three-row JFK-LAX capture with every row read and none parsed."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    for row in gfid._rows_from_ds1(payload).rows:
        row[0][2][0][20] = None
    return json.dumps(payload)


# ───────────────────────── the page is named ─────────────────────────


@pytest.mark.parametrize("name", _ERROR_PAGES)
def test_a_ds1_holding_a_server_error_is_that_error(name: str) -> None:
    """Red at the base, which raised `_BoardlessPageError`: "carried no readable
    ds:1 payload; the page shape changed"."""
    with pytest.raises(GfSearchServerError) as caught:
        _read(_error_page(name))
    assert caught.value.code == 13
    assert not isinstance(caught.value, GfPageShapeError)


@pytest.mark.parametrize("html", _committed_pages())
def test_no_committed_page_reads_as_a_server_error(
    html: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard, green at the base by behavior: every committed page reads as
    it did before the detector, rows or refusal. Red at the base only on the
    missing name."""
    assert gfid._ds1_error_status(html) is None
    read = _outcome(html)
    monkeypatch.setattr(gfid, "_ds1_error_status", _no_status)
    assert _outcome(html) == read


@pytest.mark.parametrize(
    ("html", "refusal", "message"),
    [
        pytest.param(_NO_BOARD, gfid._BoardlessPageError, re.escape(_BOARDLESS), id="no-ds1"),
        pytest.param(
            _error_page().replace("({key: 'ds:1'", "({key: 'ds:4'"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="error-keyed-ds4",
        ),
        pytest.param(
            _error_page().replace("data:[13,null,", "data:[13,null,,"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="undecodable-status",
        ),
        pytest.param(
            _error_page().replace("data:[13,", "data:[true,"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="bool-code",
        ),
        pytest.param(
            _page("[null, null]"), GfPageShapeError, re.escape(_TOO_SHORT), id="too-short"
        ),
        pytest.param(
            _page(_ds1("ds1_blocks_relocated.json")),
            GfPageShapeError,
            r"ds:1 holds flight rows at \[[\d, ]+\], not at \[2, 3\] \(found 0 there\); "
            r"the payload layout changed",
            id="rows-elsewhere",
        ),
        pytest.param(
            _page(_none_parsed()),
            gfid._PageUnreadError,
            r"none of 3 Google Flights rows parsed; the row shape changed",
            id="none-parsed",
        ),
    ],
)
def test_a_real_shape_change_keeps_its_type_and_message(
    html: str, refusal: type[GfBackendError], message: str
) -> None:
    """A guard, green at the base by behavior: a page with no server error in
    its `ds:1` is refused as the base refused it."""
    with pytest.raises(GfBackendError) as caught:
        _read(html)
    assert type(caught.value) is refusal
    assert re.match(message, str(caught.value))


@pytest.mark.parametrize(
    ("bags", "remedy"),
    [
        (False, "use [bold]--backend matrix[/]"),
        (True, "drop [bold]--bags[/] to search Matrix, which prices no bags"),
    ],
    ids=["plain", "bags"],
)
def test_the_refusal_names_the_server_error_and_its_code(bags: bool, remedy: str) -> None:
    """Red at the base, which had no type to render; its page rendered as
    "Google Flights' page shape changed"."""
    refusal = cli._gf_refusal(GfSearchServerError(13), bags=bags)
    assert refusal.note == "Google Flights answered with a server error (status 13)"
    assert refusal.message == (
        "[yellow]Google Flights answered with a server error (status 13).[/] "
        f"Retry later, or {remedy}."
    )


def test_doctor_reads_the_server_error_as_upstream() -> None:
    """Red at the base, where the page read as "shape", a cause that does not
    lift on its own."""
    assert _doctor._classify(GfSearchServerError(13)) == (
        "upstream",
        "Google Flights answered the search with a server error (status 13)",
    )


def test_the_price_graph_does_not_refuse_on_a_server_error() -> None:
    """A guard, green at the base, which suppressed the page as a shape error:
    the graph is its own request."""
    _gf_calgraph._refuse_a_wall(PageFetch(_error_page(), _URL, 200))


# ───────────────────────── the page is read again ─────────────────────────

_THROTTLED = "<html><body>Our systems have detected unusual traffic</body></html>"


class _Clock:
    """`time`, recording each sleep rather than taking it."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Installed over `gf_session`'s own stand-in when both are asked for:
    fixtures a test names are set up in order, and this one comes last."""
    recorder = _Clock()
    monkeypatch.setattr(gfid, "time", recorder)
    return recorder


def _http() -> gfid.Board[gfid.GFlightWithId]:
    return gfid._one_call_laddered(_one_way_filters(), gfid.HTTP_TRANSPORT)


def test_a_server_error_then_the_board_reads_the_board_after_a_pause(
    gf_session: Callable[..., Any], clock: _Clock
) -> None:
    """Red at the base by its sleeps, `[]`: the page re-read at once. One
    read was all the base gave a page it took for one with no board."""
    fake = gf_session(_error_page(), _html("ds1_nyc_lon_token"))
    board = _http()
    assert (_shape(board), len(fake.gets), clock.sleeps) == ((300, 488.0), 2, [2.0])


def test_a_server_error_twice_then_the_board_pauses_longer_the_second_time(
    gf_session: Callable[..., Any], clock: _Clock
) -> None:
    """Red at the base by its sleeps, `[]`."""
    fake = gf_session(_error_page(), _error_page(), _html("ds1_nyc_lon_token"))
    board = _http()
    assert (_shape(board), len(fake.gets), clock.sleeps) == ((300, 488.0), 3, [2.0, 6.0])


def test_a_server_error_throughout_is_refused_as_one_after_three_reads(
    gf_session: Callable[..., Any], clock: _Clock
) -> None:
    """Red at the base, which refused it as `_BoardlessPageError` after three
    reads and no pause."""
    fake = gf_session(_error_page())
    with pytest.raises(GfSearchServerError) as caught:
        _http()
    assert (caught.value.code, len(fake.gets), clock.sleeps) == (13, 3, [2.0, 6.0])


def test_a_server_error_then_a_throttle_spends_no_more_than_the_ladder(
    gf_session: Callable[..., Any], clock: _Clock
) -> None:
    """Red at the base, which spent 7 GETs: three reads of the page, then a
    ladder of its own. Each re-read is one of the call's wall attempts here."""
    fake = gf_session(_error_page(), _error_page(), _THROTTLED)
    with pytest.raises(GfThrottledError):
        _http()
    assert (len(fake.gets), clock.sleeps[:2]) == (5, [2.0, 6.0])


def test_a_server_error_spends_no_rung_of_the_shared_ladder(
    gf_session: Callable[..., Any], clock: _Clock
) -> None:
    """A guard, green at the base by behavior: the base never brought this page
    to the ladder. The shared round is the per-IP wall's, and a server error
    on one page says nothing about it."""
    gf_session(_error_page())
    with gfid.shared_throttle_ladder():
        ladder = gfid._fanout_ladder.get()
        assert ladder is not None
        with pytest.raises(GfSearchServerError):
            _http()
        assert (ladder._wall.spent, ladder._wall.owner) == (0, None)
    assert clock.sleeps == [2.0, 6.0]


def test_auto_does_not_open_chrome_on_a_server_error(
    gf_session: Callable[..., Any], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard, green at the base by behavior (no Chrome, after three reads):
    only a throttle moves `auto` to Chrome. Red at the base on the type."""
    opened: list[bool] = []

    def session(*, headed: bool) -> object:
        opened.append(headed)
        pytest.fail("auto opened Chrome on a server error")

    monkeypatch.setattr(_gf_browser, "session", session)
    fake = gf_session(_error_page())
    with pytest.raises(GfSearchServerError):
        gfid._one_call_laddered(_one_way_filters(), gfid.GfTransport(mode="auto"))
    assert (opened, len(fake.gets), clock.sleeps) == ([], 3, [2.0, 6.0])


def test_chrome_navigates_a_server_error_once_more_without_a_pause(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A guard, green at the base, which navigated again as for a page with no
    board: a navigation costs seconds, so rung 2 keeps its two."""
    chrome = _chrome(monkeypatch, _error_page(), _html("ds1_nyc_lon_chrome"))
    board = gfid._one_call_laddered(_one_way_filters(), gfid.GfTransport(mode="browser"))
    assert (_shape(board), chrome.navigations, clock.sleeps) == ((300, 488.0), 2, [])


def test_chrome_refuses_a_server_error_twice_as_that_error(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Red at the base, which refused it as `_BoardlessPageError`."""
    chrome = _chrome(monkeypatch, _error_page())
    with pytest.raises(GfSearchServerError):
        gfid._one_call_laddered(_one_way_filters(), gfid.GfTransport(mode="browser"))
    assert (chrome.navigations, clock.sleeps) == (2, [])


def test_a_search_pauses_at_most_eight_seconds_for_server_errors(
    gf_session: Callable[..., Any], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red without the search's budget, which paused 8 s for each page: the ten
    return pages of the round trip and its Cheapest tab, 88 s. Green at the base,
    which paused for none; red there on the refusal it named."""
    fake = gf_session(_answered("ds1_nyc_lon_token"), _error_page())
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    argv = _search("--return", _RET.isoformat(), "--fast")
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert sum(clock.sleeps) <= 8.0, (clock.sleeps, len(fake.gets))
    stderr = " ".join(result.stderr.split())
    assert "Google Flights answered with a server error (status 13)" in stderr, stderr
    assert "page shape" not in stderr
