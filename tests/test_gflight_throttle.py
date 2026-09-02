# pyright: reportPrivateUsage=false
"""GF throttle detection on both transports, and the retry that wraps them.

Two transports, two block signals: the date grid still POSTs an RPC and reads a
code-13 error envelope out of the body (`_is_throttle_block`); the search path
GETs a page and reads the final URL and the body (`_is_page_throttled`), because
an outright 429 is raised by fli's client and never reaches that predicate.
`retry_throttled` backs off the same way for both.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from flight_cli import _gflight_ids
from flight_cli._gf_errors import GfThrottledError
from flight_cli._gflight_ids import _is_consent_page, _is_page_throttled, _is_throttle_block

# A genuine RPC throttle body: HTTP 200 wrapper with a code-13 ErrorResponse.
_BLOCK_BODY = (
    ')]}\'\n\n[["wrb.fr",null,null,null,null,[13,null,'
    '[["type.googleapis.com/travel.frontend.flights.ErrorResponse",[[null]]]]]]]'
)
_FILTERS = cast("Any", None)  # patched _one_call ignores its arg


@pytest.fixture(autouse=True)
def _no_sleep_no_jitter(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(*_a: object) -> None:
        return None

    def _zero() -> float:
        return 0.0

    monkeypatch.setattr(_gflight_ids.time, "sleep", _noop)
    monkeypatch.setattr(_gflight_ids.random, "random", _zero)


# ──────────────────── detection: RPC body (date grid) ──────────────────


def test_is_throttle_block_true_on_error_envelope() -> None:
    assert _is_throttle_block(_BLOCK_BODY)


def test_is_throttle_block_false_on_empty_or_data() -> None:
    assert not _is_throttle_block("")
    assert not _is_throttle_block(')]}\'\n[["wrb.fr",null,"realpayloadhere"]]')


# ──────────────────── detection: search page (search) ──────────────────


def test_is_page_throttled_on_sorry_redirect() -> None:
    assert _is_page_throttled(
        final_url="https://www.google.com/sorry/index?continue=x", html="<html>captcha</html>"
    )


def test_is_page_throttled_on_the_interstitial_served_in_place() -> None:
    """Google also serves the block at the requested URL with HTTP 200, leaving
    the body as the only tell. An HTTP 429 never reaches this predicate — fli's
    client raises it (see `_one_call`)."""
    assert _is_page_throttled(
        final_url="https://www.google.com/travel/flights?tfs=abc",
        html="<html>Our systems have detected unusual traffic</html>",
    )


def test_is_page_throttled_false_on_a_served_page() -> None:
    assert not _is_page_throttled(
        final_url="https://www.google.com/travel/flights?tfs=abc", html="<html>results</html>"
    )


def test_is_consent_page_on_the_interstitial() -> None:
    assert _is_consent_page(final_url="https://consent.google.com/m?continue=x", html="")
    assert _is_consent_page(final_url="", html='<form action="https://consent.google.com/save">')


def test_is_consent_page_false_on_an_ordinary_page() -> None:
    assert not _is_consent_page(final_url="https://www.google.com/travel/flights", html="<html>")


# ─────────────────────────── throttle retry ────────────────────────────


def test_retry_recovers_from_transient_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    data: list[Any] = [object()]

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise GfThrottledError("throttled")
        return data

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) is data
    assert calls["n"] == 3  # two blocks (backoff+retry) then success


def test_retry_raises_when_throttle_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        raise GfThrottledError("throttled")

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    with pytest.raises(GfThrottledError):
        _gflight_ids._one_call_with_retry(_FILTERS)
    assert calls["n"] == _gflight_ids._THROTTLE_RETRY_ATTEMPTS + 1


def test_search_path_does_not_retry_a_parsed_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page either decodes or raises, so an empty board is Google's answer.
    Retrying it would re-fetch megabytes to be told the same thing."""
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) == []
    assert calls["n"] == 1


def test_throttle_backoff_survives_an_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # A throttle then an empty: the throttle is retried, the empty is returned.
    seq: list[Any] = ["throttle", []]
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        item = seq[calls["n"]]
        calls["n"] += 1
        if item == "throttle":
            raise GfThrottledError("throttled")
        return cast("list[Any]", item)

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) == []
    assert calls["n"] == 2
