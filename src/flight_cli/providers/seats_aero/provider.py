"""Seats.aero AwardProvider — implements the AwardProvider Protocol.

Maps `AvailabilityTrip` (per-cabin, per-program, per-flight-set) → AwardFlight
(one per unique flight-set, with multiple cabins inside). Grouping is by
`(FlightNumbers, DepartsAt, Source)` so a single physical journey priced in
Y/W/J/F across the same program collapses to one AwardFlight with all four
cabins listed.

What this provider does NOT do that PointsPath does:
  - No transfer-partner bank metadata (seats.aero doesn't expose it).
    `funding_banks` is left empty; the render shows "—".
  - No miles-to-cash valuation. `miles_to_cash_ratio = 0.0` and the per-
    cabin ¢/mi column reads as missing.

These are intentional gaps — fill them in via work-rq94 / work-eb7k if
the per-program valuation lands as a separate workstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..base import AwardFlight, CabinAward
from .auth import SeatsAuthError, is_configured
from .client import SeatsAeroClient, SeatsAeroError

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from ...pp.client import CashFlightHint
    from ..base import LegQuery
    from .models import SeatsAvailabilityTrip

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]


# Mileage program slug → display name. Seats.aero uses lowercased no-space
# slugs ("aeroplan", "virginatlantic"); we render them in the same casing as
# PointsPath's airline labels so the columns look consistent in the joined
# table. Unknowns fall back to title-case of the slug.
_PROGRAM_LABELS: dict[str, str] = {
    "aeroplan": "Air Canada Aeroplan",
    "american": "American Airlines",
    "british": "British Airways",
    "delta": "Delta SkyMiles",
    "etihad": "Etihad Guest",
    "finnair": "Finnair Plus",
    "flyingblue": "Flying Blue",
    "gol": "Smiles",
    "ihg": "IHG Rewards",
    "iberia": "Iberia Plus",
    "jetblue": "JetBlue TrueBlue",
    "lifemiles": "Avianca LifeMiles",
    "marriott": "Marriott Bonvoy",
    "qantas": "Qantas Frequent Flyer",
    "qatar": "Qatar Privilege Club",
    "saudia": "Saudia AlFursan",
    "singapore": "Singapore KrisFlyer",
    "tap": "TAP Miles&Go",
    "turkish": "Turkish Miles&Smiles",
    "united": "United MileagePlus",
    "virginatlantic": "Virgin Atlantic Flying Club",
}


# Seats.aero cabin slug → CabinAward.cabin string. Aligns with PointsPath's
# "Economy"/"Business"/"First"/"Premium" labels so the renderer doesn't
# have to disambiguate by provider.
_CABIN_LABELS: dict[str, str] = {
    "economy": "Economy",
    "premium": "Premium",
    "business": "Business",
    "first": "First",
}


def _program_label(slug: str) -> str:
    return _PROGRAM_LABELS.get(slug, slug.title())


def _cabin_label(slug: str) -> str:
    return _CABIN_LABELS.get(slug.lower(), slug.title())


def _first_flight_number(flight_numbers: str) -> str:
    """`"AA4671, BA216"` → `"AA4671"`. The matcher keys on the first
    marketing flight number, same convention as PointsPath. Multi-segment
    journeys with later legs on different carriers get matched by the
    first leg only — this loses some matches but stays consistent with
    the rest of the join pipeline."""
    return flight_numbers.split(",", 1)[0].strip()


def _group_trips_to_awards(
    trips: list[SeatsAvailabilityTrip],
    *,
    tax_currency: str,
) -> list[AwardFlight]:
    """Collapse per-cabin trips into AwardFlights.

    Group key: (FlightNumbers, DepartsAt, Source). Same flight set on the
    same departure time priced via the same program merges into one award
    with multiple CabinAward entries. The trip's RemainingSeats and Stops
    are taken from the first trip in each group (they're per-flight-set,
    not per-cabin).

    Skips trips whose cabin we can't recognise — they're emitted as a
    distinct AwardFlight under the fallback _cabin_label rule rather than
    dropped silently.
    """
    grouped: dict[tuple[str, str, str], AwardFlight] = {}
    for t in trips:
        key = (t.FlightNumbers, t.DepartsAt, t.Source)
        af = grouped.get(key)
        cabin_award = CabinAward(
            cabin=_cabin_label(t.Cabin),
            miles=t.MileageCost,
            tax_usd=t.TotalTaxes / 100.0,  # seats.aero returns cents
            tax_currency=tax_currency,
        )
        if af is None:
            grouped[key] = AwardFlight(
                origin=t.OriginAirport,
                destination=t.DestinationAirport,
                departure=t.DepartsAt,
                arrival=t.ArrivesAt,
                flight_number=_first_flight_number(t.FlightNumbers),
                num_connections=t.Stops,
                provider="Seats.aero",
                program=_program_label(t.Source),
                miles_to_cash_ratio=0.0,
                funding_banks=[],
                cabins=[cabin_award],
            )
        else:
            af.cabins.append(cabin_award)
    return list(grouped.values())


class SeatsAeroProvider:
    """AwardProvider implementation wrapping SeatsAeroClient.

    Construct via `await SeatsAeroProvider.create(...)`. The factory does
    a load_key() up-front so missing-key errors surface before the registry
    hands the provider out.
    """

    name: str = "Seats.aero"

    def __init__(
        self,
        client: SeatsAeroClient,
        *,
        sources: tuple[str, ...] | None = None,
    ) -> None:
        self._client = client
        # Optional program filter (seats.aero's `sources=` query param).
        # When None, the API returns all programs the route is monitored on.
        self._sources = sources

    @classmethod
    async def create(
        cls,
        *,
        explicit_airlines: tuple[str, ...] | None = None,
    ) -> SeatsAeroProvider:
        """Build a configured provider.

        `explicit_airlines` reuses the PointsPath kwarg shape so the registry
        can call both providers with the same signature. For seats.aero,
        the value is forwarded as `sources=` (the mileage-program filter,
        not a marketing carrier filter — the API has separate params for
        each and the user-facing concept maps more naturally to programs).
        Caller wanting to filter by operating carrier should use the
        --provider-opt seats.carriers=... path that the CLI threads through.
        """
        if not is_configured():
            msg = "Seats.aero is not configured."
            raise SeatsAuthError(msg)
        client = SeatsAeroClient()
        # explicit_airlines from the legacy --pp-airlines wiring is the
        # closest concept seats.aero has to "filter by program"; the
        # provider-opt path (seats.airlines=...) lands here through the
        # registry's per-provider option dispatch.
        return cls(client, sources=explicit_airlines)

    async def aclose(self) -> None:
        # SeatsAeroClient owns the underlying httpx.AsyncClient; close
        # explicitly via its context manager exit semantics.
        await self._client.__aexit__(None, None, None)

    @property
    def enabled(self) -> bool:
        # Once construct() has succeeded, enabled is implied. The Protocol
        # demands a property; we keep this dynamic in case future tiers
        # (e.g. trial quota exhausted) need to flip it.
        return True

    async def search_leg(
        self,
        leg: LegQuery,
        *,
        cabins: tuple[str, ...],
        num_passengers: int = 1,
        cash_hints: tuple[CashFlightHint, ...] = (),
    ) -> list[AwardFlight]:
        """One call to seats.aero per leg, with `include_trips=true` so we
        get flight-level detail that the matcher can join against.

        `cabins` is the user's requested set ("Economy", "Business"). We
        lowercase + pass to seats.aero's `cabins=` filter, which the API
        treats as "results must have these cabins available." That lets us
        avoid pulling thousands of unrequested-cabin entries on a noisy
        route.

        `cash_hints` is ignored — seats.aero has no Google-flight-ID echo
        mechanism. The matcher falls back to flight#+date / route+time as
        usual.
        """
        _ = num_passengers, cash_hints
        cabin_slugs = tuple(c.lower() for c in cabins) if cabins else None
        try:
            page = await self._client.search(
                origin=leg.origin,
                destination=leg.destination,
                start_date=leg.date,
                end_date=leg.date,
                include_trips=True,
                cabins=cabin_slugs,
                sources=self._sources,
            )
        except SeatsAeroError as e:
            # Provider-level failures (auth, network, schema) are non-fatal:
            # log + return [] so the registry can move on to other providers.
            log.warning("seats_aero_search_failed", error=str(e), status=e.status)
            return []
        except Exception as e:  # noqa: BLE001 — propagate-to-registry pattern
            log.warning("seats_aero_search_failed", error=str(e))
            return []

        # Collect all trips across all (program, date) availability items
        # for this leg. The matcher is provider-blind: it gets a flat list
        # of AwardFlight and joins by (flight#, date) / (route, time).
        all_trips: list[SeatsAvailabilityTrip] = []
        tax_currency = "USD"
        for item in page.data:
            if item.TaxesCurrency:
                tax_currency = item.TaxesCurrency
            all_trips.extend(item.AvailabilityTrips)
        return _group_trips_to_awards(all_trips, tax_currency=tax_currency)
