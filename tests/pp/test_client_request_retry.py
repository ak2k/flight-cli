# pyright: reportPrivateUsage=false
"""Tests for PPClient._request's 401 -> refresh -> retry behaviour.

The retry happens deep inside the async request path; in production it
triggers only when a token expires mid-session, so it's exercised by
chance rather than by design. These tests pin the behaviour with a
MockTransport so regressions surface immediately."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest

from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPApiError, PPClient

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable


def _tokens(access: str = "OLD_TOKEN") -> Tokens:
    return Tokens(
        access_token=access,
        refresh_token="REFRESH",  # noqa: S106 — dummy test value, not a real credential
        expires_at=9999999999,
        user_email="test@example.com",
    )


def _recording_refresh_stub(
    calls: list[Tokens], new_access: str = "NEW_TOKEN"
) -> Callable[[Tokens], Tokens]:
    """Build a typed monkeypatch replacement for `refresh_tokens` that
    records each invocation into `calls` and returns a fresh-looking Tokens."""

    def stub(t: Tokens) -> Tokens:
        calls.append(t)
        return _tokens(access=new_access)

    return stub


def _scripted_handler(
    responses: list[httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """Hand back (i+1)-th response on the (i+1)-th call; record every request."""
    captured: list[httpx.Request] = []
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        idx = state["i"]
        state["i"] += 1
        return responses[idx]

    return handler, captured


def _client_with_transport(tokens: Tokens, transport: httpx.MockTransport) -> PPClient:
    """Construct a PPClient and swap its httpx client for one backed by the mock."""
    pp = PPClient(tokens)
    # Replace the real AsyncClient with one wired through the mock transport.
    # The constructor's client has nothing to clean up (no I/O happened yet);
    # tearing it down would force an event-loop dance in a sync fixture.
    pp._client = httpx.AsyncClient(
        base_url=API_BASE, transport=transport, headers=pp._client.headers
    )
    return pp


# ─────────────────────────────────── 401 → refresh → retry ─────────────────────────────


def test_401_refresh_retry_returns_second_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full happy path: 401 → refresh_tokens called once → retry → 200."""
    refresh_calls: list[Tokens] = []

    def _stub_refresh(t: Tokens) -> Tokens:
        refresh_calls.append(t)
        return _tokens(access="NEW_TOKEN")

    monkeypatch.setattr(client_mod, "refresh_tokens", _stub_refresh)

    handler, captured = _scripted_handler(
        [
            httpx.Response(401, text="expired"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    pp = _client_with_transport(_tokens(), httpx.MockTransport(handler))

    async def go() -> httpx.Response:
        try:
            return await pp._request("GET", "/api/pricing-info")
        finally:
            await pp.aclose()

    r = anyio.run(go)

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(refresh_calls) == 1, "refresh_tokens should be called exactly once"
    assert len(captured) == 2, "request should be sent twice (initial + retry)"
    assert captured[0].headers["authorization"] == "Bearer OLD_TOKEN"
    assert captured[1].headers["authorization"] == "Bearer NEW_TOKEN", (
        "retry must use the refreshed access token"
    )


def test_non_401_does_not_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-401 status passes through unmodified; refresh must not fire."""
    refresh_calls: list[Tokens] = []
    monkeypatch.setattr(client_mod, "refresh_tokens", _recording_refresh_stub(refresh_calls))

    handler, captured = _scripted_handler([httpx.Response(500, text="server error")])
    pp = _client_with_transport(_tokens(), httpx.MockTransport(handler))

    async def go() -> httpx.Response:
        try:
            return await pp._request("GET", "/api/pricing-info")
        finally:
            await pp.aclose()

    r = anyio.run(go)

    assert r.status_code == 500
    assert refresh_calls == [], "non-401 must not trigger refresh"
    assert len(captured) == 1, "no retry on non-401"


def test_double_401_returns_second_response_without_third_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the retry also 401s, we surface that response instead of looping forever."""
    refresh_calls: list[Tokens] = []
    monkeypatch.setattr(client_mod, "refresh_tokens", _recording_refresh_stub(refresh_calls))

    handler, captured = _scripted_handler(
        [
            httpx.Response(401, text="expired"),
            httpx.Response(401, text="still expired"),
        ]
    )
    pp = _client_with_transport(_tokens(), httpx.MockTransport(handler))

    async def go() -> httpx.Response:
        try:
            return await pp._request("GET", "/api/pricing-info")
        finally:
            await pp.aclose()

    r = anyio.run(go)

    assert r.status_code == 401
    assert len(refresh_calls) == 1, "refresh runs once even when retry also 401s"
    assert len(captured) == 2, "retry-once semantics: no third request"


# ───────────── catalog endpoints wrap HTTP failures in a domain error ─────────────


def test_pricing_info_raises_domain_error_not_httpx(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """AGENTS.md Principle 1: httpx.HTTPStatusError must not reach a caller."""
    monkeypatch.setattr("flight_cli.pp.client.PRICING_CACHE", tmp_path / "pricing.json")
    handler, _ = _scripted_handler([httpx.Response(500, text="boom")])

    async def go() -> None:
        pp = _client_with_transport(_tokens(), httpx.MockTransport(handler))
        with pytest.raises(PPApiError) as ei:
            _ = await pp.pricing_info()
        assert ei.value.status == 500
        assert ei.value.endpoint == "/api/pricing-info"
        assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)
        await pp.aclose()

    anyio.run(go)


def test_extension_config_raises_domain_error_not_httpx(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr("flight_cli.pp.client.EXT_CONFIG_CACHE", tmp_path / "ext.json")
    handler, _ = _scripted_handler([httpx.Response(503, text="down")])

    async def go() -> None:
        pp = _client_with_transport(_tokens(), httpx.MockTransport(handler))
        with pytest.raises(PPApiError):
            _ = await pp.extension_config()
        await pp.aclose()

    anyio.run(go)
