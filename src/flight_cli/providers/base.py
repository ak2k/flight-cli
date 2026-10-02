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

`LegQuery` is the per-query input: one airport pair of one leg, since the
providers take one airport per end. It is provider-agnostic by construction.

`award_run` holds one search's award phase: every failure a provider swallows is
recorded into it with `record_failure`, so the search can name them all in one
line rather than one log line each.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .._console_text import CTRL

if TYPE_CHECKING:
    from collections.abc import Generator

    from ..pp.client import CashFlightHint


@dataclass
class LegQuery:
    """One award query: one airport pair of one leg. `slice_index` points at
    the corresponding Slice in each cash Itinerary; `label` is the
    user-facing leg name (e.g. "outbound JFK,EWR→LHR 2026-11-04"). A leg
    over an airport set is several queries sharing its slice_index and
    label."""

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
    # Seats the provider says are left at this price, when it says. None means
    # "not reported" — NOT "none available"; PointsPath doesn't expose it and
    # seats.aero's value is often 0 through staleness rather than sell-out.
    # The renderer uses it only to flag an award that cannot seat the party.
    remaining_seats: int | None = None


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
    # Every segment's marketing flight number, in order, when the provider
    # supplies them (seats.aero does; PointsPath returns only the first).
    # `flight_number` is segment 0, so two journeys that share a first segment
    # and diverge afterwards are indistinguishable without this — seats.aero
    # returns both "AA1444, BA216" and "AA1444, AA100" on one route+date, and
    # collapsing them lets the cheaper journey's price render on the other's
    # row. Empty when the provider can't say, which the matcher treats as
    # "no segment evidence" rather than agreement.
    segment_flight_numbers: list[str] = field(default_factory=list[str])
    # Connection airport codes in order (["DFW"]); empty for a nonstop OR when
    # the provider doesn't say. Distinguishing those two states is the caller's
    # job — see `_by_journey_shape`, which pairs this with `num_connections`.
    #
    # This is the ONLY journey-shape signal PointsPath gives beyond the first
    # flight number, and Matrix populates the directly comparable
    # `Slice.stops`, so it works cross-provider where segment numbers (which
    # only seats.aero sends) do not. Live MSY->LHR has four distinct AA1650
    # journeys sharing a departure minute and connection count.
    stop_airports: list[str] = field(default_factory=list[str])

    # provider/program metadata — used for rendering only
    provider: str = ""  # display name, e.g. "PointsPath", "seats.aero"
    program: str = ""  # mileage program, e.g. "United", "American Airlines"
    miles_to_cash_ratio: float = 0.0  # provider's valuation in ¢/mi
    funding_banks: list[str] = field(default_factory=list[str])

    cabins: list[CabinAward] = field(default_factory=list[CabinAward])

    # Opaque ID PP echoes back via `matchedGoogleFlightId` when the provider
    # was given a cash hint with the same `flight_id`. Empty when no match,
    # or when the cash side didn't carry an ID (Matrix backend). The matcher
    # uses this as its primary key when populated — exact-equality join vs.
    # the (flight#, date) / (route, time) heuristics it falls back to.
    matched_google_flight_id: str = ""


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
        cash_hints: tuple[CashFlightHint, ...] = (),
    ) -> list[AwardFlight]:
        """Run all per-airline (or whatever the provider's atomic unit is)
        queries for one leg across the given cabins, return the merged
        normalized flights.

        `cash_hints` carry per-cash-itinerary opaque IDs (today: Google Flights
        `data[0][17]`) that providers MAY use to ask their upstream to echo
        back a precise match identifier. Today only PointsPath uses them
        (via its `enableGoogleFlightMatching`); other providers may ignore.

        Errors are the provider's to log at debug and to `record_failure`;
        should return `[]` on failure rather than raising, so one provider's
        outage doesn't sink the augmented render."""
        ...


@dataclass(frozen=True)
class ProviderFailure:
    """One request a provider gave up on. `airline` names the part of the
    provider that failed, when it fans out per airline; `reason` is one line."""

    provider: str
    reason: str
    airline: str | None = None


@dataclass
class AwardRun:
    """One search's award phase, as its providers report into it."""

    failures: list[ProviderFailure] = field(default_factory=list[ProviderFailure])


# A context variable rather than a parameter, so `search_leg` and every provider
# keep their signatures. anyio's tasks copy the context they start in, and the
# copy still points at the one `AwardRun`.
_RUN: ContextVar[AwardRun | None] = ContextVar("award_run", default=None)


@contextmanager
def award_run() -> Generator[AwardRun]:
    """Collect the failures of the award lookups made inside this block."""
    run = AwardRun()
    token = _RUN.set(run)
    try:
        yield run
    finally:
        _RUN.reset(token)


def record_failure(provider: str, reason: str, *, airline: str | None = None) -> None:
    """Note a failure the caller is about to swallow. Outside `award_run`, a no-op."""
    run = _RUN.get()
    if run is not None:
        run.failures.append(ProviderFailure(provider, reason, airline))


# Enough of an error body to say what went wrong; past it is usually a page of HTML.
_MAX_BODY = 200


def _one_line(text: str) -> str:
    """Whitespace runs as one space and control characters gone, so a reason is
    one line, and one made of nothing but ESCs reads as blank."""
    return " ".join(text.translate(CTRL).split())


def exception_reason(e: BaseException) -> str:
    """`str(e)`, or the class name when that is blank: `httpx.ReadTimeout("")`
    carries nothing else."""
    return _one_line(str(e)) or type(e).__name__


def http_reason(status: int, body: str = "") -> str:
    """`HTTP 503: <body>`, or `HTTP 503` when the body says nothing."""
    shown = _one_line(body)[:_MAX_BODY]
    return f"HTTP {status:d}: {shown}" if shown else f"HTTP {status:d}"
