# pyright: reportPrivateUsage=false
"""Regression test for work-dgmkv: `run_pp_for_search` must create, use, and
close its providers inside a *single* event loop.

The original bug ran `anyio.run(_go)` to gather awards (loop A), then a second
`anyio.run(_aclose_all, providers)` to close them (loop B). The providers hold
async HTTP transports (curl_cffi/httpx) whose sockets are bound to loop A;
tearing them down from loop B made their `__del__`/close fire `loop.call_soon`
on the now-closed loop A — a full `RuntimeError: Event loop is closed`
traceback and exit 1 on every otherwise-successful search.

We don't need a live network here: monkeypatch the registry call so the only
thing under test is the loop discipline. The invariant is captured precisely —
the loop a provider is constructed in must be the same object it is closed in.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from flight_cli.pp import cli as pp_cli
from flight_cli.providers.base import LegQuery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from flight_cli.models import SearchResult
    from flight_cli.providers.base import AwardFlight


def _no_cash_hints(*_args: object, **_kwargs: object) -> tuple[object, ...]:
    return ()


class _LoopRecordingProvider:
    """Minimal provider stand-in. Records the running loop at close time."""

    name = "fake"

    def __init__(self) -> None:
        self.closed_in: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        self.closed_in = asyncio.get_running_loop()


def test_providers_closed_in_same_loop_they_were_built_in(
    monkeypatch: Any,
) -> None:
    record: dict[str, asyncio.AbstractEventLoop | _LoopRecordingProvider] = {}

    async def fake_gather_awards(
        legs: list[LegQuery],
        **_kwargs: object,
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        # Constructed inside whatever loop `_go` runs in — exactly what a real
        # provider's transport binds its sockets to.
        provider = _LoopRecordingProvider()
        record["built_in"] = asyncio.get_running_loop()
        record["provider"] = provider
        per_leg: list[list[AwardFlight]] = [[] for _ in legs]
        return per_leg, [provider]

    # Neutralize the PP token pre-flight and the cash-hint extraction so the
    # function body reaches `_go` without needing real tokens or a SearchResult.
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr(pp_cli, "cash_hints_from_search_result", _no_cash_hints)
    monkeypatch.setattr(pp_cli, "gather_awards", fake_gather_awards)

    legs = [
        LegQuery(
            origin="JFK",
            destination="LHR",
            date="2026-08-15",
            slice_index=0,
            label="JFK→LHR",
        )
    ]

    # pp_only + json_out returns right after serialization — no `join`, no
    # SearchResult introspection. res is never inspected on this path.
    pp_cli.run_pp_for_search(
        cast("SearchResult", object()),
        legs=legs,
        pp_only=True,
        json_out=True,
    )

    provider = cast("_LoopRecordingProvider", record["provider"])
    built_in = cast("asyncio.AbstractEventLoop", record["built_in"])
    assert provider.closed_in is not None, "provider was never closed"
    assert provider.closed_in is built_in, (
        "provider closed in a different event loop than it was built in — "
        "the two-anyio.run teardown bug (work-dgmkv) has regressed"
    )


def test_clean_exit_does_not_raise_event_loop_closed(monkeypatch: Any) -> None:
    """End-to-end-ish: the whole call completes without surfacing the
    'Event loop is closed' RuntimeError, even when a provider's aclose touches
    the loop (as a real curl_cffi/httpx transport teardown does)."""

    async def fake_gather_awards(
        legs: list[LegQuery],
        **_kwargs: object,
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        provider = _LoopRecordingProvider()
        per_leg: list[list[AwardFlight]] = [[] for _ in legs]
        return per_leg, [provider]

    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr(pp_cli, "cash_hints_from_search_result", _no_cash_hints)
    monkeypatch.setattr(pp_cli, "gather_awards", fake_gather_awards)

    legs: Sequence[LegQuery] = [
        LegQuery(
            origin="JFK",
            destination="LHR",
            date="2026-08-15",
            slice_index=0,
            label="JFK→LHR",
        )
    ]

    # Should not raise.
    pp_cli.run_pp_for_search(
        cast("SearchResult", object()),
        legs=list(legs),
        pp_only=True,
        json_out=True,
    )
