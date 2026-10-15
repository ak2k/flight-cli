"""A wall the backoff cannot outlast drops the saved Google cookie.

`_persist_cookies` saves the session NID and `_seed_cookies_once` hands it to
every later process for `_COOKIE_TTL_S`. After a wall the retry could not
outlast, the saved NID is the identity Google just walled.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import Any, cast

import pytest

from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import GfThrottledError

_FILTERS = cast("Any", None)  # `_one_call` is stubbed below and never encodes it


class _Cookie:
    name = "NID"
    value = "532=walled"
    domain = ".google.com"
    path = "/"


class _Jar:
    jar = (_Cookie(),)


class _Session:
    cookies = _Jar()


class _Client:
    def _session(self) -> _Session:
        return _Session()


def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """A fresh saved NID under tmp_path, latches armed, sleeping a no-op."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"persisted": False})
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())
    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)
    path = gfid._cookie_path()  # pyright: ignore[reportPrivateUsage]
    assert path.is_relative_to(tmp_path)
    path.parent.mkdir(parents=True)
    cookie = {"name": "NID", "value": "532=walled", "domain": ".google.com", "path": "/"}
    path.write_text(json.dumps({"saved_at": time.time(), "cookies": [cookie]}))
    return path


def _walled(monkeypatch: pytest.MonkeyPatch, *, clears_after: int | None = None) -> list[int]:
    """`_one_call` raises a throttle on every call, or on the first `clears_after`."""
    calls: list[int] = []

    def fake(*_a: object, **_k: object) -> list[object]:
        calls.append(1)
        if clears_after is None or len(calls) <= clears_after:
            raise GfThrottledError("walled")
        return []

    monkeypatch.setattr(gfid, "_one_call", fake)
    return calls


def test_a_throttle_the_backoff_cannot_outlast_drops_the_saved_cookie(
    saved: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _walled(monkeypatch)
    with pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)  # pyright: ignore[reportPrivateUsage]
    assert not saved.exists()


def test_a_wall_the_retry_outlasts_keeps_the_saved_cookie(
    saved: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _walled(monkeypatch, clears_after=1)
    assert gfid._one_call_with_retry(_FILTERS) == []  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 2
    assert saved.exists()


def test_a_later_success_in_the_same_process_does_not_write_the_cookie_back(
    saved: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _walled(monkeypatch)
    with pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)  # pyright: ignore[reportPrivateUsage]
    gfid._persist_cookies(_Client())  # pyright: ignore[reportPrivateUsage]
    assert not saved.exists()


def test_a_failed_unlink_is_logged_and_the_throttle_still_propagates(
    saved: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_self: pathlib.Path, *, missing_ok: bool = False) -> None:
        raise PermissionError("read-only cache")

    monkeypatch.setattr(pathlib.Path, "unlink", refuse)
    _walled(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger=gfid.log.name), pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)  # pyright: ignore[reportPrivateUsage]
    assert saved.exists()
    assert "could not drop the saved gflight cookies: read-only cache" in caplog.text


def test_a_wall_with_no_saved_cookie_logs_nothing_to_drop(
    saved: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    saved.unlink()
    _walled(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger=gfid.log.name), pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)  # pyright: ignore[reportPrivateUsage]
    assert "could not drop" not in caplog.text
