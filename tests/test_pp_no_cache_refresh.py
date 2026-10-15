# pyright: reportPrivateUsage=false
"""`--no-cache` and `FLIGHT_NO_CACHE` reach PointsPath's own caches.

A search under either asks PointsPath for its program catalog and its
extension config again, instead of reading the copies kept on disk for 24 hours
and 7 days. Matrix and PointsPath answer from stubs; no test here leaves the
machine."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import Itinerary, ItineraryDetails, SearchResult, Slice, SliceEndpoint
from flight_cli.pp import cli as pp_cli
from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPClient
from flight_cli.providers import registry
from flight_cli.providers.pointspath import provider as pp_provider

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_DEP = date.today() + timedelta(days=45)
_CATALOG = {"pricingInfos": [{"airline": "United", "milesToCashRatio": 0.012}]}
_CONFIG: dict[str, object] = {"featureFlags": {}}
_TOKENS = Tokens(
    access_token="TOKEN",  # noqa: S106 — dummy test value
    refresh_token="REFRESH",  # noqa: S106 — dummy test value
    expires_at=9999999999,
)


def _matrix_answer(*_a: object, **_kw: object) -> SearchResult:
    sol = Itinerary(
        id="sol-1",
        displayTotal="USD300.00",
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=["UA1"],
                    departure=f"{_DEP.isoformat()}T06:00",
                    origin=SliceEndpoint(code="EWR"),
                    destination=SliceEndpoint(code="LHR"),
                )
            ],
            carriers=[],
        ),
    )
    return SearchResult(solutionCount=1, solutions=[sol]).model_copy(  # pyright: ignore[reportCallIssue]
        update={"session": "s-1", "solution_set": "ss-1"}
    )


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """The PointsPath paths a search requests. Both caches hold a fresh copy,
    so a search that reads them requests neither."""
    paths: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/pricing-info":
            return httpx.Response(200, json=_CATALOG)
        if request.url.path == "/api/extension-config":
            return httpx.Response(200, json=_CONFIG)
        return httpx.Response(204)

    class _Client(PPClient):
        def __init__(self, tokens: Tokens) -> None:
            super().__init__(tokens)
            self._client = httpx.AsyncClient(
                base_url=API_BASE, transport=httpx.MockTransport(answer)
            )

    pricing = tmp_path / "pp_pricing.json"
    config = tmp_path / "pp_extension_config.json"
    pricing.write_text(json.dumps(_CATALOG))
    config.write_text(json.dumps(_CONFIG))
    monkeypatch.setattr(client_mod, "PRICING_CACHE", pricing)
    monkeypatch.setattr(client_mod, "EXT_CONFIG_CACHE", config)
    monkeypatch.setattr(client_mod, "UNSUPPORTED_CACHE", tmp_path / "unsupported.json")
    monkeypatch.setattr(pp_provider, "PPClient", _Client)
    monkeypatch.setattr(pp_provider, "get_valid_tokens", lambda: _TOKENS)
    monkeypatch.setattr(pp_provider, "load_tokens", lambda: _TOKENS)
    monkeypatch.setattr(registry, "pp_is_configured", lambda: True)
    monkeypatch.setattr(registry, "seats_is_configured", lambda: False)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: _TOKENS)
    monkeypatch.setattr(cli, "_run", _matrix_answer)
    monkeypatch.delenv("FLIGHT_NO_CACHE", raising=False)
    return paths


def _search(*extra: str, backend: str = "matrix", env: dict[str, str] | None = None) -> None:
    result = CliRunner().invoke(
        cli.app,
        ["search", "EWR", "LHR", "--dep", _DEP.isoformat(), "--backend", backend, *extra],
        env={"COLUMNS": "200", **(env or {})},
    )
    assert result.exit_code == 0, result.output


def _catalog_and_config(paths: list[str]) -> list[str]:
    return [p for p in paths if p in {"/api/pricing-info", "/api/extension-config"}]


def test_no_cache_asks_pointspath_for_its_catalog_and_config_again(asked: list[str]) -> None:
    _search("--no-cache")

    assert _catalog_and_config(asked) == ["/api/pricing-info", "/api/extension-config"]


def test_flight_no_cache_asks_pointspath_for_its_catalog_and_config_again(
    asked: list[str],
) -> None:
    _search(env={"FLIGHT_NO_CACHE": "1"})

    assert _catalog_and_config(asked) == ["/api/pricing-info", "/api/extension-config"]


def test_no_cache_on_google_asks_pointspath_for_its_catalog_and_config_again(
    asked: list[str], gf_session: Callable[..., Any], gf_board: Callable[..., str]
) -> None:
    _ = gf_session(gf_board(30))

    _search("--fast", "--no-cache", backend="gflight")

    assert _catalog_and_config(asked) == ["/api/pricing-info", "/api/extension-config"]


def test_a_search_without_either_reads_both_cached_copies(asked: list[str]) -> None:
    _search()

    assert _catalog_and_config(asked) == []
    assert "/api/airline-search" in " ".join(asked)
