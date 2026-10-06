# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_low_check.py follows; the constructor has no transport
# injection point.
"""The low-row check's Matrix client never blocks the bound.

A 403 on the check's chain search is the check's no-answer line: the client
does not re-bootstrap the API key, which would run synchronously and hold the
event loop past `_LOW_CHECK_SECONDS`."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import pytest

from flight_cli import _api_key, cli, client
from flight_cli.client import ApiKeyResolutionError, MatrixClient
from test_low_check import _b6, _chain_text, _low, _routed, _run, _under
from test_verify import _chain, _Matrix, _served

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

_LINE = "Matrix asked for row "


class _Forbidden(_Matrix):
    """Matrix that answers the default search and answers 403 to the chain search."""

    def refuse(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        if any(s.get("routeLanguage") for s in body.get("inputs", {}).get("slices", [])):
            self.bodies.append(body)
            return httpx.Response(403, json={})
        return self.handler(request)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    handler: Callable[[httpx.Request], Any],
) -> None:
    """Every client the CLI builds, with the keyword arguments the check passes
    to its own."""

    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: val for k, val in kw.items() if k in ("impersonate", "rebootstrap")},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)


def test_a_403_on_the_chain_search_is_no_answer_without_re_bootstrapping(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Red at the base: the client calls `resolve_api_key` on the 403, and the
    line is no answer only after that returns."""
    cache = tmp_path / ".matrix-key"
    monkeypatch.setattr(_api_key, "_CACHE_PATH", cache)
    assert tmp_path in _api_key._CACHE_PATH.parents
    cache.write_text("stale\n")
    resolved: list[dict[str, Any]] = []

    def resolve(**kw: Any) -> str:
        resolved.append(kw)
        time.sleep(0.3)
        return "fresh-key"

    monkeypatch.setattr(client, "resolve_api_key", resolve)
    fake = _Forbidden()
    _install(monkeypatch, tmp_path, fake.refuse)
    low = _low()
    fake.probe = _chain(_b6("USD999.00"))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): no answer: "
        "ApiKeyResolutionError: Matrix rejected the API key with HTTP 403."
    ) in under, under
    assert "key=" not in under
    assert resolved == []
    assert len(_routed(fake)) == 1
    assert not cache.exists()


def test_a_client_that_may_re_bootstrap_still_retries_once_on_a_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The default client, every other command's, re-resolves its key on a 403
    and surfaces the existing error when the retry is refused too."""
    monkeypatch.setattr(_api_key, "_CACHE_PATH", tmp_path / ".matrix-key")
    resolved: list[dict[str, Any]] = []

    def resolve(**kw: Any) -> str:
        resolved.append(kw)
        return "fresh-key"

    monkeypatch.setattr(client, "resolve_api_key", resolve)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params["key"])
        return httpx.Response(403, json={})

    async def go() -> None:
        c = MatrixClient(api_key="test-key", cache_dir=str(tmp_path), rps=1000.0)
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with c:
            await c._post("https://example.invalid/v1/search", {}, cache=False)

    with pytest.raises(ApiKeyResolutionError, match="even after re-bootstrapping"):
        anyio.run(go)
    assert resolved == [{"force_bootstrap": True}]
    assert sent == ["test-key", "fresh-key"]
