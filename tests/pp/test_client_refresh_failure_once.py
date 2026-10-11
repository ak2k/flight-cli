# pyright: reportPrivateUsage=false
"""A refresh that fails is not repeated by the requests queued behind it.

A refresh that raises leaves the token unchanged, so without a record of the
failure each request queued behind it finds the token still stale inside the
lock and refreshes it again, one after another."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import PPAuthError, Tokens
from flight_cli.pp.client import API_BASE, PPClient

if TYPE_CHECKING:
    from collections.abc import Callable

_WAITERS = 8
_REFUSED = "Supabase refresh failed: HTTP 400 refresh token expired"


def _tokens(access: str) -> Tokens:
    return Tokens(
        access_token=access,
        refresh_token="REFRESH",  # noqa: S106 — dummy test value, not a real credential
        expires_at=9999999999,
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> PPClient:
    pp = PPClient(_tokens("STALE"))
    pp._client = httpx.AsyncClient(
        base_url=API_BASE, transport=httpx.MockTransport(handler), headers=pp._client.headers
    )
    return pp


def _always_401(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(401, text="expired")


async def _outcome(pp: PPClient) -> str:
    try:
        r = await pp._request("GET", "/api/x")
    except PPAuthError as e:
        return f"PPAuthError: {e}"
    return str(r.status_code)


def test_queued_401s_share_one_failed_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """_WAITERS requests sent with STALE all get a 401 before the refresh
    raises; one refresh is tried and each request raises its error."""
    all_rejected = threading.Event()
    refreshed_from: list[str] = []
    rejected = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal rejected
        rejected += 1
        if rejected == _WAITERS:
            all_rejected.set()
        return httpx.Response(401, text="expired")

    def refused(t: Tokens) -> Tokens:
        assert all_rejected.wait(10), "the test's own 401s never all arrived"
        refreshed_from.append(t.access_token)
        raise PPAuthError(_REFUSED)

    monkeypatch.setattr(client_mod, "refresh_tokens", refused)
    pp = _client(handler)
    outcomes: list[str] = []

    async def one() -> None:
        outcomes.append(await _outcome(pp))

    async def go() -> None:
        try:
            async with anyio.create_task_group() as tg:
                for _ in range(_WAITERS):
                    tg.start_soon(one)
        finally:
            await pp.aclose()

    anyio.run(go)

    assert outcomes == [f"PPAuthError: {_REFUSED}"] * _WAITERS
    assert refreshed_from == ["STALE"], f"{len(refreshed_from)} refreshes: {refreshed_from}"


def test_a_refusal_is_kept_for_the_token_it_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later request sent with the refused token raises the same error
    without a call; one sent with another token refreshes that token."""
    refreshed_from: list[str] = []

    def refused(t: Tokens) -> Tokens:
        refreshed_from.append(t.access_token)
        raise PPAuthError(_REFUSED)

    monkeypatch.setattr(client_mod, "refresh_tokens", refused)
    pp = _client(_always_401)

    async def go() -> list[str]:
        try:
            outcomes = [await _outcome(pp)]
            outcomes.append(await _outcome(pp))
            pp._tokens = _tokens("OTHER")
            outcomes.append(await _outcome(pp))
            return outcomes
        finally:
            await pp.aclose()

    assert anyio.run(go) == [f"PPAuthError: {_REFUSED}"] * 3
    assert refreshed_from == ["STALE", "OTHER"]


def test_a_refresh_that_fails_for_another_reason_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a PPAuthError is kept: any other failure (here a dropped
    connection) says nothing about the token, so the next request tries."""
    refreshed_from: list[str] = []

    def dropped(t: Tokens) -> Tokens:
        refreshed_from.append(t.access_token)
        msg = "connection dropped"
        raise httpx.ConnectError(msg)

    monkeypatch.setattr(client_mod, "refresh_tokens", dropped)
    pp = _client(_always_401)

    async def go() -> None:
        try:
            for _ in range(2):
                with pytest.raises(httpx.ConnectError):
                    await pp._request("GET", "/api/x")
        finally:
            await pp.aclose()

    anyio.run(go)

    assert refreshed_from == ["STALE", "STALE"]
