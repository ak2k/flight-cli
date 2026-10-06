# pyright: reportPrivateUsage=false
"""A second Ctrl-C ends a search that has no browser left to stop.

`interrupt_guard`'s handler ignores every later SIGINT, and what that ignore
buys is protection for the driver stop it runs. With no driver open the stop
has nothing to protect, and an ignored second Ctrl-C leaves the user waiting
out a worker that still sends Google requests. Handlers are called directly
and no signal is sent, except in the two subprocess cases, which are the only
place a real second signal can be shown to end (or not end) the process."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import GfThrottledError

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_sigint() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """Start from Python's own disposition: an earlier test may leave SIGINT
    ignored, and an ignore is what these tests look for."""
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, previous)


def _interrupt_inside_the_guard() -> None:
    """Enter the guard, call its installed handler once, and let the interrupt out."""
    with pytest.raises(KeyboardInterrupt), gfb.interrupt_guard():
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)


def test_the_next_ctrl_c_is_not_ignored_when_no_driver_was_open() -> None:
    before = signal.getsignal(signal.SIGINT)

    _interrupt_inside_the_guard()

    assert signal.getsignal(signal.SIGINT) is before


def test_the_next_ctrl_c_is_still_ignored_while_a_driver_is_open() -> None:
    session = gfb.GfBrowserSession(headed=False)
    gfb._remember(session)

    _interrupt_inside_the_guard()

    assert session._dead is True
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN


def test_the_stop_runs_under_the_ignore_even_when_no_driver_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logging lock the stop can leave held is why the ignore goes in first;
    the disposition handed back afterwards must not move that earlier."""
    seen: list[object] = []

    def _stop() -> None:
        seen.append(signal.getsignal(signal.SIGINT))

    monkeypatch.setattr(gfb, "stop_all_drivers", _stop)

    _interrupt_inside_the_guard()

    assert seen == [signal.SIG_IGN]


def test_the_escalation_line_is_printed_when_no_interrupt_was_seen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gfb.announce_escalation()

    assert "opening Chrome" in capsys.readouterr().err


def test_the_escalation_line_is_not_printed_after_the_first_ctrl_c(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gfb._interrupt_state["seen"] = True

    gfb.announce_escalation()

    assert capsys.readouterr().err == ""
    assert gfb._notice_state["printed"] is False


def test_a_throttle_after_the_first_ctrl_c_prints_no_escalation_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _throttled(*_a: Any, **_kw: Any) -> Any:
        raise GfThrottledError("rate-limited")

    def _refused(*_a: Any, **_kw: Any) -> Any:
        raise KeyboardInterrupt  # `_ensure_page` refusing a launch the interrupt got ahead of

    monkeypatch.setattr(gfid, "_one_call_with_retry", _throttled)
    monkeypatch.setattr(gfid, "_one_call_browser", _refused)
    gfb._interrupt_state["seen"] = True
    filters: Any = object()

    with gfid.search_escalation(), pytest.raises(KeyboardInterrupt):
        gfid._one_call_auto(filters, headed=False)

    assert capsys.readouterr().err == ""


# A weave whose worker thread waits `hold` seconds, with a second thread sending
# SIGINT twice, 50 ms apart. `browser` registers a stub session first, so a
# driver is open and nothing real is launched.
_PROBE = textwrap.dedent(
    """
    import os, signal, sys, threading, time
    import anyio
    from flight_cli import _gf_browser as gfb
    from flight_cli import cli

    mode, hold = sys.argv[1], float(sys.argv[2])
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if mode == "browser":
        gfb._remember(gfb.GfBrowserSession(headed=False))

    def work():
        threading.Event().wait(hold)
        print("WORKER_FINISHED", flush=True)

    async def go():
        await anyio.to_thread.run_sync(work)

    def sender():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=sender, daemon=True).start()
    try:
        cli._run_the_weave(go, {}, mode)
    except KeyboardInterrupt:
        print("MAIN_CANCELLED", flush=True)
    """
)


def _probe(mode: str) -> str:
    done = subprocess.run(  # noqa: S603 — this interpreter and a literal probe
        [sys.executable, "-c", _PROBE, mode, "3"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.stdout


def test_a_second_ctrl_c_ends_an_auto_search_before_its_worker_finishes() -> None:
    out = _probe("auto")

    assert "MAIN_CANCELLED" in out
    assert "WORKER_FINISHED" not in out


def test_a_second_ctrl_c_is_ignored_on_a_browser_search_holding_a_driver() -> None:
    out = _probe("browser")

    assert "MAIN_CANCELLED" in out
    assert "WORKER_FINISHED" in out
