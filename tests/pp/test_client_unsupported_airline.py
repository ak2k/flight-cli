# pyright: reportPrivateUsage=false
"""End-to-end wiring of the unsupported-airline negative cache.

The helpers are unit-tested in test_client.py; these pin the behaviour that
actually saves the round-trips — that a 400 "unsupported airline" is recorded
rather than warned about, and that a recorded airline is never requested
again."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPClient, SearchSpec

if TYPE_CHECKING:
    import pathlib


def _tokens() -> Tokens:
    return Tokens(
        access_token="TOKEN",  # noqa: S106 — dummy test value
        refresh_token="REFRESH",  # noqa: S106 — dummy test value
        expires_at=9999999999,
        user_email="test@example.com",
    )


def _client(transport: httpx.MockTransport) -> PPClient:
    pp = PPClient(_tokens())
    pp._client = httpx.AsyncClient(
        base_url=API_BASE, transport=transport, headers=pp._client.headers
    )
    return pp


def _spec() -> SearchSpec:
    return SearchSpec(origin="MSY", destination="MIA", date="2026-09-09")


_UNSUPPORTED = '{"error":"unsupported airline"}'


def test_unsupported_400_is_recorded(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=_UNSUPPORTED)

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        await pp.airline_search(_spec(), "ThaiAirways")
        await pp.aclose()

    anyio.run(go)
    written: Any = json.loads(cache.read_text())
    assert "ThaiAirways" in written


def test_recorded_airline_is_not_requested_again(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """The point of the cache: the skipped airline costs zero HTTP calls."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body: Any = json.loads(req.content)
        airline = str(body["airline"])
        seen.append(airline)
        if airline == "ThaiAirways":
            return httpx.Response(400, text=_UNSUPPORTED)
        return httpx.Response(200, json={"outboundFlights": [], "inboundFlights": []})

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        await pp.airline_search_many(_spec(), ("American", "ThaiAirways"))
        await pp.airline_search_many(_spec(), ("American", "ThaiAirways"))
        await pp.aclose()

    anyio.run(go)
    assert seen.count("ThaiAirways") == 1  # learned on run 1, skipped on run 2
    assert seen.count("American") == 2  # the working airline is unaffected


def test_transient_400_is_not_recorded(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """A 400 that isn't an unsupported-airline verdict must not blacklist a
    working airline — it stays a warning and the airline is retried."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":"bad route"}')

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        await pp.airline_search(_spec(), "American")
        await pp.aclose()

    anyio.run(go)
    assert not cache.exists()


def test_rate_limit_with_unsupported_body_is_not_recorded(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """Status is checked as well as body: a 429 must never blacklist."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=_UNSUPPORTED)

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        await pp.airline_search(_spec(), "American")
        await pp.aclose()

    anyio.run(go)
    assert not cache.exists()


def test_concurrent_rejections_all_survive(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """airline_search_many fans out through a task group; every airline
    rejected in one run must be recorded, not lost to read-modify-write
    interleaving."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    rejected = ("ANA", "Finnair", "Southwest", "ThaiAirways", "CathayPacific")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=_UNSUPPORTED)

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        await pp.airline_search_many(_spec(), rejected)
        await pp.aclose()

    anyio.run(go)
    written: Any = json.loads(cache.read_text())
    assert set(written) == set(rejected)
