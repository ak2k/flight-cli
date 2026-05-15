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


class HttpTransport:
    """Wraps httpx + curl_cffi with rate-limit, retry, and optional disk cache.

    The cache is content-addressed by (url, sorted-JSON-body) and stores the
    decoded response body as JSON. It exists so we can develop the CLI / parser
    against captured responses while Matrix is in one of its frequent
    empty-calendar brownouts.
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
        self._cache_read = cache_read
        self._cache_write = cache_write

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ────────────────────────── cache helpers ─────────────────────────────

    def _cache_key(self, url: str, body: dict[str, Any] | None) -> str:
        h = hashlib.sha256()
        h.update(url.encode())
        if body is not None:
            h.update(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()[:24]

    def _cache_path(self, key: str) -> pathlib.Path:
        return self._cache_dir / f"{key}.json"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        p = self._cache_path(key)
        if not p.exists():
            return None
        try:
            return cast("dict[str, Any]", json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("cache_read_failed", key=key, error=str(e))
            return None

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        try:
            self._cache_path(key).write_text(json.dumps(value, indent=2))
        except OSError as e:
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
            hit = self._cache_get(cache_key)
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
            self._cache_put(cache_key, data)
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
            hit = self._cache_get(cache_key)
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
            self._cache_put(cache_key, data)
        return data
