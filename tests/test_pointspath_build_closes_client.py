"""A PointsPath build that does not finish closes the HTTP client it opened:
the award deadline cutting it, or a request it made failing."""

from __future__ import annotations

from typing import override

import anyio
import pytest
from anyio.lowlevel import checkpoint

from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import PPClient
from flight_cli.pp.models import PricingInfoResponse
from flight_cli.providers import registry
from flight_cli.providers.base import AwardProvider, ProviderFailure, award_run
from flight_cli.providers.pointspath import provider as pp_provider


class _Client(PPClient):
    """A PPClient whose catalog and config calls are scripted, and which
    records its close."""

    opened: list[_Client] = []  # noqa: RUF012 — the test's own record of what the build opened
    pricing: str = "hang"
    config: str = "answer"

    def __init__(self, tokens: Tokens) -> None:
        super().__init__(tokens)
        self.closed = False
        _Client.opened.append(self)

    @override
    async def pricing_info(self, *, force_refresh: bool = False) -> PricingInfoResponse:
        if _Client.pricing == "hang":
            await anyio.sleep_forever()
        if _Client.pricing == "raise":
            msg = "catalog refused"
            raise RuntimeError(msg)
        return PricingInfoResponse()

    @override
    async def extension_config(self, *, force_refresh: bool = False) -> dict[str, object]:
        if _Client.config == "hang":
            await anyio.sleep_forever()
        return {}

    @override
    async def aclose(self) -> None:
        # A checkpoint, so a close in a cancelled scope is cut before it records.
        await checkpoint()
        self.closed = True
        await super().aclose()


@pytest.fixture(autouse=True)
def scripted_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.opened = []
    _Client.pricing = "hang"
    _Client.config = "answer"
    monkeypatch.setattr(pp_provider, "PPClient", _Client)
    monkeypatch.setattr(pp_provider, "get_valid_tokens", lambda: Tokens("access", "refresh", 0))
    monkeypatch.setattr(registry, "pp_is_configured", lambda: True)


def _build(budget_secs: float) -> tuple[AwardProvider | None, list[ProviderFailure]]:
    async def run() -> tuple[AwardProvider | None, list[ProviderFailure]]:
        with award_run(budget_secs) as run_:
            built = await registry._build_pointspath(None)  # pyright: ignore[reportPrivateUsage]
            return built, list(run_.failures)

    return anyio.run(run)


def test_a_build_the_award_deadline_cuts_closes_its_client() -> None:
    built, failures = _build(0.05)

    assert built is None
    assert failures == [ProviderFailure("PointsPath", "not answered within 0.05 s")]
    assert [c.closed for c in _Client.opened] == [True]


def test_a_catalog_request_that_fails_closes_its_client() -> None:
    _Client.pricing = "raise"

    built, failures = _build(5)

    assert built is None
    assert failures == [ProviderFailure("PointsPath", "catalog refused")]
    assert [c.closed for c in _Client.opened] == [True]


def test_a_config_request_the_award_deadline_cuts_closes_its_client() -> None:
    _Client.pricing = "answer"
    _Client.config = "hang"

    built, failures = _build(0.05)

    assert built is None
    assert failures == [ProviderFailure("PointsPath", "not answered within 0.05 s")]
    assert [c.closed for c in _Client.opened] == [True]


def test_a_finished_build_leaves_its_client_open_for_the_search() -> None:
    _Client.pricing = "answer"

    built, failures = _build(5)

    assert built is not None
    assert failures == []
    assert [c.closed for c in _Client.opened] == [False]
