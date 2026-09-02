# pyright: reportPrivateUsage=false
"""The disk cache must not keep a Matrix failure.

Matrix reports failures as HTTP 200 with a top-level `error` object, so a cached
one is replayed until the cache dir is cleared (`_cache_get` has no expiry) and a
transient brownout reads as permanent — the tell is an identical request_id coming
back instantly on a retry (work-h70kv.8)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio

from flight_cli._http import HttpTransport

if TYPE_CHECKING:
    import pathlib

    import pytest

_ERROR_BODY: dict[str, Any] = {
    "error": {"message": "Internal server error.", "type": "internal"},
    "id": "5UrI3idC6NmIEscHr0l4EH",
}
_GOOD_BODY: dict[str, Any] = {
    "calendar": {"months": []},
    "solutionCount": 0,
    "id": "GRnyexQx8gZR0JQnZ0l4EA",
}


def test_error_body_is_not_cached_but_a_real_one_is(tmp_path: pathlib.Path) -> None:
    async def _go() -> None:
        # Created, used and closed inside one anyio.run — the transport holds an
        # async client (AGENTS.md rule 8). No request is made.
        async with HttpTransport(cache_dir=tmp_path, rps=1.0) as h:
            h._cache_put("err", _ERROR_BODY)
            h._cache_put("ok", _GOOD_BODY)

    anyio.run(_go)
    assert not (tmp_path / "err.json").exists()  # a failure is not a response
    assert (tmp_path / "ok.json").exists()  # ...and the guard is not a blanket off-switch


def test_non_error_shapes_still_cache(tmp_path: pathlib.Path) -> None:
    """The guard reads `value` before knowing its shape, and `value` is whatever
    `r.json()` decoded (typed Any at both call sites). A JSON array, or an `error`
    that is not an object, must still cache rather than blow up in _cache_put."""
    list_body: list[dict[str, str]] = [{"a": "1"}]
    odd_error: dict[str, Any] = {"error": "not an object", "calendar": {"months": []}}

    async def _go() -> None:
        async with HttpTransport(cache_dir=tmp_path, rps=1.0) as h:
            h._cache_put("list", list_body)
            h._cache_put("odd", odd_error)

    anyio.run(_go)
    assert (tmp_path / "list.json").exists()  # not a dict -> not an error body
    assert (tmp_path / "odd.json").exists()  # `error` must be an object, as in client.py


def test_stale_error_body_is_evicted_on_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-side guard cannot help an entry cached before it landed, and the
    cache never expires — so a brownout stored back then would be replayed forever
    without a network call. Seed one under the key `post_json` computes, then drive
    the real read path: it must miss, go out, and overwrite."""
    url = "https://example.invalid/v1/search"
    params = {"key": "k", "alt": "json"}
    body: dict[str, Any] = {"name": "calendar", "inputs": {"startDate": "2026-10-10"}}
    posts = {"n": 0}
    seeded: dict[str, pathlib.Path] = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return _GOOD_BODY

    async def _fake_post(*_a: object, **_k: object) -> _FakeResponse:
        posts["n"] += 1
        return _FakeResponse()

    async def _go() -> dict[str, Any]:
        async with HttpTransport(cache_dir=tmp_path, rps=1.0) as h:
            # Exactly the key post_json derives for this request.
            key = h._cache_key(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()), body)
            seeded["path"] = h._cache_path(key)
            seeded["path"].write_text(json.dumps(_ERROR_BODY))  # written before the guard
            monkeypatch.setattr(h._client, "post", _fake_post)
            return await h.post_json(url, body, params=params)

    got = anyio.run(_go)
    assert posts["n"] == 1  # the stale error was a MISS -> the request went out
    assert got == _GOOD_BODY
    assert json.loads(seeded["path"].read_text()) == _GOOD_BODY  # and it was overwritten
