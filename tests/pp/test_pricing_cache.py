# pyright: reportPrivateUsage=false
"""`PPClient.pricing_info` caches only an answer that is a catalog.

The catalog file is what every later search reads for 24 hours, so an answer
that is not a catalog must not replace it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from flight_cli.pp import client as client_mod
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPClient

if TYPE_CHECKING:
    import pathlib

_GOOD = json.dumps({"pricingInfos": [{"airline": "Delta", "milesToCashRatio": 0.012}]})
_NEW = json.dumps({"pricingInfos": [{"airline": "United"}, {"airline": "Delta"}]})


def _catalog(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    path = tmp_path / "cache" / "pp_pricing.json"
    monkeypatch.setattr(client_mod, "PRICING_CACHE", path)
    # The path is fixed at import, so the test moves it before anything writes.
    assert tmp_path in path.parents
    return path


def _ask(body: str) -> list[str]:
    """`pricing_info(force_refresh=True)` against an answer of `body`; the
    programs it returned, or the exception type's name when it raised."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    async def go() -> list[str]:
        pp = PPClient(
            Tokens(
                access_token="TOKEN",  # noqa: S106 — dummy test value, not a real credential
                refresh_token="REFRESH",  # noqa: S106 — dummy test value, not a real credential
                expires_at=9999999999,
            )
        )
        pp._client = httpx.AsyncClient(base_url=API_BASE, transport=httpx.MockTransport(handler))
        try:
            info = await pp.pricing_info(force_refresh=True)
        except ValueError as e:  # JSONDecodeError and ValidationError alike
            return [type(e).__name__]
        finally:
            await pp.aclose()
        return [p.airline for p in info.pricingInfos]

    return anyio.run(go)


@pytest.mark.parametrize(
    ("body", "answer"),
    [
        ("<html>maintenance</html>", ["JSONDecodeError"]),
        ("", ["JSONDecodeError"]),
        ('{"error": "nope"}', []),
        ('{"pricingInfos": []}', []),
        ('{"pricingInfos": null}', []),
    ],
    ids=["html", "empty-body", "error-object", "empty-list", "null-list"],
)
def test_an_answer_that_is_not_a_catalog_leaves_the_cached_catalog(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, body: str, answer: list[str]
) -> None:
    cache = _catalog(tmp_path, monkeypatch)
    cache.parent.mkdir()
    cache.write_text(_GOOD)
    assert _ask(body) == answer
    assert cache.read_text() == _GOOD


def test_an_answer_that_is_not_a_catalog_creates_no_cache_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _catalog(tmp_path, monkeypatch)
    assert _ask('{"error": "nope"}') == []
    assert not cache.exists()
    assert not cache.parent.exists()


def test_a_catalog_answer_replaces_the_cached_catalog_with_its_body(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _catalog(tmp_path, monkeypatch)
    cache.parent.mkdir()
    cache.write_text(_GOOD)
    assert _ask(_NEW) == ["United", "Delta"]
    assert cache.read_text() == _NEW
