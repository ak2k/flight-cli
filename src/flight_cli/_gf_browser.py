"""Rung 2 of the Google Flights page transport: a real Chrome navigation.

Rung 1 GETs `https://www.google.com/travel/flights?tfs=…` over curl_cffi. It
reaches Google's backend — the TLS fingerprint passes — but a thin client earns
a small rate budget, and when that budget runs out the page comes back as a
`/sorry/` interstitial. Real Chrome, driving the same URL, gets the generous
budget: measured 2026-06-15, patchright with `channel="chrome"` pulled 10/10
results from an IP that was simultaneously throttling curl_cffi.

This module supplies **bytes only**. `_gflight_ids._rows_from_page_html` reads
them, exactly as it reads rung 1's, so there is one parser and one set of
verdicts about what a block means. Verified 2026-09-02 on JFK-LAX: curl_cffi
and a headless Chrome navigation of the same URL in the same minute both
decoded 30 rows with the identical first `flight_id`.

Three things are deliberate and easy to undo by accident:

- **`response.text()`, never `page.content()`.** `content()` serializes the
  live DOM, which Google's own JavaScript has already rewritten; `text()` is
  the HTTP response body, the same bytes curl_cffi sees, `ds:1` blob intact.
- **`wait_until="commit"`.** `response.text()` waits for the body regardless,
  so a later readiness state buys nothing. Measured the same day:
  `commit` and `domcontentloaded` returned the same 30 rows and the same id.
- **The session is thread-local.** The CLI runs the query inside
  `anyio.to_thread.run_sync`; a playwright object touched from a thread other
  than the one that made it raises `greenlet.error` and strands a live Chrome.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from rich.console import Console

from ._gf_errors import GfBrowserUnavailableError
from ._gflight_ids import PageFetch, cache_dir

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

log = logging.getLogger(__name__)

# DIVERGE: a transport module writing to the console. The launch notice lives at
# the launch site, not in `cli`, because opening Chrome is a visible window and
# tens of seconds of latency — the one thing no caller may forget to announce.
_err = Console(stderr=True)

_NAV_TIMEOUT_MS = 30_000
# `commit` returns as soon as the navigation commits; the body arrives with
# `response.text()` either way (see the module docstring).
_WAIT_UNTIL = "commit"
# Escape hatch for a Chrome that isn't where `channel="chrome"` looks; pointing
# it at a missing binary is also how a caller forces the typed refusal.
_BROWSER_BIN_ENV = "FLIGHT_CLI_GF_BROWSER_BIN"
_PROFILE_DIR_NAME = "gf-browser-profile"
# Chromium's single-instance guard writes these into the profile dir and
# normally removes them on a clean exit; a survivor means another process holds
# the profile, or an interrupted run left it behind.
_SINGLETON_GLOB = "Singleton*"

_INSTALL_HINT = (
    "Install it with `uv pip install 'flight-cli[browser]'` "
    "(plus a one-time `uvx --from patchright patchright install chrome`)."
)


def _profile_dir() -> pathlib.Path:
    """Rung 2's Chrome profile, under the CLI cache dir.

    Its own directory, never `pp.auth.BROWSER_PROFILE_DIR`: that profile holds
    a logged-in PointsPath session, and a search backend has no business
    reading it — nor colliding with a login flow over Chromium's
    single-instance lock. This profile holds nothing but Google's own
    anonymous session cookies."""
    return cache_dir() / _PROFILE_DIR_NAME


def _profile_is_locked(profile: pathlib.Path) -> bool:
    """True when the profile still carries Chromium's single-instance files.

    `Path.exists()` is the wrong test: `SingletonLock` is a symlink to
    `<host>-<pid>` and reads as missing once that target is gone, which is
    exactly the interrupted-run case worth naming."""
    try:
        return any(p.is_symlink() or p.exists() for p in profile.glob(_SINGLETON_GLOB))
    except OSError:
        return False


def _launch_failure(profile: pathlib.Path, detail: str) -> GfBrowserUnavailableError:
    """Turn a launch failure into a refusal whose remedy names the likely cause.

    A locked profile is the one failure the user can clear themselves, and the
    two ways in — a second `flight` running now, or a run killed mid-navigation
    — need different actions, so both are spelled out."""
    if _profile_is_locked(profile):
        return GfBrowserUnavailableError(
            f"Chrome could not open Google Flights' browser profile at {profile}: {detail}",
            remedy=(
                "Another `flight` process is holding it, or an interrupted run left it "
                f"locked; wait for the other run to finish, or remove {profile}/Singleton* "
                "and retry."
            ),
        )
    return GfBrowserUnavailableError(f"Chrome failed to launch for Google Flights: {detail}")


def _playwright_factory() -> Callable[[], Any]:
    """patchright's `sync_playwright`, imported on demand.

    A seam, not a convenience. The guarded import keeps patchright optional for
    everyone who never reaches rung 2, and `tests/conftest.py` replaces this
    function so that any test which would have launched a browser fails
    instead."""
    try:
        from patchright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as e:
        raise GfBrowserUnavailableError(
            "Google Flights' browser rung needs patchright, which isn't installed.",
            remedy=_INSTALL_HINT,
        ) from e
    return sync_playwright


_notice_state: dict[str, bool] = {"printed": False}


def _announce() -> None:
    """One line, once per process, before the first launch.

    Rung 2 opens a window and can take the better part of a minute; without
    this the user watches a silent terminal and an unexplained Chrome."""
    if _notice_state["printed"]:
        return
    _notice_state["printed"] = True
    _err.print("[dim]Google Flights: opening Chrome (rung 2)…[/]")


class GfBrowserSession:
    """A persistent-context Chrome, opened lazily and reused across legs.

    Lazily because constructing a session must stay free: `session()` hands one
    out before anyone knows whether a leg will actually need it. Reused because
    the launch is the expensive part (~2-5 s) and a round trip fetches one page
    per pinned outbound.
    """

    def __init__(self, *, headed: bool) -> None:
        self._headed = headed
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def __enter__(self) -> GfBrowserSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_html(self, url: str) -> PageFetch:
        """Navigate to `url` and hand back the response body and status.

        Every failure below is the same fact to the caller — rung 2 could not
        produce bytes — and none of them says anything about the route, so they
        share one type. What Google *answered* (a throttle, a consent wall) is
        not decided here; that is the parser's call, from these three fields."""
        page = self._ensure_page()
        try:
            response = page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)
        # patchright's error tree is broad, and all of it means the same thing: no bytes.
        except Exception as e:
            raise GfBrowserUnavailableError(
                f"Chrome could not load Google Flights' search page: {_detail(e)}"
            ) from e
        if response is None:
            raise GfBrowserUnavailableError(
                "Chrome navigated to Google Flights' search page but returned no response."
            )
        try:
            html = response.text()
            final_url = str(response.url)
            status_code = int(response.status)
        # A body that cannot be read is the same refusal as one that never arrived.
        except Exception as e:
            raise GfBrowserUnavailableError(
                f"Chrome loaded Google Flights' search page but its body could not be read: "
                f"{_detail(e)}"
            ) from e
        return PageFetch(html=html, final_url=final_url, status_code=status_code)

    def close(self) -> None:
        """Shut the context and the driver down; swallow every failure.

        Idempotent, and called from a `finally` — a close that raised would
        replace the search's own error with a teardown one."""
        context, playwright = self._context, self._playwright
        self._page = self._context = self._playwright = None
        if context is not None:
            _swallow("context", context.close)
        if playwright is not None:
            _swallow("driver", playwright.stop)

    def _ensure_page(self) -> Any:
        """This session's page, launching Chrome on first use."""
        if self._page is not None:
            return self._page
        factory = _playwright_factory()  # its own typed refusal; never re-wrapped below
        profile = _profile_dir()
        _announce()
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise GfBrowserUnavailableError(
                f"Google Flights' browser profile directory {profile} could not be created: {e}"
            ) from e
        try:
            self._playwright = factory().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=not self._headed,
                no_viewport=True,
                **_launch_target(),
            )
            self._page = self._context.new_page()
        # Any launch failure — missing Chrome, locked profile, driver crash — is one
        # refusal to the caller, who cannot act on the distinctions patchright draws.
        except Exception as e:
            self.close()
            raise _launch_failure(profile, _detail(e)) from e
        return self._page


def _swallow(what: str, shutdown: Callable[[], object]) -> None:
    """Run one teardown step, logging rather than raising. Teardown runs from a
    `finally`, where a raise would replace the real error with this one."""
    try:
        shutdown()
    except Exception as e:  # noqa: BLE001 — teardown is best-effort, never fatal
        log.debug("could not close the gflight browser %s: %s", what, e)


def _launch_target() -> dict[str, str]:
    """Which Chrome to run. `channel="chrome"` uses the installed real Chrome,
    whose fingerprint is the whole point of this rung; an explicit binary
    replaces the channel rather than joining it (patchright rejects both)."""
    override = os.environ.get(_BROWSER_BIN_ENV)
    return {"executable_path": override} if override else {"channel": "chrome"}


def _detail(e: BaseException) -> str:
    """The actionable part of a driver error, punctuated.

    patchright appends a multi-line call log to every message and names its
    whole exception tree `Error`, so neither the class nor the tail helps a
    user. The terminal period matters: the remedy sentence is concatenated
    after this, and without it the two run together."""
    lines = [line.strip() for line in str(e).strip().splitlines() if line.strip()]
    text = lines[0] if lines else e.__class__.__name__
    return text if text.endswith((".", "!", "?")) else f"{text}."


_sessions = threading.local()


def session(*, headed: bool) -> GfBrowserSession:
    """This thread's session, created (not launched) on first ask.

    Thread-local, because the caller runs inside `anyio.to_thread.run_sync` and
    a playwright object is bound to its creating thread. Every leg of one
    search runs in that thread, so the launch is paid once per search rather
    than once per leg."""
    existing: GfBrowserSession | None = getattr(_sessions, "current", None)
    if existing is None:
        existing = GfBrowserSession(headed=headed)
        _sessions.current = existing
    return existing


def close_thread_session() -> None:
    """Close this thread's session if it made one. Idempotent, and safe to call
    on a thread that never touched rung 2."""
    existing: GfBrowserSession | None = getattr(_sessions, "current", None)
    _sessions.current = None
    if existing is not None:
        existing.close()


def _close_at_exit() -> None:
    """Best-effort close for a session the *exiting* thread owns.

    Not a substitute for the caller's `finally`, and structurally cannot become
    one: `_sessions` is thread-local, so an interpreter shutdown running on the
    main thread sees only a main-thread session. A worker thread's session is
    closed by that worker or not at all — reaching across would raise
    `greenlet.error` and strand the Chrome it was trying to kill."""
    try:
        close_thread_session()
    except Exception as e:  # noqa: BLE001 — the interpreter is going away; nothing to report to
        log.debug("atexit close of the gflight browser session failed: %s", e)


atexit.register(_close_at_exit)
