# pyright: reportPrivateUsage=false
"""Requests that meet a 401 under one stale token share a single token refresh.

The refresh runs in a worker thread and takes a while; every request sent with
the stale token gets its 401 before it returns. Each one then queues on the
refresh lock, so without a check inside the lock each refreshes the token the
holder before it just refreshed, one after another."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import httpx

from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, DEFAULT_CONCURRENCY, PPClient

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

_WAITERS = 8


def _tokens(access: str) -> Tokens:
    return Tokens(
        access_token=access,
        refresh_token="REFRESH",  # noqa: S106 — dummy test value, not a real credential
        expires_at=9999999999,
    )


def _client(transport: httpx.MockTransport, *, concurrency: int = DEFAULT_CONCURRENCY) -> PPClient:
    pp = PPClient(_tokens("STALE"), concurrency=concurrency)
    pp._client = httpx.AsyncClient(
        base_url=API_BASE, transport=transport, headers=pp._client.headers
    )
    return pp


async def _until(cond: Callable[[], bool]) -> None:
    while not cond():
        await anyio.lowlevel.checkpoint()


def test_queued_401s_refresh_the_token_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """_WAITERS requests sent with STALE all get a 401 before the refresh
    returns; one refresh serves them all and each retries on its token."""
    all_rejected = threading.Event()
    refreshed_from: list[str] = []
    rejected = 0
    retried_with: list[str] = []

    def stub_refresh(t: Tokens) -> Tokens:
        assert all_rejected.wait(10), "the test's own 401s never all arrived"
        refreshed_from.append(t.access_token)
        return _tokens(f"FRESH{len(refreshed_from)}")

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal rejected
        bearer = req.headers["authorization"].removeprefix("Bearer ")
        if bearer == "STALE":
            rejected += 1
            if rejected == _WAITERS:
                all_rejected.set()
            return httpx.Response(401, text="expired")
        retried_with.append(bearer)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(client_mod, "refresh_tokens", stub_refresh)
    pp = _client(httpx.MockTransport(handler))
    statuses: list[int] = []

    async def one() -> None:
        statuses.append((await pp._request("GET", "/api/x")).status_code)

    async def go() -> None:
        try:
            async with anyio.create_task_group() as tg:
                for _ in range(_WAITERS):
                    tg.start_soon(one)
        finally:
            await pp.aclose()

    anyio.run(go)

    assert statuses == [200] * _WAITERS
    assert refreshed_from == ["STALE"], f"{len(refreshed_from)} refreshes: {refreshed_from}"
    assert retried_with == ["FRESH1"] * _WAITERS


def test_a_401_on_the_refreshed_token_refreshes_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check is on the token a request was sent with: a later request sent
    with the token the last refresh returned, and rejected, refreshes again."""
    refreshed_from: list[str] = []

    def stub_refresh(t: Tokens) -> Tokens:
        refreshed_from.append(t.access_token)
        return _tokens(f"FRESH{len(refreshed_from)}")

    def handler(req: httpx.Request) -> httpx.Response:
        bearer = req.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(200 if bearer == "FRESH2" else 401, text="x")

    monkeypatch.setattr(client_mod, "refresh_tokens", stub_refresh)
    pp = _client(httpx.MockTransport(handler))

    async def go() -> list[int]:
        try:
            first = await pp._request("GET", "/api/x")
            second = await pp._request("GET", "/api/x")
            return [first.status_code, second.status_code]
        finally:
            await pp.aclose()

    # The first request's retry on FRESH1 is rejected and returned, not looped.
    assert anyio.run(go) == [401, 200]
    assert refreshed_from == ["STALE", "FRESH1"]


def test_a_request_queued_behind_a_refresh_sends_the_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one slot: A's 401 starts a refresh, X takes the slot and holds it,
    B waits for it. The refresh returns before X lets go, so B sends the new
    token and never meets a 401 of its own."""
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refreshed_from: list[str] = []
    bearers: dict[str, list[str]] = {"/a": [], "/x": [], "/b": []}
    holding_slot = anyio.Event()
    release_slot = anyio.Event()

    def stub_refresh(t: Tokens) -> Tokens:
        refresh_started.set()
        assert release_refresh.wait(10), "the test never let the refresh return"
        refreshed_from.append(t.access_token)
        return _tokens("FRESH1")

    async def handler(req: httpx.Request) -> httpx.Response:
        bearer = req.headers["authorization"].removeprefix("Bearer ")
        bearers[req.url.path].append(bearer)
        if req.url.path == "/x":
            holding_slot.set()
            await release_slot.wait()
            return httpx.Response(200, text="x")
        return httpx.Response(401 if bearer == "STALE" else 200, text="x")

    monkeypatch.setattr(client_mod, "refresh_tokens", stub_refresh)
    pp = _client(httpx.MockTransport(handler), concurrency=1)
    stale = pp._tokens
    statuses: dict[str, int] = {}

    async def one(path: str) -> None:
        statuses[path] = (await pp._request("GET", path)).status_code

    async def go() -> None:
        try:
            with anyio.fail_after(10):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(one, "/a")
                    await _until(refresh_started.is_set)
                    tg.start_soon(one, "/x")
                    await holding_slot.wait()
                    tg.start_soon(one, "/b")
                    await _until(lambda: pp._sem.statistics().tasks_waiting == 1)
                    release_refresh.set()
                    await _until(lambda: pp._tokens is not stale)
                    release_slot.set()
        finally:
            await pp.aclose()

    anyio.run(go)

    assert statuses == {"/a": 200, "/x": 200, "/b": 200}
    assert bearers["/b"] == ["FRESH1"]
    assert refreshed_from == ["STALE"]
