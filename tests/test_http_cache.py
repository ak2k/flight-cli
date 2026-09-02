# pyright: reportPrivateUsage=false
"""The disk cache must not keep a Matrix failure.

Matrix reports failures as HTTP 200 with a top-level `error` object, so a cached
one is replayed until the cache dir is cleared (`_cache_get` has no expiry) and a
transient brownout reads as permanent — the tell is an identical request_id coming
back instantly on a retry (work-h70kv.8)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from flight_cli._http import HttpTransport

if TYPE_CHECKING:
    import pathlib

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
