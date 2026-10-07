# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""The "stopped pinning" line words a throttle as the rung that met it, as `cli._gf_refusal` does.

Rung 1 is faked at its GET (`_one_call`) and rung 2 at `_one_call_browser`."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli._gf_errors import GfBrowserUnavailableError, GfThrottledError, GfTransportError
from test_gf_auto_escalation import (
    _FAST_JSON,
    _THREE,
    _no_backoff,  # noqa: F401 # pyright: ignore[reportUnusedImport] — the autouse fixture
    _run,
    _rungs,
    _throttled_from,
)
from test_gf_chunked_search import _Google
from test_gf_full_board import _DEP, _RET

if TYPE_CHECKING:
    from collections.abc import Callable

_BROWSER_RUNG = (
    "stopped pinning: 2 of 3 return boards skipped; Google Flights rate-limited the browser rung"
)
_THIS_IP = "stopped pinning: 2 of 3 return boards skipped; Google Flights rate-limited this IP"


def _stopped_pinning(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "stopped pinning" in r.getMessage()]


def _round_trip(*extra: str) -> Any:
    when = ["--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
    return _run("JFK,LGA,EWR", "LAX", *when, *_FAST_JSON, *extra)


def _refused(*_a: object, **_kw: object) -> Any:
    raise GfThrottledError("rate-limited")


def _failing_from(get: int, google: _Google, failure: Exception) -> Callable[..., Any]:
    """`google`, raising `failure` on its `get`-th request and every one after it."""
    seen: list[bool] = []

    def chrome(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        seen.append(True)
        if len(seen) >= get:
            raise failure
        return google(filters, currency=currency, cheapest=cheapest)

    return chrome


def test_an_escalated_search_words_the_pin_stop_as_the_browser_rung(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Red at the base: pin 2 throttles on http, escalates, and throttles on Chrome.
    The base's "stopped pinning" line says "rate-limited this IP" for that
    Chrome throttle, while the page line says "the browser rung"."""
    google = _Google(_THREE)
    _rungs(monkeypatch, [], http=_throttled_from(3, google), chrome=_refused)
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        result = _round_trip("--gf-transport", "auto")
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 2
    assert _stopped_pinning(caplog) == [_BROWSER_RUNG], caplog.text
    assert "this IP" not in caplog.text, caplog.text


def test_an_explicit_browser_search_words_the_pin_stop_as_the_browser_rung(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Red at the base: the same Chrome throttle under `--gf-transport browser`."""
    google = _Google(_THREE)
    _rungs(monkeypatch, [], http=_refused, chrome=_throttled_from(3, google))
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        result = _round_trip("--gf-transport", "browser")
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 2
    assert _stopped_pinning(caplog) == [_BROWSER_RUNG], caplog.text
    assert "this IP" not in caplog.text, caplog.text


def test_an_explicit_http_search_keeps_the_ip_wording(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Green at the base: rung 1's throttle is the IP's."""
    google = _Google(_THREE)
    _rungs(monkeypatch, [], http=_throttled_from(3, google), chrome=_refused)
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        result = _round_trip("--gf-transport", "http")
    assert result.exit_code == 0, result.output
    assert _stopped_pinning(caplog) == [_THIS_IP], caplog.text


@pytest.mark.parametrize(
    ("failure", "why"),
    [
        (GfTransportError("x"), "Google Flights was unreachable"),
        (
            GfBrowserUnavailableError("Chrome died.", remedy="Retry."),
            "the browser rung stopped — Chrome died. Retry.",
        ),
    ],
    ids=["transport", "unavailable"],
)
def test_a_browser_stop_that_is_not_a_throttle_keeps_its_own_words(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    why: str,
) -> None:
    """Green at the base: only a throttle is worded by rung."""
    google = _Google(_THREE)
    _rungs(monkeypatch, [], http=_refused, chrome=_failing_from(3, google, failure))
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        result = _round_trip("--gf-transport", "browser")
    assert result.exit_code == 0, result.output
    assert _stopped_pinning(caplog) == [f"stopped pinning: 2 of 3 return boards skipped; {why}"]
