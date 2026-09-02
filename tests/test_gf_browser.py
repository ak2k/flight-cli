# pyright: reportPrivateUsage=false
"""Rung 2: a real Chrome fetching the same Google Flights search page.

Two claims carry this file. **One parser** — rung 2 hands over bytes and the
same `_rows_from_page_html` decides what they mean, so a `/sorry/` page fetched
through Chrome is a throttle for exactly the reason it is over curl_cffi. And
**no test launches a browser** — the autouse guard in `conftest.py` replaces the
launcher for every test here except the handful marked `gf_browser`, which drive
a fake playwright object graph and never open a window.

Nothing in this file touches the network.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import (
    GfBrowserUnavailableError,
    GfPageShapeError,
    GfThrottledError,
)
from flight_cli.cli import _resolve_gf_transport
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli._gflight_ids import GFlightWithId

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "gflight_page"
_PAGE_URL = "https://www.google.com/travel/flights?tfs=abc"
_SORRY_URL = "https://www.google.com/sorry/index?continue=x"


def _page(name: str = "ds1_jfk_lax_3rows.json") -> str:
    """A minimal page carrying the fixture's `ds:1` blob, as Google inlines it."""
    return (
        "<!doctype html><html><body><script>"
        f"AF_initDataCallback({{key: 'ds:1', hash: '2', "
        f"data:{(FIXTURE_DIR / name).read_text()}, sideChannel: {{}}}});"
        "</script></body></html>"
    )


# ───────────────────────── a fake playwright object graph ──────────────────────
# Shaped exactly like the slice of patchright's API `_gf_browser` drives, so a
# signature drift shows up as a test failure rather than at the first live run.


class _FakeResponse:
    def __init__(self, *, body: str, url: str, status: int, body_error: Exception | None) -> None:
        self._body = body
        self._body_error = body_error
        self.url = url
        self.status = status

    def text(self) -> str:
        if self._body_error is not None:
            raise self._body_error
        return self._body


class _FakePage:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.gotos: list[tuple[str, str, int]] = []
        self.url = ""

    def goto(self, url: str, *, wait_until: str, timeout: int) -> _FakeResponse | None:
        self.gotos.append((url, wait_until, timeout))
        self.url = url
        outcome = self._outcomes[min(len(self.gotos) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return cast("_FakeResponse | None", outcome)


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext, launch_error: Exception | None) -> None:
        self._context = context
        self._launch_error = launch_error
        self.launch_kwargs: dict[str, Any] = {}

    def launch_persistent_context(self, **kwargs: Any) -> _FakeContext:
        self.launch_kwargs = kwargs
        if self._launch_error is not None:
            raise self._launch_error
        return self._context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def start(self) -> _FakePlaywright:
        return self

    def stop(self) -> None:
        self.stopped = True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    outcomes: list[Any] | None = None,
    launch_error: Exception | None = None,
) -> _FakePlaywright:
    """Point the launcher seam at a fake browser and the cache dir at tmp_path."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv(gfb._BROWSER_BIN_ENV, raising=False)
    page = _FakePage(
        outcomes
        if outcomes is not None
        else [_FakeResponse(body=_page(), url=_PAGE_URL, status=200, body_error=None)]
    )
    pw = _FakePlaywright(_FakeChromium(_FakeContext(page), launch_error))

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
    def __init__(self, *, text: str, status_code: int, url: str) -> None:
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        raise AssertionError("_fetch_page must not classify the response")


def test_fetch_page_returns_the_response_without_judging_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_page` is the GET and nothing else: a 500 is data it hands back,
    not an exception it raises.

    This pins the contract, NOT the live rung-1 path — the fake client stands
    in for fli's, whose own `Client.get` calls `raise_for_status()` inside a
    three-attempt retry and so turns a non-2xx into `SearchHTTPError` before
    `_fetch_page` ever sees it. What the contract buys is rung 2, where a
    navigation really does report a status without raising."""

    class _Client:
        def get(self, url: str, **_kw: object) -> _FakeHttpResponse:
            return _FakeHttpResponse(text="boom", status_code=500, url=url)

    def _stub_tfs(_filters: Any) -> bytes:
        return b"\x08\x1c"

    def _no_seed(_client: Any) -> None:
        return None

    monkeypatch.setattr(gfid, "get_client", _Client)
    monkeypatch.setattr(gfid, "build_search_tfs", _stub_tfs)
    monkeypatch.setattr(gfid, "_seed_cookies_once", _no_seed)

    html, final_url, status_code = gfid._fetch_page(cast("Any", None))
    assert (html, status_code) == ("boom", 500)
    assert "tfs=" in final_url


def test_a_server_error_is_a_typed_shape_refusal() -> None:
    """A 5xx used to surface as curl_cffi's own `HTTPError`, which the Matrix
    fallback seam does not catch. Typed, it degrades like every other refusal."""
    with pytest.raises(GfPageShapeError, match="HTTP 503"):
        gfid._rows_from_page_html("", final_url=_PAGE_URL, status_code=503)


def test_a_throttle_outranks_the_status_check() -> None:
    """429 is both "blocked" and "not 2xx"; it has to read as the throttle,
    because that is the one the caller can back off and retry."""
    with pytest.raises(GfThrottledError):
        gfid._rows_from_page_html("", final_url=_PAGE_URL, status_code=429)
    with pytest.raises(GfThrottledError):
        gfid._rows_from_page_html("", final_url=_SORRY_URL, status_code=200)


def test_browser_bytes_and_http_bytes_reach_the_same_rows() -> None:
    """The invariant the whole rung rests on: rung 2 supplies bytes, never
    interpretation, so identical bytes must yield identical rows."""
    html = _page()
    rows = gfid._rows_from_page_html(html, final_url=_PAGE_URL, status_code=200)
    assert len(rows) == 3
    assert all(r.flight_id for r in rows)
    assert all(a.legroom_class for r in rows for a in r.amenities)


# ───────────────────────────── the ladder ──────────────────────────────────────


class _RecordingSession:
    """Stands in for a `GfBrowserSession`: counts navigations, serves fixtures."""

    def __init__(self, result: Any = None) -> None:
        self.urls: list[str] = []
        self._result = result

    def get_html(self, url: str) -> Any:
        self.urls.append(url)
        if isinstance(self._result, Exception):
            raise self._result
        return gfb.PageFetch(html=_page(), final_url=_PAGE_URL, status_code=200)


def _filters(*, round_trip: bool) -> Any:
    legs = (Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 10, 14)),)
    if round_trip:
        legs += (Leg(origins=("LAX",), destinations=("JFK",), date=date(2026, 10, 24)),)
    return to_fli_filter(SpecificDateSearch(legs=legs, options=SearchOptions()))


@pytest.fixture
def no_rung_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rung 1 must not run at all under `--gf-transport browser`."""

    def _forbidden(_f: Any) -> list[GFlightWithId]:
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
    session = _RecordingSession()
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


@pytest.mark.parametrize("mode", ["http", "auto"])
def test_the_http_rungs_never_consult_the_browser(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """`auto` is documented as identical to `http` until the escalation rung
    lands; this is what makes that documentation true."""

    def _forbidden(*, headed: bool) -> object:
        raise AssertionError(f"mode={mode} reached the browser rung (headed={headed})")

    def _rung_one(_filters_arg: Any) -> list[GFlightWithId]:
        return gfid._rows_from_page_html(_page(), final_url=_PAGE_URL, status_code=200)

    monkeypatch.setattr(gfb, "session", _forbidden)
    monkeypatch.setattr(gfid, "_one_call", _rung_one)
    out = gfid.search_with_ids(
        _filters(round_trip=False), top_n=5, transport=gfid.GfTransport(mode=mode)
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

    def _rung_one(_filters_arg: Any) -> list[GFlightWithId]:
        pytest.fail("rung 2 fell back into rung 1's retry ladder")

    monkeypatch.setattr(gfb, "session", _hand_out)
    monkeypatch.setattr(gfid, "_one_call", _rung_one)

    with pytest.raises(GfBrowserUnavailableError):
        gfid.search_with_ids(_filters(round_trip=False), transport=gfid.GfTransport(mode="browser"))
    assert len(session.urls) == 1  # one navigation, no retry


# ───────────────────── the session: launch, navigate, close ────────────────────


@pytest.mark.gf_browser
def test_a_navigation_becomes_rows_and_the_launch_is_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pw = _install(monkeypatch, tmp_path)
    with gfb.GfBrowserSession(headed=False) as session:
        fetch = session.get_html(_PAGE_URL)
        session.get_html(_PAGE_URL)

    assert fetch.status_code == 200
    rows = gfid._rows_from_page_html(
        fetch.html, final_url=fetch.final_url, status_code=fetch.status_code
    )
    assert len(rows) == 3  # the navigation's bytes go straight into the one parser
    assert _page_of(pw).gotos == [(_PAGE_URL, "commit", 30_000)] * 2  # one launch, two navs
    assert capsys.readouterr().err.count("opening Chrome") == 1
    assert pw.chromium._context.closed and pw.stopped


@pytest.mark.gf_browser
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


@pytest.mark.gf_browser
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


@pytest.mark.gf_browser
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
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117
        with pytest.raises(GfBrowserUnavailableError, match=expected) as e:
            session.get_html(_PAGE_URL)
    # The remedy travels in the message, because both refusal renderers in
    # `cli` print `str(e)` and nothing else — and it reads as a sentence.
    assert ". Retry, or use `--gf-transport http`" in str(e.value)


@pytest.mark.gf_browser
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
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117
        with pytest.raises(GfBrowserUnavailableError, match="body could not be read"):
            session.get_html(_PAGE_URL)


@pytest.mark.gf_browser
def test_a_launch_failure_names_the_browser_not_the_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _install(monkeypatch, tmp_path, launch_error=RuntimeError("Chromium is not installed"))
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError, match="failed to launch") as e:
        session.get_html(_PAGE_URL)
    assert "Chromium is not installed" in str(e.value)


@pytest.mark.gf_browser
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


@pytest.mark.gf_browser
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


@pytest.mark.gf_browser
def test_a_missing_patchright_names_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional dependency is the most likely reason rung 2 never runs, so
    the refusal is the only place a user learns what to install."""
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    with pytest.raises(GfBrowserUnavailableError, match="patchright") as e:
        gfb._playwright_factory()
    assert "flight-cli[browser]" in str(e.value)


@pytest.mark.gf_browser
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


# ─────────────────────────────── the guard itself ──────────────────────────────


def test_the_suite_refuses_to_launch_a_real_browser() -> None:
    """Proof that `conftest.py`'s guard is armed for an unmarked test: without
    it, this call would start a driver process and open Chrome.

    It fails with pytest's own `Failed`, which is a `BaseException` — so the
    `except Exception` wrappers in `_gf_browser` and `cli` cannot swallow it."""
    with pytest.raises(BaseException, match="real browser launcher") as e:
        gfb._playwright_factory()
    assert not isinstance(e.value, Exception)


# ───────────────────────────── the CLI option ──────────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "http", "browser"])
def test_every_documented_transport_resolves(mode: str) -> None:
    resolved = _resolve_gf_transport(mode, headed=True)
    assert (resolved.mode, resolved.headed) == (mode, True)


def test_an_unknown_transport_is_rejected_by_name() -> None:
    import typer

    with pytest.raises(typer.BadParameter, match="chrome"):
        _resolve_gf_transport("chrome", headed=False)


def test_the_default_transport_is_rung_one() -> None:
    """A default of `browser` would open a window on an ordinary search; a
    default of `auto` would promise an escalation that does not exist yet."""
    assert gfid.HTTP_TRANSPORT.mode == "http"
    assert gfid.HTTP_TRANSPORT.headed is False
    assert gfid.GfTransport() == gfid.HTTP_TRANSPORT


def test_the_fixture_is_the_shape_the_page_serves() -> None:
    """Guards the helper above: if the fixture stops being a three-row `ds:1`
    payload, every parity assertion here becomes vacuous."""
    payload = json.loads((FIXTURE_DIR / "ds1_jfk_lax_3rows.json").read_text())
    assert len(gfid._rows_from_ds1(payload)) == 3
