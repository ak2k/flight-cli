"""Glue between a search result and the award-provider (PointsPath) overlay.

Builds the per-leg `LegQuery` list the matcher fans out, derives the PP cabin
set from the requested cash cabins, computes the per-itinerary cash basis maps
the ¢/mi renderer needs (single- and multi-cabin), and sizes the widened
per-cabin query page for multi-cabin overlap. Imported back into `cli` (under
private-name aliases) so the paths reference them as module globals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._multi_cabin import parse_price
from ._parsing import ROUND_TRIP_LEGS
from .domain import Cabin
from .providers.base import LegQuery

if TYPE_CHECKING:
    from ._dispatch import ProviderSelection
    from ._multi_cabin import MultiCabinRow
    from .domain import Leg
    from .models import SearchResult

# Domain Cabin enum → PP API cabin string. Used for PP cabin derivation and the
# per-itinerary cash-basis maps the ¢/mi renderer reads.
CABIN_TO_PP_NAME: dict[Cabin, str] = {
    Cabin.COACH: "Economy",
    Cabin.PREMIUM_COACH: "Premium economy",
    Cabin.BUSINESS: "Business",
    Cabin.FIRST: "First",
}

# When --cabin selects multiple cabins, each cabin's per-query top-N is bumped
# so the client-side merge has overlap to work with. Cheapest economy and
# cheapest business on a given route are often different carriers entirely
# (e.g. JFK-LHR: VS in economy, FI in business) — a top-5 query per cabin
# almost never overlaps, leaving the J column rendered as all "—".
# Bumping per-cabin queries to ~25-50 itineraries lets the join surface
# matching itineraries that exist in both cabins' results.
#
# Capped to bound response size (each itinerary costs bytes + parse time);
# Matrix and gflight both tolerate page sizes in this range comfortably.
MULTI_CABIN_QUERY_BUMP_FACTOR = 5
MULTI_CABIN_QUERY_BUMP_CAP = 100


def build_pp_legs(legs: tuple[Leg, ...]) -> list[LegQuery]:
    """One PP query per Matrix leg. slice_index lets the matcher join PP
    award results to the correct Itinerary slice in each Matrix solution."""
    out: list[LegQuery] = []
    for i, leg in enumerate(legs):
        if not leg.date or not leg.origins or not leg.destinations:
            continue
        n = len(legs)
        kind = (
            "outbound"
            if i == 0 and n > 1
            else "return"
            if i == 1 and n == ROUND_TRIP_LEGS
            else f"leg {i + 1}"
            if n > ROUND_TRIP_LEGS
            else "one-way"
        )
        iso = leg.date.isoformat()
        out.append(
            LegQuery(
                origin=leg.origins[0],
                destination=leg.destinations[0],
                date=iso,
                slice_index=i,
                label=f"{kind} {leg.origins[0]}→{leg.destinations[0]} {iso}",
            ),
        )
    return out


def bumped_query_top_n(top_n: int, cabin_count: int) -> int:
    """Per-cabin query page size for a multi-cabin search.

    Single-cabin invocations get `top_n` unchanged. Multi-cabin gets
    `top_n * factor` capped at the bump ceiling. The visible row count
    after merge is still `top_n` (renderer trims by sort cabin) — the
    bump only widens the search space the join can draw from.
    """
    if cabin_count <= 1:
        return top_n
    return min(top_n * MULTI_CABIN_QUERY_BUMP_FACTOR, MULTI_CABIN_QUERY_BUMP_CAP)


def derive_pp_cabins(cash_cabins: tuple[Cabin, ...]) -> tuple[str, ...]:
    """Map cash cabin list → PP cabin list for the PP overlay.

    Adds First when Business is requested but First isn't: award seekers
    treat business/first as a paired premium tier, and First is rare enough
    that surfacing it costs almost nothing while filling a real research
    gap. The reverse promotion (First → +Business) isn't applied — asking
    for First means the user has already made that call.
    """
    out: list[str] = [CABIN_TO_PP_NAME[c] for c in cash_cabins]
    if Cabin.BUSINESS in cash_cabins and Cabin.FIRST not in cash_cabins:
        out.append(CABIN_TO_PP_NAME[Cabin.FIRST])
    return tuple(out)


def pp_cabins_for_multi(sel: ProviderSelection, cabins: tuple[Cabin, ...]) -> str | None:
    """PP cabins for a multi-cabin search. User's `--provider-opt pp.cabins=`
    wins; otherwise derive from `--cabin` with the business→+first rule."""
    user_set = sel.pp_cabins()
    if user_set is not None:
        return user_set
    return ",".join(derive_pp_cabins(cabins))


def cash_per_cabin_single(res: SearchResult, query_cabin: Cabin) -> dict[int, dict[str, float]]:
    """Build the per-itinerary cash map for a single-cabin invocation.

    The PP renderer needs to know which PP cabin name the cash field on each
    itinerary corresponds to — otherwise it can't compute ¢/mi against the
    right cash basis. For single-cabin runs the answer is the queried cabin
    applied uniformly.
    """
    name = CABIN_TO_PP_NAME[query_cabin]
    out: dict[int, dict[str, float]] = {}
    for it in res.solutions:
        cash = parse_price(it.price)
        if cash is not None:
            out[id(it)] = {name: cash}
    return out


def cash_per_cabin_multi(rows: list[MultiCabinRow]) -> dict[int, dict[str, float]]:
    """Build the per-itinerary cash map for a multi-cabin merged result.

    `rows` carries each itinerary alongside the prices observed in each cabin.
    Object identity is preserved through the merge (and through PP's matcher
    and de-dup), so `id(row.itinerary)` is a stable lookup key.
    """
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        prices: dict[str, float] = {}
        for cab, price in r.prices.items():
            cash = parse_price(price)
            if cash is not None:
                prices[CABIN_TO_PP_NAME[cab]] = cash
        if prices:
            out[id(r.itinerary)] = prices
    return out
