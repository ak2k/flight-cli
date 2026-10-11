# pyright: reportPrivateUsage=false
"""A second Ctrl-C ends an `auto` search without waiting out its worker.

`tests/test_gf_second_ctrl_c.py` times a second signal against a worker that
sleeps, so it passes or fails with the machine's load. The cases here hold the
worker on an event that is never set and place the second signal by
construction, so the process either ends within `_EXIT_WITHIN` or never does.
Each runs a real process: only a real signal shows whether the interpreter's
shutdown, which joins the worker, can be interrupted."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gf_browser as gfb

if TYPE_CHECKING:
    from collections.abc import Generator

    from flight_cli._gf_common import GfTransportMode

_EXIT_WITHIN = 10.0

# A weave whose worker blocks on an event nothing sets. The first SIGINT comes
# from a thread once the worker runs. `where` places the second one: `close`
# prints a line without flushing and sends it from inside
# `asyncio.Runner.close`, once the first has been handled; `shutdown` sends it
# after the weave has returned, while the interpreter is joining the worker.
# `browser` registers a stub session first, so a driver is open and nothing
# real is launched.
_PROBE = textwrap.dedent(
    """
    import asyncio, os, signal, sys, threading, time
    import anyio
    from flight_cli import _gf_browser as gfb
    from flight_cli import cli

    mode, where = sys.argv[1], sys.argv[2]
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if mode == "browser":
        gfb._remember(gfb.GfBrowserSession(headed=False))

    real_stop = gfb.stop_all_drivers

    def stop():
        print("DRIVERS_STOPPED", flush=True)
        real_stop()

    gfb.stop_all_drivers = stop

    started = threading.Event()
    weave_over = threading.Event()

    def work():
        print("WORKER_STARTED", flush=True)
        started.set()
        threading.Event().wait()
        print("WORKER_FINISHED", flush=True)

    async def go():
        await anyio.to_thread.run_sync(work)

    real_close = asyncio.Runner.close

    def close(self):
        if where == "close":
            print("UNFLUSHED_LINE")
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)
        real_close(self)

    asyncio.Runner.close = close

    def sender():
        started.wait(30)
        os.kill(os.getpid(), signal.SIGINT)
        if where == "shutdown":
            weave_over.wait(30)
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=sender, daemon=True).start()
    try:
        cli._run_the_weave(go, {}, mode)
    except KeyboardInterrupt:
        print("MAIN_CANCELLED", flush=True)
    weave_over.set()
    """
)


# One thread writes more to a stream than its pipe holds while nothing reads
# it, so the write blocks holding the stream's lock. The pipe stops taking
# writes only once that write has filled it, which is when the signal is sent,
# to the main thread. `main` writes on the thread the handler runs on;
# `worker` writes from another one while the main thread sleeps.
_BLOCKED_WRITE_PROBE = textwrap.dedent(
    """
    import select, signal, sys, threading, time
    from flight_cli import _gf_browser as gfb

    stream, writer = getattr(sys, sys.argv[1]), sys.argv[2]

    def sender():
        while select.select([], [stream.fileno()], [], 0)[1]:
            time.sleep(0.01)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

    def write():
        stream.write("x" * (4 << 20))
        stream.flush()

    with gfb.second_ctrl_c_exits():
        threading.Thread(target=sender, daemon=True).start()
        if writer == "main":
            write()
        else:
            threading.Thread(target=write, daemon=True).start()
            time.sleep(60)
    """
)


def _run(mode: str, where: str) -> tuple[int | None, str, str]:
    """The probe's exit status, stdout and stderr; no status if it outlived
    `_EXIT_WITHIN` and had to be killed."""
    proc = subprocess.Popen(  # noqa: S603 — this interpreter and a literal probe
        [sys.executable, "-c", _PROBE, mode, where],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=_EXIT_WITHIN)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return None, out, err
    return proc.returncode, out, err


def _assert_the_second_ctrl_c_ended_the_weave(where: str) -> None:
    status, out, err = _run("auto", where)

    assert status == 130, (out, err)
    assert "WORKER_STARTED" in out
    assert "DRIVERS_STOPPED" in out
    assert "WORKER_FINISHED" not in out
    assert "Traceback" not in err


def test_a_second_ctrl_c_in_the_runner_close_ends_an_auto_weave_with_status_130() -> None:
    _assert_the_second_ctrl_c_ended_the_weave("close")


def test_a_second_ctrl_c_in_the_interpreter_shutdown_ends_an_auto_weave_with_status_130() -> None:
    _assert_the_second_ctrl_c_ended_the_weave("shutdown")


def test_the_exit_flushes_what_stdout_still_held() -> None:
    status, out, _err = _run("auto", "close")

    assert status == 130
    assert "UNFLUSHED_LINE" in out


def _run_blocked_write(stream: str, writer: str) -> tuple[int | None, str]:
    """The probe's exit status and stderr, without the probe's own `x`s. Nothing
    reads either stream until the probe has ended, so the write stays blocked."""
    proc = subprocess.Popen(  # noqa: S603 — this interpreter and a literal probe
        [sys.executable, "-c", _BLOCKED_WRITE_PROBE, stream, writer],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        status = proc.wait(timeout=_EXIT_WITHIN)
    except subprocess.TimeoutExpired:
        proc.kill()
        status = None
    _out, err = proc.communicate()
    return status, err.lstrip("x")


def test_a_ctrl_c_inside_a_blocked_stdout_write_still_exits_130() -> None:
    """The handler's flush re-enters the write the signal interrupted."""
    status, err = _run_blocked_write("stdout", "main")

    assert status == 130, err
    assert "Traceback" not in err


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_ctrl_c_while_a_worker_write_blocks_still_exits_130(stream: str) -> None:
    """A worker's write holds the stream's lock and cannot finish, as a log line
    does on a stderr nobody drains, so the handler's flush waits on that lock."""
    status, err = _run_blocked_write(stream, "worker")

    assert status == 130, err
    assert "Traceback" not in err


def test_a_second_ctrl_c_is_still_ignored_while_a_driver_is_open() -> None:
    """The ignore protects the stop; the exit is only for a search with no
    driver, so the probe's second signal does nothing and the process stays."""
    status, out, _err = _run("browser", "close")

    assert status is None, out
    assert "MAIN_CANCELLED" in out


@pytest.fixture
def keep_sigint() -> Generator[None]:
    previous = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, previous)


def _recording_exit(armed_seen: list[bool]) -> Any:
    @contextlib.contextmanager
    def _cm(*, armed: bool = True) -> Generator[None]:
        armed_seen.append(armed)
        yield

    return _cm


@pytest.mark.parametrize(
    ("mode", "expected"), [("http", False), ("auto", True), ("browser", False)]
)
def test_only_an_auto_weave_arms_the_exit(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None, mode: GfTransportMode, expected: bool
) -> None:
    from flight_cli import cli

    armed_seen: list[bool] = []
    monkeypatch.setattr(gfb, "second_ctrl_c_exits", _recording_exit(armed_seen), raising=False)

    async def _nothing() -> None:
        """A weave with no work in it: the arming happens before it runs."""

    cli._run_the_weave(_nothing, {}, mode)

    assert armed_seen == [expected]
