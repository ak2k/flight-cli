"""A patchright driver that cannot start: one quiet refusal that names the driver.

patchright's `start()` spawns a node driver. When that spawn fails, the
transport parks the error on a future nobody reads, and asyncio prints
"Future exception was never retrieved" with a ~40-line traceback after the
typed refusal. The refusal also told the user to install Chrome, which is not
what is missing.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli._gf_errors import GfBrowserUnavailableError

if TYPE_CHECKING:
    import pathlib

_UNRETRIEVED = "Future exception was never retrieved"


def _no_trace_left(caplog: pytest.LogCaptureFixture) -> bool:
    """True when collecting every dead object logged no unretrieved future.

    `caplog.set_level` on the `asyncio` logger, because `stop_driver` raises
    that logger to CRITICAL for the rest of the process and a test that ran
    after one would otherwise pass without looking."""
    gc.collect()
    return not any(_UNRETRIEVED in r.getMessage() for r in caplog.records)


class _FailedTransport:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.on_error_future: asyncio.Future[None] = loop.create_future()
        self.on_error_future.set_exception(FileNotFoundError(2, "No such file or directory"))


class _FailedConnection:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._transport = _FailedTransport(loop)


class _DriverThatCannotStart:
    """The slice of patchright's manager a failed `start()` leaves behind."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._connection = _FailedConnection(loop)

    def start(self) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'node'")


def _install_real_patchright(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> list[asyncio.AbstractEventLoop]:
    """Point the seam at the real `sync_playwright` and a driver path that does not exist.

    Nothing is spawned: `start()` fails at the first `exec`, so no node and no
    Chrome run. `MATRIX_CACHE_DIR` is a tmp dir, checked before the session can
    make a profile directory in it. Returns the event loops patchright makes,
    because a failed `start()` never closes its own and the test must."""
    from patchright.sync_api import sync_playwright

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", str(tmp_path / "no-such-node"))
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
    loops = _install_real_patchright(monkeypatch, tmp_path)
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


def test_a_failed_driver_start_retrieves_the_error_it_left_on_the_transport(
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


def test_a_manager_without_the_transport_future_still_refuses_cleanly(
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
