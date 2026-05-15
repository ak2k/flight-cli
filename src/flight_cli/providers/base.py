"""Provider-neutral types for the award-augmentation pipeline.

`AwardFlight` is the normalized shape every provider produces: one
airline+flight+date+cabin-set bundle, plus per-provider metadata (program,
funding banks, valuation). `match.py:join` indexes a flat `list[AwardFlight]`
by both (flight#, date) and (origin, dest, departure-minute) keys to bridge
codeshares (the marketing flight# != operating flight# case).

`AwardProvider` is the Protocol every source implements. Today: PointsPath.
Planned: seats.aero (work-2eoa). The `enabled` flag gates whether a provider
runs without raising — a provider with missing tokens reports `enabled=False`
and the registry skips it silently.

`LegQuery` is the per-leg input. It carried over unchanged from `pp/cli.py`
and is provider-agnostic by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class LegQuery:
    """One leg's award query. `slice_index` points at the corresponding Slice
    in each cash Itinerary; `label` is the user-facing leg name (e.g.
    "outbound JFK→LHR")."""

    origin: str
    destination: str
    date: str  # YYYY-MM-DD
    slice_index: int
    label: str


@dataclass
class CabinAward:
    """One cabin's award price for a single flight. Provider-neutral."""

    cabin: str  # "Economy" / "Business" / "First" / provider-specific synonyms
    miles: int
    tax_usd: float
    tax_currency: str
    is_basic_economy: bool | None = None


@dataclass
class AwardFlight:
    """One award flight, normalized across providers.

    Identity fields (origin/destination/departure/flight_number) are what
    `match.py` joins on. The rest is per-provider metadata that the renderer
    consumes verbatim.
    """

    # identity — used for matching against cash itineraries.
    # `departure` is ISO local "YYYY-MM-DDTHH:MM:SS" (or :HH:MM — matcher
    # tolerates either). `flight_number` is the marketing flight number;
    # matcher normalizes case + whitespace before comparison.
    origin: str
    destination: str
    departure: str
    arrival: str
    flight_number: str
    num_connections: int = 0

    # provider/program metadata — used for rendering only
    provider: str = ""  # display name, e.g. "PointsPath", "seats.aero"
    program: str = ""  # mileage program, e.g. "United", "American Airlines"
    miles_to_cash_ratio: float = 0.0  # provider's valuation in ¢/mi
    funding_banks: list[str] = field(default_factory=list[str])

    cabins: list[CabinAward] = field(default_factory=list[CabinAward])


@runtime_checkable
class AwardProvider(Protocol):
    """The interface every award source implements.

    A provider knows: (a) whether it's configured/enabled (tokens/keys),
    (b) how to search a single leg and return a normalized AwardFlight list.

    The streaming `stream()` method in the issue body is deferred until we
    actually have ≥2 providers with materially different latencies; one-shot
    `search_leg` is enough for the cash → augmented render-replace pattern.
    """

    name: str

    @property
    def enabled(self) -> bool:
        """True iff tokens/keys are present and (best-effort) non-expired.

        A False provider is silently skipped by the registry — no hard error
        unless the user explicitly asked for it via `--<provider>-only`."""
        ...

    async def search_leg(
        self,
        leg: LegQuery,
        *,
        cabins: tuple[str, ...],
        num_passengers: int = 1,
    ) -> list[AwardFlight]:
        """Run all per-airline (or whatever the provider's atomic unit is)
        queries for one leg across the given cabins, return the merged
        normalized flights. Errors are the provider's to log; the function
        should return `[]` on failure rather than raising, so one provider's
        outage doesn't sink the augmented render."""
        ...
