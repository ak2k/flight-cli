# pyright: reportPrivateUsage=false
"""PointsPath's two cache files hold only an answer the client can use.

`extension_config` is served from its file for 7 days and `pricing_info` for
24 hours, so a body that does not parse must not be written there, and a file
that does not parse (an older build wrote it, or a write was cut short) is a
miss that the next answer replaces."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Literal, assert_never

import anyio
import httpx
import pytest

from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPApiError, PPClient

if TYPE_CHECKING:
    import pathlib

_Endpoint = Literal["ext", "pricing"]

_EXT = json.dumps({"featureFlags": {"enableDelta": 1}})
_PRICING = json.dumps({"pricingInfos": [{"airline": "Delta", "milesToCashRatio": 0.012}]})
_GOOD: dict[_Endpoint, str] = {"ext": _EXT, "pricing": _PRICING}


def _caches(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> dict[_Endpoint, pathlib.Path]:
    paths: dict[_Endpoint, pathlib.Path] = {
        "ext": tmp_path / "cache" / "pp_extension_config.json",
        "pricing": tmp_path / "cache" / "pp_pricing.json",
    }
    # The paths are fixed at import, so the test moves them before anything writes.
    monkeypatch.setattr(client_mod, "EXT_CONFIG_CACHE", paths["ext"])
    monkeypatch.setattr(client_mod, "PRICING_CACHE", paths["pricing"])
    assert all(tmp_path in p.parents for p in paths.values())
    return paths


def _ask(endpoint: _Endpoint, body: str, *, force_refresh: bool = False) -> tuple[str, int]:
    """One call against an answer of `body`; the exception type's name or "ok",
    and how many requests went out."""
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, text=body)

    async def go() -> str:
        pp = PPClient(
            Tokens(
                access_token="TOKEN",  # noqa: S106 — dummy test value, not a real credential
                refresh_token="REFRESH",  # noqa: S106 — dummy test value, not a real credential
                expires_at=9999999999,
            )
        )
        pp._client = httpx.AsyncClient(base_url=API_BASE, transport=httpx.MockTransport(handler))
        try:
            match endpoint:
                case "ext":
                    _ = await pp.extension_config(force_refresh=force_refresh)
                case "pricing":
                    _ = await pp.pricing_info(force_refresh=force_refresh)
                case _:
                    assert_never(endpoint)
        except (ValueError, PPApiError) as e:  # JSONDecodeError and ValidationError alike
            return type(e).__name__
        finally:
            await pp.aclose()
        return "ok"

    return anyio.run(go), len(requests)


def _seed(path: pathlib.Path, text: str, *, age_secs: float = 0) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    when = time.time() - age_secs
    os.utime(path, (when, when))


@pytest.mark.parametrize("prior", [None, _EXT], ids=["no-cache", "good-cache"])
@pytest.mark.parametrize("body", ["<html>maintenance</html>", ""], ids=["html", "empty-body"])
def test_an_extension_config_answer_that_is_not_json_is_not_cached(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, body: str, prior: str | None
) -> None:
    cache = _caches(tmp_path, monkeypatch)["ext"]
    if prior is not None:
        _seed(cache, prior)
    assert _ask("ext", body, force_refresh=True) == ("JSONDecodeError", 1)
    assert cache.read_text() == prior if prior is not None else not cache.exists()


@pytest.mark.parametrize("body", ["[]", "null", '"maintenance"'])
def test_an_extension_config_answer_that_is_not_an_object_is_not_cached(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    cache = _caches(tmp_path, monkeypatch)["ext"]
    assert _ask("ext", body, force_refresh=True) == ("PPApiError", 1)
    assert not cache.exists()


def test_an_extension_config_object_is_cached_and_then_served_without_a_request(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _caches(tmp_path, monkeypatch)["ext"]
    assert _ask("ext", _EXT) == ("ok", 1)
    assert cache.read_text() == _EXT
    assert _ask("ext", '{"featureFlags": {}}') == ("ok", 0)
    assert cache.read_text() == _EXT


@pytest.mark.parametrize("stored", ["<html>maintenance</html>", "", "[]"])
def test_an_extension_config_cache_that_is_not_an_object_is_refetched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    cache = _caches(tmp_path, monkeypatch)["ext"]
    _seed(cache, stored)
    assert _ask("ext", _EXT) == ("ok", 1)
    assert cache.read_text() == _EXT


@pytest.mark.parametrize(
    "stored",
    ["<html>maintenance</html>", "", "[]", '{"error": "nope"}', '{"pricingInfos": []}'],
    ids=["html", "empty-file", "array", "error-object", "empty-list"],
)
def test_a_pricing_cache_that_is_not_a_catalog_is_refetched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    cache = _caches(tmp_path, monkeypatch)["pricing"]
    _seed(cache, stored)
    assert _ask("pricing", _PRICING) == ("ok", 1)
    assert cache.read_text() == _PRICING


@pytest.mark.parametrize("endpoint", ["ext", "pricing"])
def test_a_fresh_cache_is_served_and_a_stale_one_is_refetched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, endpoint: _Endpoint
) -> None:
    cache = _caches(tmp_path, monkeypatch)[endpoint]
    match endpoint:
        case "ext":
            other = _PRICING
        case "pricing":
            other = _EXT
        case _:
            assert_never(endpoint)
    _seed(cache, _GOOD[endpoint])
    assert _ask(endpoint, _GOOD[endpoint]) == ("ok", 0)
    _seed(cache, other, age_secs=8 * 24 * 3600)
    assert _ask(endpoint, _GOOD[endpoint]) == ("ok", 1)
    assert cache.read_text() == _GOOD[endpoint]
