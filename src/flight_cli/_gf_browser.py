"""Rung 2 of the Google Flights page transport: a real Chrome navigation.

Rung 1 GETs `https://www.google.com/travel/flights?tfs=…` over curl_cffi. It
reaches Google's backend — the TLS fingerprint passes — but a thin client earns
a small rate budget, and when that budget runs out the page comes back as a
`/sorry/` interstitial. Real Chrome, driving the same URL, gets the generous
budget: measured 2026-06-15, patchright with `channel="chrome"` pulled 10/10
results from an IP that was simultaneously throttling curl_cffi.

This module supplies **bytes only**. `_gflight_ids._rows_from_page_html` reads
them, exactly as it reads rung 1's, so there is one parser and one set of
verdicts about what a block means. Both rungs read one board because both send
the `HeadlessChrome` UA token, by which Google picks the board it serves a
multi-airport search; a headed window is given it (`_send_the_headless_token`).
Measured 2026-10-05 on NYC-LON: 300 rows from USD488 under the token, a curated
72 from USD679 without it. A single airport pair reads one board either way.

Three things are deliberate and easy to undo by accident:

- **`response.text()` first, `page.content()` only when Chrome has dropped
  the body.** `text()` is the HTTP response body, the same bytes curl_cffi
  sees, `ds:1` blob intact. `content()` serializes the live DOM, which Google's
  own JavaScript has already rewritten, so it is the fallback and not the rule
  (`_navigated_page`).
- **`wait_until="domcontentloaded"`, not `commit`.** Both return the same 30
  rows and the same first id (measured 2026-09-02), so this is not about what
  arrives — it is about what bounds it. `response.text()` takes NO timeout, at
  any layer: patchright sends the body request with no deadline, and the driver
  arms its timer only when one is supplied, so a stalled body read hangs
  forever. `commit` returns before the body exists and leaves that read outside
  `goto`'s ceiling; `domcontentloaded` puts it inside, which is the only
  timeout rung 2 has. A default round trip makes 11 of these reads.
- **The session is thread-local.** The CLI runs the query inside
  `anyio.to_thread.run_sync`; a playwright object touched from a thread other
  than the one that made it raises `greenlet.error` and strands a live Chrome.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import re
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from rich.console import Console

from ._gf_common import PageFetch, cache_dir
from ._gf_errors import BROWSER_DEFAULT_REMEDY, GfBrowserUnavailableError

if TYPE_CHECKING:
    import pathlib
    import types
    from collections.abc import Callable, Generator

log = logging.getLogger(__name__)
# Resolved at import because `stop_driver` touches it from inside a signal
# handler. `getLogger` takes the logging module lock, and a handler runs between
# two bytecodes of whatever the main thread was doing — including a frame that
# already holds it. `setLevel` alone is safe there (that lock is re-entrant, and
# nothing under it does I/O); a lookup would be one more acquisition for nothing.
_asyncio_log = logging.getLogger("asyncio")

# DIVERGE: a transport module writing to the console. The launch notice lives at
# the launch site, not in `cli`, because launching Chrome costs seconds a caller
# would otherwise wait through unexplained — the one thing none may forget.
_err = Console(stderr=True)

_NAV_TIMEOUT_MS = 30_000
# The one bound on the body read: `response.text()` has no timeout of its own,
# so the wait state has to hold the body inside `goto`'s ceiling (module docstring).
_WAIT_UNTIL = "domcontentloaded"
# One deadline for the whole of `capture` — navigation, click and the wait for
# the page's own request to finish — so a slow step shortens the next one's
# allowance instead of adding its own.
_CAPTURE_TIMEOUT_S = 45.0
_CLICK_TIMEOUT_MS = 20_000
# Reading the page after a failed click gets its own bound: the capture's
# deadline is usually spent by then, and a zero budget would mean no timeout.
_SNAPSHOT_TIMEOUT_MS = 3_000
# Enough to show what a page offered without printing a whole page of controls.
_SHOWN_BUTTONS = 40
_SHOWN_CHARS = 200
# One line of patchright's ARIA snapshot per button: `- button "Name"`, the
# name JSON-quoted, then optionally a colon and the button's text. A key that
# YAML would misread (a name holding `: `, ` #` or a brace, say) is wrapped
# whole in single quotes, each `'` in it doubled: `- 'button "Stops: Any"'`.
_BUTTON_LINE = re.compile(
    r"""^\s*- (?:button ("(?:[^"\\]|\\.)*")|'button ("(?:[^"\\']|\\.|'')*"))""", re.MULTILINE
)
# What Chrome says when asked for a body it has already dropped from its buffer.
_EVICTED = "Request content was evicted from inspector cache"
# The sync API delivers events only while one of its calls is running, so the
# capture waits in short driver-side sleeps and reads what they delivered.
_POLL_MS = 100
# A driver that answers `start()` with garbage is still running; it exits on
# stdin EOF, and one still up after this is killed, because the refusal drops
# the session's only handle on it.
_DRIVER_EXIT_TIMEOUT_S = 5.0
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

# A launch failure is usually a missing Chrome, and the bare default remedy
# ("Retry…") never says so — it reads as a transient blip for a condition that
# will fail identically forever. patchright finds Chrome by channel, so the two
# fixes are installing it or pointing the override at it.
_LAUNCH_REMEDY = (
    "Install Chrome (`uvx --from patchright patchright install chrome`), or point "
    f"`{_BROWSER_BIN_ENV}` at an existing binary. {BROWSER_DEFAULT_REMEDY}"
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
    — need different actions, so both are spelled out.

    Both the profile state AND the driver text have to agree before we call it a
    lock. A `SIGKILL`ed run leaves `Singleton*` behind indefinitely, so the
    profile test alone would blame the lock for every later failure — including
    a missing Chrome, whose real cause would then go unnamed while the user
    deletes lock files that were never the problem. The default remedy is
    appended either way, so `--gf-transport http` is offered even when the
    lock diagnosis is right and the user cannot clear it."""
    if _profile_is_locked(profile) and "singleton" in detail.lower():
        return GfBrowserUnavailableError(
            f"Chrome could not open Google Flights' browser profile at {profile}: {detail}",
            remedy=(
                "Another `flight` process is holding it, or an interrupted run left it "
                f"locked; wait for the other run to finish, or remove {profile}/Singleton* "
                f"and retry. {BROWSER_DEFAULT_REMEDY}"
            ),
        )
    return GfBrowserUnavailableError(
        f"Chrome failed to launch for Google Flights: {detail}", remedy=_LAUNCH_REMEDY
    )


def _driver_failure(detail: str) -> GfBrowserUnavailableError:
    """The refusal for a node driver that would not start.

    That is patchright's own install, not Chrome: the launch has not been tried,
    so `_LAUNCH_REMEDY` would send the user to a browser that is not the missing
    piece."""
    return GfBrowserUnavailableError(
        f"patchright's driver failed to start for Google Flights: {detail}",
        remedy=f"{_INSTALL_HINT} {BROWSER_DEFAULT_REMEDY}",
    )


def _quiet_driver_failure(manager: Any) -> None:
    """Keep asyncio from reporting again the error a failed `start()` raised.

    What that start left on the loop patchright made for this manager reports
    itself, with a traceback, when collected: the error parked where nothing
    awaits it (the transport's `on_error_future`, the connection's `init`
    task), an `init` task still waiting on a driver that sent garbage, and that
    driver's pipes, whose finalizer raises once the loop's own has closed it.
    So the loop's handler drops every report, and a driver that was spawned is
    let exit on stdin EOF, or killed at `_DRIVER_EXIT_TIMEOUT_S`, and reaped on
    that loop now, which leaves its pipes nothing to do. Its stdout is drained
    meanwhile, as patchright's own stop does: nothing else reads it once
    `start()` has raised, and a driver with more to write than the pipe holds
    would block on its way out. A loop patchright did not make is the caller's
    and keeps its reports. A lookup, as `_driver_process_id` is: a patchright
    build that moves an attribute leaves the report printed and the refusal
    intact."""
    try:
        loop, own_loop = manager._loop, manager._own_loop
    except AttributeError:
        return
    if not own_loop:
        return
    loop.set_exception_handler(_drop_report)
    try:
        driver = manager._connection._transport._proc
    except AttributeError:
        return
    try:
        loop.run_until_complete(asyncio.wait_for(driver.communicate(), _DRIVER_EXIT_TIMEOUT_S))
    except TimeoutError:
        # Through asyncio's handle, which signals nothing once the child is
        # reaped, so a pid the OS has reused is never hit.
        with contextlib.suppress(ProcessLookupError):
            driver.kill()
        with contextlib.suppress(TimeoutError):
            loop.run_until_complete(asyncio.wait_for(driver.wait(), _DRIVER_EXIT_TIMEOUT_S))


def _drop_report(_loop: asyncio.AbstractEventLoop, _context: dict[str, Any]) -> None:
    pass


def _parked_driver_error(manager: Any) -> Exception | None:
    """The error a driver that exited before the handshake left on patchright's
    `init` task. `start()` then raises an `AttributeError` about patchright's own
    state, which names no cause a user can act on. A lookup, as
    `_driver_process_id` is."""
    try:
        task = manager._connection._init_task
        error = task.exception() if task.done() and not task.cancelled() else None
    except AttributeError:
        return None
    return error if isinstance(error, Exception) else None


def _playwright_factory() -> Callable[[], Any]:
    """patchright's `sync_playwright`, imported on demand.

    A seam, not a convenience. The guarded import keeps patchright optional for
    everyone who never reaches rung 2, and `tests/conftest.py` replaces this
    function so that any test which would have launched a browser fails
    instead."""
    try:
        # Deferred: patchright is an optional extra, and importing it at module
        # scope would make `flight` unrunnable for everyone who never uses rung 2.
        from patchright.sync_api import sync_playwright  # noqa: PLC0415 — see above
    except ImportError as e:
        raise GfBrowserUnavailableError(
            "Google Flights' browser rung needs patchright, which isn't installed.",
            remedy=_INSTALL_HINT,
        ) from e
    return sync_playwright


_notice_state: dict[str, bool] = {"printed": False}


def _announce() -> None:
    """One line, once per process, before the first launch.

    Rung 2 costs a browser launch and a few seconds per search; without this
    the user watches a silent terminal, and with --gf-headed an unexplained
    Chrome window."""
    if _notice_state["printed"]:
        return
    _notice_state["printed"] = True
    _err.print("[dim]Google Flights: opening Chrome (rung 2)…[/]")


def announce_escalation() -> None:
    """The line an `auto` search prints when a throttle moves it to Chrome, in
    place of `_announce`'s: it says Chrome is opening, and why. Silent once an
    interrupt has been seen: `_ensure_page` refuses that launch, so nothing opens."""
    if _interrupt_state["seen"]:
        return
    _notice_state["printed"] = True
    _err.print(
        "[dim]Google Flights rate-limited the request; opening Chrome (rung 2) for the "
        "rest of this search…[/]"
    )


class Control(NamedTuple):
    """A control on the page, found the way assistive technology finds it: by
    ARIA role and exact accessible name. Not a CSS selector, which Google's
    generated class names change without notice."""

    role: str
    name: str


class CapturedResponse(NamedTuple):
    """One response the page's own code received, with its whole body."""

    url: str
    status: int
    body: str


class GfBrowserSession:
    """A persistent-context Chrome, opened lazily and reused across legs.

    Lazily because constructing a session must stay free: `session()` hands one
    out before anyone knows whether a leg will actually need it. Reused because
    the launch is the expensive part (~2-5 s) and a round trip fetches one page
    per pinned outbound.
    """

    def __init__(self, *, headed: bool) -> None:
        self._headed = headed
        # The manager `sync_playwright()` returns, kept for the whole life of the
        # session because it is the only handle on the driver PROCESS.
        # `_playwright` is what that manager started, and it names no subprocess.
        self._manager: Any = None
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        # "This browser is finished, and its sync API must not be driven again."
        # See `close`: after an interrupt unwinds a patchright call, every
        # further call through that API spins forever.
        self._dead = False

    @property
    def finished(self) -> bool:
        """True once this browser is done and its sync API must not be driven again.

        A property rather than a bare read of `_dead`: `close_thread_session`
        needs this from module scope, where reading the attribute directly is a
        `reportPrivateUsage` error."""
        return self._dead

    def __enter__(self) -> GfBrowserSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_html(self, url: str) -> PageFetch:
        """Navigate to `url` and hand back the page's HTML and status.

        Every failure below is the same fact to the caller — rung 2 could not
        produce bytes — and none of them says anything about the route, so they
        share one type. What Google *answered* (a throttle, a consent wall) is
        not decided here; that is the parser's call, from these three fields."""
        page = self._ensure_page()
        try:
            response = page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT_MS)
        # patchright's error tree is broad, and all of it means the same thing: no bytes.
        except Exception as e:
            self._interrupted_or_raise()
            raise GfBrowserUnavailableError(
                f"Chrome could not load Google Flights' search page: {_detail(e)}"
            ) from e
        # Nothing is caught here: the arm records that a patchright call was
        # unwound by something that is not an error — the user's interrupt — and
        # re-raises it untouched. What it records is that the greenlet running
        # this session's event loop died with it, which is what `close` reads.
        except BaseException:
            self._dead = True
            raise
        if response is None:
            raise GfBrowserUnavailableError(
                "Chrome navigated to Google Flights' search page but returned no response."
            )
        try:
            return _navigated_page(page, response)
        # A body that cannot be read is the same refusal as one that never arrived.
        except Exception as e:
            self._interrupted_or_raise()
            raise GfBrowserUnavailableError(
                f"Chrome loaded Google Flights' search page but its body could not be read: "
                f"{_detail(e)}"
            ) from e
        # The same record one step later, and for the same reason.
        except BaseException:
            self._dead = True
            raise

    def capture(
        self,
        url: str,
        wanted: Callable[[str], bool],
        *,
        click: Control | None = None,
        check_page: Callable[[PageFetch], object] | None = None,
        timeout_s: float = _CAPTURE_TIMEOUT_S,
    ) -> CapturedResponse:
        """Navigate to `url`, optionally click one control, and return the first
        response the page itself receives whose URL `wanted` accepts.

        The page makes the request, so it carries whatever the page signs it
        with; nothing here runs script in the page or touches a request.

        The listeners go on BEFORE the navigation, so a request the page fires
        while loading is seen. The page is reused across calls, so only requests
        issued after this navigation's own main-frame request count: a late
        answer to the previous document's request is not this page's answer.

        The body is read only once the request has finished — `response.text()`
        takes no timeout, and a chunked body read earlier would come back cut
        short or never. Navigation, click and that wait all draw on one deadline
        of `timeout_s`.

        `check_page` is handed the navigation's own page before any click (its
        body, or its DOM once Chrome has dropped the body), and refuses by
        raising: a throttle or consent interstitial has no control to click, so
        without it those walls would surface as a click timeout. Its exception
        propagates unwrapped. Every other failure is
        `GfBrowserUnavailableError`; a failed click's carries what the page
        showed (URL, title, visible buttons), read after the failure."""
        page = self._ensure_page()
        deadline = time.monotonic() + timeout_s
        requests: list[Any] = []
        responses: list[Any] = []
        finished: list[Any] = []

        # Functions, not bound `list.append`s: the driver caches its wrapper on
        # the handler object, and a builtin method takes no attributes.
        def on_request(request: Any) -> None:
            requests.append(request)

        def on_response(response: Any) -> None:
            responses.append(response)

        def on_finished(request: Any) -> None:
            finished.append(request)

        installed: list[tuple[str, Callable[[Any], None]]] = []
        try:
            for event, handler in (
                ("request", on_request),
                ("response", on_response),
                ("requestfinished", on_finished),
            ):
                page.on(event, handler)
                installed.append((event, handler))
            nav = self._drive(
                "Chrome could not load Google Flights' page",
                lambda: page.goto(
                    url, wait_until=_WAIT_UNTIL, timeout=_budget_ms(deadline, _NAV_TIMEOUT_MS)
                ),
            )
            if nav is None:
                raise GfBrowserUnavailableError(
                    "Chrome navigated to Google Flights' page but returned no response."
                )
            if check_page is not None:
                check_page(
                    self._drive(
                        "Chrome loaded Google Flights' page but its body could not be read",
                        lambda: _navigated_page(page, nav),
                    )
                )
            if click is not None:
                try:
                    self._drive(
                        f"Chrome could not click {click.name!r} on Google Flights' page",
                        lambda: page.get_by_role(click.role, name=click.name, exact=True).click(
                            timeout=_budget_ms(deadline, _CLICK_TIMEOUT_MS)
                        ),
                    )
                except GfBrowserUnavailableError as e:
                    raise GfBrowserUnavailableError(
                        f"{e.reason} {self._page_showed(page)}", remedy=e.remedy
                    ) from e
            while (hit := _first_finished_match(requests, responses, finished, wanted)) is None:
                self._drive(
                    "Chrome stopped while waiting for Google Flights' page",
                    lambda: page.wait_for_timeout(_budget_ms(deadline, _POLL_MS)),
                )
            body = self._drive(
                "Chrome received the page's response but its body could not be read", hit.text
            )
            return CapturedResponse(url=str(hit.url), status=int(hit.status), body=body)
        finally:
            for event, handler in installed:
                page.remove_listener(event, handler)

    def _drive[T](self, failure: str, step: Callable[[], T]) -> T:
        """Run one call into the driver with this session's failure handling.

        The same two arms as `get_html`: an error is a refusal, unless it is the
        echo of a stop we made; an interrupt records that the driver's greenlet
        died and goes on untouched."""
        try:
            return step()
        except GfBrowserUnavailableError:
            raise
        # patchright's error tree is broad, and all of it means the same thing here.
        except Exception as e:
            self._interrupted_or_raise()
            raise GfBrowserUnavailableError(f"{failure}: {_detail(e)}") from e
        except BaseException:
            self._dead = True
            raise

    def _page_showed(self, page: Any) -> str:
        """What the page showed after a failed step, as one sentence.

        A failure to read it is that sentence instead: the step's own failure is
        what the caller needs, and a second error would hide it."""
        try:
            return self._drive(
                "Chrome could not read the page afterward", lambda: _describe_page(page)
            )
        except GfBrowserUnavailableError as e:
            return e.reason

    def _stop_a_late_driver_or_raise(self, manager: Any) -> None:
        """Stop a driver that finished starting after the interrupt went past.

        The stop reaches only drivers that already exist, so one still starting
        when the handler ran is invisible to it — and the handler took
        `_manager` on its way past, so the local handle is the only one left
        naming the process this call spawned. Stopping it here is what keeps the
        browser from being launched after the shutdown."""
        if not _interrupt_state["seen"]:
            return
        self._manager = manager
        self.stop_driver()
        raise KeyboardInterrupt

    def _interrupted_or_raise(self) -> None:
        """Report a navigation that failed BECAUSE we stopped it as the interrupt
        it is, rather than letting the caller wrap it as a refusal.

        Killing the driver is how a stop reaches a navigation running on a thread
        no exception can reach, and what that navigation then raises is an
        ordinary transport error. Wrapped as one it becomes "Chrome was
        unreachable" — which the pin loop absorbs, ending a run the user asked to
        end with a warning about a network they never had trouble with."""
        if self._dead:
            raise KeyboardInterrupt

    def stop_driver(self) -> None:
        """Stop this session's browser from ANY thread, including a signal handler.

        The one teardown that is neither loop-bound nor greenlet-bound: killing
        the driver process. Every other way out — `context.close()`,
        `playwright.stop()` — goes through patchright's sync API, which may only
        be driven by the thread that built the object and only while that
        thread's event loop is alive. A worker sitting in `page.goto` satisfies
        neither, and cannot be woken by an exception or by cancellation; only the
        transport dying wakes it.

        Chrome needs no signal of its own. The driver holds its remote-debugging
        pipe and Chrome exits when that peer disappears. That exit is asynchronous
        to this call and to the CLI's — nothing here waits for it — and it is what
        clears `Singleton*`, within about a tenth of a second of the CLI's exit.
        That figure is a bound that has held rather than a measurement: the check
        behind it polls at 0.1 s and cannot resolve a span that short, and the
        settle moves with load. Nothing here should be tightened on a rerun.

        `SIGKILL` rather than the `SIGINT` the driver handles gracefully: the
        graceful path writes its last frames into a Python that is already
        unwinding, and the `EPIPE` that follows is an unhandled `error` event —
        25 lines of Node stack on the user's terminal, on the run they asked to
        end. A dead driver writes nothing. POSIX-only; this rung runs a real
        Chrome and has no Windows path.

        The pid is resolved HERE and not recorded at launch, because the session
        is registered before `start()` is called: an interrupt after the driver is
        spawned inside `start()` still finds the process that call spawned.

        The recorded manager can in principle name a driver that already exited
        and whose pid the OS reused — `close` clears it only after
        `playwright.stop()` returns, so that window spans a normal teardown. It
        is accepted rather than closed: the alternative is a liveness check that
        is itself racy, and every caller here is an interrupt path where the
        driver was alive moments earlier.

        The session is dead afterwards whether or not a pid was found, because
        the caller has already decided this browser is finished."""
        manager, self._manager = self._manager, None
        self._dead = True
        # Killing the driver mid-handshake leaves patchright's own `init` task
        # holding the transport error, and asyncio reports every future nobody
        # retrieved — measured at 14 stderr lines and a traceback, about a
        # connection this process cut on purpose. Raised on every stop, before
        # the pid is even looked up, because the report is written when the task
        # is collected rather than when it fails: a stop that found no pid still
        # severs the pipe by dropping the manager. The narrower fix — an
        # exception handler on the driver's own loop, or retrieving that one
        # future — needs a fourth private patchright attribute and cannot be
        # reached from a signal handler, so this takes the blunt one. It costs
        # asyncio's diagnostics for the rest of the process's life, bought by the
        # fact that a stop is already under way.
        _asyncio_log.setLevel(logging.CRITICAL)
        pid = _driver_process_id(manager)
        if pid is None:
            # The arm below reports the kill it could not make; without this one
            # a driver whose pid this build cannot name is indistinguishable
            # from a session that had no driver to stop.
            log.debug("no driver pid for this gflight session; nothing to stop")
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:  # already gone, or never ours
            log.debug("could not stop the gflight browser driver %d: %s", pid, e)

    def close(self) -> None:
        """Shut the context and the driver down.

        Idempotent, and called from a `finally`, so every `Exception` a teardown
        step raises is logged and dropped: raising one here would replace the
        search's own error with it.

        A `KeyboardInterrupt` is the one exception to that. The driver shutdown
        still runs, and the interrupt surfaces after it. Dropping it would let
        `--fast` finish rendering a table after the user asked the process to
        stop.

        None of that holds once the session is dead, and the sync API must not be
        touched then. An interrupt that unwinds a patchright call kills the
        greenlet its event loop runs in; a `context.close()` after that posts a
        task to a loop nobody drives and spins on a dead greenlet — measured at
        30 s and still going, ended only by `SIGKILL`. Stopping the driver IS the
        close in that state."""
        if self._dead:
            # Stop FIRST, then drop the handles. Four things set the dead flag
            # and only one of them killed anything — the stop itself did, the
            # three arms recording an unwound patchright call did not — and
            # dropping the handles makes the driver unreachable for good.
            # Stopping here is what makes "dead" mean "the driver is down"
            # whichever one set it. On the path that already killed, this is a
            # no-op.
            self.stop_driver()
            self._page = self._context = self._playwright = None
            _forget(self)
            return
        context, playwright = self._context, self._playwright
        self._page = self._context = self._playwright = None
        from_context: KeyboardInterrupt | None = None
        from_driver: KeyboardInterrupt | None = None
        try:
            if context is not None:
                from_context = _swallow("context", context.close)
        finally:
            # The driver shutdown runs even when a signal lands in the gap
            # between the two steps: skipping it would leave a Chrome running
            # that nothing will come back for. But the session can DIE inside the
            # first step — `_swallow` hands that interrupt back rather than
            # letting it out, so the check on the way in is already behind us —
            # and the sync API must not be driven once it has. Stopping IS the
            # close in that state, and it is a no-op when the stop already
            # happened.
            if self._dead:
                self.stop_driver()
            elif playwright is not None:
                from_driver = _swallow("driver", playwright.stop)
        self._manager = None
        _forget(self)
        interrupt = from_context or from_driver
        if interrupt is not None:
            raise interrupt

    def _ensure_page(self) -> Any:
        """This session's page, launching Chrome on first use."""
        if self._page is not None:
            return self._page
        if _interrupt_state["seen"]:
            # The stop already ran and had nothing of this session's to find.
            # Refusing here is what keeps a launch from opening a Chrome that
            # the shutdown has already gone past.
            raise KeyboardInterrupt
        factory = _playwright_factory()  # its own typed refusal; never re-wrapped below
        profile = _profile_dir()
        _announce()
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise GfBrowserUnavailableError(
                f"Google Flights' browser profile directory {profile} could not be created: {e}"
            ) from e
        manager: Any = None
        try:
            # Registered BEFORE the driver starts. `stop_driver` resolves the pid
            # when it needs it, so an interrupt after the driver is spawned inside
            # `start()` still reaches the process that call spawned.
            manager = factory()
            self._manager = manager
            _remember(self)
            self._playwright = manager.start()
            self._stop_a_late_driver_or_raise(manager)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=not self._headed,
                no_viewport=True,
                **_launch_target(),
            )
            self._page = self._context.new_page()
            if self._headed:
                _send_the_headless_token(self._context, self._page)
        # Any launch failure — missing Chrome, locked profile, driver crash — is one
        # typed refusal to the caller, who cannot act on the distinctions patchright
        # draws. Only the wording differs, by whether `start()` returned.
        except Exception as e:
            if self._playwright is None:
                # `start()` raised, so no launch was tried: the driver is what failed.
                # The reap runs the driver's loop, so an interrupt can land in it, and
                # the sibling arm below never sees one raised from inside this one.
                try:
                    _quiet_driver_failure(manager)
                except BaseException:
                    self._dead = True
                    raise
                self.close()
                raise _driver_failure(_detail(_parked_driver_error(manager) or e)) from e
            self.close()
            raise _launch_failure(profile, _detail(e)) from e
        # The launch block's broad `except Exception` above calls `close()`; an
        # interrupt skips it, so this is where the launch records that patchright
        # was unwound. Re-raised untouched — the caller's `finally` closes, and
        # the flag is what tells that close to stop the driver rather than drive
        # a sync API whose greenlet is gone.
        except BaseException:
            self._dead = True
            raise
        return self._page


def _swallow(what: str, shutdown: Callable[[], object]) -> KeyboardInterrupt | None:
    """Run one teardown step, logging rather than raising, and hand back the one
    thing the caller must not lose: a `KeyboardInterrupt`. Everything else,
    `Exception` or not, is logged and returns `None`.

    Teardown runs from a `finally`, where a raise would replace the real error
    with this one — so a failing `close()` is logged and dropped.

    The catch is `BaseException`, not `Exception`, because `close` runs two of
    these in sequence: anything escaping the first would skip the driver
    shutdown and strand a live Chrome, the exact outcome this module exists to
    prevent. That sequence is the healthy path only — a session whose patchright
    calls were unwound by an interrupt runs neither step, and reaches the same
    outcome by killing the driver instead (`close`). Signals are delivered to the
    main thread, so the realistic way one arrives is a Ctrl-C on `--fast` or at
    `atexit`; the enriched path tears down from an anyio worker.

    Only the interrupt is handed back, because it is the only one of these that
    is the user's instruction rather than someone else's control flow.
    Re-raising an `asyncio.CancelledError` out of teardown would escape the
    enriched path's `except Exception` and cancel the task group, taking a
    Matrix query that was still running and still authoritative with it;
    `SystemExit` and `GeneratorExit` belong to the interpreter and to the
    generator that raised them. A teardown step is not where any of those gets
    decided.

    Deliberately the opposite choice from `_ensure_page`, which catches
    `Exception` narrowly so the suite's `pytest.fail` guard — a `BaseException`
    on purpose — cannot be swallowed by the code under test. The guard raises
    from `_playwright_factory`, outside any `_swallow`, so the breadth here
    cannot reach it."""
    try:
        shutdown()
    except BaseException as e:  # noqa: BLE001 — see the docstring: never fatal here
        log.debug("could not close the gflight browser %s: %s", what, e)
        return e if isinstance(e, KeyboardInterrupt) else None
    return None


def _send_the_headless_token(context: Any, page: Any) -> None:
    """Make a headed window's User-Agent say `HeadlessChrome`, as a headless
    one's already does.

    Google serves a multi-airport search a different board by that token: under
    it the 300 cheapest rows across every airport pair, under plain `Chrome` a
    curated ~75. Headless Chrome and rung 1 both send it, so a headed window
    without it would read a board the default search never shows. Set once on
    the session's one page, so every navigation on it carries the token. A UA
    that already has it is left alone, and a headless page is never set to
    plain `Chrome/`: that reads the curated board."""
    cdp = context.new_cdp_session(page)
    ua = str(cdp.send("Browser.getVersion")["userAgent"])
    if "HeadlessChrome/" not in ua:
        cdp.send(
            "Emulation.setUserAgentOverride",
            {"userAgent": ua.replace(" Chrome/", " HeadlessChrome/")},
        )


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


def _one_line(text: str, limit: int = _SHOWN_CHARS) -> str:
    """`text` on one line, cut at `limit` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def _unquote(name: str) -> str:
    """A snapshot's JSON-quoted button name, unquoted."""
    try:
        return str(json.loads(name))
    except ValueError:
        return name.strip('"')


def _describe_page(page: Any) -> str:
    """The page's URL, its title and its visible buttons' accessible names.

    The snapshot lists only what a user could reach, so a hidden control is not
    named. A page showing no "Price graph" button, a consent form or another
    site all end in the same click timeout; this is what tells them apart."""
    snapshot = str(page.aria_snapshot(timeout=_SNAPSHOT_TIMEOUT_MS))
    names = [
        _one_line(_unquote(bare or quoted.replace("''", "'")))
        for bare, quoted in _BUTTON_LINE.findall(snapshot)
    ]
    shown = ", ".join(f'"{n}"' for n in names[:_SHOWN_BUTTONS])
    if not names:
        buttons = "no visible buttons"
    elif len(names) > _SHOWN_BUTTONS:
        buttons = f"visible buttons: {shown}, and {len(names) - _SHOWN_BUTTONS} more"
    else:
        buttons = f"visible buttons: {shown}"
    url, title = _one_line(str(page.url)), _one_line(page.title())
    return f'The page showed URL {url}, title "{title}", {buttons}.'


def _navigated_page(page: Any, nav: Any) -> PageFetch:
    """The page a navigation served, for the rows parser or a wall check.

    Chrome answers the body read from its DevTools buffer, which can drop a
    results page's bytes before they are asked for. The DOM stands in then. It
    keeps the `ds:1` blob: on one JFK-LAX navigation, measured 2026-09-28, the
    body and the DOM decoded to the same 102 rows with the same flight ids in
    the same order. It also keeps the throttle sentence and the consent form a
    wall check looks for. The URL and status are the navigation's either way.
    Any other read failure propagates."""
    try:
        html = nav.text()
    # patchright names its whole error tree `Error`; Chrome's message is the only tell.
    except Exception as e:
        if _EVICTED not in str(e):
            raise
        html = page.content()
    return PageFetch(html=html, final_url=str(nav.url), status_code=int(nav.status))


def _budget_ms(deadline: float, cap_ms: float) -> float:
    """What is left of a capture's deadline for one driver call, capped.

    Refuses rather than returning zero: to patchright a zero timeout means NO
    timeout, which would turn a spent deadline into an unbounded wait."""
    left_ms = (deadline - time.monotonic()) * 1000
    if left_ms <= 0:
        raise GfBrowserUnavailableError(
            "Google Flights' page did not deliver the response it was expected to make in time."
        )
    return min(cap_ms, left_ms)


def _is_main_navigation(request: Any) -> bool:
    """True for a navigation of the page's top frame."""
    try:
        return bool(request.is_navigation_request()) and request.frame.parent_frame is None
    # `frame` raises for a service-worker request, which is not a navigation.
    except Exception:  # noqa: BLE001 — patchright's `Error` is not importable here unguarded
        return False


def _first_finished_match(
    requests: list[Any], responses: list[Any], finished: list[Any], wanted: Callable[[str], bool]
) -> Any | None:
    """The first response to this navigation that `wanted` accepts, once its
    request has finished; None while there is none, or while it is still
    arriving.

    "This navigation" is every request from its own main-frame request on.
    Requests are compared by identity: patchright hands out one wrapper per
    request, so the object a response names is the one the listener saw."""
    start = next((i for i, r in enumerate(requests) if _is_main_navigation(r)), None)
    if start is None:
        return None
    ours = requests[start:]
    for response in responses:
        request = response.request
        if not any(request is r for r in ours) or not wanted(str(response.url)):
            continue
        return response if any(request is f for f in finished) else None
    return None


def _driver_process_id(manager: Any) -> int | None:
    """The pid of patchright's node driver, or None if this build hides it.

    Private attributes, deliberately: patchright hands out no public handle on
    the process it spawned, and a stop that has to cross a thread has nothing
    else to aim at. Read against patchright 1.59.1 —
    `PlaywrightContextManager.__enter__` builds the `Connection`
    (`sync_api/_context_manager.py:39`) over a `PipeTransport` whose `connect()`
    assigns `_proc` (`_impl/_connection.py:234`, `_impl/_transport.py:93`).

    A lookup rather than an assertion, because `pyproject.toml` pins
    `patchright>=1.59` with no upper bound: a build that moves any of the three
    is one `uv sync` away, and it returns None here rather than raising inside a
    signal handler. `_proc` is assigned only after `create_subprocess_exec`
    returns, so for one await a child exists that this cannot name — self-healing,
    because that driver exits on stdin EOF.

    A None from a build that MOVED the chain is not the same as a None from a
    session that never launched, and only the first is a problem: it is a driver
    nobody can stop. `tests/test_gf_browser.py` pins those three assignments
    against the installed patchright, so a bump that moves any of them fails
    there rather than as an orphan Chrome nobody reported."""
    try:
        return int(manager._connection._transport._proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None


# Re-entrant, and that is the whole reason it exists: the SIGINT handler runs on
# the main thread between two bytecodes of whatever that thread was doing —
# including a `_remember` or `_forget` that already holds this lock. A plain
# `Lock` deadlocks there, in a handler, with the browser still open.
_live_lock = threading.RLock()
# Every session that has started a driver, on every thread. The thread-local
# below answers "which session is MINE"; this answers "what is open in this
# PROCESS", which is the only question a signal handler can ask. Strong
# references, which is harmless for a set that lives as long as the process and
# whose entries are removed by `close`.
_live: set[GfBrowserSession] = set()
# Set by the handler BEFORE it snapshots the registry, and cleared by the guard
# on its way in. A stop reaches only a driver that already exists, so a launch
# still on its way up is invisible to it — this is what such a launch reads
# instead, on the way in and again once its own driver does exist. Process-wide
# because the launch runs on a worker and the handler runs on the main thread.
_interrupt_state: dict[str, bool] = {"seen": False}


def _remember(session: GfBrowserSession) -> None:
    with _live_lock:
        _live.add(session)


def _forget(session: GfBrowserSession) -> None:
    with _live_lock:
        _live.discard(session)


def stop_all_drivers() -> None:
    """Stop every browser open in this process, from wherever the caller stands.

    A session that never launched was never registered, and one whose manager is
    already gone degrades to a no-op, so this is safe to call at any moment."""
    with _live_lock:
        open_now = list(_live)
    for s in open_now:
        s.stop_driver()


@contextlib.contextmanager
def interrupt_guard(*, armed: bool = True) -> Generator[None]:
    """Make a Ctrl-C during a browser search reach the browser.

    Armed by the caller, and the two arms decide differently. The fast arm arms
    it for every Google Flights search, before the transport is known: that
    search runs on the thread the signal is delivered to, so nothing after the
    handler can block. The ignore the handler installs is not scoped to that
    wait, though: when the handler stopped a driver it is never restored, so it
    stands for the rest of the process's life, and what makes arming this arm
    broadly safe is that by then there is nothing left for a second Ctrl-C to
    stop. The enriched arm arms it only for the transports that can open a
    browser, `browser` and `auto`, which escalates a throttle to one, because
    there the search runs on a worker no interrupt reaches — on `http` the first
    Ctrl-C cannot free that worker, and an ignored second one leaves nothing that
    can. An `auto` search that never escalated has no driver for the handler to
    stop, so it hands the next Ctrl-C back (below).

    The default handler raises `KeyboardInterrupt` on the main thread and stops
    there, which leaves the two arms broken in different ways. On `--fast` the
    interrupt unwinds patchright's own call and kills the greenlet its event loop
    runs in, so the teardown that follows has nothing left to drive. On the
    enriched path the navigation is on a worker thread that no exception and no
    cancellation can reach, and `anyio.to_thread.run_sync` will not abandon it,
    so the process waits out every remaining navigation. Stopping the drivers
    FIRST fixes both: the worker's navigation fails at once and unwinds through
    its own `finally`, and the main thread's teardown knows not to use the sync
    API.

    A handler, and not a `try/except` around the call, because the exception
    arrives too late — by then the enriched path is inside anyio's unwind,
    waiting for the very worker this exists to wake.

    Arm it OUTSIDE `anyio.run`, never inside a coroutine or a worker.
    `asyncio.Runner` installs its own SIGINT handler only when the current
    disposition is `signal.default_int_handler` (`asyncio/runners.py:102-104`)
    and restores that default on the way out if its own is still installed
    (`:125-129`) — so a guard armed from inside would be replaced or reset. Armed
    from outside, the Runner installs nothing and the interrupt lands as a plain
    `KeyboardInterrupt` wherever the main thread stands. On 3.12 those are
    CPython's own lines: anyio imports `asyncio.Runner` rather than using its
    vendored copy (`anyio/_backends/_asyncio.py:111-112`).

    After a first one that finds a driver open, SIGINT is IGNORED for the rest of
    the process's life — including by the restore below, which is skipped. There
    is nothing left for a second Ctrl-C to stop: the drivers are dead and the exit
    is already running.
    What it would do instead is land in the middle of that exit, as a second
    `KeyboardInterrupt` through interpreter finalisation. The cost is real: a
    shutdown that ever did hang could no longer be interrupted from the same
    terminal. A first one that finds no driver open puts the previous disposition
    back once the stop has returned, so a second Ctrl-C ends the wait on a worker,
    as it does on `http`. The ignore still goes in first in that case too, and a
    launch not yet registered reads `_interrupt_state` rather than the register.

    Only the main thread may install a handler; on any other this is a no-op that
    still runs its body, which is correct — that thread's Ctrl-C arrives on the
    main one anyway."""
    if not armed or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    seen = False
    # Cleared on the way IN and nowhere else. The only thing that sets it is
    # the handler, which sets `seen` in the same breath — so on a normal
    # completion below it is already false, and after an interrupt it stays
    # set for the rest of that shutdown, exactly as the ignore does.
    _interrupt_state["seen"] = False

    def _on_sigint(_signum: int, _frame: types.FrameType | None) -> None:
        nonlocal seen
        # FIRST, before anything that can be re-entered. `Logger.setLevel` under
        # the stop below clears the logging manager's cache, and that release is
        # not guarded by a `try`/`finally` — a second SIGINT arriving inside it
        # would raise out and leave the logging module lock held for the life of
        # the process.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seen = True
        # BEFORE the snapshot: a launch that has not registered a driver yet
        # cannot be reached by the stop, and this is what it reads instead.
        _interrupt_state["seen"] = True
        # Read once and used after the stop: it decides whether the ignore stays.
        with _live_lock:
            driver_open = bool(_live)
        stop_all_drivers()
        if not driver_open:
            signal.signal(signal.SIGINT, previous)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        yield
    finally:
        if not seen:
            signal.signal(signal.SIGINT, previous)


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


_scope_depth = threading.local()


@contextlib.contextmanager
def session_scope() -> Generator[None]:
    """Hold this thread's session open across several searches, closing it once.

    One search closes its own session as it returns. Several searches on one
    thread — a multi-cabin run at rung 2 — would then pay the launch per cabin,
    and a close whose Chrome has not finished exiting still holds the profile
    lock the next launch needs. Inside this scope the close is deferred to the
    scope's own exit, which runs on the same thread and on every path out."""
    depth = getattr(_scope_depth, "n", 0)
    _scope_depth.n = depth + 1
    try:
        yield
    finally:
        _scope_depth.n = depth
        if depth == 0:
            close_thread_session()


def close_thread_session() -> None:
    """Close this thread's session if it made one. Idempotent, and safe to call
    on a thread that never touched rung 2."""
    existing: GfBrowserSession | None = getattr(_sessions, "current", None)
    # A scope defers the close of a HEALTHY session only. One whose patchright
    # calls were unwound must never be handed to the next search in the scope:
    # `_ensure_page` returns the page it already has without consulting the
    # flag, and a call through a sync API whose greenlet is gone spins on a loop
    # nobody drives.
    if getattr(_scope_depth, "n", 0) and existing is not None and not existing.finished:
        return
    _sessions.current = None
    if existing is not None:
        existing.close()


def _close_at_exit() -> None:
    """Best-effort close for a session the *exiting* thread owns.

    Not a substitute for the caller's `finally`, and structurally cannot become
    one: `_sessions` is thread-local, so an interpreter shutdown running on the
    main thread sees only a main-thread session. A worker thread's session is
    closed by that worker or not at all — reaching across would raise
    `greenlet.error` and strand the Chrome it was trying to kill. The one thing
    that DOES cross a thread is a signal to the driver process, and it is the
    interrupt path that sends it: by the time this runs there is no search left
    to stop, only a process on its way out.

    `BaseException`, unlike the caller's `finally`: `close` re-raises a Ctrl-C
    so a run still in progress stops, and at interpreter shutdown there is no
    run left to stop — only a traceback for a process that was already
    leaving."""
    try:
        close_thread_session()
    except BaseException as e:  # noqa: BLE001 — the interpreter is going away; nothing to report to
        log.debug("atexit close of the gflight browser session failed: %s", e)


atexit.register(_close_at_exit)
