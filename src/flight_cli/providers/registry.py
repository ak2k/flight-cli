"""Provider registry + per-leg fan-out.

Today: hardcoded PointsPath entry. When seats.aero lands (work-2eoa) it
joins via the same `_discover` list. The auto-enable rule is: provider's
configuration check passes → instance constructed → leg fan-out includes it.

The fan-out gathers awards from all enabled providers in parallel for each
leg, then concatenates them. The matcher is provider-blind: a flat
`list[AwardFlight]` is exactly what it consumes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import structlog

from .pointspath.provider import PointsPathProvider
from .pointspath.provider import is_configured as pp_is_configured
from .seats_aero.auth import is_configured as seats_is_configured
from .seats_aero.provider import SeatsAeroProvider

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from ..pp.client import CashFlightHint
    from .base import AwardFlight, AwardProvider, LegQuery

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]


async def _construct_enabled(
    *,
    pp_airlines: tuple[str, ...] | None = None,
    seats_sources: tuple[str, ...] | None = None,
    provider_filter: tuple[str, ...] | None = None,
) -> list[AwardProvider]:
    """Build the list of enabled provider instances.

    Each provider's auto-enable check (`is_configured`) runs first; only
    configured providers get instantiated (which is when network/auth
    actually happens). Failures during construction are logged and swallowed
    so one provider's outage can't take down the others.

    `provider_filter` (when non-None) restricts to a named subset. Matching
    is case-insensitive against the provider's short name (e.g. "pp",
    "seats"). A filter that names no configured providers yields an empty
    list — the caller decides whether that's a hard error (--awards-only)
    or silent skip (cash-only path).
    """
    out: list[AwardProvider] = []
    allow_pp = provider_filter is None or _matches(provider_filter, "pp")
    if allow_pp and pp_is_configured():
        try:
            out.append(await PointsPathProvider.create(explicit_airlines=pp_airlines))
        except Exception as e:  # noqa: BLE001 — per-provider failures are non-fatal
            log.warning("provider_init_failed", provider="PointsPath", error=str(e))
    allow_seats = provider_filter is None or _matches(provider_filter, "seats")
    if allow_seats and seats_is_configured():
        try:
            out.append(await SeatsAeroProvider.create(explicit_airlines=seats_sources))
        except Exception as e:  # noqa: BLE001 — per-provider failures are non-fatal
            log.warning("provider_init_failed", provider="Seats.aero", error=str(e))
    return out


def _matches(filter_: tuple[str, ...], name: str) -> bool:
    """Case-insensitive membership test for the provider filter."""
    return any(p.strip().lower() == name.lower() for p in filter_)


async def _gather_one_leg(
    providers: list[AwardProvider],
    leg: LegQuery,
    *,
    cabins: tuple[str, ...],
    num_passengers: int = 1,
    cash_hints: tuple[CashFlightHint, ...] = (),
) -> list[AwardFlight]:
    """Run all providers concurrently for one leg, concatenate results."""
    results: list[list[AwardFlight]] = [[] for _ in providers]

    async def runner(idx: int, p: AwardProvider) -> None:
        try:
            # cash_hints is provider-optional — providers that don't take it
            # via Protocol can be called without the kwarg by Python's
            # liberal **kwargs forwarding. PointsPathProvider accepts it.
            results[idx] = await p.search_leg(  # type: ignore[call-arg]
                leg,
                cabins=cabins,
                num_passengers=num_passengers,
                cash_hints=cash_hints,
            )
        except Exception as e:  # noqa: BLE001 — surface provider failures, keep others
            log.warning("provider_search_failed", provider=p.name, error=str(e))

    async with anyio.create_task_group() as tg:
        for i, p in enumerate(providers):
            tg.start_soon(runner, i, p)

    out: list[AwardFlight] = []
    for r in results:
        out.extend(r)
    return out


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
    """End-to-end registry call: construct enabled providers, fan out per leg.

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
    providers = await _construct_enabled(
        pp_airlines=pp_airlines,
        seats_sources=seats_sources,
        provider_filter=provider_filter,
    )
    per_leg: list[list[AwardFlight]] = []
    for i, leg in enumerate(legs):
        hints: tuple[CashFlightHint, ...] = ()
        if cash_hints_per_leg and i < len(cash_hints_per_leg):
            hints = cash_hints_per_leg[i]
        per_leg.append(
            await _gather_one_leg(
                providers,
                leg,
                cabins=cabins,
                num_passengers=num_passengers,
                cash_hints=hints,
            ),
        )
    return per_leg, providers


def has_any_configured() -> bool:
    """Cheap check: is at least one provider's auto-enable predicate true?

    Used by the CLI's `_should_run_awards` gating so the decision stays
    provider-blind. Adding a third provider here is the same one-line
    or-in."""
    return pp_is_configured() or seats_is_configured()
