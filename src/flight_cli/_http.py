"""Low-level HTTP transport: httpx + curl_cffi TLS fingerprint, rate-limit,
retry, and an optional on-disk response cache for offline development."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.to_thread
import diskcache  # pyright: ignore[reportMissingTypeStubs]  # DIVERGE: no stubs shipped; Profile-B edge
import httpx
import stamina
import structlog
from aiolimiter import AsyncLimiter
from httpx_curl_cffi import AsyncCurlTransport, CurlOpt

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

# structlog.get_logger() is typed Any; pin to BoundLogger so downstream
# call sites are typed without spreading reportAny across the module.
log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

DEFAULT_IMPERSONATE = "chrome"  # alias → latest curl_cffi knows about

# 5xx + 408 are retryable per AGENTS.md (`http.HTTPStatus.*` is well-typed;
# `httpx.codes.*` is mis-typed as tuple — see AGENTS.md gotchas).
_SERVER_ERROR_FLOOR = HTTPStatus.INTERNAL_SERVER_ERROR.value  # 500
_REQUEST_TIMEOUT = HTTPStatus.REQUEST_TIMEOUT.value  # 408

# Headers required by the Alkali backend. Values copied from real SPA captures.
ALKALI_HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-JavaScript-User-Agent": "google-api-javascript-client/1.1.0",
    "X-Goog-Encode-Response-If-Executable": "base64",
    "x-alkali-application-key": "applications/matrix",
    "x-alkali-auth-apps-namespace": "alkali_v2",
    "x-alkali-auth-entities-namespace": "alkali_v2",
    "Origin": "https://matrix.itasoftware.com",
    "Referer": "https://matrix.itasoftware.com/",
}


def _is_retryable(exc: Exception) -> bool:
    """Retry on transient network errors and 5xx. Surface 4xx immediately."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        s = exc.response.status_code
        return s >= _SERVER_ERROR_FLOOR or s == _REQUEST_TIMEOUT
    return False


# How long a cached search body may be served. Matrix is research-only — the
# booking handoff is the Google Flights link — so a slightly stale fare costs a
# re-search, never a bad purchase. Long enough to dedupe the repeat queries a
# single command makes (multi-cabin merge, calendar fan-out, a `--pick`
# re-render); short enough that nobody acts on a quarter-hour-old fare.
CACHE_TTL_SECS = 15 * 60

# Bounds the cache directory. Entries are ~20 KB, so this is generous; without
# it the store only grows, which is what the previous hand-rolled cache did
# (77 days of orphans, no eviction).
CACHE_SIZE_LIMIT_BYTES = 256 * 1024 * 1024


def is_cacheable_body(data: dict[str, Any]) -> bool:
    """Whether a decoded response body may be stored.

    Matrix signals failure with HTTP 200 plus `{"error": {...}}` (see
    `MatrixApiError`), so status code alone cannot gate this and no HTTP-aware
    cache library can either — the judgement needs Matrix's own shape. Caching
    those bodies made a transient brownout permanent: a live cache inspected
    during review held 19 such entries, including `Internal server error`, each
    replayed forever because nothing expired.
    """
    return "error" not in data


class HttpTransport:
    """Wraps httpx + curl_cffi with rate-limit, retry, and optional disk cache.

    The cache is content-addressed by (url, sorted-JSON-body) and stores the
    decoded response body. It exists so we can develop the CLI / parser against
    captured responses while Matrix is in one of its frequent empty-calendar
    brownouts, and so one command's repeated queries hit the network once.

    Storage is `diskcache` (SQLite/WAL) rather than hand-rolled files. That
    choice was measured, not assumed: `diskcache` is sync-only, but through
    `anyio.to_thread` (which reuses its worker pool) a set costs ~0.15 ms
    against ~0.12 ms for a raw JSON write — noise beside a 30-45 s Matrix
    call, and the upstream's own async caveat about executor overhead applies
    to `asyncio.run()` spawning a fresh pool per call, which we don't do. In
    exchange we get per-entry TTL, LRU eviction and a size cap from a tested
    implementation; this project had already shipped two expiry bugs in
    hand-rolled caches (mtime-refresh, restamp-on-read).
    """

    def __init__(
        self,
        *,
        impersonate: str = DEFAULT_IMPERSONATE,
        rps: float = 1.0,
        concurrency: int = 3,
        timeout: float = 180.0,
        connect_timeout: float = 10.0,
        cache_dir: pathlib.Path | str | None = None,
        cache_read: bool = True,
        cache_write: bool = True,
        cache_ttl: float = CACHE_TTL_SECS,
    ) -> None:
        self._transport = AsyncCurlTransport(
            # `impersonate` is a curl_cffi BrowserTypeLiteral string at runtime;
            # accept any str from callers and let curl_cffi validate.
            impersonate=cast("Any", impersonate),
            curl_options={CurlOpt.FRESH_CONNECT: True},
            default_headers=True,
        )
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
        )
        # aiolimiter requires bucket capacity >= 1 token; invert for sub-1 rps
        if rps < 1.0:
            self._limiter = AsyncLimiter(max_rate=1, time_period=1.0 / rps)
        else:
            self._limiter = AsyncLimiter(max_rate=rps, time_period=1.0)
        self._sem = anyio.Semaphore(concurrency)

        if cache_dir is None:
            cache_dir = pathlib.Path(
                os.environ.get("MATRIX_CACHE_DIR") or pathlib.Path.home() / ".cache" / "flight-cli"
            )
        self._cache_dir = pathlib.Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Any = diskcache.Cache(  # pyright: ignore[reportUnknownMemberType]
            str(self._cache_dir / "http"),
            size_limit=CACHE_SIZE_LIMIT_BYTES,
        )
        self._cache_read = cache_read
        self._cache_write = cache_write
        self._cache_ttl = cache_ttl

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        self._cache.close()

    # ────────────────────────── cache helpers ─────────────────────────────

    def _cache_key(self, url: str, body: dict[str, Any] | None) -> str:
        h = hashlib.sha256()
        h.update(url.encode())
        if body is not None:
            h.update(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()[:24]

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Fetch a live entry, or None. Expiry is diskcache's, keyed per entry."""
        try:
            hit: Any = await anyio.to_thread.run_sync(self._cache.get, key)  # pyright: ignore[reportUnknownArgumentType]
        except Exception as e:  # noqa: BLE001 — a cache read must never fail a request
            log.warning("cache_read_failed", key=key, error=str(e))
            return None
        return cast("dict[str, Any] | None", hit)

    async def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        """Store a response body unless it is an application-level error."""
        if not is_cacheable_body(value):
            log.debug("cache_skip_error_body", key=key)
            return
        try:
            await anyio.to_thread.run_sync(
                lambda: self._cache.set(key, value, expire=self._cache_ttl),  # pyright: ignore[reportUnknownMemberType]
            )
        except Exception as e:  # noqa: BLE001 — a cache write must never fail a request
            log.warning("cache_write_failed", key=key, error=str(e))

    # ───────────────────────────── public API ──────────────────────────────

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, cache: bool = True
    ) -> dict[str, Any]:
        cache_key = self._cache_key(
            url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()),
            None,
        )
        if cache and self._cache_read:
            hit = await self._cache_get(cache_key)
            if hit is not None:
                log.debug("cache_hit", method="GET", url=url)
                return hit

        async with self._limiter, self._sem:

            @stamina.retry(
                on=_is_retryable,
                attempts=3,
                wait_initial=2.0,
                wait_jitter=1.0,
                wait_max=15.0,
                timeout=120.0,
            )
            async def _send() -> httpx.Response:
                r = await self._client.get(url, params=params, headers=ALKALI_HEADERS)
                if r.status_code >= _SERVER_ERROR_FLOOR:
                    r.raise_for_status()
                return r

            r = await _send()

        r.raise_for_status()
        data = r.json()
        if cache and self._cache_write:
            await self._cache_put(cache_key, data)
        return data

    async def post_json(
        self,
        url: str,
        body: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
        cache: bool = True,
    ) -> dict[str, Any]:
        # NB: payload includes a unique-ish bgProgramResponse on captured
        # bodies; we strip that field before hashing so two semantically equal
        # requests share a cache entry.
        body_for_hash = {k: v for k, v in body.items() if k != "bgProgramResponse"}
        cache_key = self._cache_key(
            url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()),
            body_for_hash,
        )
        if cache and self._cache_read:
            hit = await self._cache_get(cache_key)
            if hit is not None:
                log.debug("cache_hit", method="POST", url=url)
                return hit

        async with self._limiter, self._sem:

            @stamina.retry(
                on=_is_retryable,
                attempts=3,
                wait_initial=2.0,
                wait_jitter=1.0,
                wait_max=15.0,
                timeout=240.0,
            )
            async def _send() -> httpx.Response:
                r = await self._client.post(
                    url,
                    params=params,
                    json=body,
                    headers=ALKALI_HEADERS,
                )
                if r.status_code >= _SERVER_ERROR_FLOOR:
                    r.raise_for_status()
                return r

            r = await _send()

        r.raise_for_status()
        data = r.json()
        if cache and self._cache_write:
            await self._cache_put(cache_key, data)
        return data
