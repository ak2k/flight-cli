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
    GfBackendError,
    GfBrowserUnavailableError,
    GfPageShapeError,
    GfThrottledError,
    GfUpstreamStatusError,
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
    """What fli's client hands back — it has already called `raise_for_status()`,
    so a response reaching us is 2xx and carries no status worth reading."""

    def __init__(self, *, text: str, url: str) -> None:
        self.text = text
        self.url = url


def test_fetch_page_reports_a_2xx_by_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_fetch_page` hands on the body and the final URL, and reports OK.

    Not laziness — fli raised on anything else before returning, so there is no
    other status this rung could truthfully report. The field exists in the
    triple for the browser rung, whose navigation reports a real one. (The
    429 -> GfThrottledError mapping around the GET is covered end-to-end in
    tests/test_gflight_page.py, including against the real fli client.)"""

    class _Client:
        def get(self, url: str, **_kw: object) -> _FakeHttpResponse:
            return _FakeHttpResponse(text="<html>board</html>", url=url)

    def _stub_tfs(_filters: Any) -> bytes:
        return b"\x08\x1c"

    def _no_seed(_client: Any) -> None:
        return None

    monkeypatch.setattr(gfid, "get_client", _Client)
    monkeypatch.setattr(gfid, "build_search_tfs", _stub_tfs)
    monkeypatch.setattr(gfid, "_seed_cookies_once", _no_seed)

    page = gfid._fetch_page(cast("Any", None))
    assert page.html == "<html>board</html>"
    assert "tfs=" in page.final_url
    assert page.status_code == 200


def test_a_server_error_is_its_own_refusal_not_a_shape_error() -> None:
    """A non-2xx degrades to Matrix through the seam every other refusal uses,
    but it is NOT a shape error: nothing was served to re-derive an extract
    from. Reading Google declining to serve as "the parser is broken" sends the
    next reader hunting an extract bug during an outage.

    Only a navigation gets here with a status — fli raises on a non-2xx before
    the curl_cffi rung can report one."""
    with pytest.raises(GfUpstreamStatusError, match="HTTP 503") as e:
        gfid._rows_from_page_html(gfid.PageFetch("", _PAGE_URL, 503))
    assert e.value.status_code == 503
    assert not isinstance(e.value, GfPageShapeError)
    # Still a GfBackendError, so the Matrix-fallback seams keep catching it.
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

    Only the browser rung can reach this with a 429 — Chrome reports the status
    where fli would have raised — and the interstitial it usually arrives as is
    caught by URL or body instead."""
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
    """Stands in for a `GfBrowserSession`: counts navigations, serves fixtures."""

    def __init__(self, result: Any = None) -> None:
        self.urls: list[str] = []
        self._result = result

    def get_html(self, url: str) -> Any:
        self.urls.append(url)
        if isinstance(self._result, Exception):
            raise self._result
        return gfid.PageFetch(html=_page(), final_url=_PAGE_URL, status_code=200)


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

    def _rung_one(_filters_arg: Any) -> list[GFlightWithId]:
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
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117
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
    with gfb.GfBrowserSession(headed=False) as session:  # noqa: SIM117
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
    assert _resolve_gf_transport(mode) == mode


def test_an_unknown_transport_is_rejected_by_name() -> None:
    import typer

    with pytest.raises(typer.BadParameter, match="chrome"):
        _resolve_gf_transport("chrome")


def test_resolving_a_transport_does_not_load_the_google_flights_stack() -> None:
    """Every search validates this flag, including Matrix-only ones, so it must
    not drag in `_gflight_ids` — that module costs fli's import, and a Matrix
    search has no use for it. `_gflight_results` builds the value instead."""
    import subprocess
    import sys

    probe = (
        "import sys, flight_cli.cli as c;"
        "c._resolve_gf_transport('browser');"
        "print('flight_cli._gflight_ids' in sys.modules)"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


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
    import io

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


def test_the_help_text_keeps_the_extra_and_installs_with_uv() -> None:
    """The same markup trap, on the one line that tells a user how to get the
    rung at all. `uv` because that is this project's package manager."""
    from typer.testing import CliRunner

    from flight_cli import cli

    result = CliRunner().invoke(cli.app, ["search", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "uv pip install 'flight-cli[browser]'" in flat


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
    from flight_cli.cli import _GF_TRANSPORT_BROWSER, _gf_refusal

    http = _gf_refusal(GfThrottledError("x"))
    browser = _gf_refusal(GfThrottledError("x"), transport=_GF_TRANSPORT_BROWSER)
    assert "does not" in _render(browser.message)
    assert "--backend matrix" in _render(browser.message)
    assert "retry" in _render(http.message).lower()
    assert browser.note != http.note
    for r in (http, browser):
        assert "this IP" not in _render(r.message)


def test_a_multi_cabin_browser_search_says_it_is_using_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chromium single-instances the profile dir, so a thread-per-cabin fan-out
    cannot each hold one. Downgrading is right; doing it silently is not.

    The dispatch is stubbed out: this is about the line the user sees, and the
    real one would query two backends over the network."""
    from typer.testing import CliRunner

    from flight_cli import cli

    dispatched: list[str] = []

    def _stub(**_kw: Any) -> None:
        dispatched.append("multi")

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _stub)
    monkeypatch.setattr(cli, "_run_gflight_path_multi", _stub)

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
            "--gf-transport",
            "browser",
            "--cash-only",
            "-n",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert dispatched == ["multi"]
    assert result.output.count("multi-cabin uses http") == 1  # said once, not per cabin


def test_no_downgrade_note_when_google_flights_is_never_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--backend matrix` runs no rung at all, so there is nothing to downgrade.
    Announcing one describes a decision nobody made, and points the user at a
    transport flag that had no bearing on the search they ran."""
    from typer.testing import CliRunner

    from flight_cli import cli

    dispatched: list[str] = []

    def _stub(**_kw: Any) -> None:
        dispatched.append("multi")

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _stub)
    monkeypatch.setattr(cli, "_run_gflight_path_multi", _stub)

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
    assert dispatched == ["multi"]  # it still ran, it just says nothing about rungs
    assert "multi-cabin uses http" not in result.output


def test_a_single_cabin_browser_search_prints_no_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart: the line must not fire where the browser rung is live."""
    from typer.testing import CliRunner

    from flight_cli import cli

    def _stub(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(cli, "_run_gflight_path", _stub)
    monkeypatch.setattr(cli, "_run_enriched_path", _stub)

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
            "--cash-only",
            "-n",
            "1",
            "--fast",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "multi-cabin" not in result.output


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
    board = gfid._rows_from_ds1(payload)
    assert len(board.rows) == 3
    assert board.blocks_seen == 2  # both row blocks present, so an empty board is authoritative
    assert board.misplaced == ()  # no relocated rows, so the parser reaches the rows at all
