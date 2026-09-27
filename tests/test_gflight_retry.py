# pyright: reportPrivateUsage=false
"""`retry_throttled`'s empty policy, and why the search path opts out of it.

The date grid still POSTs Google's RPC, which answers a cold curl_cffi session
with an empty body (HTTP 200, nothing to raise on); the client warms up across
calls on the same session, so retrying in place recovers where a fresh session
would not. The search page has no such state: it either decodes into rows or
raises a typed refusal, so a parsed-empty board is Google's answer and re-
fetching a multi-megabyte page can only produce it again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import flight_cli._gflight_ids as gfid

if TYPE_CHECKING:
    from flight_cli._gflight_ids import GFlightWithId


def _no_sleep(_seconds: float) -> None:
    return None


def _filters() -> Any:
    # _one_call is monkeypatched in every test, so the filter is never inspected.
    return cast("Any", None)


# ───────────────── retry_empty=True (the date grid's policy) ────────────────


def test_retries_until_first_nonempty(monkeypatch: Any) -> None:
    seq: list[list[GFlightWithId]] = [[], [], cast("list[GFlightWithId]", [object()])]
    calls = {"n": 0}

    def fake_one_call() -> list[GFlightWithId]:
        out = seq[calls["n"]]
        calls["n"] += 1
        return out

    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    result = gfid.retry_throttled(fake_one_call)
    assert len(result) == 1
    assert calls["n"] == 3  # two empties retried, third returned


def test_stops_immediately_on_first_success(monkeypatch: Any) -> None:
    calls = {"n": 0}

    def fake_one_call() -> list[GFlightWithId]:
        calls["n"] += 1
        return cast("list[GFlightWithId]", [object()])

    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    result = gfid.retry_throttled(fake_one_call)
    assert len(result) == 1
    assert calls["n"] == 1  # no wasted retries when the first call works


def test_gives_up_after_max_attempts_and_returns_empty(monkeypatch: Any) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def always_empty() -> list[GFlightWithId]:
        calls["n"] += 1
        return []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gfid.time, "sleep", record_sleep)

    result = gfid.retry_throttled(always_empty)
    assert result == []
    assert calls["n"] == gfid._EMPTY_RETRY_ATTEMPTS
    # Slept between attempts but not after the last one.
    assert len(sleeps) == gfid._EMPTY_RETRY_ATTEMPTS - 1


# ───────────────── retry_empty=False (the search path's policy) ─────────────


def test_retry_empty_false_returns_the_first_empty(monkeypatch: Any) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def always_empty() -> list[GFlightWithId]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(gfid.time, "sleep", sleeps.append)

    assert gfid.retry_throttled(always_empty, retry_empty=False) == []
    assert calls["n"] == 1
    assert sleeps == []


def test_search_path_costs_one_call_on_an_empty_board(monkeypatch: Any) -> None:
    """`_one_call_with_retry` is what the search path uses; an authoritative
    zero-row page must cost exactly one fetch and no backoff."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_one_call(_f: Any) -> list[GFlightWithId]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(gfid, "_one_call", fake_one_call)
    monkeypatch.setattr(gfid.time, "sleep", sleeps.append)

    assert gfid._one_call_with_retry(_filters()) == []
    assert calls["n"] == 1
    assert sleeps == []
