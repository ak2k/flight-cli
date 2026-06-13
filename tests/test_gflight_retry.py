# pyright: reportPrivateUsage=false
"""Retry-on-empty for the gflight backend (cold-session navigation).

Google Flights answers a cold curl_cffi session with an empty body (HTTP 200,
nothing to raise on). fli's client warms up across calls on the same session,
so retrying in place recovers — a fresh session would not. `_one_call_with_retry`
encodes that: retry empties on the reused client, return the first non-empty.
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


def test_retries_until_first_nonempty(monkeypatch: Any) -> None:
    seq: list[list[GFlightWithId]] = [[], [], cast("list[GFlightWithId]", [object()])]
    calls = {"n": 0}

    def fake_one_call(_f: Any) -> list[GFlightWithId]:
        out = seq[calls["n"]]
        calls["n"] += 1
        return out

    monkeypatch.setattr(gfid, "_one_call", fake_one_call)
    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    result = gfid._one_call_with_retry(_filters())
    assert len(result) == 1
    assert calls["n"] == 3  # two empties retried, third returned


def test_stops_immediately_on_first_success(monkeypatch: Any) -> None:
    calls = {"n": 0}

    def fake_one_call(_f: Any) -> list[GFlightWithId]:
        calls["n"] += 1
        return cast("list[GFlightWithId]", [object()])

    monkeypatch.setattr(gfid, "_one_call", fake_one_call)
    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    result = gfid._one_call_with_retry(_filters())
    assert len(result) == 1
    assert calls["n"] == 1  # no wasted retries when the first call works


def test_gives_up_after_max_attempts_and_returns_empty(monkeypatch: Any) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def always_empty(_f: Any) -> list[GFlightWithId]:
        calls["n"] += 1
        return []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gfid, "_one_call", always_empty)
    monkeypatch.setattr(gfid.time, "sleep", record_sleep)

    result = gfid._one_call_with_retry(_filters())
    assert result == []
    assert calls["n"] == gfid._EMPTY_RETRY_ATTEMPTS
    # Slept between attempts but not after the last one.
    assert len(sleeps) == gfid._EMPTY_RETRY_ATTEMPTS - 1
