"""Idempotent structlog setup. Call `configure(level)` once at CLI startup.

Output goes through structlog.dev.ConsoleRenderer + stderr — readable for
a human running the CLI, structured under the hood so future debugging
glue (binding request IDs, piping to a file) is one line not a rewrite.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, override

import structlog

if TYPE_CHECKING:
    from structlog.typing import Processor

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
# this handler writes. Page text reaches these records — a refusal quotes the
# board it could not read — and stderr redirected to a file keeps every byte for
# whatever reads the file next.
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
    # back as something it does not say.
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
            self.handleError(record)


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


def _stderr_logger_factory(*_args: object) -> structlog.PrintLogger:
    """A structlog logger writing to whatever `sys.stderr` is when it is built.

    structlog's default factory writes to STDOUT, which is the stream
    `--format json` writes its document to — a retry warning or a rate-limit
    pause there splices a diagnostic into a machine consumer's input. This
    module's docstring, `configure`'s, and the CLI's `-v` help all say stderr,
    so this is what makes them true.

    Resolved per logger rather than captured at import, for the same reason the
    stdlib handler resolves it per record: this process replaces `sys.stderr`."""
    return structlog.PrintLogger(file=sys.stderr)


def configure(level: str = "warning") -> None:
    """Configure structlog for human-readable stderr output, and give the one
    stdlib-logging module in the package somewhere for its records to go.

    Idempotent: safe to call multiple times (tests + app entry).
    """
    lvl = LEVELS.get(level.lower(), logging.WARNING)
    _configure_stdlib(lvl)
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.dev.ConsoleRenderer(colors=_stderr_wants_colour()),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )
