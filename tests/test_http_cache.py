# pyright: reportPrivateUsage=false
# DIVERGE: swapping in a MockTransport requires reaching for `_client`, the
# same pattern tests/pp/test_client_request_retry.py established. The public
# constructor has no transport injection point.
"""Tests for HttpTransport's response cache.

`_http.py` had no dedicated test file despite sitting under every network call
the tool makes. These pin the two properties that actually protect a user: an
application-level error is never memoized, and a cached fare eventually
expires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import httpx

from flight_cli._http import HttpTransport, is_cacheable_body

if TYPE_CHECKING:
    import pathlib


def _transport(tmp: pathlib.Path, handler: Any, **kw: Any) -> HttpTransport:
    t = HttpTransport(cache_dir=tmp, rps=1000.0, **kw)
    # Swap in a mock transport; the constructor's client has done no I/O yet.
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


def _counting_handler(body: dict[str, Any]) -> tuple[Any, list[int]]:
    calls = [0]

    def handler(_req: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, json=body)

    return handler, calls


# ───────────────────────── error bodies are never stored ─────────────────────


def test_matrix_error_envelope_is_not_cacheable() -> None:
    """Matrix signals failure with HTTP 200 + `{"error": ...}`, so no
    status-code or HTTP-aware rule can catch it — the check needs Matrix's own
    shape."""
    assert is_cacheable_body({"solutionList": {"solutions": []}}) is True
    assert is_cacheable_body({"error": {"message": "Internal server error."}}) is False
    assert is_cacheable_body({"error": {"message": "QPX Warning.  Bad route"}}) is False


def test_error_response_is_refetched_not_memoized(tmp_path: pathlib.Path) -> None:
    """The bug this prevents: a transient brownout became permanent. A live
    cache inspected during review held 19 such entries, each replayed forever
    because nothing expired."""
    handler, calls = _counting_handler({"error": {"message": "Internal server error."}})

    async def go() -> None:
        t = _transport(tmp_path, handler)
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        await t.aclose()

    anyio.run(go)
    assert calls[0] == 2  # both requests hit the network


def test_successful_response_is_served_from_cache(tmp_path: pathlib.Path) -> None:
    handler, calls = _counting_handler({"solutionList": {"solutions": [{"id": "a"}]}})

    async def go() -> None:
        t = _transport(tmp_path, handler)
        first = await t.post_json("https://x.test/v1/search", {"q": 1})
        second = await t.post_json("https://x.test/v1/search", {"q": 1})
        assert first == second
        await t.aclose()

    anyio.run(go)
    assert calls[0] == 1  # second request served from cache


# ────────────────────────────── entries expire ───────────────────────────────


def test_cached_entry_expires(tmp_path: pathlib.Path) -> None:
    """A fare must not be served indefinitely. The previous hand-rolled cache
    had no expiry at all: its newest entry was 16 hours old and its oldest 77
    days, all still live."""
    handler, calls = _counting_handler({"solutionList": {"solutions": []}})

    async def go() -> None:
        t = _transport(tmp_path, handler, cache_ttl=0.5)
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        await anyio.sleep(0.7)
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        await t.aclose()

    anyio.run(go)
    assert calls[0] == 2


def test_distinct_bodies_do_not_share_an_entry(tmp_path: pathlib.Path) -> None:
    """Two different searches must never resolve to one cached response."""
    handler, calls = _counting_handler({"solutionList": {"solutions": []}})

    async def go() -> None:
        t = _transport(tmp_path, handler)
        _ = await t.post_json("https://x.test/v1/search", {"origin": "MSY"})
        _ = await t.post_json("https://x.test/v1/search", {"origin": "JFK"})
        await t.aclose()

    anyio.run(go)
    assert calls[0] == 2


def test_cache_read_disabled_always_refetches(tmp_path: pathlib.Path) -> None:
    handler, calls = _counting_handler({"solutionList": {"solutions": []}})

    async def go() -> None:
        t = _transport(tmp_path, handler, cache_read=False)
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        _ = await t.post_json("https://x.test/v1/search", {"q": 1})
        await t.aclose()

    anyio.run(go)
    assert calls[0] == 2
