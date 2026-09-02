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
# comment says why). Nothing configures the stdlib root, so those records found
# no handler and `-vv` showed none of them; a warning surfaced only through
# `logging.lastResort`. The handler below is attached to the `flight_cli`
# logger, never the root, so raising our own verbosity does not also switch on
# every third-party library that logs.
_STDLIB_ROOT = "flight_cli"
_HANDLER_NAME = "flight-cli-stderr"


class _StderrHandler(logging.Handler):
    """Writes to whatever `sys.stderr` is when the record is emitted.

    `logging.StreamHandler(sys.stderr)` captures the stream object at
    construction. This handler outlives one call — `configure()` installs it
    once and re-levels it thereafter — so a pinned stream means every record
    after the first replacement of stderr goes somewhere nobody reads."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
        except (OSError, ValueError):  # closed or detached stream — never fatal
            self.handleError(record)


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
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        cache_logger_on_first_use=True,
    )
