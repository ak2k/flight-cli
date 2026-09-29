# pyright: reportPrivateUsage=false
"""Rung 2: a real Chrome fetching the same Google Flights search page.

Two claims carry this file. **One parser** — rung 2 hands over bytes and the
same `_rows_from_page_html` decides what they mean, so a `/sorry/` page fetched
through Chrome is a throttle for exactly the reason it is over curl_cffi. And
**no test launches a browser** — the autouse guard in `conftest.py` replaces the
launcher for every test here except the handful marked `gf_browser`, which drive
a fake playwright object graph and never start a driver process. Rung 2 runs
headless in under 3 s, so the cost is not the reason; a test that reached the
real launcher would hit the live network and disprove the property the CLI
advertises, that `--gf-transport http` never consults a browser.

Nothing in this file touches the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import pathlib
import signal
import sys
import threading
import time
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pytest

# One home for the callback envelope, the re-pointing rule and the stderr
# capture; each helper's own docstring says why it is shaped as it is.
from conftest import _answering, _ds1, capture_err
from conftest import _page as _page_carrying
from flight_cli import _gf_booking, _gf_explore
from flight_cli import _gf_browser as gfb
from flight_cli import _gf_common as gfc
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import (
    GfBackendError,
    GfBrowserUnavailableError,
    GfPageShapeError,
    GfThrottledError,
    GfUpstreamStatusError,
)
from flight_cli.cli import _resolve_gf_transport
from flight_cli.domain import Bags, Cabin, Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from flight_cli._gf_common import GfTransportMode
    from flight_cli._gflight_ids import GFlightWithId

_PAGE_URL = "https://www.google.com/travel/flights?tfs=abc"
_SORRY_URL = "https://www.google.com/sorry/index?continue=x"


def _page(name: str = "ds1_jfk_lax_3rows.json") -> str:
    """A page carrying the fixture's `ds:1` blob in the captured callback envelope."""
    return _page_carrying(_ds1(name))


def _return_page() -> str:
    """The outbound capture, re-pointed at the return leg `_filters` asks for.

    A pinned return board is checked against the segment it was asked to FILL,
    so the capture replayed unchanged answers the outbound a second time and is
    refused — which is what a page that dropped the pin looks like. Rung 2 has
    to answer the question it asked, exactly as rung 1 does."""
    return _page_carrying(
        _answering(
            _ds1("ds1_jfk_lax_3rows.json"),
            origin="LAX",
            destination="JFK",
            date="2026-10-24",
            name="ds1_jfk_lax_3rows.json",
        )
    )


# ───────────────────────── a fake playwright object graph ──────────────────────
# Shaped exactly like the slice of patchright's API `_gf_browser` drives, so a
# signature drift shows up as a test failure rather than at the first live run.


class _FakeResponse:
    # `BaseException`, not `Exception`: a Ctrl-C landing in the body read is one
    # of the failure modes production distinguishes, and a fake that can only
    # carry an `Exception` cannot state that test at all.
    def __init__(
        self, *, body: str, url: str, status: int, body_error: BaseException | None
    ) -> None:
        self._body = body
        self._body_error = body_error
        self.url = url
        self.status = status

    def text(self) -> str:
        if self._body_error is not None:
            raise self._body_error
        return self._body


class _FakeFrame:
    def __init__(self, parent_frame: _FakeFrame | None) -> None:
        self.parent_frame = parent_frame


_MAIN_FRAME = _FakeFrame(None)
_CHILD_FRAME = _FakeFrame(_MAIN_FRAME)


class _FakeRequest:
    """A request as the capture sees it. `frame=None` is a service worker's,
    whose `frame` raises in patchright rather than returning nothing."""

    def __init__(self, url: str, *, navigation: bool = False, frame: _FakeFrame | None) -> None:
        self.url = url
        self._navigation = navigation
        self._frame = frame
        self.finished = False

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> _FakeFrame:
        if self._frame is None:
            raise RuntimeError("Service Worker requests do not have an associated frame.")
        return self._frame


class _FakeRpcResponse:
    """A response the page's own code received. Its body is refused until the
    request has finished, which is the order the capture promises to read in."""

    def __init__(self, request: _FakeRequest, *, body: str, status: int = 200) -> None:
        self.request = request
        self.url = request.url
        self.status = status
        self._body = body
        self.reads = 0

    def text(self) -> str:
        assert self.request.finished, "body read before the request finished"
        self.reads += 1
        return self._body


# One step of what the page does: an event name and what it carries.
type _Event = tuple[str, Any]


class _FakeLocator:
    def __init__(self, page: _FakePage, role: str, name: str, exact: bool) -> None:
        self._page = page
        self._found = (role, name, exact)

    def click(self, *, timeout: float) -> None:
        from patchright.sync_api import Locator

        inspect.signature(Locator.click).bind(None, timeout=timeout)
        self._page.clicks.append((*self._found, timeout))
        if self._page.click_error is not None:
            raise self._page.click_error
        self._page.emit_all(self._page.on_click)


class _FakePage:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.gotos: list[tuple[str, str, float]] = []
        self.url = ""
        # What the page does, by the call that lets it happen: every event in
        # `on_goto` fires inside the navigation and every one in `on_click`
        # inside the click, in order; `on_wait` delivers ONE event per driver
        # sleep, so a test can put time between a response and its finish.
        self.on_goto: list[_Event] = []
        self.on_click: list[_Event] = []
        self.on_wait: list[_Event] = []
        self.click_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.remove_error: BaseException | None = None
        self.clicks: list[tuple[str, str, bool, float]] = []
        self.waits: list[float] = []
        self.listeners: dict[str, list[Callable[[Any], None]]] = {}

    def on(self, event: str, f: Callable[[Any], None]) -> None:
        from patchright.sync_api import Page

        inspect.signature(Page.on).bind(None, event, f)
        self.listeners.setdefault(event, []).append(f)

    def remove_listener(self, event: str, f: Callable[[Any], None]) -> None:
        from patchright.sync_api import Page

        inspect.signature(Page.remove_listener).bind(None, event, f)
        if self.remove_error is not None:
            raise self.remove_error
        self.listeners[event].remove(f)
        if not self.listeners[event]:
            del self.listeners[event]

    def emit(self, event: str, payload: Any) -> None:
        if event == "requestfinished":
            payload.finished = True
        for f in list(self.listeners.get(event, [])):
            f(payload)

    def emit_all(self, events: list[_Event]) -> None:
        for event, payload in events:
            self.emit(event, payload)

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _FakeLocator:
        from patchright.sync_api import Page

        inspect.signature(Page.get_by_role).bind(None, role, name=name, exact=exact)
        return _FakeLocator(self, role, name, exact)

    def wait_for_timeout(self, timeout: float) -> None:
        from patchright.sync_api import Page

        inspect.signature(Page.wait_for_timeout).bind(None, timeout)
        self.waits.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        if self.on_wait:
            self.emit(*self.on_wait.pop(0))
        else:
            # Nothing left to happen: let real time pass, so a deadline test
            # ends on the clock rather than on a spin.
            time.sleep(timeout / 1000)

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _FakeResponse | None:
        self.gotos.append((url, wait_until, timeout))
        self.url = url
        outcome = self._outcomes[min(len(self.gotos) - 1, len(self._outcomes) - 1)]
        # `BaseException` for the same reason as the body read: under the
        # narrower check a `KeyboardInterrupt` outcome is RETURNED as though it
        # were a response, and production then calls `.text()` on it — the fake
        # answering a question nobody asked instead of raising the interrupt.
        if isinstance(outcome, BaseException):
            raise outcome
        self.emit_all(self.on_goto)
        return cast("_FakeResponse | None", outcome)


class _FakeContext:
    def __init__(self, page: _FakePage, new_page_error: BaseException | None = None) -> None:
        self._page = page
        self._new_page_error = new_page_error
        self.closed = False

    def new_page(self) -> _FakePage:
        # The last step inside `_ensure_page`'s try, and the one whose failure
        # leaves a context AND a driver to take back down. Without a knob here
        # the launch block's tail is reachable by no test.
        if self._new_page_error is not None:
            raise self._new_page_error
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext, launch_error: BaseException | None) -> None:
        self._context = context
        self._launch_error = launch_error
        self.launch_kwargs: dict[str, Any] = {}
        # Counted, not just recorded: reuse is the whole reason the session is
        # an object, and "one launch" is otherwise asserted by nothing.
        self.launches = 0

    def launch_persistent_context(self, **kwargs: Any) -> _FakeContext:
        # Bound against the REAL signature before anything is recorded, so a
        # keyword patchright renamed or dropped fails here rather than at the
        # first live launch. The tests below already assert these names and
        # values; this is the half that says the names exist upstream. Binding
        # touches no browser and opens no connection.
        from patchright.sync_api import BrowserType

        inspect.signature(BrowserType.launch_persistent_context).bind(None, **kwargs)
        self.launches += 1
        self.launch_kwargs = kwargs
        if self._launch_error is not None:
            raise self._launch_error
        return self._context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium, start_error: BaseException | None = None) -> None:
        self.chromium = chromium
        self._start_error = start_error
        self.stopped = False
        self.starts = 0

    def start(self) -> _FakePlaywright:
        self.starts += 1
        # The driver handshake, and the one launch step that fails BEFORE
        # `self._playwright` is assigned — so what `close()` can still reach
        # differs here from every other launch failure, and only a knob can
        # state that.
        if self._start_error is not None:
            raise self._start_error
        return self

    def stop(self) -> None:
        self.stopped = True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    outcomes: list[Any] | None = None,
    launch_error: BaseException | None = None,
    start_error: BaseException | None = None,
    new_page_error: BaseException | None = None,
) -> _FakePlaywright:
    """Point the launcher seam at a fake browser and the cache dir at tmp_path.

    One knob per step of the launch — driver start, context launch, page open —
    because `_ensure_page` promises the same typed refusal for all three and
    only a knob at each can hold it to that."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv(gfb._BROWSER_BIN_ENV, raising=False)
    page = _FakePage(
        outcomes
        if outcomes is not None
        else [_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)]
    )
    pw = _FakePlaywright(
        _FakeChromium(_FakeContext(page, new_page_error), launch_error), start_error
    )

    def _sync_playwright() -> _FakePlaywright:
        return pw

    def _factory() -> Callable[[], _FakePlaywright]:
        return _sync_playwright

    monkeypatch.setattr(gfb, "_playwright_factory", _factory)
    return pw


def _page_of(pw: _FakePlaywright) -> _FakePage:
    return pw.chromium._context._page


# ─────────────── the split: fetching says nothing, parsing says everything ──────


class _FakeHttpResponse:
    """What fli's session hands back: a body, the URL it settled on, and the
    status Google answered with — unread and untranslated."""

    def __init__(self, *, text: str, url: str, status_code: int = 200) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code


def test_the_rung_reports_the_status_it_was_served(
    monkeypatch: pytest.MonkeyPatch, gf_session: Callable[..., Any]
) -> None:
    """`_fetch_page` hands on the body, the final URL and the status, and rules
    on none of them.

    Rung 1 goes around fli's `Client.get` and the `raise_for_status()` inside
    it, so a 429 comes back as a RESPONSE and travels to `_rows_from_page_html`
    — the one place that ranks it against the interstitial, for both rungs at
    once. Translating it here again would take it back out of there,
    silently."""
    fake = gf_session(_page())
    page = gfid._fetch_page(_filters(round_trip=False))
    assert page.html == _page()
    assert "tfs=" in page.final_url
    assert page.status_code == 200

    def _blocked(url: str, **_kw: object) -> _FakeHttpResponse:
        return _FakeHttpResponse(text="<html>blocked</html>", url=url, status_code=429)

    monkeypatch.setattr(fake._session_obj, "get", _blocked)
    throttled = gfid._fetch_page(_filters(round_trip=False))
    assert throttled.status_code == 429
    with pytest.raises(GfThrottledError):
        gfid._rows_from_page_html(throttled)


def test_a_server_error_is_its_own_refusal_not_a_shape_error() -> None:
    """A non-2xx degrades to Matrix through the seam every other refusal uses,
    but it is NOT a shape error: nothing was served to re-derive an extract
    from. Reading Google declining to serve as "the parser is broken" sends the
    next reader hunting an extract bug during an outage.

    Either rung gets here with a status: rung 1 reads it off the response and
    rung 2 off the navigation."""
    with pytest.raises(GfUpstreamStatusError, match="HTTP 503") as e:
        gfid._rows_from_page_html(gfid.PageFetch("", _PAGE_URL, 503))
    assert e.value.status_code == 503
    assert not isinstance(e.value, GfPageShapeError)
    # Still a GfBackendError, so the Matrix-fallback seams keep catching it.
    assert isinstance(e.value, GfBackendError)


@pytest.mark.parametrize("status", [199, 302, 304, 399])
def test_a_sub_400_non_2xx_is_the_same_refusal_as_a_500(status: int) -> None:
    """The boundary is non-2xx, not `>= 400`. Chrome reaches this with a 304 out
    of the persistent profile's cache, or a redirect it declined to follow —
    neither carries a page, so reading them as "Google Flights' page shape
    changed" is the misdiagnosis `GfUpstreamStatusError` exists to prevent, and
    it points the next reader at an extract bug during an outage."""
    with pytest.raises(GfUpstreamStatusError) as e:
        gfid._rows_from_page_html(gfid.PageFetch("", _PAGE_URL, status))
    assert e.value.status_code == status
    assert not isinstance(e.value, GfPageShapeError)
    assert isinstance(e.value, GfBackendError)


def test_the_navigation_waits_for_the_dom_so_the_body_read_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`response.text()` takes no timeout at any layer, so `goto`'s ceiling is
    the only bound rung 2 has — and `commit` returns before the body exists,
    leaving the read outside it. A default round trip makes 11 such reads."""
    pw = _install(monkeypatch, tmp_path)
    with gfb.GfBrowserSession(headed=False) as session:
        session.get_html(_PAGE_URL)
    assert _page_of(pw).gotos == [(_PAGE_URL, "domcontentloaded", 30_000)]


def test_a_throttle_outranks_the_status_check() -> None:
    """429 is both "blocked" and "not 2xx"; it has to read as the throttle,
    because that is the one the caller can back off and retry.

    Either rung can reach this with a 429, since nothing calls
    `raise_for_status()` on the way — and the interstitial it usually arrives as
    is caught by URL or body instead."""
    with pytest.raises(GfThrottledError):
        gfid._rows_from_page_html(gfid.PageFetch("", _PAGE_URL, 429))
    with pytest.raises(GfThrottledError):
        gfid._rows_from_page_html(gfid.PageFetch("", _SORRY_URL, 200))


def test_browser_bytes_and_http_bytes_reach_the_same_rows() -> None:
    """The invariant the whole rung rests on: rung 2 supplies bytes, never
    interpretation, so identical bytes must yield identical rows."""
    rows = gfid._rows_from_page_html(gfid.PageFetch(_page(), _PAGE_URL, 200))
    assert len(rows) == 3
    assert all(r.flight_id for r in rows)
    assert all(a.legroom_class for r in rows for a in r.amenities)


def test_a_flightless_board_through_the_browser_is_an_authoritative_empty() -> None:
    """Rung 2 must inherit the verdict rung 1 reaches on the same bytes: a board
    served with no row block at either index is a route with nothing matching,
    not a layout change. Refusing it would degrade a browser search to Matrix
    for a question Google already answered."""
    rows = gfid._rows_from_page_html(
        gfid.PageFetch(_page("ds1_flightless_board.json"), _PAGE_URL, 200)
    )
    assert rows == []


def test_relocated_rows_through_the_browser_are_a_shape_refusal() -> None:
    """The other half of the same inheritance: rows found off `[2]`/`[3]` is the
    one relocation a payload can prove, and it must refuse on rung 2 too rather
    than read as an empty board."""
    with pytest.raises(GfPageShapeError, match="the payload layout changed"):
        gfid._rows_from_page_html(
            gfid.PageFetch(_page("ds1_blocks_relocated.json"), _PAGE_URL, 200)
        )


# ───────────────────────────── the ladder ──────────────────────────────────────


class _RecordingSession:
    """Stands in for a `GfBrowserSession`: counts navigations and closes, serves
    fixtures. Never launches anything, so a test using it proves what the ladder
    does without a browser in the process."""

    def __init__(self, result: Any = None, *, pages: list[Any] | None = None) -> None:
        self.urls: list[str] = []
        self.closes = 0
        self._result = result
        # One body per navigation, the last repeating — a round trip's return
        # leg needs a different board from its outbound. An entry may be an
        # exception rather than a body, which is how a session that serves a
        # board and then dies is said; `result` raises from the first
        # navigation and cannot express that.
        self._pages = pages or [_page()]

    def close(self) -> None:
        self.closes += 1

    def get_html(self, url: str) -> Any:
        self.urls.append(url)
        if isinstance(self._result, Exception):
            raise self._result
        body = self._pages[min(len(self.urls) - 1, len(self._pages) - 1)]
        if isinstance(body, BaseException):
            raise body
        return gfid.PageFetch(html=body, final_url=_PAGE_URL, status_code=200)


def _filters(*, round_trip: bool, bags: Bags | None = None) -> Any:
    legs = (Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 10, 14)),)
    if round_trip:
        legs += (Leg(origins=("LAX",), destinations=("JFK",), date=date(2026, 10, 24)),)
    return to_fli_filter(SpecificDateSearch(legs=legs, options=SearchOptions(bags=bags)))


@pytest.fixture
def no_rung_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rung 1 must not run at all under `--gf-transport browser`."""

    def _forbidden(_f: Any, **_kw: Any) -> list[GFlightWithId]:
        raise AssertionError("rung 2 fell back into rung 1's GET")

    monkeypatch.setattr(gfid, "_one_call", _forbidden)


@pytest.mark.usefixtures("no_rung_one")
def test_browser_mode_costs_one_navigation_per_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _RecordingSession()

    def _hand_out(*, headed: bool) -> _RecordingSession:
        assert headed is False
        return session

    monkeypatch.setattr(gfb, "session", _hand_out)

    out = gfid.search_with_ids(
        _filters(round_trip=False), top_n=5, transport=gfid.GfTransport(mode="browser")
    )
    assert out is not None
    assert len(out) == 3
    assert len(session.urls) == 1


@pytest.mark.usefixtures("no_rung_one")
def test_a_round_trip_navigates_once_per_leg_on_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport rides the recursion, so the return legs stay on rung 2 —
    and they share the session, which is what keeps the launch amortized."""
    session = _RecordingSession(pages=[_page(), _return_page()])
    handed_out: list[bool] = []

    def _session(*, headed: bool) -> _RecordingSession:
        handed_out.append(headed)
        return session

    monkeypatch.setattr(gfb, "session", _session)

    out = gfid.search_with_ids(
        _filters(round_trip=True), top_n=1, transport=gfid.GfTransport(mode="browser")
    )
    assert out is not None
    assert len(session.urls) == 2  # outbound, then the one pinned return
    assert session.urls[0] != session.urls[1]  # the return leg pins the outbound
    assert handed_out == [False, False]  # one session object, asked for twice


@pytest.mark.usefixtures("no_rung_one")
def test_a_dead_browser_stops_the_pin_loop_rather_than_being_re_driven(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A browser that died is a fact about the SESSION, not about this pin's URL.

    Every remaining pin navigates on that same dead Chrome, so re-driving it
    cannot succeed and pays the navigation ceiling per pin — at the default
    `-n 10` that is ten timeouts for one already-known answer. It also reports
    one process failure as N independent board refusals, and that count is a
    sentence the user reads.

    A refusal OF THIS URL still continues: a re-shaped board, a consent wall or
    a 503 says nothing about the next pin. The line between the two arms is
    whether the failure is about the URL or about the session."""
    session = _RecordingSession(pages=[_page(), GfBrowserUnavailableError("Chrome died.")])

    def _session(*, headed: bool) -> _RecordingSession:
        return session

    monkeypatch.setattr(gfb, "session", _session)

    with (
        caplog.at_level("WARNING", logger="flight_cli._gflight_ids"),
        pytest.raises(GfBrowserUnavailableError) as e,
    ):
        gfid.search_with_ids(
            _filters(round_trip=True), top_n=3, transport=gfid.GfTransport(mode="browser")
        )
    # The outbound board, then ONE pin that met the dead session. Three rows
    # were pinnable, so re-driving would show 4.
    assert len(session.urls) == 2, session.urls
    assert "Chrome died." in str(e.value)
    # The count that was wrong: one dead session is not N board refusals.
    assert "return boards unavailable" not in caplog.text, caplog.text


@pytest.mark.usefixtures("no_rung_one")
def test_a_browser_that_dies_after_a_served_pin_still_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One served pin turns the same death into a warning, and that one line is
    then the ONLY place the user learns Chrome died.

    A partial round trip is a success by contract — exit 0, a table, no raise —
    so nothing downstream will say it again. "was unreachable" describes a
    network and sends the reader to check one; the fix here is local, and it is
    written down on the exception, so the line carries the exception's own
    reason and remedy rather than a fixed phrase.

    The sibling above is the other arm of the same `if`: the two differ by
    exactly one served pin."""
    session = _RecordingSession(
        pages=[_page(), _return_page(), GfBrowserUnavailableError("Chrome died.")]
    )

    def _session(*, headed: bool) -> _RecordingSession:
        return session

    monkeypatch.setattr(gfb, "session", _session)

    with caplog.at_level("WARNING", logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(
            _filters(round_trip=True), top_n=3, transport=gfid.GfTransport(mode="browser")
        )
    assert out is not None
    assert len(out) == 3  # pin 1's combinations, kept
    assert len(session.urls) == 3, session.urls  # outbound, pin 1 served, pin 2 dead
    assert "Chrome died." in caplog.text, caplog.text  # the reason
    assert "--gf-transport http" in caplog.text, caplog.text  # the remedy, both halves
    assert "--backend matrix" in caplog.text, caplog.text
    assert "was unreachable" not in caplog.text, caplog.text  # not the wrong diagnosis
    assert "2 of 3 return boards skipped" in caplog.text, caplog.text  # the served count


@pytest.mark.usefixtures("no_rung_one")
def test_under_bags_a_browser_that_dies_after_a_served_pin_points_at_dropping_them(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Matrix prices no bags, so under `--bags` the remedy's last resort is
    dropping them, not `--backend matrix`, which `--bags` refuses."""
    session = _RecordingSession(
        pages=[_page(), _return_page(), GfBrowserUnavailableError("Chrome died.")]
    )

    def _session(*, headed: bool) -> _RecordingSession:
        return session

    monkeypatch.setattr(gfb, "session", _session)

    with caplog.at_level("WARNING", logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(
            _filters(round_trip=True, bags=Bags(checked=1)),
            top_n=3,
            transport=gfid.GfTransport(mode="browser"),
        )
    assert out is not None
    assert len(out) == 3
    assert "Chrome died." in caplog.text, caplog.text
    assert "--gf-transport http" in caplog.text, caplog.text
    assert "--backend matrix" not in caplog.text, caplog.text
    assert "drop `--bags`" in caplog.text, caplog.text


@pytest.mark.parametrize("mode", ["http", "auto"])
def test_the_http_rungs_never_consult_the_browser(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """`auto` is documented as identical to `http` until the escalation rung
    lands; this is what makes that documentation true."""

    def _forbidden(*, headed: bool) -> object:
        raise AssertionError(f"mode={mode} reached the browser rung (headed={headed})")

    def _rung_one(_filters_arg: Any, **_kw: Any) -> list[GFlightWithId]:
        return gfid._rows_from_page_html(gfid.PageFetch(_page(), _PAGE_URL, 200))

    monkeypatch.setattr(gfb, "session", _forbidden)
    monkeypatch.setattr(gfid, "_one_call", _rung_one)
    out = gfid.search_with_ids(
        _filters(round_trip=False),
        top_n=5,
        transport=gfid.GfTransport(mode=cast("Any", mode)),  # parametrized as a plain str
    )
    assert out is not None
    assert len(out) == 3


def test_a_browser_refusal_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 4: rung 2 does not fall back into rung 1's retry ladder. Under
    an explicit `--gf-transport browser` the user asked for the browser, and
    quietly spending ~22 s of curl_cffi backoff instead would be a different
    search than the one they ran."""
    session = _RecordingSession(GfBrowserUnavailableError("Chrome died."))

    def _hand_out(*, headed: bool) -> _RecordingSession:
        assert headed is False
        return session

    def _rung_one(_filters_arg: Any, **_kw: Any) -> list[GFlightWithId]:
        pytest.fail("rung 2 fell back into rung 1's retry ladder")

    monkeypatch.setattr(gfb, "session", _hand_out)
    monkeypatch.setattr(gfid, "_one_call", _rung_one)

    with pytest.raises(GfBrowserUnavailableError):
        gfid.search_with_ids(_filters(round_trip=False), transport=gfid.GfTransport(mode="browser"))
    assert len(session.urls) == 1  # one navigation, no retry


# ───────────────────── the session: launch, navigate, close ────────────────────


def test_a_navigation_becomes_rows_and_the_launch_is_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pw = _install(monkeypatch, tmp_path)
    with gfb.GfBrowserSession(headed=False) as session:
        fetch = session.get_html(_PAGE_URL)
        session.get_html(_PAGE_URL)

    assert fetch.status_code == 200
    rows = gfid._rows_from_page_html(fetch)
    assert len(rows) == 3  # the navigation's bytes go straight into the one parser
    assert _page_of(pw).gotos == [(_PAGE_URL, "domcontentloaded", 30_000)] * 2
    # The claim is "one launch, TWO navs". Without these two counters the first
    # half was asserted by nothing: a session that relaunched Chrome on every
    # navigation would have passed every other line in this test.
    assert pw.chromium.launches == 1
    assert pw.starts == 1
    assert capsys.readouterr().err.count("opening Chrome") == 1
    assert pw.chromium._context.closed and pw.stopped


def test_the_context_is_launched_against_real_chrome_and_our_own_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`channel="chrome"` is the whole point — a bundled Chromium has the
    fingerprint rung 1 already has. And the profile is ours: pointing it at the
    PointsPath login profile would hand Google a logged-in session and collide
    with the login flow over Chromium's single-instance lock."""
    from flight_cli.pp.auth import BROWSER_PROFILE_DIR

    pw = _install(monkeypatch, tmp_path)
    with gfb.GfBrowserSession(headed=True) as session:
        session.get_html(_PAGE_URL)

    kwargs = pw.chromium.launch_kwargs
    assert kwargs["channel"] == "chrome"
    assert "executable_path" not in kwargs
    assert kwargs["headless"] is False
    assert kwargs["user_data_dir"] == str(tmp_path / "gf-browser-profile")
    assert pathlib.Path(kwargs["user_data_dir"]) != BROWSER_PROFILE_DIR


def test_an_explicit_binary_replaces_the_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """patchright rejects `channel` and `executable_path` together, so the
    override has to displace it rather than join it."""
    pw = _install(monkeypatch, tmp_path)
    monkeypatch.setenv(gfb._BROWSER_BIN_ENV, "/opt/chrome")
    with gfb.GfBrowserSession(headed=False) as session:
        session.get_html(_PAGE_URL)

    assert pw.chromium.launch_kwargs["executable_path"] == "/opt/chrome"
    assert "channel" not in pw.chromium.launch_kwargs


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (RuntimeError("Timeout 30000ms exceeded.\ncall log:\n  - navigating"), "could not load"),
        (None, "no response"),
    ],
)
def test_a_failed_navigation_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, outcome: Any, expected: str
) -> None:
    """A timeout and a null response are the same fact to the caller — rung 2
    produced no bytes — and neither says anything about the route."""
    _install(monkeypatch, tmp_path, outcomes=[outcome])
    # Two statements because they are two things: the session's lifetime, and
    # the claim about what happens inside it. One `with` reads as though the
    # session itself were what raises.
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfBrowserUnavailableError, match=expected) as e:
            session.get_html(_PAGE_URL)
    # The remedy travels in the message, because both refusal renderers in
    # `cli` print `str(e)` and nothing else — and it reads as a sentence.
    assert ". Retry, or use `--gf-transport http`" in str(e.value)


def test_an_unreadable_body_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _install(
        monkeypatch,
        tmp_path,
        outcomes=[
            _FakeResponse(
                body="", url=_PAGE_URL, status=200, body_error=RuntimeError("body unavailable")
            )
        ],
    )
    # Two statements because they are two things: the session's lifetime, and
    # the claim about what happens inside it. One `with` reads as though the
    # session itself were what raises.
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfBrowserUnavailableError, match="body could not be read"):
            session.get_html(_PAGE_URL)


def test_a_launch_failure_names_the_browser_not_the_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _install(monkeypatch, tmp_path, launch_error=RuntimeError("Chromium is not installed"))
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError, match="failed to launch") as e:
        session.get_html(_PAGE_URL)
    assert "Chromium is not installed" in str(e.value)


def test_a_locked_profile_says_so_and_says_how_to_clear_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Chromium single-instances a profile dir. Two ways in: a second `flight`
    running right now, or a run killed mid-navigation — the second is the one a
    user cannot diagnose without being told, so both are in the message.

    The lock file is a symlink to a possibly-dead pid, which is exactly why the
    check cannot be `Path.exists()`."""
    profile = tmp_path / "gf-browser-profile"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to("some-host-4242")
    _install(monkeypatch, tmp_path, launch_error=RuntimeError("ProcessSingleton failed"))

    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError) as e:
        session.get_html(_PAGE_URL)
    message = str(e.value)
    assert "interrupted run" in message
    assert f"{profile}/Singleton*" in message
    assert not (profile / "SingletonLock").exists()  # dangling: exists() is the wrong test
    assert (profile / "SingletonLock").is_symlink()


def test_a_failed_launch_leaves_no_driver_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The driver process starts before the browser does; a launch that raises
    after that has to take it back down or the CLI hangs on exit."""
    pw = _install(monkeypatch, tmp_path, launch_error=RuntimeError("nope"))
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError):
        session.get_html(_PAGE_URL)
    assert pw.stopped


@pytest.mark.parametrize(
    ("start_error", "new_page_error", "driver_stopped"),
    [
        (RuntimeError("driver handshake failed"), None, False),
        (None, RuntimeError("target page closed"), True),
    ],
    ids=["start", "new_page"],
)
def test_the_other_two_launch_steps_are_the_same_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    start_error: BaseException | None,
    new_page_error: BaseException | None,
    driver_stopped: bool,
) -> None:
    """The launch is three steps and the caller is promised one refusal for all
    of them: they cannot act on the distinctions patchright draws.

    The driver state is where the two genuinely differ, and it is asserted
    rather than smoothed over. `self._playwright` is assigned only after
    `start()` RETURNS, so a `start()` that raises leaves `close()` nothing to
    stop; a `new_page()` that raises has a driver up and it is stopped on the
    way out. That asymmetry is what the code does today — whether real
    patchright strands a node process in the first window is not decidable
    from here."""
    pw = _install(monkeypatch, tmp_path, start_error=start_error, new_page_error=new_page_error)
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError):
        session.get_html(_PAGE_URL)
    assert pw.stopped is driver_stopped


def test_an_uncreatable_profile_dir_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The step before the launch, and the same promise. A read-only or
    unwritable cache dir is a refusal naming the directory, not an `OSError`
    reaching the CLI's generic handler as an unexplained failure."""
    _install(monkeypatch, tmp_path)

    def _denied(*_a: Any, **_kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(pathlib.Path, "mkdir", _denied)
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError, match="could not be created") as e:
        session.get_html(_PAGE_URL)
    assert str(tmp_path / "gf-browser-profile") in str(e.value)


@pytest.mark.gf_browser
def test_a_missing_patchright_names_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional dependency is the most likely reason rung 2 never runs, so
    the refusal is the only place a user learns what to install."""
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    with pytest.raises(GfBrowserUnavailableError, match="patchright") as e:
        gfb._playwright_factory()
    assert "flight-cli[browser]" in str(e.value)


def test_close_is_idempotent_and_survives_a_failing_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`close` runs from a `finally`; a raise there would replace the search's
    own error with a teardown one."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    session.get_html(_PAGE_URL)

    def _explode() -> None:
        raise RuntimeError("context already gone")

    monkeypatch.setattr(pw.chromium._context, "close", _explode)
    session.close()
    session.close()
    assert pw.stopped


def test_a_ctrl_c_in_teardown_still_stops_the_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The regression `_swallow`'s `BaseException` catch exists to prevent: an
    interrupt escaping the context close would skip `playwright.stop()`, which
    is what kills the node driver, and leave Chrome running as an orphan.

    A `RuntimeError` here proves nothing — the narrower `except Exception` this
    replaced caught that identically."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    session.get_html(_PAGE_URL)

    def _interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pw.chromium._context, "close", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        session.close()
    assert pw.stopped


def test_a_ctrl_c_in_teardown_is_not_dropped_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`--fast --gf-transport browser` tears Chrome down after a SUCCESSFUL
    search, on the main thread, where a Ctrl-C can land. Swallowing it there
    would have the CLI carry on and render a table for a run the user already
    asked to stop.

    Ordering is the whole point: the interrupt surfaces only after both teardown
    steps have run, so stopping the run never costs a stranded browser."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    session.get_html(_PAGE_URL)
    order: list[str] = []

    def _interrupt() -> None:
        order.append("context")
        raise KeyboardInterrupt

    def _stop() -> None:
        order.append("driver")
        pw.stopped = True

    monkeypatch.setattr(pw.chromium._context, "close", _interrupt)
    monkeypatch.setattr(pw, "stop", _stop)
    with pytest.raises(KeyboardInterrupt):
        session.close()
    assert order == ["context", "driver"]
    # Idempotent afterwards: the second close has nothing left and must not
    # re-raise an interrupt the caller has already seen.
    session.close()


# ── a Ctrl-C on the way IN, at each of the three steps that catch broadly ──────
# Teardown is pinned above. These three are the other side of it: the search
# itself, where the catches are `except Exception` on purpose. A Ctrl-C is not
# an `Exception`, so it escapes them — and it must, because a refusal degrades
# to Matrix and finishes with a table, which is the one thing a user who asked
# the process to stop must not be handed.


def test_a_ctrl_c_during_the_navigation_is_not_turned_into_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The likeliest place for an interrupt: a navigation with a 30 s ceiling.

    Widened to `BaseException` the catch would hand back "Chrome could not load
    Google Flights' search page: KeyboardInterrupt", the CLI would degrade to
    Matrix, and a run the user stopped would end in a table."""
    _install(monkeypatch, tmp_path, outcomes=[KeyboardInterrupt()])
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(KeyboardInterrupt) as e:
        session.get_html(_PAGE_URL)
    assert not isinstance(e.value, GfBrowserUnavailableError)
    session.close()


def test_a_ctrl_c_during_the_body_read_is_not_turned_into_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The same property one step later. The page answered; reading it is still
    a place the user's interrupt can land, and it is still their instruction
    rather than a fact about Google Flights."""
    _install(
        monkeypatch,
        tmp_path,
        outcomes=[
            _FakeResponse(body="", url=_PAGE_URL, status=200, body_error=KeyboardInterrupt())
        ],
    )
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(KeyboardInterrupt) as e:
        session.get_html(_PAGE_URL)
    assert not isinstance(e.value, GfBrowserUnavailableError)
    session.close()


def test_a_ctrl_c_during_the_launch_is_not_turned_into_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """And once more on the way in. The launch block is the broadest catch of
    the three — every distinction patchright draws collapses into one refusal
    there — so it is the one where an interrupt is most easily lost.

    What the interrupt leaves behind is the other half of the contract. The
    driver process is STOPPED and the sync API is never driven again: an
    interrupt that unwinds a patchright call kills the greenlet running that
    call's event loop, so a `context.close()` afterwards spins on a dead greenlet
    until something kills the process. Both halves are asserted, because only one
    of them can fail on its own — `pw.stopped` is set only by the healthy
    `close()`, which a dead session never runs, so it holds whether or not
    anything was stopped, and the kill is what says which."""
    pw = _install(monkeypatch, tmp_path, launch_error=KeyboardInterrupt())
    killed: list[tuple[int, int]] = []
    fake_pid = 424242
    monkeypatch.setattr(gfb, "_driver_process_id", _fixed_pid(fake_pid))
    monkeypatch.setattr(gfb.os, "kill", _record_kill(killed))
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(KeyboardInterrupt) as e:
        session.get_html(_PAGE_URL)
    assert not isinstance(e.value, GfBrowserUnavailableError)
    session.close()
    # the sync API is never driven after the interrupt unwound it …
    assert pw.stopped is False
    # … but the driver is stopped rather than dropped
    assert killed == [(fake_pid, signal.SIGKILL)]


@pytest.mark.parametrize(
    "escaping",
    [asyncio.CancelledError, SystemExit, GeneratorExit],
    ids=["CancelledError", "SystemExit", "GeneratorExit"],
)
def test_a_base_exception_that_is_not_the_user_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, escaping: type[BaseException]
) -> None:
    """Teardown hands back a `KeyboardInterrupt` and nothing else. It is the only
    one of these that is the user's instruction rather than somebody else's
    control flow, and teardown is not where the rest get decided.

    A re-raised `asyncio.CancelledError` would escape the enriched path's
    `except Exception` and cancel the task group, taking a Matrix query that was
    still running and still authoritative with it. `SystemExit` and
    `GeneratorExit` belong to the interpreter and to the generator that raised
    them. Every one of them still has to leave the driver stopped."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    session.get_html(_PAGE_URL)

    def _explode() -> None:
        raise escaping

    monkeypatch.setattr(pw.chromium._context, "close", _explode)
    session.close()
    assert pw.stopped


def test_the_driver_stops_even_when_the_context_step_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`_swallow` catches everything it is handed, so the only way the first step
    escapes is a signal delivered in the gap between the two calls. The `finally`
    is what covers that window: without it the interrupt leaves the node driver
    running, and Chrome outlives the process that launched it.

    Raised from the seam rather than sent as a real signal — a test that raced
    `os.kill` against two function calls would pass or fail on timing."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    session.get_html(_PAGE_URL)
    real = gfb._swallow
    steps: list[str] = []

    def _interrupt_after_the_context(
        what: str, shutdown: Callable[[], object]
    ) -> KeyboardInterrupt | None:
        steps.append(what)
        result = real(what, shutdown)
        if what == "context":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(gfb, "_swallow", _interrupt_after_the_context)
    with pytest.raises(KeyboardInterrupt):
        session.close()
    assert steps == ["context", "driver"]
    assert pw.stopped


def test_a_round_trip_pays_for_one_launch_and_navigates_per_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The economics of the rung: the launch is the expensive part (~2-5 s) and
    a round trip pays it once, however many legs it pins. Driven through the
    real `GfBrowserSession` on a fake playwright, so the launch count is a
    measurement rather than a restatement of the ladder's monkeypatching."""
    pw = _install(
        monkeypatch,
        tmp_path,
        outcomes=[
            _FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None),
            _FakeResponse(body=_return_page(), url=_PAGE_URL, status=200, body_error=None),
        ],
    )
    monkeypatch.setattr(gfb, "_sessions", threading.local())

    def _rung_one(_f: Any, **_kw: Any) -> list[GFlightWithId]:
        raise AssertionError("rung 2 fell back into rung 1's GET")

    monkeypatch.setattr(gfid, "_one_call", _rung_one)
    out = gfid.search_with_ids(
        _filters(round_trip=True), top_n=1, transport=gfid.GfTransport(mode="browser")
    )
    assert out is not None
    assert pw.chromium.launches == 1
    assert pw.starts == 1
    assert len(_page_of(pw).gotos) == 2  # outbound, then the one pinned return
    assert capsys.readouterr().err.count("opening Chrome") == 1
    gfb.close_thread_session()


def test_an_unreadable_profile_dir_is_treated_as_unlocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The contract: an `OSError` reading the profile means UNLOCKED.

    Erring the other way would refuse the rung outright and tell the user to
    delete files the process just proved it cannot see. Reporting "not locked"
    lets the launch proceed and produce the real error, whatever it is."""
    profile = tmp_path / "gf-browser-profile"
    profile.mkdir(parents=True)

    def _boom(_self: pathlib.Path, _pattern: str) -> object:
        raise PermissionError("profile is not readable")

    monkeypatch.setattr(pathlib.Path, "glob", _boom)
    assert gfb._profile_is_locked(profile) is False
    # And the launch failure that follows names the real cause, not the lock.
    e = gfb._launch_failure(profile, "Chromium distribution 'chrome' is not found.")
    assert "Singleton*" not in str(e)


# ─────────────────── one session per thread, closed by that thread ─────────────


def test_a_session_is_reused_within_a_thread_and_private_to_it() -> None:
    """Bound to the creating thread because the caller runs inside
    `anyio.to_thread.run_sync` and playwright objects are not thread-safe."""
    mine = gfb.session(headed=False)
    assert gfb.session(headed=False) is mine

    theirs: list[Any] = []
    worker = threading.Thread(target=lambda: theirs.append(gfb.session(headed=False)))
    worker.start()
    worker.join()
    assert theirs[0] is not mine


def test_closing_a_thread_with_no_session_is_a_noop() -> None:
    """Called from `_gflight_results`' `finally` on every non-http run, whether
    or not a leg ever reached the browser."""
    gfb.close_thread_session()
    gfb.close_thread_session()


def test_closing_clears_the_slot_so_the_next_search_relaunches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    def _record_close(_self: gfb.GfBrowserSession) -> None:
        closed.append(True)

    monkeypatch.setattr(gfb.GfBrowserSession, "close", _record_close)
    first = gfb.session(headed=False)
    gfb.close_thread_session()
    assert closed == [True]
    assert gfb.session(headed=False) is not first


# ───────────── the finally that owns the session's life ───────────────────────


def _drive_gflight_results(
    monkeypatch: pytest.MonkeyPatch, *, mode: GfTransportMode, blow_up: Exception | None = None
) -> list[int]:
    """Run the real `_gflight_results` on `mode` with rung 2 replaced by a
    recorder, and report how many times its session was closed.

    Calls the function under test rather than the CLI seam above it: every
    existing browser test stubs `_run_gflight_path` out entirely, which is why
    the `finally` that closes Chrome had no coverage at all."""
    from flight_cli import cli

    closed: list[int] = []

    class _Session:
        def close(self) -> None:
            closed.append(1)

    session = _Session()

    def _hand_out(*, headed: bool) -> _Session:
        assert headed is False
        return session

    monkeypatch.setattr(gfb, "session", _hand_out)
    monkeypatch.setattr(gfb, "_sessions", threading.local())
    # `close_thread_session` closes the THREAD's session, so the thread-local
    # has to hold the same object the ladder was handed.
    gfb._sessions.current = session

    def _search(*_a: Any, **_kw: Any) -> list[Any]:
        if blow_up is not None:
            raise blow_up
        return []

    monkeypatch.setattr(gfid, "search_with_ids", _search)
    legs = (Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 10, 14)),)
    try:
        cli._gflight_results(legs, SearchOptions(), 5, mode, False)
    except Exception as e:  # the raising case is half the contract
        assert e is blow_up
    return closed


def test_the_browser_session_is_closed_when_the_search_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playwright object may only be closed by its creating thread, and the
    enrich path runs the query in an `anyio` worker — so this `finally` is the
    only guarantee a Chrome does not outlive the search."""
    assert _drive_gflight_results(monkeypatch, mode="browser") == [1]


def test_the_browser_session_is_closed_when_the_search_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that matters more: a refusal mid-search must not strand Chrome.
    The exception still propagates — the close is not allowed to replace it."""
    assert _drive_gflight_results(monkeypatch, mode="browser", blow_up=RuntimeError("boom")) == [1]


@pytest.mark.parametrize("mode", ["http", "auto"])
def test_an_http_search_never_reaches_for_the_closer(
    monkeypatch: pytest.MonkeyPatch, mode: GfTransportMode
) -> None:
    """Rung 1 opens nothing to close. The call would be a no-op, but reaching
    for it at all would read as though an http search might hold a Chrome —
    the one thing this transport promises it never does.

    `auto` too: it is documented four times over as identical to http, and the
    ladder maps it to rung 1. A gate that treated it as a possible Chrome
    holder would make one of those two statements false."""
    assert _drive_gflight_results(monkeypatch, mode=mode) == []


# ─────────────────────────────── the guard itself ──────────────────────────────


def test_the_suite_refuses_to_launch_a_real_browser() -> None:
    """Proof that `conftest.py`'s guard is armed for an unmarked test: without
    it, this call would start a driver process and open Chrome.

    It fails with pytest's own `Failed`, which is a `BaseException` — so the
    `except Exception` wrappers in `_gf_browser` and `cli` cannot swallow it."""
    with pytest.raises(BaseException, match="real browser launcher") as e:
        gfb._playwright_factory()
    assert not isinstance(e.value, Exception)


@pytest.mark.parametrize("label", ["gf_browser", "plain"])
def test_a_parametrize_id_cannot_disarm_the_browser_guard(label: str) -> None:
    """`request.keywords` is not the marker set — it also carries node names,
    parametrize ids and the containing directory. On that predicate THIS test,
    on its first parameter, opted itself out of the guard without a marker and
    could have reached the real launcher and the live network.

    The `plain` parameter is the control: both must be guarded identically."""
    assert label in {"gf_browser", "plain"}
    with pytest.raises(BaseException, match="real browser launcher"):
        gfb._playwright_factory()


# ───────────────────────────── the CLI option ──────────────────────────────────


@pytest.mark.parametrize("mode", gfc.VALID_TRANSPORT_MODES)
def test_every_documented_transport_resolves(mode: str) -> None:
    """Parametrized over the accepted set itself rather than a copy of it, so a
    rung added to `GfTransportMode` arrives here without anyone remembering to
    list it."""
    assert _resolve_gf_transport(mode) == mode


def test_an_unknown_transport_is_rejected_by_name() -> None:
    import typer

    with pytest.raises(typer.BadParameter, match="chrome"):
        _resolve_gf_transport("chrome")


def test_the_cli_and_the_ladder_name_the_same_transports() -> None:
    """One definition, said out loud: the CLI, the ladder and the leaf hold the
    same objects rather than three copies that happen to agree today.

    A fourth spelling of the three modes, pinned to `GfTransportMode` by
    nothing, would put a rename on either side at `_one_call_laddered`'s runtime
    raise — reaching the first user who happened to pick the renamed mode
    rather than this suite."""
    from flight_cli import cli

    assert gfid.GfTransportMode is gfc.GfTransportMode
    assert cli.VALID_TRANSPORT_MODES is gfc.VALID_TRANSPORT_MODES
    assert set(cli.VALID_TRANSPORT_MODES) == {"auto", cli.TRANSPORT_HTTP, cli.TRANSPORT_BROWSER}
    for mode in cli.VALID_TRANSPORT_MODES:
        assert gfid.GfTransport(mode=mode).mode == mode


@pytest.mark.parametrize("mode", gfc.VALID_TRANSPORT_MODES)
def test_every_documented_transport_has_a_rung(
    monkeypatch: pytest.MonkeyPatch, mode: GfTransportMode
) -> None:
    """`assert_never` makes a forgotten rung a basedpyright error; this is the
    runtime half of the same claim. A mode the CLI accepts and the ladder has no
    branch for reaches the user as a bare `ValueError` out of the middle of a
    query."""
    rungs: list[str] = []

    def _http(_filters: Any, **_kw: Any) -> list[Any]:
        rungs.append("http")
        return []

    def _browser(_filters: Any, *, headed: bool, **_kw: Any) -> list[Any]:
        assert headed is False
        rungs.append("browser")
        return []

    monkeypatch.setattr(gfid, "_one_call_with_retry", _http)
    monkeypatch.setattr(gfid, "_one_call_browser", _browser)
    assert gfid._one_call_laddered(cast("Any", None), gfid.GfTransport(mode=mode)) == []
    assert len(rungs) == 1, f"{mode} reached {rungs} rungs"


def test_resolving_a_transport_does_not_load_the_google_flights_stack() -> None:
    """Every search validates this flag, including Matrix-only ones, so it must
    not drag in `_gflight_ids` — that module costs fli's import, and a Matrix
    search has no use for it. `_gflight_results` builds the value instead.

    `_gf_common` is loaded, and that is the point of it: the vocabulary this
    validates against lives in a leaf that imports only the standard library, so
    naming and narrowing the modes costs a Matrix search nothing."""
    import subprocess
    import sys

    probe = (
        "import sys, flight_cli.cli as c;"
        "c._resolve_gf_transport('browser');"
        "print(sorted(m for m in sys.modules if m.startswith('flight_cli._gf')));"
        "print(any(m == 'fli' or m.startswith('fli.') for m in sys.modules))"
    )
    out = subprocess.run(  # noqa: S603 — argv is this interpreter and a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded, fli_loaded = out.stdout.strip().splitlines()
    assert loaded == "['flight_cli._gf_common', 'flight_cli._gf_errors']"
    # The module list is a proxy for the cost; this is the cost. A future import
    # that reaches fli by some other route would keep the line above green.
    assert fli_loaded == "False"


def test_importing_rung_two_does_not_import_patchright() -> None:
    """What makes `_gflight_ids`' plain `from . import _gf_browser` free, and
    what the deferred import it replaced claimed falsely: rung 2's MODULE costs
    no optional dependency. The guarded import lives inside
    `_playwright_factory` and runs at launch.

    Run in a subprocess because this suite has already imported both. The first
    half of the assertion is what keeps the second honest — patchright is in the
    dev group, so "not loaded" is a fact about the import and not about a
    missing package."""
    import subprocess
    import sys

    probe = (
        "import importlib.util, sys, flight_cli._gf_browser;"
        "print(importlib.util.find_spec('patchright') is not None,"
        " any(m.startswith('patchright') for m in sys.modules))"
    )
    out = subprocess.run(  # noqa: S603 — argv is this interpreter and a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True False"


def test_the_refusal_note_carries_the_remedy_too() -> None:
    """The enrich path is the DEFAULT search and prints only the note.

    So the note is not a shorter message here — a user whose browser rung will
    not start learns what to install from this string or from nothing."""
    from flight_cli.cli import _gf_refusal

    refusal = _gf_refusal(
        GfBrowserUnavailableError("Chrome is missing.", remedy="Install the thing.")
    )
    assert "Install the thing." in refusal.note
    assert "Install the thing." in refusal.message


def _render(markup: str) -> str:
    """What a user actually sees: the markup pass rich applies before printing.

    Asserting on the template instead would pass while the terminal shows
    something else — which is exactly how `[browser]` went missing."""
    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=200, force_terminal=False, no_color=True).print(markup)
    return buf.getvalue()


def test_the_install_extra_survives_the_markup_pass() -> None:
    """`rich_markup_mode="rich"` reads `[browser]` as a style tag and deletes
    it, so an unescaped remedy prints `uv pip install 'flight-cli'` — a command
    that installs the package WITHOUT the browser extra and leaves the user
    exactly where they started. Both renderings have to survive it."""
    from flight_cli.cli import _gf_refusal

    e = GfBrowserUnavailableError(
        "Google Flights' browser rung needs patchright, which isn't installed.",
        remedy=gfb._INSTALL_HINT,
    )
    refusal = _gf_refusal(e)
    assert "flight-cli[browser]" in _render(f"[dim]{refusal.note}[/]")
    assert "flight-cli[browser]" in _render(refusal.message)


def test_driver_text_carrying_markup_renders_verbatim() -> None:
    """patchright's text is arbitrary and reaches a markup-mode console. A
    closing tag it never opened raises `MarkupError` from `print`, turning a
    typed refusal — the thing that should degrade cleanly to Matrix — into a
    crash."""
    from flight_cli.cli import _gf_refusal

    e = GfBrowserUnavailableError("Chrome said [/x] no.", remedy="Then do [/y].")
    refusal = _gf_refusal(e)
    for text in (_render(f"[dim]{refusal.note}[/]"), _render(refusal.message)):
        assert "[/x]" in text
        assert "[/y]" in text


@pytest.mark.parametrize(
    ("matrix_answered", "awards_only"),
    [(True, False), (True, True), (False, False)],
    ids=["stdout", "awards-only", "matrix-silent"],
)
def test_the_default_search_path_escapes_the_note_exactly_once(
    monkeypatch: pytest.MonkeyPatch, matrix_answered: bool, awards_only: bool
) -> None:
    """The note leaves `_gf_refusal` console-ready, so the reporter prints it as
    it stands.

    Escaping it a second time does not raise — it puts a visible backslash in
    front of every bracket the driver's text carried, on the DEFAULT search
    path, which is the one place this sentence is guaranteed to be read. That
    is invisible to a test that only asserts the bracket survived, so this one
    asserts the backslash did not.

    All three arms, because they are three separate calls that each print the
    note: one is a footnote on stdout beside a Matrix table, one says the same
    news on stderr where no table will be rendered, and one is the whole
    outcome. Both streams are collected, since which one an arm writes to is
    its own property and is pinned elsewhere.

    Each arm is driven twice: once with hostile markup for the escaping, once
    with a throttle for the WORDING. The second is what holds the transport to
    travelling this far — dropped, the arm silently reverts to the http
    sentence, which names a retry ladder the browser rung does not run."""
    from rich.console import Console

    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf, width=200, force_terminal=False, no_color=True)
    )
    err_buf = capture_err(monkeypatch)
    cli._report_enriched_gf_failure(
        GfBrowserUnavailableError("Chrome said [/x] no.", remedy="Then do [/y]."),
        matrix_answered=matrix_answered,
        awards_only=awards_only,
    )

    out = buf.getvalue() + err_buf.getvalue()
    assert "[/x]" in out, out
    assert "[/y]" in out, out
    assert "\\[/x]" not in out, out
    assert "\\[/y]" not in out, out

    buf2 = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf2, width=200, force_terminal=False, no_color=True)
    )
    err_buf2 = capture_err(monkeypatch)
    cli._report_enriched_gf_failure(
        GfThrottledError("Google Flights rate-limited the request"),
        matrix_answered=matrix_answered,
        awards_only=awards_only,
        transport=cli.TRANSPORT_BROWSER,
    )
    worded = buf2.getvalue() + err_buf2.getvalue()
    assert "browser rung" in worded, worded
    assert "Wait a moment and retry" not in worded, worded


def test_the_multi_cabin_fan_out_escapes_every_cabins_note_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-cabin line renders once PER CABIN, so re-escaping it mangles the
    same sentence four times on a four-cabin fan-out.

    Asserted per cabin rather than once: an assertion that only looked for one
    clean occurrence would pass while the rest were backslashed. No transport
    is passed and none should be — the fan-out is rung 1 by design, and a
    browser mode reaching it is coerced to http before any cabin queries."""
    from flight_cli import cli

    err_buf = capture_err(monkeypatch)

    def _boom(*_a: Any, **_kw: Any) -> list[Any]:
        raise GfBrowserUnavailableError("Chrome said [/x] no.", remedy="Then do [/y].")

    monkeypatch.setattr(cli, "_gflight_results", _boom)
    results = cli._run_gflight_multi(
        legs=_one_leg(),
        opts=SearchOptions(),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        top_n=1,
    )

    out = err_buf.getvalue()
    assert results == {}  # both cabins refused, so neither column exists
    assert out.count("[/x]") == 2, out
    assert "\\[/x]" not in out, out


def _one_leg() -> tuple[Leg, ...]:
    return (Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 10, 14)),)


class _DeadMatrix:
    """A `MatrixClient` that answers nothing, so the enriched path lands on its
    early-exit branch.

    Shared, because three tests below need the Google half's report to BE the
    outcome rather than a footnote beside a Matrix table, and a Matrix client
    that answered would send them somewhere else — through a merge and a render
    of results they never built, or in the live case onto the network."""

    def __init__(self, **_kw: Any) -> None: ...

    async def __aenter__(self) -> _DeadMatrix:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *_a: Any, **_kw: Any) -> Any:
        from flight_cli.client import MatrixApiError

        raise MatrixApiError("matrix is unreachable", kind="server")


def test_a_browser_rung_throttle_reaches_the_enriched_path_in_browser_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enrich path prints the refusal the rung it RAN ON produces.

    Rung 2 has no retry ladder, so rung 1's "wait a moment and retry, use
    --gf-transport browser" would name a recovery this run does not have and a
    rung it was already using. The transport has to travel the whole way from
    the CLI option to the wording, and this is the last frame of that."""
    import typer

    from flight_cli import cli
    from flight_cli.cli import TRANSPORT_BROWSER

    buf = capture_err(monkeypatch)

    def _throttled(*_a: Any, **_kw: Any) -> list[Any]:
        raise GfThrottledError("Google Flights rate-limited the request")

    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    monkeypatch.setattr(cli, "_gflight_results", _throttled)
    with pytest.raises(typer.Exit):
        cli._run_enriched_path(
            legs=_one_leg(),
            opts=SearchOptions(),
            top_n=5,
            run_pp=False,
            sel=None,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
            gf_mode=TRANSPORT_BROWSER,
        )
    out = buf.getvalue()
    assert "browser rung" in out, out
    assert "Wait a moment and retry" not in out, out


def test_a_browser_rung_throttle_reaches_the_fast_path_in_browser_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim on `--fast`, which is a different frame and its own
    `_gf_refusal` call. Two paths carrying one transport is two chances to drop
    it, and dropping it is silent — the http wording is a valid sentence."""
    import typer

    from flight_cli import cli
    from flight_cli.cli import TRANSPORT_BROWSER

    buf = capture_err(monkeypatch)

    def _throttled(*_a: Any, **_kw: Any) -> list[Any]:
        raise GfThrottledError("Google Flights rate-limited the request")

    monkeypatch.setattr(cli, "_gflight_results", _throttled)
    with pytest.raises(typer.Exit):
        cli._run_gflight_path(
            legs=_one_leg(),
            opts=SearchOptions(),
            top_n=5,
            json_out=False,
            gf_mode=TRANSPORT_BROWSER,
        )
    out = buf.getvalue()
    assert "browser rung" in out, out
    assert "Wait a moment and retry" not in out, out


def test_an_untyped_crash_carrying_markup_survives_the_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic handler prints whatever fli or a driver put in the message,
    and that text is arbitrary. A closing tag it never opened raises
    `MarkupError` out of `print`, so a one-line "the query failed" becomes a
    traceback — on the path whose whole job is to fail readably."""
    import typer

    from flight_cli import cli

    buf = capture_err(monkeypatch)

    def _boom(*_a: Any, **_kw: Any) -> list[Any]:
        raise RuntimeError("fli said [/x] no")

    monkeypatch.setattr(cli, "_gflight_results", _boom)
    with pytest.raises(typer.Exit):
        cli._run_gflight_path(legs=_one_leg(), opts=SearchOptions(), top_n=5, json_out=False)
    assert "[/x]" in buf.getvalue()


def test_an_untyped_crash_carrying_markup_survives_the_enriched_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same trap on the DEFAULT path. Here the crash is meant to be a footnote —
    Matrix is still authoritative — so a `MarkupError` raised while printing it
    would take down a search that had another backend to fall back to.

    Matrix is failed too, so the assertion lands on the early-exit branch rather
    than on a merge and render of results this test never built."""
    import typer

    from flight_cli import cli

    buf = capture_err(monkeypatch)

    def _boom(*_a: Any, **_kw: Any) -> list[Any]:
        raise RuntimeError("fli said [/x] no")

    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    monkeypatch.setattr(cli, "_gflight_results", _boom)
    with pytest.raises(typer.Exit):
        cli._run_enriched_path(
            legs=_one_leg(),
            opts=SearchOptions(),
            top_n=5,
            run_pp=False,
            sel=None,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )
    assert "[/x]" in buf.getvalue()


def _flat_help(output: str) -> str:
    """`search --help` as a reader takes it in, with rich's layout removed.

    Two passes, and both are load-bearing. The options panel draws a border at
    each end of every line, so a phrase rich chose to wrap comes back with
    `│ │` buried in it; collapsing whitespace alone then fails on where the
    wrap landed rather than on what the line says. Any edit to a help string
    above this one moves those wrap points, so an assertion that survives only
    the current layout is not asserting the sentence."""
    return " ".join(output.replace("│", " ").split())


def test_the_help_text_keeps_the_extra_and_installs_with_uv() -> None:
    """The same markup trap, on the one line that tells a user how to get the
    rung at all. `uv` because that is this project's package manager."""
    from typer.testing import CliRunner

    from flight_cli import cli

    result = CliRunner().invoke(cli.app, ["search", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    flat = _flat_help(result.output)
    assert "uv pip install 'flight-cli[browser]'" in flat


def test_the_transport_help_describes_the_serialised_multi_cabin_run() -> None:
    """`--help` and the behaviour are one claim, so they say the same thing.

    A multi-cabin `browser` search is served, one cabin at a time, by one
    Chrome. That is slower than the parallel rung-1 fan-out by roughly an order
    of magnitude, and a flag whose help promises only the capability sells a
    ten-second wait as a free upgrade — so the price is part of the sentence.
    The http fallback is named as what it is now: what happens when Chrome
    cannot open, not what a multi-cabin search always does."""
    from typer.testing import CliRunner

    from flight_cli import cli

    result = CliRunner().invoke(cli.app, ["search", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    flat = _flat_help(result.output)
    assert "runs its cabins one at a time through a single Chrome" in flat
    assert "~10s for two cabins" in flat
    assert "falls back to http if Chrome cannot open" in flat
    # The claim this replaces. Its absence is the point: the flag no longer
    # restricts anything to single-cabin.
    assert "applies to single-cabin searches" not in flat


def test_four_browser_failures_read_as_four_different_notes(tmp_path: pathlib.Path) -> None:
    """The enrich path is the DEFAULT search and prints only the note. Built
    from the remedy alone it said "Retry" for a missing Chrome, a nav timeout, a
    null response and an unreadable body alike — four causes, one useless line,
    and the only one a user can act on never named."""
    from flight_cli.cli import _gf_refusal

    profile = tmp_path / "gf-browser-profile"  # never created: no lock to find
    failures = [
        gfb._launch_failure(profile, "Chromium distribution 'chrome' is not found."),
        GfBrowserUnavailableError("Chrome could not load the page: Timeout 30000ms exceeded."),
        GfBrowserUnavailableError("Chrome navigated but returned no response."),
        GfBrowserUnavailableError("Chrome loaded the page but its body could not be read: EOF."),
    ]
    notes = [_gf_refusal(e).note for e in failures]
    assert len(set(notes)) == len(notes)
    assert all("unavailable" in n for n in notes)
    # The launch failure is the one with a local fix, so it names Chrome.
    assert "install chrome" in notes[0]
    assert gfb._BROWSER_BIN_ENV in notes[0]


def test_a_stale_lock_does_not_claim_an_unrelated_launch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A `SIGKILL`ed run leaves `Singleton*` behind indefinitely. On the profile
    test alone every later failure then reads as a lock — so a user with no
    Chrome deletes lock files, retries, and hits the same wall with the real
    cause still unnamed. The driver text has to agree before we blame the lock."""
    profile = tmp_path / "gf-browser-profile"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to("some-host-4242")
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    assert gfb._profile_is_locked(profile)  # the stale file is really there

    e = gfb._launch_failure(profile, "Chromium distribution 'chrome' is not found.")
    assert "failed to launch" in str(e)
    assert "is not found" in str(e)
    assert "Singleton*" not in str(e)  # not blamed on the lock it did not cause


def test_a_real_lock_is_still_diagnosed_and_offers_the_http_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """The other side of the corroboration: when the driver names the singleton
    the diagnosis stands — and it now also offers `--gf-transport http`, which
    works while the other run holds the profile and the user can do nothing."""
    profile = tmp_path / "gf-browser-profile"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to("some-host-4242")

    e = gfb._launch_failure(profile, "Failed to create a ProcessSingleton for your profile.")
    assert "interrupted run" in str(e)
    assert f"{profile}/Singleton*" in str(e)
    assert "--gf-transport http" in str(e)


def test_a_browser_throttle_does_not_promise_a_retry_it_will_not_make() -> None:
    """Rung 2 runs no retry ladder, so "wait a moment and retry" describes a
    recovery the caller does not have. And neither wording blames the IP: the
    budget is per client context, which is the premise of the whole rung."""
    from flight_cli.cli import TRANSPORT_BROWSER, _gf_refusal

    http = _gf_refusal(GfThrottledError("x"))
    browser = _gf_refusal(GfThrottledError("x"), transport=TRANSPORT_BROWSER)
    assert "does not" in _render(browser.message)
    assert "--backend matrix" in _render(browser.message)
    assert "retry" in _render(http.message).lower()
    assert browser.note != http.note
    for r in (http, browser):
        assert "this IP" not in _render(r.message)


def test_a_non_2xx_offers_a_move_the_user_can_make() -> None:
    """ "Try again" is advice this arm cannot support.

    Nothing here knows whether the status repeats — a 5xx may be transient, a
    revalidated 304 will not be — so the remedy names moves instead: the other
    backend, and the other way of fetching the same page. The arm is shared by
    both rungs, which is why it offers `--gf-transport` at all and why it
    asserts nothing about stickiness.

    The `.message` is what the refusal's consumers print, so that is what is
    asserted; `_render` is what a terminal shows after rich's markup pass."""
    from flight_cli.cli import _gf_refusal

    rendered = _render(_gf_refusal(GfUpstreamStatusError(503)).message)
    assert "--gf-transport http" in rendered
    assert "--backend matrix" in rendered
    assert "try again" not in rendered.lower()


def _no_chrome_rung(seen: list[tuple[str, Any]] | None = None) -> Any:
    """A rung that refuses at the browser transport and answers over http.

    The refusal a machine with no Chrome raises, at the frame the multi-cabin
    paths reach the rung through — so the series runner, the fallback and the
    fan-out all run for real and only the network does not.

    It carries the install remedy rather than the default one because
    `_INSTALL_HINT` is the shipped remedy a markup-mode console would damage:
    its `[browser]` is a style tag. A writer that prints a constant instead of
    `e.remedy`, or prints it unescaped, is then readable off the buffer."""

    def _rung(
        _legs: Any, opts: Any, _top_n: Any, gf_mode: Any = None, _headed: Any = False
    ) -> list[Any]:
        if seen is not None:
            seen.append((str(opts.cabin.value), gf_mode))
        if gf_mode == gfc.TRANSPORT_BROWSER:
            raise GfBrowserUnavailableError(
                "Chrome failed to launch for Google Flights: no browser on this machine.",
                remedy=gfb._INSTALL_HINT,
            )
        return []

    return _rung


def _no_chrome_search(monkeypatch: pytest.MonkeyPatch, *extra: str) -> Any:
    """A two-cabin `--gf-transport browser` search on a machine with no Chrome.

    The whole CLI runs — the dispatch, the series runner, the fall-through and
    the fan-out — and only the rung is substituted, so what each node below
    reads off the streams is what a user would see."""
    from typer.testing import CliRunner

    from flight_cli import cli

    monkeypatch.setattr(cli, "_gflight_results", _no_chrome_rung())
    return CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            "2026-10-14",
            "--cabin",
            "coach,business",
            "--gf-transport",
            "browser",
            "--cash-only",
            "-n",
            "1",
            *extra,
        ],
    )


def _forbid_browser_session(monkeypatch: pytest.MonkeyPatch, why: str) -> list[bool]:
    """Make the session seam fail loudly, and hand back the log of asks.

    The rung is stubbed wherever this is used, so nothing downstream would
    notice a session being made; the seam is the only place the claim "no
    browser" can be held."""
    asked: list[bool] = []

    def _session(*, headed: bool) -> Any:
        asked.append(headed)
        raise AssertionError(why)

    monkeypatch.setattr(gfb, "session", _session)
    return asked


def test_a_multi_cabin_browser_search_says_it_is_using_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It says so where the fact is known — when rung 2 cannot open at all.

    The cabins are served one at a time by one Chrome now, so a multi-cabin
    browser search is no longer a downgrade by construction. What is still true
    is that a machine with no browser answers over http, and says so ONCE for
    the fan-out rather than once per cabin.

    Captured through `capture_err`: this line carries a driver's own text, and
    at the 80 columns rich falls back to under `CliRunner` it is several
    rendered lines, so a substring assertion would fail on where the wrap
    landed rather than on what was said."""
    buf = capture_err(monkeypatch)
    result = _no_chrome_search(monkeypatch)
    assert result.exit_code == 0, result.output
    assert buf.getvalue().count("multi-cabin is using http") == 1  # said once, not per cabin
    assert "multi-cabin" not in result.stdout


def test_an_http_multi_cabin_search_never_reaches_rung_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--gf-transport http` opens no browser, and every cabin says so.

    The positive fact the flag promises, held at the seam: on http no cabin
    reaches rung 2, and a session is never even asked for."""
    asked = _forbid_browser_session(monkeypatch, "an http search must not open a browser session")
    seen = _multi_cabin_transports(monkeypatch, "--gf-transport", "http")
    # Sorted: rung 1 runs the cabins in parallel, so their order is the
    # threadpool's rather than the user's.
    assert sorted(seen) == [("BUSINESS", "http", False), ("COACH", "http", False)]
    assert asked == []


def test_the_downgrade_note_is_not_part_of_the_json_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdout under `--format json` is one document, and the note is prose.

    Printed there it is the FIRST thing on stdout, so the document does not
    parse at all — an exit 0 a consumer cannot read. Driven through the
    no-Chrome path, which is the one that prints the line now: the whole
    fan-out is served over http, the document is the real one the fan-out
    wrote, and the parse is the assertion rather than the absence of a
    substring.

    Captured through `capture_err` for the reason the node above it is."""
    buf = capture_err(monkeypatch)
    result = _no_chrome_search(monkeypatch, "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"COACH": [], "BUSINESS": []}
    assert "multi-cabin is using http" in buf.getvalue()  # it was said, just not there


# ─────────── the option a user typed, all the way to the launch call ───────────
# The CLI-level tests above stub the dispatch and the rung-level ones call the
# rung directly, so between them sits a stretch of plumbing — four call frames
# carrying `gf_mode` and `gf_headed` — that nothing traverses. Severed anywhere
# along it, `--gf-transport browser --gf-headed` still parses, still validates,
# still exits 0, and runs rung 1 over curl_cffi: the user is handed the thin
# client they asked to replace, silently. These three tests are what joins the
# two ends.


def _transport_seen(
    monkeypatch: pytest.MonkeyPatch, *extra: str, enriched: bool = False
) -> list[tuple[Any, ...]]:
    """The `(gf_mode, gf_headed)` pair the rung entry point was actually handed.

    Recorded at `_gflight_results` because that is the last frame before the
    transport becomes a `GfTransport` — everything above it is the wire this
    exists to check, and everything below is pinned by the ladder tests.

    `enriched` picks the other of the two dispatch arms: without `--fast` and
    without `--format json` the search takes the enriched path, which reaches
    the rung through a worker thread and so carries the pair a second, separate
    way. Matrix is dead there so the run ends in the typed refusal rather than
    on the network."""
    from typer.testing import CliRunner

    from flight_cli import cli

    calls: list[tuple[Any, ...]] = []

    def _record(_legs: Any, _opts: Any, _top_n: Any, *rest: Any) -> list[Any]:
        calls.append(rest)
        return []

    monkeypatch.setattr(cli, "_gflight_results", _record)
    argv = ["search", "JFK", "LAX", "--dep", "2026-10-14", "--cash-only", "-n", "1"]
    if enriched:
        monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    else:
        argv += ["--fast", "--format", "json"]
    result = CliRunner().invoke(cli.app, [*argv, *extra])
    assert result.exit_code == (1 if enriched else 0), result.output
    return calls


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([], ("http", False)),
        (["--gf-transport", "browser"], ("browser", False)),
        (["--gf-transport", "browser", "--gf-headed"], ("browser", True)),
        (["--gf-transport", "auto", "--gf-headed"], ("auto", True)),
    ],
    ids=["default", "browser", "browser-headed", "auto-headed"],
)
def test_the_fast_path_is_handed_the_transport_the_user_named(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], expected: tuple[str, bool]
) -> None:
    """`--fast`, `--format json` and the deprecated command share this frame."""
    assert _transport_seen(monkeypatch, *extra) == [expected]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([], ("http", False)),
        (["--gf-transport", "browser"], ("browser", False)),
        (["--gf-transport", "browser", "--gf-headed"], ("browser", True)),
        (["--gf-transport", "auto", "--gf-headed"], ("auto", True)),
    ],
    ids=["default", "browser", "browser-headed", "auto-headed"],
)
def test_the_enriched_path_is_handed_the_transport_the_user_named(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], expected: tuple[str, bool]
) -> None:
    """The DEFAULT search, and a different frame: the pair crosses a thread
    boundary here, as positional worker arguments rather than keywords."""
    assert _transport_seen(monkeypatch, *extra, enriched=True) == [expected]


def _multi_cabin_transports(
    monkeypatch: pytest.MonkeyPatch, *extra: str, cabins: str = "coach,business"
) -> list[tuple[str, Any, Any]]:
    """The `(cabin, gf_mode, gf_headed)` triple EVERY cabin reached the rung with.

    `_transport_seen` above is the single-cabin model and is not reusable here:
    it asserts one recorded call and it appends `--fast`, which the multi-cabin
    dispatch does not read. This records one entry per cabin and takes the
    multi-cabin arm, where the pair crosses two more frames than it does there.

    `--format json` so the run ends in a document rather than a rendered table:
    the rung is stubbed, so there are no rows to render."""
    from typer.testing import CliRunner

    from flight_cli import cli

    calls: list[tuple[str, Any, Any]] = []

    def _record(_legs: Any, opts: Any, _top_n: Any, *rest: Any) -> list[Any]:
        calls.append((str(opts.cabin.value), *rest))
        return []

    monkeypatch.setattr(cli, "_gflight_results", _record)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            "2026-10-14",
            "--cabin",
            cabins,
            "--cash-only",
            "-n",
            "1",
            "--format",
            "json",
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    return calls


def test_every_cabin_of_a_multi_cabin_browser_search_reaches_the_rung_as_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire the multi-cabin dispatch adds: four frames from the option to
    the rung.

    Severed anywhere along it, `--gf-transport browser --cabin y,j` still
    parses, still exits 0 and quietly runs rung 1 for every cabin — the thin
    client the user asked to replace, with nothing on either stream saying so.

    In order, unlike rung 1's: the cabins are served one at a time."""
    assert _multi_cabin_transports(monkeypatch, "--gf-transport", "browser") == [
        ("COACH", "browser", False),
        ("BUSINESS", "browser", False),
    ]


def test_every_cabin_of_a_multi_cabin_auto_search_reaches_the_rung_as_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coercion's OTHER arm: only a browser mode becomes http at the fan-out.

    `auto` is http today, so replacing the conditional with the constant is
    invisible to every other check — and the escalation the conditional exists
    for would then arrive already flattened. Sorted, because this arm fans its
    cabins out in parallel."""
    assert sorted(_multi_cabin_transports(monkeypatch, "--gf-transport", "auto")) == [
        ("BUSINESS", "auto", False),
        ("COACH", "auto", False),
    ]


def test_a_headed_multi_cabin_browser_search_opens_a_headed_chrome_for_every_cabin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--gf-headed`'s VALUE, on the arm that serialises.

    Dropping the argument from the rung call is an arity error and fails
    loudly; passing the literal `False` is silent, and a run the user asked to
    watch then opens a headless Chrome and exits 0 with a full table. Only an
    assertion on the value can tell those two apart, and the single-cabin
    headed assertions are on a frame this arm does not take."""
    assert _multi_cabin_transports(monkeypatch, "--gf-transport", "browser", "--gf-headed") == [
        ("COACH", "browser", True),
        ("BUSINESS", "browser", True),
    ]


def _multi_at_rung_two(*cabins: Cabin) -> dict[Cabin, list[Any]]:
    """The multi-cabin fan-out at rung 2, on one leg — the call these nodes share.

    Called rather than dispatched through the CLI: the frames above it are
    pinned by their own nodes, and what these hold is the fan-out's own
    decisions."""
    from flight_cli import cli

    return cli._run_gflight_multi(
        legs=(Leg.of("JFK", "LAX", date(2026, 10, 14)),),
        opts=SearchOptions(cabin=cabins[0]),
        cabins=cabins,
        top_n=1,
        gf_mode=gfc.TRANSPORT_BROWSER,
    )


def test_a_multi_cabin_browser_run_enters_the_interrupt_guard_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One guard around the whole cabin list, never one per cabin.

    A guard clears the process-wide interrupt latch on its way in and restores
    SIGINT's disposition on its way out, so a guard entered per cabin would
    erase the stop the previous cabin recorded and re-arm a signal that cabin
    had set to be ignored — the second Ctrl-C would then raise straight through
    a shutdown already in progress. Three cabins, because the hazard needs a
    second entry to appear at all."""
    from flight_cli import cli

    entries: list[bool] = []
    real = gfb.interrupt_guard

    @contextlib.contextmanager
    def _counting(*, armed: bool = True) -> Generator[None]:
        entries.append(armed)
        with real(armed=armed):
            yield

    def _rung(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(gfb, "interrupt_guard", _counting)
    monkeypatch.setattr(cli, "_gflight_results", _rung)
    _multi_at_rung_two(Cabin.COACH, Cabin.BUSINESS, Cabin.FIRST)
    assert entries == [True]


@pytest.mark.gf_browser
def test_two_cabins_at_rung_two_share_one_browser_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """One Chrome for the whole cabin list — the point of serialising at all.

    A launch is the expensive part of rung 2, and Chromium single-instances the
    profile directory: a second launch that overlaps the first fails on its
    lock, and one that merely follows it races Chrome's asynchronous release of
    the `Singleton*` files. The stub closes the thread's session on its way out
    exactly as `_gflight_results` does, so what makes this one launch instead of
    two is the scope deferring that close."""
    from flight_cli import cli

    pw = _install(monkeypatch, tmp_path)

    def _rung(
        _legs: Any, _opts: Any, _top_n: Any, _mode: Any = None, headed: Any = False
    ) -> list[Any]:
        try:
            gfb.session(headed=bool(headed)).get_html(_PAGE_URL)
            return []
        finally:
            gfb.close_thread_session()

    monkeypatch.setattr(cli, "_gflight_results", _rung)
    _multi_at_rung_two(Cabin.COACH, Cabin.BUSINESS)
    assert pw.chromium.launches == 1
    assert len(_page_of(pw).gotos) == 2  # one navigation per cabin, one browser


@pytest.mark.gf_browser
def test_a_session_scope_defers_the_close_and_makes_it_once_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Inside the scope the close is deferred; on the way out it happens once.

    Deferred and not cancelled: a session the scope forgot to close leaves a
    Chrome holding the profile for the life of the process, which is the next
    run's launch failure."""
    _install(monkeypatch, tmp_path)
    closes: list[str] = []

    with gfb.session_scope():
        s = gfb.session(headed=False)
        s.get_html(_PAGE_URL)
        monkeypatch.setattr(s, "close", lambda: closes.append("close"))
        gfb.close_thread_session()
        gfb.close_thread_session()
        assert closes == []  # deferred, both times
        assert gfb.session(headed=False) is s  # and the same browser is handed back
    assert closes == ["close"]


@pytest.mark.gf_browser
def test_a_finished_session_is_never_handed_to_the_next_search_in_a_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The scope defers the close of a HEALTHY session only.

    `_ensure_page` returns the page it already has without consulting the flag,
    so a finished session handed to the next cabin is a call through a sync API
    whose greenlet is gone — measured elsewhere as a spin on a loop nobody
    drives, ended only by a kill. Pinned here rather than end to end: everything
    that sets the flag today also raises past the cabin loop, so no shipped
    handler reaches the next cabin with one."""
    _install(monkeypatch, tmp_path)

    with gfb.session_scope():
        s1 = gfb.session(headed=False)
        gfb.close_thread_session()
        assert gfb.session(headed=False) is s1  # healthy: kept for the next cabin
        s1._dead = True
        assert s1.finished
        gfb.close_thread_session()
        assert gfb.session(headed=False) is not s1  # finished: not handed on


def test_a_launch_failure_before_any_cabin_runs_the_whole_fan_out_on_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Chrome on the machine, and the answer still arrives.

    Rung 2 that cannot open at all says nothing about the query, so refusing
    the search would withhold a table http can serve. The alternative the
    fan-out must not take is returning the cabins it has: before the first one
    is served that is no cabins, and an exit 0 with an empty table reads as a
    route with no fares.

    Captured through `capture_err`, whose console is as wide as the longest
    refusal this can carry."""
    from flight_cli import cli

    buf = capture_err(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _no_chrome_rung())
    out = _multi_at_rung_two(Cabin.COACH, Cabin.BUSINESS)
    # Sorted, so the parallel fan-out's completion order is not the assertion.
    assert sorted(out) == [Cabin.BUSINESS, Cabin.COACH]  # every cabin answered, over http
    assert buf.getvalue().count("multi-cabin is using http") == 1
    # The remedy too, and the refusal's own rather than a constant the line
    # could hold instead: a run whose http rung is also refused has nothing else
    # to go on, and this line is the only one printed before the first cabin.
    assert gfb._INSTALL_HINT in buf.getvalue()
    # Rendered, not just written: `[browser]` is a style tag to the markup pass
    # typer runs, and unescaped it is deleted — leaving a command that installs
    # the package without the extra that carries the browser rung.
    assert "flight-cli[browser]" in buf.getvalue()


def test_the_fallback_fan_out_queries_with_the_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fall-through has to CHANGE the transport, not just leave the rung.

    Without that, the fan-out closes over the mode the caller asked for and
    every cabin attempts rung 2 again — measured, before this line existed: the
    fallback was announced, all three launch attempts failed, and the run ended
    at exit 1 with nothing on stdout at all.

    Captured through `capture_err` for the reason the node above it is."""
    from flight_cli import cli

    capture_err(monkeypatch)
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(cli, "_gflight_results", _no_chrome_rung(seen))
    _multi_at_rung_two(Cabin.COACH, Cabin.BUSINESS)
    assert seen[0] == ("COACH", "browser")  # the first cabin tried the rung
    # Sorted: rung 1 runs its cabins in parallel.
    assert sorted(seen[1:]) == [("BUSINESS", "http"), ("COACH", "http")]


def test_a_launch_failure_after_a_cabin_was_served_stays_a_per_cabin_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a cabin has rows, a later refusal is that cabin's note and no more.

    Re-running the fan-out would discard the rows already in hand and refill the
    table from the other rung — and a comparison whose columns came from two
    different fetches of a moving market is not one answer. So the cabin that
    failed is missing and says why, which is what every other per-cabin failure
    on this path already does.

    Captured through `capture_err`: the per-cabin refusal is the same long line
    the downgrade carries."""
    from flight_cli import cli

    buf = capture_err(monkeypatch)
    seen: list[tuple[str, Any]] = []
    raised: list[GfBrowserUnavailableError] = []

    def _rung(
        _legs: Any, opts: Any, _top_n: Any, mode: Any = None, _headed: Any = False
    ) -> list[Any]:
        seen.append((str(opts.cabin.value), mode))
        if opts.cabin is Cabin.BUSINESS:
            e = GfBrowserUnavailableError("Chrome failed to launch for Google Flights: gone.")
            raised.append(e)
            raise e
        return ["one-row"]

    monkeypatch.setattr(cli, "_gflight_results", _rung)
    out = _multi_at_rung_two(Cabin.COACH, Cabin.BUSINESS)
    assert list(out) == [Cabin.COACH]  # the served cabin is kept
    assert seen == [("COACH", "browser"), ("BUSINESS", "browser")]  # the fan-out did not re-run
    assert "multi-cabin is using http" not in buf.getvalue()
    # The whole sentence, not its prefix: what makes the note worth printing is
    # the refusal it renders, and a prefix holds while that body goes empty.
    note = cli._gf_refusal(raised[0], transport=cli.TRANSPORT_BROWSER).note
    assert f"Google Flights BUSINESS: {note}" in buf.getvalue()
    assert ".." not in buf.getvalue()  # one full stop, not the writer's on top of the note's


def test_the_downgrade_line_goes_to_stderr_and_the_table_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two streams, two audiences: the notice is for a person, stdout is the answer.

    The same rule the JSON document lives by, on the path that renders a table:
    a prose line written to stdout is inside whatever the caller is reading,
    and `--format json` is only the case where that is loudest."""
    buf = capture_err(monkeypatch)
    result = _no_chrome_search(monkeypatch)
    assert result.exit_code == 0, result.output
    assert "multi-cabin is using http" in buf.getvalue()
    assert "multi-cabin" not in result.stdout
    assert result.stdout.strip()  # the answer still arrived


@pytest.mark.usefixtures("no_rung_one")
def test_the_headed_flag_reaches_the_launch_call_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The whole wire in one run: the flags a user typed, through the dispatch,
    the rung entry, the ladder and the session, to the keyword the browser is
    actually launched with.

    `headless is False` is what `--gf-headed` MEANS, and it is read off the
    launch call rather than off anything that reports it. Nothing is stubbed
    between the option and that call; rung 1 is blocked, so a transport that
    failed to arrive shows up as no launch at all rather than as a quiet
    fallback to curl_cffi."""
    from typer.testing import CliRunner

    from flight_cli import cli

    pw = _install(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            "2026-10-14",
            "--gf-transport",
            "browser",
            "--gf-headed",
            "--fast",
            "--cash-only",
            "-n",
            "1",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert pw.chromium.launches == 1
    assert pw.chromium.launch_kwargs["headless"] is False
    assert pw.chromium.launch_kwargs["channel"] == "chrome"
    assert len(_page_of(pw).gotos) == 1


def test_a_matrix_multi_cabin_search_opens_no_browser_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--backend matrix` runs no rung at all, whatever `--gf-transport` says.

    A transport flag with no bearing on the search must not cost a browser, a
    profile lock or a Chrome launch."""
    from typer.testing import CliRunner

    from flight_cli import cli

    # One token per ARM, never one shared token: a stub that records the same
    # word on both arms holds whichever one ran, so the routing this node is
    # about is the thing it cannot see.
    dispatched: list[str] = []

    def _matrix(**_kw: Any) -> None:
        dispatched.append("matrix")

    def _gflight(**_kw: Any) -> None:
        dispatched.append("gflight")

    asked = _forbid_browser_session(monkeypatch, "a Matrix search must not open a browser session")
    monkeypatch.setattr(cli, "_run_matrix_path_multi", _matrix)
    monkeypatch.setattr(cli, "_run_gflight_path_multi", _gflight)

    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            "2026-10-14",
            "--cabin",
            "coach,business",
            "--backend",
            "matrix",
            "--gf-transport",
            "browser",
            "--cash-only",
            "-n",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert dispatched == ["matrix"]  # the Matrix arm ran, and only it
    assert asked == []


def test_a_single_cabin_browser_search_is_not_serialised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The series runner is the multi-cabin shape, and only that.

    The boundary: one cabin keeps the single-cabin path, with the one
    `interrupt_guard` entry that path already makes, rather than acquiring a
    second by being routed through a runner built for a list."""
    from flight_cli import cli

    entered: list[str] = []

    def _series(**_kw: Any) -> None:
        entered.append("series")
        raise AssertionError("a single-cabin search must not take the series runner")

    monkeypatch.setattr(cli, "_gflight_cabins_in_series", _series)
    assert _transport_seen(monkeypatch, "--gf-transport", "browser") == [("browser", False)]
    assert entered == []


def test_the_default_transport_is_rung_one() -> None:
    """A default of `browser` would put a Chrome launch and a live navigation in
    the path of every ordinary search; a default of `auto` would promise an
    escalation that does not exist yet."""
    assert gfid.HTTP_TRANSPORT.mode == "http"
    assert gfid.HTTP_TRANSPORT.headed is False
    assert gfid.GfTransport() == gfid.HTTP_TRANSPORT


def test_the_fixture_is_the_shape_the_page_serves() -> None:
    """Guards the helper above: if the fixture stops being a three-row `ds:1`
    payload, every parity assertion here becomes vacuous."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    board = gfid._rows_from_ds1(payload)
    assert len(board.rows) == 3
    assert board.blocks_seen == 2  # both row blocks present, so an empty board is authoritative
    assert board.misplaced == ()  # no relocated rows, so the parser reaches the rows at all


# ───────────────────────── stopping a browser from another thread ──────────────
# The rung's teardown is the thread that built the session, and only while that
# thread's event loop is alive. A Ctrl-C satisfies neither: on the enriched arm
# the navigation is on a worker no exception reaches, and on `--fast` the
# interrupt has already killed the greenlet the loop runs in. These pin what is
# left — a kill of the driver process — and what the session does afterwards.
#
# The fake is honest about STRUCTURE and dishonest about BLOCKING: its `goto`
# blocks in pure Python, which a signal breaks, while real patchright blocks in a
# greenlet driving an asyncio transport. So nothing here claims a timing, an
# orphan count, `Singleton*` residue or the absence of stderr noise. Those are
# measured against a real Chrome, and a fake that cannot spin cannot state them.


def _record_kill(into: list[tuple[int, int]]) -> Callable[[int, int], None]:
    """A stand-in for `os.kill` that records instead of signalling.

    Typed rather than a lambda so the checker sees the pid and the signal, and
    named once here because every stop test needs it. No test sends a real
    signal: the pids are invented, and a `SIGKILL` aimed at an invented number
    is aimed at whatever process happens to hold it."""

    def _kill(pid: int, sig: int) -> None:
        into.append((pid, sig))

    return _kill


def _fixed_pid(pid: int) -> Callable[[Any], int | None]:
    """A stand-in for the driver pid lookup that answers the way the real one
    does for a session with no manager: nothing to stop."""

    def _lookup(manager: Any) -> int | None:
        return pid if manager is not None else None

    return _lookup


def _source(owner: object, name: str) -> str:
    """The source of `owner.name`, read from a module the checker knows nothing
    about.

    patchright ships no type information, so both the attribute lookup and the
    function it yields arrive partially unknown. Taking the name rather than the
    function keeps that narrowing in one place instead of at each call."""
    return inspect.getsource(getattr(cast("Any", owner), name))


class _Recorder:
    """A context/driver pair that records the sync-API calls made on it.

    Zero calls is the claim, and a claim about something NOT happening needs a
    double that would have recorded it happening."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def close(self) -> None:
        self.calls.append("context.close")

    def stop(self) -> None:
        self.calls.append("playwright.stop")


@pytest.fixture
def keep_sigint() -> Iterator[None]:
    """Save and restore this process's SIGINT disposition around a test.

    The guard deliberately leaves `SIG_IGN` installed once an interrupt has been
    handled — for the life of the process, which in a test run is the life of the
    whole suite. Without this, the first test to trigger the handler makes Ctrl-C
    do nothing for every test after it."""
    previous = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, previous)


def test_a_dead_session_closes_without_driving_the_sync_api() -> None:
    """A stopped session's `close()` touches neither the context nor the driver.

    Driving them is what hangs: the interrupt that killed this session also
    killed the greenlet its event loop runs in, and `context.close()` then posts
    a task to a loop nobody drives and spins on that dead greenlet. The recorder
    is what makes "nothing was called" a measurement — the healthy case beside it
    proves the recorder would have seen the calls."""
    rec = _Recorder()
    session = gfb.GfBrowserSession(headed=False)
    session._context = session._playwright = rec
    session._dead = True
    session.close()
    assert rec.calls == []

    live = _Recorder()
    healthy = gfb.GfBrowserSession(headed=False)
    healthy._context = healthy._playwright = live
    healthy.close()
    assert live.calls == ["context.close", "playwright.stop"]


def test_a_dead_close_stops_the_driver_before_it_drops_the_handles() -> None:
    """*Dead* has to mean *the driver is down*, whichever thing set it.

    Three things set it and only one of them killed anything: the stop itself,
    and the two arms that record an interrupt unwinding a patchright call. Once
    the handles are dropped the driver is unreachable for good, so the stop
    cannot be left to whichever cause happened to fire."""
    killed: list[tuple[int, int]] = []
    session = gfb.GfBrowserSession(headed=False)
    session._manager = object()
    session._dead = True
    with pytest.MonkeyPatch.context() as m:
        m.setattr(gfb, "_driver_process_id", _fixed_pid(4242))
        m.setattr(gfb.os, "kill", _record_kill(killed))
        session.close()
    assert killed == [(4242, signal.SIGKILL)]
    assert session._manager is None


def test_a_stop_inside_the_context_step_stops_the_rest_of_the_teardown() -> None:
    """The window the check on the way in cannot see: the session dies INSIDE
    the first teardown step.

    `_swallow` hands the interrupt back as a return value rather than letting it
    out, so the handler's stop runs and execution carries on into the `finally`
    with the entry check already behind it. `rec.calls` is what discriminates —
    the sync API must not be driven once the greenlet its event loop runs in is
    gone, and only a double that would have recorded the call can say it was
    never made. The rest is contract.

    One entry in `killed` is a real "exactly once": the handler's
    `stop_all_drivers()` takes `_manager` on the way in, so the stop this window
    adds finds nothing left to name. The added stop is inert on the path that
    already killed, which is what lets it sit on both.

    And the interrupt still escapes. `_swallow` returned it, so the re-raise
    below the teardown is what carries it — a close that took the dead path by
    returning early would lose the user's stop instead.

    No signal is sent: `os.kill` records, like every other stop test here."""
    rec = _Recorder()
    killed: list[tuple[int, int]] = []
    session = gfb.GfBrowserSession(headed=False)
    session._context = session._playwright = rec
    session._manager = object()
    gfb._remember(session)
    real = gfb._swallow

    def _the_handler_fires_inside_the_context_step(
        what: str, shutdown: Callable[[], object]
    ) -> KeyboardInterrupt | None:
        if what != "context":
            return real(what, shutdown)
        shutdown()
        gfb.stop_all_drivers()  # what the installed SIGINT handler does …
        return KeyboardInterrupt()  # … and what `_swallow` hands back

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gfb, "_swallow", _the_handler_fires_inside_the_context_step)
        m.setattr(gfb, "_driver_process_id", _fixed_pid(4242))
        m.setattr(gfb.os, "kill", _record_kill(killed))
        with pytest.raises(KeyboardInterrupt):
            session.close()

    assert rec.calls == ["context.close"]
    assert killed == [(4242, signal.SIGKILL)]
    assert session._page is None
    assert session._context is None
    assert session._playwright is None
    assert session._manager is None
    assert session not in gfb._live


def test_a_stop_kills_the_driver_with_sigkill_and_never_asks_it_to_close() -> None:
    """`SIGKILL`, and the constant is asserted because the alternatives all work.

    `SIGTERM` stops Chrome but leaves the node driver running. `SIGINT` stops
    both — the driver has a handler for it — but that graceful path writes its
    last frames into a Python that is already unwinding, and the unhandled
    `EPIPE` puts 25 lines of Node stack on the terminal of the run the user asked
    to end. A dead driver writes nothing. Nothing else here distinguishes the
    three, so a test that did not name the signal would pass on all of them."""
    killed: list[tuple[int, int]] = []
    session = gfb.GfBrowserSession(headed=False)
    session._manager = object()

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gfb, "_driver_process_id", _fixed_pid(99))
        m.setattr(gfb.os, "kill", _record_kill(killed))
        session.stop_driver()
        assert killed == [(99, signal.SIGKILL)]
        assert session._dead is True
        # Idempotent: the manager is taken on the way in, so a second stop —
        # which a second Ctrl-C or a later `close()` makes — signals nothing.
        session.stop_driver()
    assert killed == [(99, signal.SIGKILL)]


def test_stop_all_drivers_reaches_every_registered_session() -> None:
    """The registry answers the only question a signal handler can ask: what is
    open in this PROCESS. The thread-local beside it answers "which session is
    mine", which the handler's thread cannot use — the session it has to stop
    belongs to a worker."""
    killed: list[tuple[int, int]] = []
    first = gfb.GfBrowserSession(headed=False)
    second = gfb.GfBrowserSession(headed=False)
    first._manager, second._manager = object(), object()
    gfb._remember(first)
    gfb._remember(second)
    pids = {id(first._manager): 11, id(second._manager): 22}

    def _lookup(manager: Any) -> int | None:
        return pids.get(id(manager))

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gfb, "_driver_process_id", _lookup)
        m.setattr(gfb.os, "kill", _record_kill(killed))
        gfb.stop_all_drivers()
    assert sorted(pid for pid, _ in killed) == [11, 22]
    assert first._dead and second._dead
    # The stop marks them; the close is what takes them off the register, and it
    # runs on the thread that owns each session.
    assert gfb._live == {first, second}
    first.close()
    second.close()
    assert gfb._live == set()


def test_a_session_that_never_launched_is_never_registered() -> None:
    """Constructing a session is free and opens nothing, so a stop must not have
    to know which of them got as far as a driver."""
    gfb.GfBrowserSession(headed=False)
    assert gfb._live == set()
    gfb.stop_all_drivers()  # over an empty register: a no-op, not an error


def test_a_session_this_test_registers_is_visible_to_this_test() -> None:
    """Half of a pair, and it does NOT clean up after itself.

    A test whose session reaches the launcher and then fails leaves an entry
    behind — which is exactly the shape of the run this rung is about — so the
    register has to be reset BETWEEN tests rather than by them."""
    session = gfb.GfBrowserSession(headed=False)
    gfb._remember(session)
    assert gfb._live == {session}


def test_the_registry_the_previous_test_filled_is_empty_again() -> None:
    """The other half. The register is process-wide state, like the launch notice
    and the thread-local session beside it, and the autouse fixture resets all
    three. A leaked entry would let one test's session be stopped by another
    test's interrupt — and the test that leaked it is never the one that fails."""
    assert gfb._live == set()


def test_a_session_is_registered_before_its_driver_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The register is filled before the driver exists, and that order is the
    whole reason a stop from a signal handler reaches anything.

    Driven through `_ensure_page`, which is the only path that registers: every
    other register test here calls `_remember` by hand and so cannot see the
    ordering at all. `start()` is the window — a stop arriving inside it either
    finds this session or leaves the driver that call spawned behind. A driver
    left behind there is stopped by the read after `start()` returns, before any
    browser opens; the order is what lets a stop that does arrive in time find
    anything at all."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    registered_at_start: list[bool] = []
    real_start = pw.start

    def _start() -> _FakePlaywright:
        registered_at_start.append(session in gfb._live)
        return real_start()

    monkeypatch.setattr(pw, "start", _start)
    session._ensure_page()
    assert registered_at_start == [True]
    session.close()


def test_a_launch_asked_for_after_the_stop_refuses_before_it_opens_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A stop reaches only drivers that exist, so a launch that has not started
    one is invisible to it — and on this arm the launch runs on a worker the
    interrupt never reaches, so nothing else stops it either.

    Read on the way IN, before the factory: the cheapest of the two reads, and
    the one that covers the whole interval from the guard's first Ctrl-C to the
    driver handshake — which on a cold worker is most of a second."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    gfb._interrupt_state["seen"] = True

    with pytest.raises(KeyboardInterrupt):
        session._ensure_page()

    assert pw.starts == 0
    assert pw.chromium.launches == 0
    assert session not in gfb._live


def test_the_refusal_after_the_stop_comes_before_the_launch_is_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Where the latch is READ, which the refusal above cannot tell apart.

    The notice says a Chrome is opening. Read below it, the same refusal still
    raises and still opens nothing — every assertion above stays green — but the
    run has already told the user it is opening a browser it then never opens,
    on the one path where the user is watching for exactly that. It also spends
    the once-per-process notice, so the next launch in the same run, which does
    open one, is silent."""
    pw = _install(monkeypatch, tmp_path)
    session = gfb.GfBrowserSession(headed=False)
    gfb._interrupt_state["seen"] = True

    with pytest.raises(KeyboardInterrupt):
        session._ensure_page()

    assert gfb._notice_state["printed"] is False
    assert pw.chromium.launches == 0  # the premise: nothing was opened to announce


def test_a_driver_that_starts_after_the_stop_is_killed_before_the_browser_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The other read, and the one with something to clean up.

    The handler fires DURING `start()`: it takes `_manager` and finds no pid,
    because the driver it would have killed does not exist yet — so the stop
    stops nothing and the process this call is spawning survives it. The read
    after `start()` returns is the only place that can still name it, through
    the local handle the session no longer holds. What it must NOT do is open a
    browser."""
    pw = _install(monkeypatch, tmp_path)
    killed: list[tuple[int, int]] = []
    started: list[bool] = []
    session = gfb.GfBrowserSession(headed=False)
    real_start = pw.start

    def _the_handler_fires_while_the_driver_starts() -> _FakePlaywright:
        gfb._interrupt_state["seen"] = True  # what the handler sets first
        gfb.stop_all_drivers()  # …and what it does next: nothing to find
        started.append(True)
        return real_start()

    # `_fixed_pid` answers for any non-`None` manager, which would let the stop
    # above kill a driver that does not exist yet and take the premise with it.
    def _pid_once_the_driver_exists(manager: Any) -> int | None:
        return 4242 if manager is not None and started else None

    monkeypatch.setattr(pw, "start", _the_handler_fires_while_the_driver_starts)
    monkeypatch.setattr(gfb, "_driver_process_id", _pid_once_the_driver_exists)
    monkeypatch.setattr(gfb.os, "kill", _record_kill(killed))

    with pytest.raises(KeyboardInterrupt):
        session._ensure_page()

    assert pw.starts == 1
    assert pw.chromium.launches == 0  # the discriminating one: no browser
    assert killed == [(4242, signal.SIGKILL)]  # the late driver, killed once
    assert session._dead is True


def test_the_guard_clears_the_interrupt_latch_on_the_way_in(keep_sigint: None) -> None:
    """The latch is per guarded search, not per process.

    Set and never cleared, it would refuse every later launch in the same
    process — the serialised second search a multi-cabin run makes, say. It is
    cleared here and nowhere else: the only thing that sets it is the handler,
    which sets the guard's own `seen` in the same breath, so a clean way out
    finds it already false and an interrupted one leaves it set for the rest of
    that shutdown — exactly as it leaves the ignore. A clear on the way out
    would be a statement no state can reach."""
    gfb._interrupt_state["seen"] = True

    with gfb.interrupt_guard():
        assert gfb._interrupt_state["seen"] is False

    assert gfb._interrupt_state["seen"] is False


def test_the_register_lock_is_re_entrant_for_the_thread_already_holding_it() -> None:
    """The handler runs on the main thread between two bytecodes of whatever
    that thread was doing — including a `_remember` or a `_forget` holding this
    lock — and then stops every driver through the same lock.

    Asserted by a non-blocking re-acquire rather than by a second blocking one.
    The failure being pinned is a deadlock, and a test that reproduced it would
    hang rather than fail; a plain `Lock` answers `False` here instead, which is
    a failure that arrives."""
    with gfb._live_lock:
        got = gfb._live_lock.acquire(blocking=False)
        if got:
            gfb._live_lock.release()
    assert got is True


def test_the_driver_pid_lookup_answers_none_for_a_shape_it_does_not_know() -> None:
    """A guarded lookup, never an assertion: it runs inside a signal handler,
    where raising would replace the user's stop with a crash. Every level of the
    chain is checked, because a patchright bump can move any one of them.

    The walk that SUCCEEDS is checked last and belongs here: a lookup that
    answered `None` for every shape would satisfy all four refusals above while
    leaving every live driver unnameable — a browser marked finished whose Chrome
    nothing ever stops, reported as nothing at all."""
    assert gfb._driver_process_id(None) is None
    assert gfb._driver_process_id(object()) is None

    # The classes below are named for the private ATTRIBUTES they stand in for,
    # so each stand-in reads as the level of the chain it truncates.
    class _NoTransport:
        _connection = object()

    class _NoProc:
        class _connection:
            _transport = object()

    class _NotAPid:
        class _connection:
            class _transport:
                class _proc:
                    pid = "not a number"

    class _RealShape:
        class _connection:
            class _transport:
                class _proc:
                    pid = 4242

    assert gfb._driver_process_id(_NoTransport()) is None
    assert gfb._driver_process_id(_NoProc()) is None
    assert gfb._driver_process_id(_NotAPid()) is None
    assert gfb._driver_process_id(_RealShape()) == 4242


def test_the_driver_pid_chain_is_the_one_the_installed_patchright_builds() -> None:
    """The three private assignments the pid lookup walks, pinned against the
    installed patchright.

    Load-bearing rather than decorative. A bump that renames any of them makes
    the lookup answer `None` for a session that DOES hold a driver — a browser
    marked finished whose Chrome nothing will ever stop, reported as nothing at
    all. `pyproject.toml` pins `patchright>=1.59` with no upper bound, so that
    bump is one sync away. This fails first, and loudly, instead."""
    from patchright._impl._connection import Connection
    from patchright._impl._transport import PipeTransport
    from patchright.sync_api._context_manager import PlaywrightContextManager

    assert "self._connection = Connection(" in _source(PlaywrightContextManager, "__enter__")
    assert "self._transport = transport" in _source(Connection, "__init__")
    assert "self._proc = await asyncio.create_subprocess_exec(" in _source(PipeTransport, "connect")


def test_a_navigation_on_a_stopped_session_is_the_interrupt_it_really_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Killing the driver is how a stop reaches a navigation on another thread,
    and what that navigation then raises is an ordinary transport error.

    Left as one it becomes "Chrome could not load Google Flights' search page",
    which the pin loop absorbs into a warning about a network the user never had
    trouble with — on the run they asked to end, with the exit code of a refusal
    rather than of a stop."""
    _install(
        monkeypatch,
        tmp_path,
        outcomes=[RuntimeError("Target page, context or browser has been closed")],
    )
    session = gfb.GfBrowserSession(headed=False)
    session._dead = True
    with pytest.raises(KeyboardInterrupt) as e:
        session.get_html(_PAGE_URL)
    assert not isinstance(e.value, GfBrowserUnavailableError)


def test_a_body_read_on_a_stopped_session_is_the_interrupt_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The same one step later. The page answered and the stop landed while its
    body was being read, which is the longer of the two windows: `response.text()`
    carries no timeout at any layer."""
    _install(
        monkeypatch,
        tmp_path,
        outcomes=[
            _FakeResponse(
                body="",
                url=_PAGE_URL,
                status=200,
                body_error=RuntimeError("Target closed"),
            )
        ],
    )
    session = gfb.GfBrowserSession(headed=False)
    session._dead = True
    with pytest.raises(KeyboardInterrupt) as e:
        session.get_html(_PAGE_URL)
    assert not isinstance(e.value, GfBrowserUnavailableError)


def test_a_stop_mid_round_trip_is_not_absorbed_by_the_pin_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    no_rung_one: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole reason the interrupt is re-raised rather than wrapped, driven
    through the loop that would otherwise swallow it.

    The pin loop treats a dead browser as a fact about the SESSION and stops
    pinning — correctly, for a browser that died on its own. A browser this
    process stopped on purpose is the user's instruction instead, and it has to
    travel out past every arm here."""
    pw = _install(
        monkeypatch,
        tmp_path,
        outcomes=[_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)],
    )
    session = gfb.session(headed=False)
    page = _page_of(pw)
    real_goto = page.goto
    calls: list[int] = []

    def _goto(url: str, *, wait_until: str, timeout: int) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return real_goto(url, wait_until=wait_until, timeout=timeout)
        # What a stop from another thread does, in the order it does it: the
        # session is marked finished, and the navigation already in flight then
        # fails the way a severed transport fails.
        session._dead = True
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(page, "goto", _goto)
    with caplog.at_level("WARNING"), pytest.raises(KeyboardInterrupt):
        gfid.search_with_ids(
            _filters(round_trip=True), top_n=1, transport=gfid.GfTransport(mode="browser")
        )
    assert "stopped pinning" not in caplog.text, caplog.text
    gfb.close_thread_session()


# ─────────────────── arming the interrupt, and what it costs the exit ──────────


def test_the_guard_installs_a_handler_and_gives_it_back_on_a_clean_search(
    keep_sigint: None,
) -> None:
    """Armed for the duration of a search and no longer. A search that ends
    normally leaves the process's Ctrl-C exactly as it found it — this rung is a
    guest in a CLI that is mostly not a browser."""
    before = signal.getsignal(signal.SIGINT)
    with gfb.interrupt_guard():
        armed = signal.getsignal(signal.SIGINT)
        assert armed is not before
        assert callable(armed)
    assert signal.getsignal(signal.SIGINT) is before


def test_the_guard_stops_every_driver_and_then_ignores_the_next_ctrl_c(
    keep_sigint: None,
) -> None:
    """What the handler does, in the order that matters.

    `SIG_IGN` goes in FIRST, before anything a second signal could re-enter: the
    stop below raises the `asyncio` logger's level, and clearing the logging
    manager's cache releases that module's lock without a `try`/`finally`, so a
    second SIGINT arriving inside it would leave the lock held for the life of
    the process. The stop itself is where that order is read, because it is the
    only moment the two orderings differ.

    And the ignore OUTLIVES the guard, deliberately. Once the drivers are dead
    and the exit is running there is nothing left for a second Ctrl-C to stop;
    what it would do instead is land in the middle of that shutdown, as a second
    `KeyboardInterrupt` through interpreter finalisation."""
    stopped: list[str] = []

    def _stop() -> None:
        # Read DURING the stop, which is the only place the two orderings
        # differ: after the guard both leave `SIG_IGN` behind, so an assertion
        # taken there holds either way and pins nothing.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        # Read here for the same reason: the refusal a launch still on its way
        # up will make has to be in place before the registry is snapshotted,
        # because that snapshot is what misses such a launch.
        assert gfb._interrupt_state["seen"] is True
        stopped.append("stopped")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gfb, "stop_all_drivers", _stop)
        with pytest.raises(KeyboardInterrupt), gfb.interrupt_guard():
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            # Raised by calling the handler rather than by sending a signal: a
            # test that raced `os.kill` against the interpreter would pass or
            # fail on timing, and this is about what the handler DOES.
            handler(signal.SIGINT, None)
    assert stopped == ["stopped"]
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN


def test_the_guard_is_a_no_op_off_the_main_thread_and_still_runs_its_body() -> None:
    """`signal.signal` raises on any other thread, and the enriched arm's search
    runs on one. Nothing needs installing there — a Ctrl-C is delivered to the
    main thread whichever thread was working — but the body still has to run, or
    the guard would decide whether the search happens."""
    ran: list[str] = []
    failed: list[BaseException] = []

    def _work() -> None:
        try:
            with gfb.interrupt_guard():
                ran.append("body")
        # Broad on purpose: what this asserts is that NOTHING escaped, and the
        # failure it guards against — `signal.signal` off the main thread — is a
        # `ValueError` that would otherwise die in a thread nobody is watching.
        except BaseException as e:
            failed.append(e)

    t = threading.Thread(target=_work)
    t.start()
    t.join()
    assert failed == []
    assert ran == ["body"]


def _recording_guard(seen: list[object]) -> Callable[..., contextlib.AbstractContextManager[None]]:
    """A stand-in for `interrupt_guard` that runs the real one and records what
    its `__enter__` left installed on SIGINT.

    One recorder for both arming questions: `len(seen)` is how many times the
    guard was entered, and each entry is the disposition it installed — or the
    one it decided to leave alone. The read happens INSIDE the guard and nowhere
    else. By the time the guarded work runs, `anyio.run` has opened a loop and
    `asyncio.Runner` may have installed a handler of its own
    (`asyncio/runners.py:102-104`), so a read taken there measures asyncio
    instead of this. And the disposition rather than the argument, because a
    keyword that stopped being read would still be handed over."""
    real = gfb.interrupt_guard

    @contextlib.contextmanager
    def _guard(*, armed: bool = True) -> Generator[None]:
        with real(armed=armed):
            seen.append(signal.getsignal(signal.SIGINT))
            yield

    return _guard


def test_the_fast_arm_arms_the_guard_around_its_whole_search(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None
) -> None:
    """The `--fast` arm arms it once, around the search rather than around the
    browser: the transport is not known here, and a Ctrl-C is answerable only
    while the process still holds the driver.

    A structural claim — how many times the guard was entered and what it
    installed — which is what a substitute can say honestly. The end-to-end
    exit-code test further down passes with no guard at all, so it pins the 130
    and not the arming."""
    from flight_cli import cli

    before = signal.getsignal(signal.SIGINT)
    seen: list[object] = []
    monkeypatch.setattr(gfb, "interrupt_guard", _recording_guard(seen))

    def _no_rows(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _no_rows)
    legs = (Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 10, 14)),)
    cli._run_gflight_path(legs=legs, opts=SearchOptions(), top_n=3, json_out=True)

    assert len(seen) == 1
    assert seen[0] is not before
    assert callable(seen[0])


@pytest.mark.parametrize("mode", ["http", "auto", "browser"])
def test_the_enriched_arm_arms_the_guard_only_for_the_browser_transport(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None, mode: GfTransportMode
) -> None:
    """The other arm decides per transport, and the reason is the worker.

    This half runs inside `anyio.to_thread.run_sync`, which will not abandon its
    worker, and no signal reaches that thread. On the browser transport the
    handler's stop is what frees it, which is the whole point of arming. On a
    transport that holds no driver the stop frees nothing, and an ignore that
    outlives the first Ctrl-C throws away the only key left: the second one,
    which is what breaks the join interpreter shutdown is blocked on.

    `auto` sits with `http` because the ladder maps it to rung 1, the same
    reading the closer test above takes."""
    from flight_cli import cli

    before = signal.getsignal(signal.SIGINT)
    seen: list[object] = []
    state: dict[str, Any] = {}
    monkeypatch.setattr(gfb, "interrupt_guard", _recording_guard(seen))

    async def _nothing() -> None:
        """A weave with no work in it: the arming happens before it runs."""

    cli._run_the_weave(_nothing, state, mode)

    if mode == "browser":
        assert len(seen) == 1
        assert seen[0] is not before
        assert callable(seen[0])
    else:
        assert seen == [before]


def test_the_enriched_path_hands_its_transport_to_the_arming_decision(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None
) -> None:
    """The decision above only reaches a user through one call, and that call is
    the whole of the wiring.

    The test above drives `_run_the_weave` directly, so it says what the weave
    does with a transport it is given and nothing about whether anything gives
    it one. This one starts a leg further out, at the frame the CLI actually
    calls, with both halves stubbed into their shortest failing shape — so what
    it pins is that `--gf-transport browser` still arrives at the guard.

    The reading is taken inside the guard's `__enter__` and nowhere else: by the
    time the guarded work runs, `anyio.run` has opened a loop and
    `asyncio.Runner` may have installed a handler of its own. The refusal both
    stubbed halves force is asserted rather than suppressed, so a new failure
    mode arrives as a failure instead of as a pass. Browser only, deliberately —
    the per-transport decision is already pinned three ways by the test above,
    and a second parametrize here would buy a node and no discrimination."""
    import typer

    from flight_cli import cli

    before = signal.getsignal(signal.SIGINT)
    seen: list[object] = []
    monkeypatch.setattr(gfb, "interrupt_guard", _recording_guard(seen))
    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)

    def _no_rows(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _no_rows)

    with pytest.raises(typer.Exit):
        cli._run_enriched_path(
            legs=_one_leg(),
            opts=SearchOptions(),
            top_n=5,
            run_pp=False,
            sel=None,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
            gf_mode=cli.TRANSPORT_BROWSER,
        )

    assert len(seen) == 1
    assert seen[0] is not before
    assert callable(seen[0])


async def _sleep_briefly() -> None:
    """A child the group is still running when the host frame raises."""
    import anyio

    await anyio.sleep(0.01)


def _weave_raising(where: str, exc: BaseException) -> Any:
    """A `go` shaped exactly like `cli._run_enriched_path._go`: one task started
    into a task group, and a host frame that runs after it — which is where the
    first Google table is painted, and so where a Ctrl-C most often lands."""

    async def _go() -> None:
        import anyio

        async with anyio.create_task_group() as tg:
            if where == "a-task":

                async def _raise() -> None:
                    raise exc

                tg.start_soon(_raise)
            else:
                tg.start_soon(_sleep_briefly)
                raise exc

    return _go


@pytest.mark.parametrize("where", ["a-task", "the-host-frame"])
def test_an_interrupt_inside_the_weave_leaves_it_as_a_bare_interrupt(
    where: str, keep_sigint: None
) -> None:
    """A task group hands its caller a GROUP, and every handler between here and
    the exit matches the bare class only.

    With the guard armed, `asyncio.Runner` installs no handler of its own
    (`asyncio/runners.py:102-104`), so the interrupt is raised wherever the main
    thread stands — inside the group. Wrapped, it matches nothing: typer turns a
    `KeyboardInterrupt` into exit 130 and a group of them into a traceback and
    exit 1. `type(...) is`, not `isinstance`, because a group of one interrupt
    passes `pytest.raises(KeyboardInterrupt)` for the wrong reason."""
    from flight_cli import cli

    state: dict[str, Any] = {}
    # Deliberately the broadest catch, because the TYPE is what is asserted
    # below: naming `KeyboardInterrupt` here would also accept a group of one.
    with pytest.raises(BaseException) as e:
        cli._run_the_weave(_weave_raising(where, KeyboardInterrupt()), state, cli.TRANSPORT_HTTP)
    assert type(e.value) is KeyboardInterrupt, e.value
    assert not isinstance(e.value, BaseExceptionGroup)
    assert state == {}


def _leaf_types(e: BaseException) -> list[str]:
    """Every non-group exception inside `e`, however deeply it is nested.

    A shallow read of `.exceptions` is not enough: splitting a group leaves the
    unhandled half wrapped in a group of its own, so the question "which
    exceptions came out of here" is only answerable recursively."""
    if isinstance(e, BaseExceptionGroup):
        inner = cast("tuple[BaseException, ...]", e.exceptions)
        return sorted(t for sub in inner for t in _leaf_types(sub))
    return [type(e).__name__]


def test_a_failing_task_is_still_stashed_rather_than_raised(keep_sigint: None) -> None:
    """The control for the arm above: a group carrying only ordinary errors keeps
    reaching the handler that stashes it, so the reporters below still decide the
    outcome and the other backend's rows are not thrown away."""
    from flight_cli import cli

    state: dict[str, Any] = {}
    cli._run_the_weave(
        _weave_raising("a-task", RuntimeError("fli fell over")), state, cli.TRANSPORT_HTTP
    )
    assert "weave_err" in state
    assert _leaf_types(state["weave_err"]) == ["RuntimeError"]
    assert "fli fell over" in str(state["weave_err"].exceptions[0])


def test_a_group_carrying_an_interrupt_and_an_error_still_leaves_as_a_group(
    keep_sigint: None,
) -> None:
    """The residual, pinned rather than fixed.

    A group holding an interrupt AND a leaf that is not an `Exception` matches
    nothing on the path out, so it reaches the exit as a traceback. Widening the
    arm to catch it would swallow the other leaf — and it needs two of them
    raised in the same instant, which nothing here has been seen to do. The pin
    says the behaviour did not change; it is not an endorsement.

    What is pinned is the OUTCOME — a group still leaves, carrying both leaves,
    and nothing is stashed. Its nesting is not: splitting a group re-wraps the
    half that was not handled, so the shape differs from the one that arrived
    while the two exceptions in it do not."""
    from flight_cli import cli

    async def _go() -> None:
        raise BaseExceptionGroup("two at once", [KeyboardInterrupt(), ValueError("x")])

    state: dict[str, Any] = {}
    with pytest.raises(BaseExceptionGroup) as e:
        cli._run_the_weave(_go, state, cli.TRANSPORT_HTTP)
    assert _leaf_types(e.value) == ["KeyboardInterrupt", "ValueError"]
    assert state == {}


# ──────────────────── what the exit code and the streams say ───────────────────
# Through the real console entry, with only the browser substituted. The exit
# code is typer's and only typer's — `typer/core.py:202-203` turns a
# `KeyboardInterrupt` into `Exit(130)`, and `:223-225` makes that the process's
# status — so nothing on the path from `get_html` up to it may catch one. These
# are what say the whole path still obeys that.


def _search_argv(*extra: str) -> list[str]:
    return [
        "search",
        "JFK",
        "LAX",
        "--dep",
        "2026-10-14",
        "--gf-transport",
        "browser",
        "--cash-only",
        "--no-cache",
        "-n",
        "3",
        *extra,
    ]


def test_a_ctrl_c_on_the_fast_arm_exits_130_with_nothing_on_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, keep_sigint: None
) -> None:
    """The `--fast` arm, end to end: the interrupt lands in the navigation and
    the command exits 130 having written nothing.

    Zero bytes is the load-bearing half. A stopped search that had already
    printed part of its answer would be read as the answer — by a person and, in
    a pipeline, by a parser — and the exit code is not consulted before the bytes
    are. This is honest with a fake browser because it is about what was WRITTEN,
    not about what blocked."""
    from typer.testing import CliRunner

    from flight_cli import cli

    _install(monkeypatch, tmp_path, outcomes=[KeyboardInterrupt()])
    result = CliRunner().invoke(cli.app, _search_argv("--backend", "gflight", "--fast"))
    assert result.exit_code == 130, (result.exit_code, result.output)
    assert result.stdout == ""
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_a_ctrl_c_on_the_enriched_arm_exits_130_and_not_as_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, keep_sigint: None
) -> None:
    """The same claim on the arm where the search runs in a worker thread.

    This is the case the unwrap in the weave exists for: the interrupt comes back
    out of the worker into the task group's host frame, and the group re-raises
    it wrapped. Unwrapped it matches no handler at all and the command ends as a
    traceback and exit 1 — a crash where the user asked for a stop."""
    from typer.testing import CliRunner

    from flight_cli import cli

    _install(monkeypatch, tmp_path, outcomes=[KeyboardInterrupt()])
    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    result = CliRunner().invoke(cli.app, _search_argv())
    assert result.exit_code == 130, (result.exit_code, result.output)
    assert result.stdout == ""
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_a_ctrl_c_after_the_first_table_is_painted_still_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, keep_sigint: None
) -> None:
    """The likeliest real Ctrl-C on this arm, and the one the "nothing on stdout"
    rule has to make room for.

    The Google table is painted about a second in and Matrix lands around forty
    seconds later, so most of the enriched run is spent with a COMPLETE table
    already on stdout. A table printed before the interrupt is not a partial
    answer, and the invariant is about partial ones: what it forbids is bytes
    that stop mid-answer. The exit code is still the user's stop, and the stop is
    still not a traceback."""
    from typer.testing import CliRunner

    from flight_cli import cli

    _install(
        monkeypatch,
        tmp_path,
        outcomes=[_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)],
    )
    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    real_paint = cli._paint_first_gf_table

    def _paint_then_interrupt(*a: Any, **kw: Any) -> None:
        real_paint(*a, **kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_paint_first_gf_table", _paint_then_interrupt)
    result = CliRunner().invoke(cli.app, _search_argv())
    assert result.exit_code == 130, (result.exit_code, result.output)
    # The painted table is on stdout, whole, and is not what the rule forbids.
    assert "JFK" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


# ───────────── capture: a response the page's own code receives ─────────────

_RPC_URL = "https://www.google.com/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetCalendarGraph?rpcids=x"
_GRAPH = gfb.Control("button", "Price graph")


def _wants_graph(url: str) -> bool:
    return "GetCalendarGraph" in url


def _nav_request() -> _FakeRequest:
    return _FakeRequest(_PAGE_URL, navigation=True, frame=_MAIN_FRAME)


def _rpc(body: str = "graph") -> tuple[_FakeRequest, _FakeRpcResponse]:
    request = _FakeRequest(_RPC_URL, frame=_MAIN_FRAME)
    return request, _FakeRpcResponse(request, body=body)


def _served(request: _FakeRequest, response: _FakeRpcResponse) -> list[_Event]:
    return [("request", request), ("response", response), ("requestfinished", request)]


def _capture_page(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _FakePage:
    pw = _install(
        monkeypatch,
        tmp_path,
        outcomes=[_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)],
    )
    return _page_of(pw)


def test_capture_catches_a_response_fired_while_the_page_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The listeners are on before the navigation starts, so an RPC the page
    makes on load — before `goto` has even returned — is the one handed back."""
    page = _capture_page(monkeypatch, tmp_path)
    request, response = _rpc("on load")
    page.on_goto = [("request", _nav_request()), *_served(request, response)]
    with gfb.GfBrowserSession(headed=False) as session:
        got = session.capture(_PAGE_URL, _wants_graph)
    assert got == gfb.CapturedResponse(url=_RPC_URL, status=200, body="on load")
    assert page.clicks == []
    [(url, wait_until, timeout)] = page.gotos
    assert (url, wait_until) == (_PAGE_URL, "domcontentloaded")
    assert 0 < timeout <= gfb._NAV_TIMEOUT_MS
    assert page.listeners == {}


def test_capture_clicks_the_control_by_role_and_exact_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    page = _capture_page(monkeypatch, tmp_path)
    page.on_goto = [("request", _nav_request())]
    request, response = _rpc("after click")
    page.on_click = _served(request, response)
    with gfb.GfBrowserSession(headed=False) as session:
        got = session.capture(_PAGE_URL, _wants_graph, click=_GRAPH)
    assert got.body == "after click"
    [(role, name, exact, timeout)] = page.clicks
    assert (role, name, exact) == ("button", "Price graph", True)
    assert 0 < timeout <= gfb._CLICK_TIMEOUT_MS
    assert page.listeners == {}


def test_capture_ignores_the_previous_documents_traffic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The page is reused, so the last document's requests can still be
    answering when the next navigation starts. Only requests issued from this
    navigation's own main-frame request on are this page's; the rest match the
    predicate and are still not the answer. A child frame's navigation and a
    service worker's request (whose `frame` raises) do not open the gate."""
    page = _capture_page(monkeypatch, tmp_path)
    stale_done, stale_done_response = _rpc("stale, finished")
    stale_late, stale_late_response = _rpc("stale, answered late")
    fresh, fresh_response = _rpc("fresh")
    page.on_goto = [
        ("request", stale_late),
        *_served(stale_done, stale_done_response),
        ("request", _FakeRequest(_PAGE_URL, navigation=True, frame=_CHILD_FRAME)),
        ("request", _FakeRequest(_PAGE_URL, frame=None)),
        ("request", _nav_request()),
        ("response", stale_late_response),
        ("requestfinished", stale_late),
        *_served(fresh, fresh_response),
    ]
    with gfb.GfBrowserSession(headed=False) as session:
        got = session.capture(_PAGE_URL, _wants_graph)
    assert got.body == "fresh"


def test_capture_reads_the_first_match_only_once_its_request_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A body read before the request finishes can come back cut short, and
    `text()` has no deadline to stop it. The capture waits for the finish, and
    holds out for the FIRST match rather than taking a later one that happened
    to finish sooner."""
    page = _capture_page(monkeypatch, tmp_path)
    first, first_response = _rpc("first, streamed")
    second, second_response = _rpc("second")
    page.on_goto = [("request", _nav_request())]
    page.on_click = [
        ("request", first),
        ("response", first_response),
        *_served(second, second_response),
    ]
    page.on_wait = [
        ("request", _FakeRequest(_PAGE_URL, frame=_MAIN_FRAME)),
        ("requestfinished", first),
    ]
    with gfb.GfBrowserSession(headed=False) as session:
        got = session.capture(_PAGE_URL, _wants_graph, click=_GRAPH)
    assert got.body == "first, streamed"
    assert (first_response.reads, second_response.reads) == (1, 0)
    assert len(page.waits) == 2
    assert all(0 < w <= gfb._POLL_MS for w in page.waits)


def test_capture_refuses_once_its_deadline_is_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A page that never makes the request ends in a typed refusal on the
    deadline, never in an unbounded wait: every wait handed to the driver is
    positive, because patchright reads a zero timeout as none at all."""
    page = _capture_page(monkeypatch, tmp_path)
    page.on_goto = [("request", _nav_request())]
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfBrowserUnavailableError, match="in time"):
            session.capture(_PAGE_URL, _wants_graph, timeout_s=0.05)
    assert page.waits
    assert all(w > 0 for w in page.waits)
    assert page.listeners == {}


def test_a_spent_deadline_is_refused_never_passed_as_no_timeout() -> None:
    with pytest.raises(GfBrowserUnavailableError):
        gfb._budget_ms(time.monotonic() - 1, 1_000)
    assert gfb._budget_ms(time.monotonic() + 60, 1_000) == 1_000


def test_a_missing_control_is_a_typed_refusal_that_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    page = _capture_page(monkeypatch, tmp_path)
    page.on_goto = [("request", _nav_request())]
    page.click_error = RuntimeError("Timeout 20000ms exceeded.\ncall log:\n  - waiting for locator")
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfBrowserUnavailableError, match=r"'Price graph'.*Timeout 20000ms"):
            session.capture(_PAGE_URL, _wants_graph, click=_GRAPH)
    assert page.listeners == {}


@pytest.mark.parametrize(
    "outcome,expected",
    [(RuntimeError("net::ERR_ABORTED\ncall log:"), "could not load"), (None, "no response")],
)
def test_a_failed_capture_navigation_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, outcome: Any, expected: str
) -> None:
    pw = _install(monkeypatch, tmp_path, outcomes=[outcome])
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfBrowserUnavailableError, match=expected):
            session.capture(_PAGE_URL, _wants_graph, click=_GRAPH)
    assert _page_of(pw).clicks == []
    assert _page_of(pw).listeners == {}


def test_check_page_sees_the_navigation_and_its_refusal_is_not_rewrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A throttle interstitial has no control to click. Handed the navigation
    first, the caller's parser names the wall; the capture neither clicks nor
    turns that verdict into "Chrome could not …"."""
    pw = _install(
        monkeypatch,
        tmp_path,
        outcomes=[
            _FakeResponse(body="<html>sorry</html>", url=_SORRY_URL, status=200, body_error=None)
        ],
    )
    seen: list[gfc.PageFetch] = []

    def _refuse(fetch: gfc.PageFetch) -> None:
        seen.append(fetch)
        raise GfThrottledError("Google Flights rate-limited the request")

    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117 — nesting keeps the session's lifetime separate from what raises inside it
        with pytest.raises(GfThrottledError):
            session.capture(_PAGE_URL, _wants_graph, click=_GRAPH, check_page=_refuse)
    assert seen == [gfc.PageFetch(html="<html>sorry</html>", final_url=_SORRY_URL, status_code=200)]
    assert _page_of(pw).clicks == []
    assert _page_of(pw).listeners == {}


def test_a_ctrl_c_during_the_capture_wait_is_not_turned_into_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The wait is where a capture spends its time. An interrupt there is the
    user's, and it leaves the session marked finished so the close that follows
    stops the driver instead of driving a dead sync API."""
    page = _capture_page(monkeypatch, tmp_path)
    page.on_goto = [("request", _nav_request())]
    page.wait_error = KeyboardInterrupt()
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(KeyboardInterrupt) as e:
        session.capture(_PAGE_URL, _wants_graph)
    assert not isinstance(e.value, GfBrowserUnavailableError)
    assert session.finished
    assert page.listeners == {}
    session.close()


_BOOKING_URL = "https://www.google.com/travel/flights/booking?tfs=x&curr=USD"
_EXPLORE_URL = "https://www.google.com/travel/explore?tfs=x&curr=USD"


def _interrupted_page(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A Ctrl-C in the capture's wait, after which removing a listener raises
    as patchright's does once the interrupt has unwound its event loop."""
    page = _capture_page(monkeypatch, tmp_path)
    page.on_goto = [("request", _nav_request())]
    page.wait_error = KeyboardInterrupt()
    page.remove_error = RuntimeError(": no running event loop")


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            lambda: _gf_booking.booking_options(_BOOKING_URL, flights=[("DL", "1")], headed=False),
            id="sellers",
        ),
        pytest.param(
            lambda: _gf_explore.explore(_EXPLORE_URL, origin="JFK", month=None, headed=False),
            id="explore",
        ),
    ],
)
def test_a_ctrl_c_during_a_page_capture_reaches_the_reader_as_the_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, read: Callable[[], object]
) -> None:
    _interrupted_page(monkeypatch, tmp_path)
    with pytest.raises(KeyboardInterrupt), gfb.session_scope():
        read()


def test_a_ctrl_c_during_explore_exits_130_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, keep_sigint: None
) -> None:
    from typer.testing import CliRunner

    from flight_cli import cli

    _interrupted_page(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli.app, ["explore", "JFK"])
    assert result.exit_code == 130, (result.exit_code, result.output)
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


def test_capture_shares_the_session_and_its_one_page_with_get_html(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """One launch serves a search page fetch and two captures, and no capture
    leaves a listener behind for the next navigation to trip."""
    pw = _install(
        monkeypatch,
        tmp_path,
        outcomes=[_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)],
    )
    page = _page_of(pw)
    with gfb.GfBrowserSession(headed=False) as session:
        session.get_html(_PAGE_URL)
        for body in ("one", "two"):
            request, response = _rpc(body)
            page.on_goto = [("request", _nav_request()), *_served(request, response)]
            assert session.capture(_PAGE_URL, _wants_graph).body == body
            assert page.listeners == {}
    assert pw.chromium.launches == 1
    assert len(page.gotos) == 3
