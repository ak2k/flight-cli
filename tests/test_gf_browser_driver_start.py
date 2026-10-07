"""A patchright driver that cannot start: one quiet refusal that names the driver.

patchright's `start()` spawns a node driver. When node cannot be spawned, runs
and exits before the handshake, or sends a frame that is not JSON, what the
failed start left behind would make asyncio print a traceback after the typed
refusal. None of these is a missing Chrome, so the refusal points at
patchright's install.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli._gf_errors import GfBrowserUnavailableError

if TYPE_CHECKING:
    import pathlib


def _no_trace_left(caplog: pytest.LogCaptureFixture) -> bool:
    """True when collecting every dead object logged nothing on `asyncio`.

    `caplog.set_level` on the `asyncio` logger, because `stop_driver` raises
    that logger to CRITICAL for the rest of the process and a test that ran
    after one would otherwise pass without looking."""
    gc.collect()
    return not any(r.name == "asyncio" for r in caplog.records)


def _join_child_watchers() -> None:
    """asyncio reaps a child on a thread of its own where it has no pidfd, and
    that thread holds the child's transport until then: collecting before it
    is done moves whatever the transport reports into a later test."""
    for thread in threading.enumerate():
        if thread.name.startswith("asyncio-waitpid-"):
            thread.join(5)


class _DriverThatCannotStart:
    """The slice of patchright's manager a failed `start()` leaves behind: the
    loop it ran on, whether patchright made that loop, and the error parked on a
    future of it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, own_loop: bool = True) -> None:
        self._loop = loop
        self._own_loop = own_loop
        self.parked: asyncio.Future[None] = loop.create_future()
        self.parked.set_exception(FileNotFoundError(2, "No such file or directory"))

    def start(self) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'node'")


def _install_real_patchright(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, node: pathlib.Path
) -> list[asyncio.AbstractEventLoop]:
    """Point the seam at the real `sync_playwright` and `node` as its driver binary.

    No real node and no Chrome run: `node` is missing, so `start()` fails at the
    first `exec`, or is a script that exits at once. `MATRIX_CACHE_DIR` is a tmp
    dir, checked before the session can make a profile directory in it. Returns
    the event loops patchright makes, because a failed `start()` never closes its
    own and the test must."""
    from patchright.sync_api import sync_playwright

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", str(node))
    assert gfb._profile_dir().is_relative_to(tmp_path)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: sync_playwright)
    loops: list[asyncio.AbstractEventLoop] = []
    new_event_loop = asyncio.new_event_loop

    def _recorded() -> asyncio.AbstractEventLoop:
        loops.append(new_event_loop())
        return loops[-1]

    monkeypatch.setattr(asyncio, "new_event_loop", _recorded)
    return loops


def test_a_real_driver_that_cannot_start_is_one_quiet_refusal_naming_the_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="asyncio")
    loops = _install_real_patchright(monkeypatch, tmp_path, tmp_path / "no-such-node")
    session = gfb.GfBrowserSession(headed=False)
    try:
        with pytest.raises(GfBrowserUnavailableError) as e:
            session.get_html("https://www.google.com/travel/flights")
        session.close()
        # Plain strings only: the exception's traceback holds the frames, and the
        # frames hold the manager whose transport carries the future.
        remedy, reason, whole = e.value.remedy, e.value.reason, str(e.value)
        del e, session

        assert gfb._INSTALL_HINT in remedy  # pyright: ignore[reportPrivateUsage]
        assert "Install Chrome" not in whole
        assert "driver" in reason
        assert "Chrome failed to launch" not in reason
        assert "no-such-node" in reason
        assert _no_trace_left(caplog)
    finally:
        for loop in loops:
            loop.close()


def test_a_real_driver_that_exits_at_once_is_one_quiet_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A driver binary that runs and exits before the handshake leaves its error
    on patchright's `init` task, not on the transport's future."""
    caplog.set_level(logging.ERROR, logger="asyncio")
    node = tmp_path / "exits-at-once"
    node.write_text("#!/bin/sh\ntrue\n")
    node.chmod(0o755)
    loops = _install_real_patchright(monkeypatch, tmp_path, node)
    session = gfb.GfBrowserSession(headed=False)
    try:
        with pytest.raises(GfBrowserUnavailableError) as e:
            session.get_html("https://www.google.com/travel/flights")
        session.close()
        reason = e.value.reason
        del e, session

        assert "driver failed to start" in reason
        assert _no_trace_left(caplog)
    finally:
        for loop in loops:
            loop.close()


def test_a_real_driver_that_sends_a_malformed_frame_is_one_quiet_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A frame that is not JSON fails `start()` while patchright's `init` task
    still waits and the driver is still up. Each reports itself when collected:
    the task through the loop, the driver's pipes from their finalizer, which
    raises once the loop is closed. The loops are closed before the collection,
    an order a collected loop's own finalizer also gives."""
    caplog.set_level(logging.WARNING, logger="asyncio")
    payload = b"not json"
    frame = tmp_path / "frame"
    frame.write_bytes(len(payload).to_bytes(4, "little") + payload)
    node = tmp_path / "sends-a-malformed-frame"
    node.write_text(f"#!/bin/sh\ncat '{frame}'\n")
    node.chmod(0o755)
    loops = _install_real_patchright(monkeypatch, tmp_path, node)
    unraisable: list[str] = []

    def _keep(u: sys.UnraisableHookArgs) -> None:
        unraisable.append(f"{u.err_msg}: {u.exc_value!r}")

    monkeypatch.setattr(sys, "unraisablehook", _keep)
    session = gfb.GfBrowserSession(headed=False)
    try:
        with pytest.raises(GfBrowserUnavailableError) as e:
            session.get_html("https://www.google.com/travel/flights")
        session.close()
        reason = e.value.reason
        del e, session
        _join_child_watchers()
    finally:
        for loop in loops:
            loop.close()
    assert "driver failed to start" in reason
    assert _no_trace_left(caplog)
    assert unraisable == []


def test_a_failed_driver_start_reports_nothing_it_left_on_its_own_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="asyncio")
    loop = asyncio.new_event_loop()
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    assert gfb._profile_dir().is_relative_to(tmp_path)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: lambda: _DriverThatCannotStart(loop))
    session = gfb.GfBrowserSession(headed=False)
    try:
        with pytest.raises(GfBrowserUnavailableError):
            session.get_html("https://www.google.com/travel/flights")
        session.close()
        del session
        assert _no_trace_left(caplog)
    finally:
        loop.close()


def test_a_driver_start_on_the_callers_loop_leaves_that_loop_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """patchright refuses its sync API inside a running loop, and the loop it
    holds then is the caller's: that one keeps asyncio's reports."""
    loop = asyncio.new_event_loop()
    manager = _DriverThatCannotStart(loop, own_loop=False)
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    assert gfb._profile_dir().is_relative_to(tmp_path)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: lambda: manager)
    session = gfb.GfBrowserSession(headed=False)
    try:
        with pytest.raises(GfBrowserUnavailableError):
            session.get_html("https://www.google.com/travel/flights")
        session.close()
        assert loop.get_exception_handler() is None
    finally:
        manager.parked.exception()
        loop.close()


def test_a_manager_without_patchrights_loop_still_refuses_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A patchright build that moves the private chain must not turn the
    refusal into an `AttributeError`."""

    class _Bare:
        def start(self) -> None:
            raise RuntimeError("driver handshake failed")

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: _Bare)
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError) as e:
        session.get_html("https://www.google.com/travel/flights")
    assert gfb._INSTALL_HINT in e.value.remedy  # pyright: ignore[reportPrivateUsage]
    session.close()


def test_a_chrome_launch_failure_keeps_the_chrome_remedy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Only a driver that fails to START changes wording: once the driver is up,
    a failed launch is still a missing Chrome."""

    class _Context:
        def launch_persistent_context(self, **_kw: Any) -> None:
            raise RuntimeError("Chromium distribution 'chrome' is not found.")

    class _Driver:
        chromium = _Context()

        def start(self) -> _Driver:
            return self

        def stop(self) -> None:
            pass

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: _Driver)
    session = gfb.GfBrowserSession(headed=False)
    with pytest.raises(GfBrowserUnavailableError) as e:
        session.get_html("https://www.google.com/travel/flights")
    assert e.value.remedy == gfb._LAUNCH_REMEDY  # pyright: ignore[reportPrivateUsage]
    assert "Chrome failed to launch" in e.value.reason
    session.close()
