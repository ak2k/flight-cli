"""Idempotent structlog setup. Call `configure(level)` once at CLI startup.

Output goes through structlog.dev.ConsoleRenderer + stderr — readable for
a human running the CLI, structured under the hood so future debugging
glue (binding request IDs, piping to a file) is one line not a rewrite.
Terminal control bytes in an event's text are dropped before rendering.
"""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, override

import httpx
import stamina.instrumentation
import structlog

if TYPE_CHECKING:
    from stamina.instrumentation import RetryDetails
    from structlog.typing import EventDict, Processor, WrappedLogger

LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# `_gflight_ids` logs through stdlib `logging`, not structlog (its own DIVERGE
# comment says why), and nothing else in the package configures the stdlib
# root. Without the handler below those records reach no handler at all: `-vv`
# prints none of them and a warning escapes only through `logging.lastResort`.
# It attaches to the `flight_cli` logger, never the root, so raising our own
# verbosity does not also switch on every third-party library that logs.
_STDLIB_ROOT = "flight_cli"
_HANDLER_NAME = "flight-cli-stderr"


# Bytes that drive a terminal rather than appear in it, dropped from every line
# this handler writes and from every string `_strip_drivers` hands the structlog
# renderer. Page text reaches these records — a refusal quotes the board it
# could not read — and stderr redirected to a file keeps every byte for whatever
# reads the file next.
#
# Its OWN table, deliberately not the console one: that helper also escapes rich
# markup, which is wrong here. This handler renders no markup, so a record
# mentioning `[bold]` should say `[bold]`, and a backslash added on the way out
# would be a byte the log did not contain. What the two share is only the
# principle, so they share no code.
#
# The whole formatted line, not just the message: the logger NAME goes through
# the format string too, and a record can be emitted on any name.
_DRIVERS = {
    **{c: None for c in range(0x20) if c not in (0x09, 0x0A)},  # C0, keeping tab and newline
    0x7F: None,  # DEL
    **{c: None for c in range(0x80, 0xA0)},  # C1, including the 8-bit CSI
    # `str.splitlines` breaks on these two as it does on `\n`. This handler
    # writes one record per line and a reader splits them back, so a page-derived
    # message carrying one arrives as TWO records — the second of them written by
    # whoever controlled the page. The console has the same pair for the same
    # reason; here the reader IS the point.
    0x2028: None,  # LINE SEPARATOR
    0x2029: None,  # PARAGRAPH SEPARATOR
    # Bidi. These reorder the run they sit in, so a record can be made to read
    # back as something it does not say. All three families are here because
    # they do it by the same algorithm: the marks reach only the neutral
    # characters beside them, a smaller effect of the same kind.
    0x061C: None,  # ARABIC LETTER MARK
    0x200E: None,  # LEFT-TO-RIGHT MARK
    0x200F: None,  # RIGHT-TO-LEFT MARK
    **{c: None for c in range(0x202A, 0x202F)},  # embeddings and overrides
    **{c: None for c in range(0x2066, 0x206A)},  # isolates
    # A lone surrogate has no utf-8 encoding, so one reaching a real stderr does
    # not mangle the record — it DROPS it, and stdlib prints "--- Logging error
    # ---" in its place. The diagnostic is lost exactly when it is needed, and a
    # StringIO accepts them, so no test on a captured stream can see it.
    **{c: None for c in range(0xD800, 0xE000)},
}


class _StderrHandler(logging.Handler):
    """Writes to whatever `sys.stderr` is when the record is emitted.

    `logging.StreamHandler(sys.stderr)` captures the stream object at
    construction. This handler outlives one call — `configure()` installs it
    once and re-levels it thereafter — so a pinned stream means every record
    after the first replacement of stderr goes somewhere nobody reads."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # stdlib's own contract, and the reason it is this wide: `self.format`
        # runs the caller's `%` interpolation, so a mismatched placeholder in a
        # `log.debug` raises TypeError from HERE. Logging must never be able to
        # end the command it was describing. RecursionError re-raises because
        # swallowing it would loop.
        try:
            sys.stderr.write(self.format(record).translate(_DRIVERS) + "\n")
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 — a log line cannot be allowed to fail a search
            # The recovery is as unfailable as the write it recovers. stdlib's
            # `handleError` writes its own report to `sys.stderr` and guards
            # only `OSError`, so the stream that just refused this record
            # refuses that report too — a CLOSED stream raises `ValueError` and
            # the failure this arm exists to absorb escapes it instead, ending a
            # command over a line that was only describing one. `_LiveStderr`
            # makes the same trade one stream over, for the same reason.
            with suppress(Exception):
                self.handleError(record)


def _strip_drivers(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Drop `_DRIVERS` from the event, its keys and every string value.

    `ConsoleRenderer` reprs a string only when it holds a space or a quote, so a
    remote body with an escape sequence in it is written raw at `-vv`. It reprs
    every other value type, which escapes what this drops from strings, so only
    `str` needs the pass. It runs as the last processor before the renderer, so
    the renderer's own color codes are added after it and survive; stripping
    the stream instead would remove them too."""
    return {
        key.translate(_DRIVERS): value.translate(_DRIVERS) if isinstance(value, str) else value
        for key, value in event_dict.items()
    }


def _stderr_wants_colour() -> bool:
    """Whether to colour stderr, for a stderr that may not be a stream at all.

    `sys.stderr` is None under pythonw, and any program may rebind or close it,
    so asking it directly turns the CLI's own startup into an AttributeError
    before it has run anything. Anything that cannot answer is not a terminal."""
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _configure_stdlib(lvl: int) -> None:
    """Put a stderr handler on the `flight_cli` stdlib logger at `lvl`.

    Idempotent by handler name, so repeated `configure()` calls re-level the one
    handler instead of stacking duplicates that print each record twice.
    Propagation is deliberately left on: the root has no handler of its own, and
    pytest's `caplog` captures at the root."""
    logger = logging.getLogger(_STDLIB_ROOT)
    logger.setLevel(lvl)
    for existing in logger.handlers:
        if getattr(existing, "name", None) == _HANDLER_NAME:
            existing.setLevel(lvl)
            return
    handler = _StderrHandler()
    handler.name = _HANDLER_NAME
    handler.setLevel(lvl)
    handler.setFormatter(logging.Formatter("%(levelname)-8s [%(name)s] %(message)s"))
    logger.addHandler(handler)


class _LiveStderr:
    """The stream `PrintLogger` writes through: whatever `sys.stderr` is NOW.

    `PrintLogger` takes a file at construction, and `configure` caches the
    bound logger on first use, so passing `sys.stderr` itself pins whichever
    stream carried the first record — and an embedding host that then replaces
    it gets the diagnostic in a closed stream or in stdout, where the
    `--format json` document goes. Both failures are written up under **A
    diagnostic resolves its stream per write** in
    docs/memories/gf_request_budget.md. Resolving per write is
    what `_StderrHandler` does for the stdlib half of this module, for the same
    reason, and a stream that cannot be written to is written nowhere.

    Truthy and left that way deliberately: `PrintLogger.__init__` reads
    `file or stdout`, so a proxy that ever tested false would route the whole
    package's diagnostics into stdout."""

    def write(self, s: str) -> int:
        stream = sys.stderr
        if stream is None:
            return 0
        # A closed stream raises ValueError, a broken one OSError, and
        # `sys.stderr` can be rebound to something that is not a stream at all
        # — `_stderr_wants_colour` below already assumes as much. Broad because
        # that list is open-ended: a re-entered write raises RuntimeError and a
        # `write` of another shape raises TypeError, and either ends the command
        # the log line was only describing. `Exception` is the trade
        # `_StderrHandler.emit` makes one stream over, and it is the trade here.
        with suppress(Exception):
            stream.write(s)
        # What `print` would have written. It discards the count, and asking
        # the stream for its own would re-raise everything just suppressed.
        return len(s)

    def flush(self) -> None:
        stream = sys.stderr
        if stream is None:
            return
        with suppress(Exception):
            stream.flush()


# One proxy for the whole package, not one per logger. structlog keys its write
# lock on the file OBJECT it was handed (`structlog._output.WRITE_LOCKS`), so a
# fresh proxy per factory call gives every module logger a lock of its own and
# they stop excluding each other — two threads then interleave characters of
# two records on the one stream both are writing to. That table is a plain dict
# holding strong references and is never pruned, so a proxy per call is also a
# proxy kept for the life of the process.
_LIVE_STDERR = _LiveStderr()


def _stderr_logger_factory(*_args: object) -> structlog.PrintLogger:
    """A structlog logger writing to whatever `sys.stderr` is per record.

    structlog's default factory writes to STDOUT, which is the stream
    `--format json` writes its document to — a retry warning or a rate-limit
    pause there splices a diagnostic into a machine consumer's input. This
    module's docstring, `configure`'s, and the CLI's `-v` help all say stderr,
    so this is what makes them true.

    Resolved per write rather than captured here, for the same reason the
    stdlib handler resolves it per record: this process replaces `sys.stderr`."""
    # reportArgumentType: `PrintLogger` annotates `file` as `TextIO` but uses
    # only `write`/`flush`, through `print`. Being one fixed stream is the
    # property this proxy exists not to have, so the annotation is the thing
    # ignored rather than a shape we could satisfy.
    return structlog.PrintLogger(file=_LIVE_STDERR)  # pyright: ignore[reportArgumentType]


def _retry_logged(details: RetryDetails) -> None:
    """Log a scheduled retry naming its cause by type, and by status for an HTTP
    status error. stamina's default hook logs `repr` of the exception, and
    httpx's status error quotes the request URL, which for Matrix carries the
    API key."""
    caused_by = type(details.caused_by).__name__
    if isinstance(details.caused_by, httpx.HTTPStatusError):
        caused_by = f"{caused_by} {details.caused_by.response.status_code:d}"
    structlog.get_logger("flight_cli.retry").warning(
        "retry_scheduled",
        callable=details.name,
        retry_num=details.retry_num,
        wait_for=round(details.wait_for, 2),
        caused_by=caused_by,
    )


def configure(level: str = "warning") -> None:
    """Configure structlog for human-readable stderr output, and give the one
    stdlib-logging module in the package somewhere for its records to go.

    Idempotent: safe to call multiple times (tests + app entry).
    """
    lvl = LEVELS.get(level.lower(), logging.WARNING)
    _configure_stdlib(lvl)
    stamina.instrumentation.set_on_retry_hooks([_retry_logged])
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        _strip_drivers,
        structlog.dev.ConsoleRenderer(colors=_stderr_wants_colour()),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )
