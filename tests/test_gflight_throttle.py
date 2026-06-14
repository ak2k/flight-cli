# pyright: reportPrivateUsage=false
"""Tests for GF throttle detection + the two-policy retry in _gflight_ids."""

from __future__ import annotations

from typing import Any, cast

import pytest

from flight_cli import _gflight_ids
from flight_cli._gflight_ids import GfThrottledError, _is_throttle_block

# A genuine throttle body: HTTP 200 wrapper with a code-13 ErrorResponse.
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


# ─────────────────────────── detection ─────────────────────────────────


def test_is_throttle_block_true_on_error_envelope() -> None:
    assert _is_throttle_block(_BLOCK_BODY)


def test_is_throttle_block_false_on_empty_or_data() -> None:
    assert not _is_throttle_block("")
    assert not _is_throttle_block(')]}\'\n[["wrb.fr",null,"realpayloadhere"]]')


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


# ─────────────────────────── cold-session empty retry ──────────────────


def test_retry_returns_empty_after_cold_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) == []
    assert calls["n"] == _gflight_ids._EMPTY_RETRY_ATTEMPTS  # bounded, no raise


def test_retry_recovers_from_cold_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    data: list[Any] = [object()]

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        return [] if calls["n"] == 1 else data

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) is data
    assert calls["n"] == 2


def test_throttle_and_empty_policies_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A throttle, then a cold empty, then data — both retry paths cooperate.
    seq: list[Any] = ["throttle", [], [object()]]
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        item = seq[calls["n"]]
        calls["n"] += 1
        if item == "throttle":
            raise GfThrottledError("throttled")
        return item

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    out = _gflight_ids._one_call_with_retry(_FILTERS)
    assert out == seq[2]
    assert calls["n"] == 3
