# pyright: reportPrivateUsage=false
"""`log.configure()` and the one module that logs through stdlib `logging`.

`_gflight_ids` classifies every Google Flights refusal, and its `log.debug` /
`log.warning` lines are the only record of a board it read as empty or a row
block it found in the wrong place. Nothing in the package configures the stdlib
root, so those records reached no handler at all: `-vv` printed none of them and
a warning surfaced only through `logging.lastResort`. What is under test is that
`configure()` gives them somewhere to go, and that it does so WITHOUT turning on
every third-party library that logs.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import pytest

from flight_cli import log as log_mod

if TYPE_CHECKING:
    from collections.abc import Iterator

_MODULE_LOGGER = "flight_cli._gflight_ids"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`configure()` mutates process-wide logging state. Left set, its level
    would silently decide what every later test in the session captures."""
    logger = logging.getLogger("flight_cli")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers


def test_debug_makes_the_module_logger_reachable() -> None:
    """`-vv` maps to this call. Before it, the module's debug lines were
    unreachable no matter what the user asked for."""
    log_mod.configure("debug")
    assert logging.getLogger(_MODULE_LOGGER).isEnabledFor(logging.DEBUG)


def test_the_default_level_leaves_debug_off() -> None:
    log_mod.configure()
    assert not logging.getLogger(_MODULE_LOGGER).isEnabledFor(logging.DEBUG)
    assert logging.getLogger(_MODULE_LOGGER).isEnabledFor(logging.WARNING)


def test_the_handler_is_scoped_to_this_package() -> None:
    """Raising our own verbosity must not switch on httpx, urllib3 and curl_cffi
    — which is what setting the level on the root logger would do."""
    log_mod.configure("debug")
    assert logging.getLogger().handlers == [] or all(
        getattr(h, "name", None) != log_mod._HANDLER_NAME for h in logging.getLogger().handlers
    )
    assert not logging.getLogger("httpx").isEnabledFor(logging.DEBUG)


def test_repeated_configuration_does_not_stack_handlers() -> None:
    """The CLI entrypoint calls this once, tests call it many times; a duplicate
    handler prints every record twice."""
    for _ in range(3):
        log_mod.configure("info")
    ours = [
        h
        for h in logging.getLogger("flight_cli").handlers
        if getattr(h, "name", None) == log_mod._HANDLER_NAME
    ]
    assert len(ours) == 1
    assert ours[0].level == logging.INFO


def test_a_module_record_reaches_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point, end to end: a debug line written by the transport
    module comes out on stderr."""
    log_mod.configure("debug")
    logging.getLogger(_MODULE_LOGGER).debug("ds:1 carried no row block at %s", [2, 3])
    assert "ds:1 carried no row block at [2, 3]" in capsys.readouterr().err


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(_MODULE_LOGGER, logging.DEBUG, __file__, 1, msg, args, None)


def test_a_bad_format_string_does_not_kill_the_command() -> None:
    """`Handler.format` runs the caller's own `%` interpolation, so a mismatched
    placeholder raises from inside emit. Logging describes the work; it must
    never be able to end it.

    Driven at the handler rather than through the logger tree on purpose:
    pytest installs its own capture handler that deliberately RE-RAISES
    formatting errors, so a logger-level call would fail on pytest's handler
    and prove nothing about ours."""
    handler = log_mod._StderrHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.handle(_record("served %d rows", "not-an-int"))  # must not raise


def test_a_missing_stderr_does_not_kill_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sys.stderr` is None under pythonw and can be replaced by anything at
    all mid-run. A log line is not worth a crash.

    `configure()` is called too, not just the handler: it asks stderr whether it
    is a terminal to decide on colour, so the CLI's very first line of work
    raised an AttributeError before anything had a chance to log."""
    log_mod.configure()
    handler = log_mod._StderrHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(sys, "stderr", None)
    log_mod.configure()  # must not raise
    handler.handle(_record("a warning nobody can read"))  # must not raise


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(None, id="pythonw-has-no-stderr"),
        pytest.param(object(), id="something-that-is-not-a-stream"),
    ],
)
def test_configure_survives_a_stderr_that_cannot_say_whether_it_is_a_tty(
    monkeypatch: pytest.MonkeyPatch, stderr: object
) -> None:
    """Colour is a presentation choice. Nothing about it is worth ending the
    command on, so anything that cannot answer is simply not a terminal."""
    monkeypatch.setattr(sys, "stderr", stderr)
    log_mod.configure("debug")
    assert log_mod._stderr_wants_colour() is False


def test_a_recursion_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one exception stdlib re-raises: swallowing it would loop, because
    `handleError` writes to the same stream that just overflowed.

    `raiseExceptions` is turned off first, so the guard is the only thing that
    can produce this. Left on — pytest's default — `handleError` re-raises from
    the fake stream itself and the test passes with the guard deleted."""

    class _Recursing:
        def write(self, _s: str) -> int:
            raise RecursionError

    handler = log_mod._StderrHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(sys, "stderr", _Recursing())
    monkeypatch.setattr(logging, "raiseExceptions", False)
    with pytest.raises(RecursionError):
        handler.handle(_record("boom"))
