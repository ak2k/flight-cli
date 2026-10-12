"""A Ctrl-C during a refused price graph's pause ends the pause.

The pause before a refused graph is read again (`server_error_waiter`) is a
sleep on a worker thread. The interrupt handler stops the drivers and nothing
else, so a sleeping worker held the command until the pause ran out. The
handler's stop now wakes it. No test here loads a page: the browser session is
replaced as in `test_gf_calgraph`.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from datetime import date
from typing import NoReturn

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_calgraph as cg
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from test_gf_calgraph import _calendar, _fixture, _matrix_answers, _search, _serve

_LONG_PAUSE_S = 30.0
_WOKEN_WITHIN_S = 3.0


def test_stopping_the_drivers_ends_the_pause_before_a_refused_graph_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: the worker slept the whole 30 s pause, so it was still
    running 3 s after the stop."""
    monkeypatch.setattr(gfid, "_SERVER_ERROR_PAUSES_S", (_LONG_PAUSE_S, _LONG_PAUSE_S))
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    outcome: list[BaseException | None] = []

    def _worker() -> None:
        try:
            cg.price_graph(_search(start=date(2026, 10, 20), end=date(2026, 11, 2)), headed=False)
        except BaseException as e:  # the test reads whichever ended it
            outcome.append(e)
        else:
            outcome.append(None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not fake.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.calls, "the first load never ran"
    time.sleep(0.2)  # the refused graph is parsed and the worker is in its pause
    gfb.stop_all_drivers()
    worker.join(_WOKEN_WITHIN_S)
    assert not worker.is_alive(), f"the pause outlasted the stop by {_WOKEN_WITHIN_S} s"
    (ended,) = outcome
    assert isinstance(ended, cg.GfPriceGraphError)
    assert ended.code == 13
    assert len(fake.calls) == 1


def test_a_ctrl_c_in_a_refused_graphs_pause_ends_the_command_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command beside Matrix: Matrix's side ends in the interrupt while the
    graph's worker sleeps, and the pool's exit waits for that worker."""
    monkeypatch.setattr(gfid, "_SERVER_ERROR_PAUSES_S", (_LONG_PAUSE_S, _LONG_PAUSE_S))
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    _matrix_answers(monkeypatch)

    def _interrupted(*_a: object, **_k: object) -> NoReturn:
        deadline = time.monotonic() + 5
        while not fake.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # the refused graph is parsed and the worker is in its pause
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_calendar", _interrupted)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _calendar(fast=False)
    assert time.monotonic() - started < _WOKEN_WITHIN_S
    assert len(fake.calls) == 1


def test_a_stop_before_the_pause_begins_still_ends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interrupt can land between the refused graph and its pause."""
    monkeypatch.setattr(gfid, "_SERVER_ERROR_PAUSES_S", (_LONG_PAUSE_S, _LONG_PAUSE_S))
    fake = _serve(monkeypatch, _fixture("error13.body"), _fixture("ow_jfk_lax.body"))
    gfb.stop_all_drivers()
    started = time.monotonic()
    with pytest.raises(cg.GfPriceGraphError) as e:
        cg.price_graph(_search(start=date(2026, 10, 20), end=date(2026, 11, 2)), headed=False)
    assert time.monotonic() - started < _WOKEN_WITHIN_S
    assert e.value.code == 13
    assert len(fake.calls) == 1


def test_a_stop_in_an_earlier_test_is_not_inherited() -> None:
    """The test above stops the drivers and never arms a guard; the conftest
    gives each test its own stop flag."""
    assert gfb.pause_unless_stopped(0.0) is True


def test_a_new_interrupt_guard_does_not_inherit_an_earlier_stop() -> None:
    gfb.stop_all_drivers()
    assert gfb.pause_unless_stopped(0.0) is False
    with gfb.interrupt_guard():
        assert gfb.pause_unless_stopped(0.0) is True


# `--fast` pauses on the main thread inside the guard, so the handler runs
# between two opcodes of the pause itself. Run n raises the SIGINT at the
# pause's n-th opcode, counting those of everything it calls; the runs stop at
# the first n past the pause's last opcode. A handler that waits on a lock the
# pause holds never returns, and the probe outlives `_EXIT_WITHIN_S`.
_EVERY_OPCODE_PROBE = textwrap.dedent(
    """
    import signal, sys
    from flight_cli import _gf_browser as gfb

    def pause_interrupted_at(n):
        seen = 0

        def trace(frame, event, _arg):
            nonlocal seen
            frame.f_trace_opcodes = True
            if event == "opcode":
                seen += 1
                if seen == n:
                    signal.raise_signal(signal.SIGINT)
            return trace

        with gfb.interrupt_guard():
            # Opcode events reach a trace only if a frame asked for them
            # before the trace was installed.
            sys._getframe().f_trace_opcodes = True
            sys.settrace(trace)
            try:
                gfb.pause_unless_stopped(0.01)
            finally:
                sys.settrace(None)

    n = 1
    while True:
        try:
            pause_interrupted_at(n)
        except KeyboardInterrupt:
            n += 1
        else:
            break
    print(f"INTERRUPTED {n - 1}", flush=True)
    """
)
_EXIT_WITHIN_S = 20.0


def test_a_ctrl_c_at_any_opcode_of_a_pause_on_the_guarded_thread_ends_it() -> None:
    proc = subprocess.Popen(  # noqa: S603 — argv is this interpreter and a literal probe
        [sys.executable, "-c", _EVERY_OPCODE_PROBE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=_EXIT_WITHIN_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"the handler hung inside a pause\n{out}\n{err}")
    assert proc.returncode == 0, err
    (interrupted,) = (int(line.split()[1]) for line in out.splitlines() if "INTERRUPTED" in line)
    assert interrupted > 0, out
