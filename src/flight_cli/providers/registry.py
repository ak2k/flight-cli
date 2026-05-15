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

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from .base import AwardFlight, AwardProvider, LegQuery

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]


async def _construct_enabled(
    *,
    pp_airlines: tuple[str, ...] | None = None,
) -> list[AwardProvider]:
    """Build the list of enabled provider instances.

    Each provider's auto-enable check (`is_configured`) runs first; only
    configured providers get instantiated (which is when network/auth
    actually happens). Failures during construction are logged and swallowed
    so one provider's outage can't take down the others.
    """
    out: list[AwardProvider] = []
    if pp_is_configured():
        try:
            out.append(await PointsPathProvider.create(explicit_airlines=pp_airlines))
        except Exception as e:  # noqa: BLE001 — per-provider failures are non-fatal
            log.warning("provider_init_failed", provider="PointsPath", error=str(e))
    return out


async def _gather_one_leg(
    providers: list[AwardProvider],
    leg: LegQuery,
    *,
    cabins: tuple[str, ...],
    num_passengers: int = 1,
) -> list[AwardFlight]:
    """Run all providers concurrently for one leg, concatenate results."""
    results: list[list[AwardFlight]] = [[] for _ in providers]

    async def runner(idx: int, p: AwardProvider) -> None:
        try:
            results[idx] = await p.search_leg(
                leg,
                cabins=cabins,
                num_passengers=num_passengers,
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
) -> tuple[list[list[AwardFlight]], list[AwardProvider]]:
    """End-to-end registry call: construct enabled providers, fan out per leg.

    Returns:
        (per_leg_awards, providers)
        per_leg_awards[i] is the flattened-across-providers list of awards
            for legs[i].
        providers is the constructed provider instances — the caller is
            responsible for closing them (PointsPath uses HTTP keepalive).
    """
    providers = await _construct_enabled(pp_airlines=pp_airlines)
    per_leg: list[list[AwardFlight]] = []
    for leg in legs:
        per_leg.append(
            await _gather_one_leg(
                providers,
                leg,
                cabins=cabins,
                num_passengers=num_passengers,
            ),
        )
    return per_leg, providers


def has_any_configured() -> bool:
    """Cheap check: is at least one provider's auto-enable predicate true?

    Used by the CLI's `--pp-only`/`--no-pp` gating in `_should_run_pp` so we
    can keep that decision provider-blind."""
    return pp_is_configured()
