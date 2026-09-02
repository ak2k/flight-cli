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
