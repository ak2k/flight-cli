# pyright: reportPrivateUsage=false
"""Terminal control bytes in a structlog event never reach stderr.

`ConsoleRenderer` writes a string value raw unless it holds a space or a quote,
so a remote body logged at `-vv` (`pp_airline_search_failed ... body=...`)
reached the terminal with its escape sequence intact. The stdlib half of
`log.py` already drops these bytes; this pins the structlog half, which every
module outside `_gflight_ids` logs through.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from flight_cli import log as log_mod

if TYPE_CHECKING:
    from collections.abc import Iterator

_BODY = "[/x]\x1b[2J"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`configure()` mutates process-wide state, and the color test installs a
    renderer that every logger cached afterwards would keep."""
    logger = logging.getLogger("flight_cli")
    level, handlers = logger.level, list(logger.handlers)
    config = structlog.get_config()
    yield
    structlog.configure(**config)
    logger.setLevel(level)
    logger.handlers[:] = handlers


def test_a_remote_body_logged_at_debug_reaches_stderr_without_its_escape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_mod.configure("debug")
    structlog.get_logger("flight_cli.pp.client").debug(
        "pp_airline_search_failed", airline="United", body=_BODY, status=500
    )
    err = capsys.readouterr().err
    assert "airline=United body=[/x][2J status=500" in err, err
    assert "\x1b" not in err, repr(err)


def test_the_event_name_the_keys_and_c1_bytes_are_dropped_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_mod.configure("debug")
    structlog.get_logger("flight_cli.x").debug(
        "refused\x1b[2J", **{"k\x1b[2J": "v", "csi": "a\x9b31mb"}
    )
    err = capsys.readouterr().err
    assert "refused[2J" in err, err
    assert "k[2J=v" in err, err
    assert "csi=a31mb" in err, err
    for driver in ("\x1b", "\x9b"):
        assert driver not in err, f"{driver!r} reached stderr: {err!r}"


def test_a_value_that_is_not_a_string_is_left_to_the_renderer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_mod.configure("debug")
    structlog.get_logger("flight_cli.x").debug("evt", err=ValueError("boom"), n=3)
    err = capsys.readouterr().err
    assert "err=ValueError('boom')" in err, err
    assert "n=3" in err, err


def test_the_renderers_own_color_codes_survive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The strip sits before the renderer, not on the stream: on a terminal the
    renderer's colors are the one set of escapes that SHOULD arrive."""
    monkeypatch.setattr(log_mod, "_stderr_wants_colour", lambda: True)
    log_mod.configure("debug")
    structlog.get_logger("flight_cli.x").debug("evt", body=_BODY)
    err = capsys.readouterr().err
    assert "\x1b[2J" not in err, repr(err)
    assert "\x1b[" in err, repr(err)


def test_the_table_is_the_stdlib_halfs_own(capsys: pytest.CaptureFixture[str]) -> None:
    """A bidi override is dropped; a zero-width joiner, which the console
    helper's table drops but no terminal acts on, is kept."""
    log_mod.configure("debug")
    structlog.get_logger("flight_cli.x").debug("evt", body="a\u202eb\u200dc")
    err = capsys.readouterr().err
    assert "body=ab\u200dc" in err, repr(err)
