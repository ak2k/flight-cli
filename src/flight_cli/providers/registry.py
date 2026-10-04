"""Provider registry + per-leg fan-out.

Today: hardcoded PointsPath entry. When seats.aero lands (work-2eoa) it
joins via the same `_discover` list. The auto-enable rule is: provider's
configuration check passes → instance constructed → leg fan-out includes it.

Each enabled provider is built and then asks every pair query at once, apart
from the others, and each query's awards are concatenated in provider order.
The matcher is provider-blind: a flat `list[AwardFlight]` is exactly what it
consumes.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import structlog

from .._envelope import narrow
from .base import answer_deadline, deadline_reason, exception_reason, record_failure
from .pointspath.provider import PointsPathProvider
from .pointspath.provider import is_configured as pp_is_configured
from .seats_aero.auth import is_configured as seats_is_configured
from .seats_aero.provider import SeatsAeroProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from structlog.stdlib import BoundLogger

    from ..pp.client import CashFlightHint
    from .base import AwardFlight, AwardProvider, LegQuery

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

# Builds one provider: None when it is not configured, or when building it
# failed, the failure recorded.
type _Build = Callable[[], Awaitable[AwardProvider | None]]


def _enabled_builders(
    *,
    pp_airlines: tuple[str, ...] | None = None,
    seats_sources: tuple[str, ...] | None = None,
    provider_filter: tuple[str, ...] | None = None,
) -> list[_Build]:
    """A builder for each provider the filter allows, in the order their
    awards are listed.

    Each provider's auto-enable check (`is_configured`) runs first; only
    configured providers get instantiated (which is when network/auth
    actually happens). Failures during construction are recorded and swallowed
    so one provider's outage can't take down the others.

    `provider_filter` (when non-None) restricts to a named subset. Matching
    is case-insensitive against the provider's short name (e.g. "pp",
    "seats"). A filter that names no configured providers builds none — the
    caller decides whether that's a hard error (--awards-only) or silent skip
    (cash-only path).
    """
    builders: list[_Build] = []
    if provider_filter is None or _matches(provider_filter, "pp"):
        builders.append(partial(_build_pointspath, pp_airlines))
    if provider_filter is None or _matches(provider_filter, "seats-aero"):
        builders.append(partial(_build_seats_aero, seats_sources))
    return builders


async def _build_pointspath(airlines: tuple[str, ...] | None) -> AwardProvider | None:
    # Checking the tokens can refresh them and building one asks PointsPath
    # for its catalog, so the deadline holds over both.
    with anyio.CancelScope(deadline=answer_deadline()) as scope:
        try:
            # A refresh is a blocking request: on the event loop no deadline
            # could cut it; in a thread the deadline stops waiting on it.
            if await anyio.to_thread.run_sync(pp_is_configured, abandon_on_cancel=True):
                return await PointsPathProvider.create(explicit_airlines=airlines)
        except Exception as e:  # noqa: BLE001 — per-provider failures are non-fatal
            narrow()
            log.debug("provider_init_failed", provider="PointsPath", error=str(e))
            record_failure("PointsPath", exception_reason(e))
    if scope.cancelled_caught:
        narrow()
        record_failure("PointsPath", deadline_reason())
    return None


async def _build_seats_aero(sources: tuple[str, ...] | None) -> AwardProvider | None:
    if not seats_is_configured():
        return None
    with anyio.CancelScope(deadline=answer_deadline()) as scope:
        try:
            return await SeatsAeroProvider.create(explicit_airlines=sources)
        except Exception as e:  # noqa: BLE001 — per-provider failures are non-fatal
            narrow()
            log.debug("provider_init_failed", provider="Seats.aero", error=str(e))
            record_failure("Seats.aero", exception_reason(e))
    if scope.cancelled_caught:
        narrow()
        record_failure("Seats.aero", deadline_reason())
    return None


def _matches(filter_: tuple[str, ...], canonical: str) -> bool:
    """Case-insensitive membership test using the canonical-name alias map.

    Filter entries are normalized through `canonical_provider`, so callers
    pass canonical names (`"pp"`, `"seats-aero"`) and match user-supplied
    aliases (`"pointspath"`, `"sa"`) without restating the alias table here.
    """
    from .._config import canonical_provider  # noqa: PLC0415 — avoid import cycle

    return any(canonical_provider(p) == canonical for p in filter_)


async def _ask(
    provider: AwardProvider,
    leg: LegQuery,
    *,
    cabins: tuple[str, ...],
    num_passengers: int,
    cash_hints: tuple[CashFlightHint, ...],
) -> list[AwardFlight]:
    """One provider's awards for one query; none when it raised."""
    try:
        # cash_hints is provider-optional — providers that don't take it
        # via Protocol can be called without the kwarg by Python's
        # liberal **kwargs forwarding. PointsPathProvider accepts it.
        return await provider.search_leg(  # type: ignore[call-arg]
            leg,
            cabins=cabins,
            num_passengers=num_passengers,
            cash_hints=cash_hints,
        )
    except Exception as e:  # noqa: BLE001 — surface provider failures, keep others
        narrow()
        log.debug("provider_search_failed", provider=provider.name, error=str(e))
        record_failure(provider.name, exception_reason(e))
        return []


async def gather_awards(
    legs: list[LegQuery],
    *,
    cabins: tuple[str, ...],
    num_passengers: int = 1,
    pp_airlines: tuple[str, ...] | None = None,
    seats_sources: tuple[str, ...] | None = None,
    cash_hints_per_leg: list[tuple[CashFlightHint, ...]] | None = None,
    provider_filter: tuple[str, ...] | None = None,
) -> tuple[list[list[AwardFlight]], list[AwardProvider]]:
    """End-to-end registry call: build each enabled provider, ask it every leg.

    `cash_hints_per_leg[i]` (when supplied) carries the gflight backend's
    captured Google Flights opaque IDs for legs[i]. The PointsPath provider
    uses them to fire `/api/airline-search` with `enable_matching=True`,
    making PP's `matchedGoogleFlightId` available as a primary join key
    downstream. When None / empty, falls back to the heuristic matcher keys.

    Returns:
        (per_leg_awards, providers)
        per_leg_awards[i] is the flattened-across-providers list of awards
            for legs[i].
        providers is the constructed provider instances — the caller is
            responsible for closing them (PointsPath uses HTTP keepalive).
    """
    builders = _enabled_builders(
        pp_airlines=pp_airlines,
        seats_sources=seats_sources,
        provider_filter=provider_filter,
    )
    hints: list[tuple[CashFlightHint, ...]] = [
        cash_hints_per_leg[i] if cash_hints_per_leg and i < len(cash_hints_per_leg) else ()
        for i in range(len(legs))
    ]
    built: list[AwardProvider | None] = [None] * len(builders)
    # answers[k][i]: the awards builder k's provider gave for legs[i].
    answers: list[list[list[AwardFlight]]] = [[[] for _ in legs] for _ in builders]

    async def build_and_ask(k: int, build: _Build) -> None:
        provider = built[k] = await build()
        if provider is None:
            return

        async def ask(i: int) -> None:
            answers[k][i] = await _ask(
                provider,
                legs[i],
                cabins=cabins,
                num_passengers=num_passengers,
                cash_hints=hints[i],
            )

        # Every query at once, started in order: their requests then queue for
        # PointsPath's slots in that order, and an airline that stalls holds one
        # slot rather than every query behind it.
        async with anyio.create_task_group() as tg:
            for i in range(len(legs)):
                tg.start_soon(ask, i)

    # Each provider asks as soon as it is built, not once all are: a provider
    # whose building stalls to the deadline would leave the others none of it.
    async with anyio.create_task_group() as tg:
        for k, build in enumerate(builders):
            tg.start_soon(build_and_ask, k, build)
    per_leg = [[af for own in answers for af in own[i]] for i in range(len(legs))]
    return per_leg, [p for p in built if p is not None]


def has_any_configured() -> bool:
    """Cheap check: is at least one provider's auto-enable predicate true?

    Used by the CLI's `_should_run_awards` gating so the decision stays
    provider-blind. Adding a third provider here is the same one-line
    or-in."""
    return pp_is_configured() or seats_is_configured()
