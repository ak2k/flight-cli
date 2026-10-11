"""Any HTTP error status Matrix answers leaves `MatrixClient` as a
`MatrixHttpError`, whose text is the status line and never the request URL.

A 403 stays its own case: it is the stale-key path, covered where the key is
resolved."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import stamina

from flight_cli import client
from flight_cli.client import MatrixApiError, MatrixClient, MatrixHttpError

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "AIzaSyTestKeyNotReal0123456789"


@pytest.mark.parametrize(
    ("status", "phrase"),
    [(404, "Not Found"), (429, "Too Many Requests"), (503, "Service Unavailable")],
)
def test_an_http_error_status_is_a_matrix_http_error_without_the_url(
    tmp_path: Path, status: int, phrase: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="no")

    async def go() -> None:
        c = MatrixClient(api_key=_KEY, cache_dir=str(tmp_path), rebootstrap=False)
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
        async with c:
            await c._post("https://example.invalid/v1/search", {}, cache=False)  # pyright: ignore[reportPrivateUsage]

    with stamina.set_testing(True, attempts=1), pytest.raises(MatrixHttpError) as caught:
        anyio.run(go)

    e = caught.value
    assert isinstance(e, MatrixApiError)
    assert str(e) == f"Matrix answered HTTP {status:d} {phrase}", str(e)
    assert (e.kind, e.status) == ("http", status)
    assert e.__cause__ is None and e.__suppress_context__
    assert "key=" not in repr(e) and _KEY not in repr(e), repr(e)


def test_a_5xx_after_the_rebootstrap_is_a_matrix_http_error_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The retry after a refused key can fail on its own terms. Both calls are
    stubbed, so neither reads nor deletes the operator's cached key."""
    answers = iter([403, 502])
    monkeypatch.setattr(client, "invalidate_cache", lambda: None)

    def resolved(**_kw: object) -> str:
        return _KEY

    monkeypatch.setattr(client, "resolve_api_key", resolved)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers), text="no")

    async def go() -> None:
        c = MatrixClient(api_key=_KEY, cache_dir=str(tmp_path))
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
        async with c:
            await c._post("https://example.invalid/v1/search", {}, cache=False)  # pyright: ignore[reportPrivateUsage]

    with stamina.set_testing(True, attempts=1), pytest.raises(MatrixHttpError) as caught:
        anyio.run(go)

    assert str(caught.value) == "Matrix answered HTTP 502 Bad Gateway", str(caught.value)
