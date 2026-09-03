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

import functools
import json
import logging
import sys
from typing import TYPE_CHECKING

import pytest

from flight_cli import log as log_mod

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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


def test_a_record_carrying_terminal_control_bytes_reaches_the_stream_without_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Page text reaches these records, so remote bytes reach this stream.

    A refusal names the board it could not read, and the classifier quotes what
    it found. Today every such call spells the payload with `%r`, which is why
    nothing has escaped — but that is a habit held at each call site, and one
    `%s` written later undoes it silently. Pinned at the handler instead, where
    it holds for calls nobody has written yet.

    The C1 byte matters as much as the ESC: a single 0x9b IS a control sequence
    introducer on a terminal that reads eight-bit sequences, and it survives
    every rule written about `\x1b[`."""
    log_mod.configure("debug")
    logging.getLogger(_MODULE_LOGGER).debug("ds:1 refused: %s", "\x1b[2Jcleared\x9b31mred\x07bell")
    err = capsys.readouterr().err
    assert "cleared" in err and "red" in err and "bell" in err, err
    for driver in ("\x1b", "\x9b", "\x07"):
        assert driver not in err, f"{driver!r} reached the terminal: {err!r}"


def test_structlog_records_go_to_stderr_so_json_stdout_stays_parseable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """structlog's default factory writes to STDOUT, which is where
    `--format json` puts its document.

    A retry warning or a rate-limit pause therefore landed in the middle of a
    machine consumer's input — not corrupting a table a human reads, but
    breaking `json.load` for the one output format that promises to be
    parseable. Every docstring in this package says these go to stderr."""
    import structlog

    log_mod.configure("warning")
    structlog.get_logger().warning("gflight throttled", backoff=1.5)
    print(json.dumps({"solutions": []}))  # the JSON document the CLI writes to stdout

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"solutions": []}, captured.out
    assert "gflight throttled" in captured.err, captured.err


def test_a_cached_structlog_logger_writes_to_the_stderr_of_the_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`configure()` caches each logger on first use, so a factory that hands
    over `sys.stderr` itself pins whichever stream was installed when the first
    record was emitted.

    Every module here logs through a proxy built at import — `_http.log` is
    one — and those outlive any one call. A host that replaces and closes the
    stream it started with then gets `ValueError: I/O operation on closed file`
    out of its next log line, which is the one thing this module's stdlib half
    already refuses to do."""
    import io

    from flight_cli import _http

    # Un-cache the module-level proxy so THIS test decides which stream it
    # binds first; monkeypatch restores whatever the session had after it.
    monkeypatch.setattr(_http.log, "bind", functools.partial(type(_http.log).bind, _http.log))

    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    log_mod.configure("warning")
    _http.log.warning("the record that caches the logger")
    assert "the record that caches the logger" in first.getvalue()

    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    first.close()
    _http.log.warning("the record after the stream was replaced")
    assert "the record after the stream was replaced" in second.getvalue()


def test_a_record_with_no_stderr_to_reach_never_falls_back_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sys.stderr` is None under pythonw and under any host that took it away.
    structlog's `PrintLogger` reads `file or stdout`, so a None stream there
    does not silence the record — it moves it to STDOUT, which is where
    `--format json` writes its document. A diagnostic is worth less than a
    parseable answer, so a record with no stream goes nowhere."""
    import structlog

    monkeypatch.setattr(sys, "stderr", None)
    log_mod.configure("warning")
    structlog.get_logger("flight_cli.nowhere").warning("a diagnostic with nowhere to go")
    print(json.dumps({"solutions": []}))  # the JSON document the CLI writes to stdout

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"solutions": []}, captured.out


def test_a_record_survives_a_real_stream_whatever_the_page_put_in_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a REAL utf-8 stream, not the StringIO a captured one hands back.

    The difference decides the test. A StringIO accepts a lone surrogate
    silently; an encoding stream raises, stdlib swallows it and prints
    "--- Logging error ---" instead, and the whole record is gone — the
    diagnostic lost precisely when a page has drifted far enough to produce
    one. A line separator is the other half: this handler writes one record per
    line, so a message carrying one arrives at a reader as two records, the
    second written by whoever controlled the page."""
    path = tmp_path / "err.log"
    with path.open("w", encoding="utf-8") as stream:
        monkeypatch.setattr(sys, "stderr", stream)
        log_mod.configure("debug")
        logging.getLogger(_MODULE_LOGGER).debug(
            "ds:1 refused: %s",
            "head\u2028forged\u2029split\ud800\u202ereversed\u2069tail\u200fmarked\u061cend",
        )
    written = path.read_text(encoding="utf-8")

    assert "--- Logging error ---" not in written, written
    for readable in ("head", "forged", "split", "reversed", "tail", "marked", "end"):
        assert readable in written, written
    for hostile in ("\u2028", "\u2029", "\ud800", "\u202e", "\u2069", "\u200f", "\u061c"):
        assert hostile not in written, f"{hostile!r} reached the stream: {written!r}"
    assert written.count("\n") == 1, f"one record must be one line: {written!r}"


def test_the_handler_keeps_the_whitespace_a_log_line_is_made_of(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stripping is for bytes that drive a terminal, not for layout. A tab
    inside a quoted payload is content, and the newline the handler itself adds
    is what makes a line a line."""
    log_mod.configure("debug")
    logging.getLogger(_MODULE_LOGGER).debug("a\tb")
    err = capsys.readouterr().err
    assert "a\tb" in err, err
    assert err.endswith("\n")


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
