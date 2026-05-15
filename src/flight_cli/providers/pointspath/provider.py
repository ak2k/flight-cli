"""PointsPath adapter: PPClient + pricing-info → list[AwardFlight].

One PPClient is reused across legs (open one HTTP client, share the
semaphore-bounded fan-out). Pricing-info is fetched once per
`enabled_airlines`-aware run and reused across legs/cabins.

Failure model: any per-leg/per-airline error is swallowed and logged via the
existing structlog channels (PPClient already does this). The provider
returns `[]` on whole-provider failure so the registry can move on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ...pp.auth import PPAuthError, get_valid_tokens
from ...pp.client import (
    DEFAULT_AIRLINES,
    CashFlightHint,
    PPClient,
    SearchSpec,
    enabled_airlines,
)
from ..base import AwardFlight, CabinAward

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from ...pp.models import (
        AirlineSearchResponse,
        OutboundFlight,
        PerCabinMilesPricing,
        PricingInfoResponse,
    )
    from ..base import LegQuery

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]


def _cabin_awards(pricing: list[PerCabinMilesPricing]) -> list[CabinAward]:
    """Lifted from pp.match._cabin_awards to keep the conversion close to the
    provider. The matcher's copy stays for now to avoid breaking imports until
    the matcher refactor lands; the duplication is intentionally short-lived."""
    out: list[CabinAward] = []
    for p in pricing:
        pp = p.perPassengerPricing
        if not pp or pp.perPassengerMilesAmount <= 0:
            continue
        out.append(
            CabinAward(
                cabin=p.cabinClass,
                miles=pp.perPassengerMilesAmount,
                tax_usd=pp.perPassengerTaxAmountUsd,
                tax_currency=pp.taxCurrencyCode or "USD",
                is_basic_economy=pp.isBasicEconomyFare,
            ),
        )
    return out


def _flight_to_award(
    of: OutboundFlight,
    *,
    program: str,
    miles_to_cash_ratio: float,
    funding_banks: list[str],
) -> AwardFlight:
    return AwardFlight(
        origin=of.origin,
        destination=of.destination,
        departure=of.localDepartureDateTime,
        arrival=of.localArrivalDateTime,
        flight_number=of.firstFlightNumber,
        num_connections=of.numConnections,
        provider="PointsPath",
        program=program,
        miles_to_cash_ratio=miles_to_cash_ratio,
        funding_banks=funding_banks,
        cabins=_cabin_awards(of.perCabinMilesPricing),
        matched_google_flight_id=of.matchedGoogleFlightId or "",
    )


class PointsPathProvider:
    """AwardProvider implementation wrapping pp.client.PPClient.

    Construct with `await PointsPathProvider.create(...)`; the factory
    eagerly validates tokens so a stale-refresh case surfaces before the
    registry hands the provider out for use.

    Accepts overrides for the airline + cabin set so the CLI's
    --pp-airlines / --pp-cabin flags can pass-through unchanged. Without
    overrides, the provider derives the airline universe from the user's
    extension-config feature flags (the same logic the old run_pp_for_search
    used).
    """

    name: str = "PointsPath"

    def __init__(
        self,
        client: PPClient,
        pricing: PricingInfoResponse,
        airlines: tuple[str, ...],
    ) -> None:
        self._client = client
        self._pricing = pricing
        self._airlines = airlines
        # airline -> (miles_to_cash_ratio, funding_banks) for fast metadata lookup
        self._meta: dict[str, tuple[float, list[str]]] = {
            p.airline: (p.milesToCashRatio, [b.bank for b in p.bankPointsInfos])
            for p in pricing.pricingInfos
        }

    @classmethod
    async def create(
        cls,
        *,
        explicit_airlines: tuple[str, ...] | None = None,
    ) -> PointsPathProvider:
        """Build a configured provider. Raises PPAuthError if tokens are
        missing/expired — caller (registry) is expected to catch and skip."""
        get_valid_tokens()  # surface auth errors up-front
        client = await PPClient.create()
        pricing = await client.pricing_info()
        if explicit_airlines:
            airlines = explicit_airlines
        else:
            try:
                ext_cfg = await client.extension_config()
                airlines = enabled_airlines(pricing, ext_cfg)
                if not airlines:
                    airlines = DEFAULT_AIRLINES
            except Exception as e:  # noqa: BLE001 — falling back is non-fatal by design
                log.warning("pp_provider_ext_config_fallback", error=str(e))
                airlines = DEFAULT_AIRLINES
        return cls(client, pricing, airlines)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def pricing(self) -> PricingInfoResponse:
        """Exposed so the PP-only renderer can keep using its provider-
        specific table during the transition period. Removed when the
        renderer migrates to AwardFlight-only inputs."""
        return self._pricing

    @property
    def enabled(self) -> bool:
        # Once constructed via create(), enabled is implied — create() raises
        # PPAuthError otherwise. The Protocol still demands the property.
        return True

    async def search_leg(
        self,
        leg: LegQuery,
        *,
        cabins: tuple[str, ...],
        num_passengers: int = 1,
        cash_hints: tuple[CashFlightHint, ...] = (),
    ) -> list[AwardFlight]:
        """Fan out one airline-search call per (airline x cabin) for this
        leg, merge results, convert to AwardFlight.

        When `cash_hints` is non-empty (gflight backend has captured Google's
        opaque flight IDs), the request goes out with `enable_matching=True`
        and PP echoes the supplied `flightId`s back via `matchedGoogleFlightId`
        on each returned award. Without hints (Matrix backend, or no IDs
        available), `enable_matching=False` and the matcher falls back to its
        flight#+date / route+time keys.
        """
        merged: dict[str, AirlineSearchResponse] = {}
        for cabin in cabins:
            spec = SearchSpec(
                origin=leg.origin,
                destination=leg.destination,
                date=leg.date,
                return_date="",
                is_round_trip_return=False,
                num_passengers=num_passengers,
                cabin_class=cabin,
                enable_matching=bool(cash_hints),
                cash_hints=cash_hints,
            )
            per_airline = await self._client.airline_search_many(spec, self._airlines)
            for airline, resp in per_airline.items():
                if airline not in merged:
                    merged[airline] = resp
                    continue
                merged[airline].outboundFlights.extend(resp.outboundFlights)

        out: list[AwardFlight] = []
        for airline, resp in merged.items():
            ratio, banks = self._meta.get(airline, (0.0, []))
            for of in resp.outboundFlights:
                out.append(
                    _flight_to_award(
                        of,
                        program=airline,
                        miles_to_cash_ratio=ratio,
                        funding_banks=banks,
                    ),
                )
        return out

    def by_airline_for_leg(self, awards: list[AwardFlight]) -> dict[str, list[AwardFlight]]:
        """Convenience view: group an AwardFlight list by program (airline).
        Used by the PP-only renderer during the transition; removed once
        the renderer takes AwardFlight directly."""
        out: dict[str, list[AwardFlight]] = {}
        for a in awards:
            out.setdefault(a.program, []).append(a)
        return out


# Auto-detect entry point used by registry.py.
def is_configured() -> bool:
    """True iff PP tokens are present and (best-effort) usable.

    Doesn't network: returns False if tokens are missing or fail the
    in-memory validity check. Token refresh (which does network) happens
    inside PPClient itself when the provider is actually invoked.
    """
    try:
        get_valid_tokens()
    except PPAuthError:
        return False
    return True
