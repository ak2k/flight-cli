"""Join Matrix cash itineraries to award flights.

Primary key: (normalized first-segment flight number, ISO departure date).
Same key can appear at most once per side per day, so a dict-lookup is enough.

Codeshare-fallback key: (origin, destination, departure datetime to minute,
operating-carrier partner group). Matrix returns codeshares under the
*marketing* flight number (e.g. AA6939 JFK→LHR), while PointsPath returns the
same physical aircraft under the *operating* flight number (e.g. BA174 —
surfaced inside the American PP query, because PP attributes codeshares to the
operator). Flight-number keys can't bridge that, but route+time can: both
sources read the same airline-published schedule.

Route+time alone is NOT an identity, though. On a dense domestic route two
carriers routinely schedule different metal off the same airport at the same
minute — MSY→MIA 2026-09-09T10:45 carries both AA3539 and DL1424. Joining on
route+time alone attached Delta's 9.1k SkyMiles price to the American
itinerary, a redemption that cannot exist (no DL/AA interline award
agreement). So the fallback additionally requires the two carriers to be
plausibly the same metal: identical IATA prefix, or co-members of an alliance
/ bilateral partnership (`_PARTNER_GROUPS`). Identical-carrier is the common
case; the partner check is what keeps the genuine AA-marketed/BA-operated
codeshare joining.

The join is provider-neutral: it takes a flat `list[AwardFlight]` (each
provider produces these from its own raw shape — see providers/base.py)
and outputs MatchedFare records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Itinerary, SearchResult
    from ..providers.base import AwardFlight

MatchKey = tuple[str, str]  # (FLIGHT_NUMBER_UPPER_NOSPACE, "YYYY-MM-DD")
# (ORIGIN_UPPER, DEST_UPPER, "YYYY-MM-DDTHH:MM") — carrier is checked
# separately (see `same_metal`), not folded into the key, because a
# codeshare's two carriers differ by design and a dict key can't express
# "equal OR partnered".
RouteTimeKey = tuple[str, str, str]

_ISO_MINUTE_LEN = 16  # "YYYY-MM-DDTHH:MM"
_IATA_PREFIX_LEN = 2

# Carriers that may appear as marketing/operating counterparts for the same
# physical flight. Alliance membership plus the bilateral JVs and equity
# partnerships that actually produce codeshares on routes we search. Keyed by
# IATA code; membership is symmetric (a shared group ⇒ plausibly same metal).
#
# This gates the route+time fallback ONLY. The (flight#, date) primary key is
# unaffected — an exact flight-number hit needs no carrier corroboration.
# Being absent here costs a codeshare match; being wrongly present costs a
# fabricated award price, which is the failure this table exists to prevent.
# Erring toward omission is deliberate.
_PARTNER_GROUPS: tuple[frozenset[str], ...] = (
    # oneworld
    frozenset({"AA", "AS", "BA", "AY", "IB", "JL", "MH", "QF", "QR", "RJ", "UL", "CX"}),
    # Star Alliance
    frozenset(
        {
            "UA",
            "AC",
            "LH",
            "OS",
            "LX",
            "SN",
            "SK",
            "TP",
            "TK",
            "NH",
            "OZ",
            "SQ",
            "TG",
            "NZ",
            "ET",
            "MS",
            "AV",
            "CM",
            "ZH",
            "CA",
            "A3",
            "BR",
        },
    ),
    # SkyTeam
    frozenset({"DL", "AF", "KL", "AZ", "AM", "KE", "MU", "CZ", "SU", "VN", "RO", "UX", "GA"}),
    # Non-alliance bilaterals that codeshare heavily
    frozenset({"DL", "VS"}),  # Delta / Virgin Atlantic JV
    frozenset({"DL", "WS"}),  # Delta / WestJet
    frozenset({"AA", "GF"}),  # American / Gulf Air
    frozenset({"AS", "B6"}),  # Alaska / JetBlue (Northeast Alliance remnant)
    frozenset({"UA", "EI"}),  # United / Aer Lingus
    frozenset({"EK", "QF"}),  # Emirates / Qantas
)


def _norm_fn(fn: str | None) -> str:
    return (fn or "").upper().replace(" ", "")


def _carrier(fn: str | None) -> str:
    """IATA carrier prefix of a flight number ('AA3539' → 'AA').

    Returns '' when the input is missing or too short to carry one, which
    `same_metal` treats as unknown-and-therefore-not-joinable.
    """
    n = _norm_fn(fn)
    return n[:_IATA_PREFIX_LEN] if len(n) > _IATA_PREFIX_LEN else ""


def same_metal(cash_fn: str | None, award_fn: str | None) -> bool:
    """Could these two flight numbers denote the same physical aircraft?

    True when the carriers are identical (the ordinary case — both sides
    reporting the same flight) or co-members of `_PARTNER_GROUPS` (the
    codeshare case the route+time fallback exists to bridge). False when
    either carrier is unparseable: an unknown carrier can't be corroborated,
    and a wrong join fabricates an unbookable award price.
    """
    c, a = _carrier(cash_fn), _carrier(award_fn)
    if not c or not a:
        return False
    if c == a:
        return True
    return any(c in g and a in g for g in _PARTNER_GROUPS)


def _iso_date(s: str | None) -> str:
    """Best-effort isolate the YYYY-MM-DD prefix from various formats."""
    if not s:
        return ""
    # PointsPath: "2026-06-09T22:00:00"
    # Matrix: "2026-06-09T22:00" / "2026-06-09 22:00"
    s = s.replace(" ", "T")
    return s[:10]


def _iso_minute(s: str | None) -> str:
    """Trim a datetime string to YYYY-MM-DDTHH:MM (minute precision). Tolerant
    of the space-separator Matrix sometimes returns. '' on missing/short."""
    if not s:
        return ""
    s = s.replace(" ", "T")
    return s[:_ISO_MINUTE_LEN] if len(s) >= _ISO_MINUTE_LEN else ""


def cash_match_key(it: Itinerary, slice_index: int = 0) -> MatchKey | None:
    """Build the match key from a Matrix itinerary's slice's first flight.

    Default slice_index=0 = outbound leg. For round-trips pass 1 to match the
    return leg; for multi-city pass 2, 3, etc.
    """
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return None
    s = itn.slices[slice_index]
    flights = s.flights or []
    if not flights:
        return None
    fn = _norm_fn(flights[0])
    dep = _iso_date(s.departure)
    if not fn or not dep:
        return None
    return (fn, dep)


def award_match_key(af: AwardFlight) -> MatchKey:
    return (_norm_fn(af.flight_number), _iso_date(af.departure))


def cash_route_time_key(it: Itinerary, slice_index: int = 0) -> RouteTimeKey | None:
    """Codeshare-fallback key: origin, destination, minute-precision departure.

    Doesn't require `slice.flights` to be populated — origin/dest/departure
    are enough to anchor a candidate. The carrier corroboration that makes
    the candidate a *match* is applied separately in `join` (`same_metal`),
    which does need `flights[0]`."""
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return None
    s = itn.slices[slice_index]
    o = ((s.origin.code if s.origin else None) or "").upper()
    d = ((s.destination.code if s.destination else None) or "").upper()
    t = _iso_minute(s.departure)
    if not (o and d and t):
        return None
    return (o, d, t)


def cash_first_flight_number(it: Itinerary, slice_index: int = 0) -> str:
    """First marketing flight number on the slice, '' when absent."""
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return ""
    flights = itn.slices[slice_index].flights or []
    return _norm_fn(flights[0]) if flights else ""


def award_route_time_key(af: AwardFlight) -> RouteTimeKey | None:
    o = (af.origin or "").upper()
    d = (af.destination or "").upper()
    t = _iso_minute(af.departure)
    if not (o and d and t):
        return None
    return (o, d, t)


@dataclass
class MatchedFare:
    """One cash itinerary with zero-or-more award flights attached."""

    itinerary: Itinerary
    awards: list[AwardFlight] = field(default_factory=list)


def cash_matched_id_key(it: Itinerary, slice_index: int = 0) -> str | None:
    """Opaque Google Flights ID for the cash slice, if the backend populated
    one. Matches against `AwardFlight.matched_google_flight_id` (echoed back
    by PP when `enableGoogleFlightMatching=true`)."""
    itn = it.itinerary
    if not itn or slice_index >= len(itn.slices):
        return None
    fid = itn.slices[slice_index].flight_id
    return fid or None


def join(  # noqa: PLR0912 — three index lookups in priority order, hard to split cleanly
    search: SearchResult,
    awards: list[AwardFlight],
    *,
    slice_index: int = 0,
) -> list[MatchedFare]:
    """Outer-join cash itineraries onto award flights.

    Match strategy, in priority order:
      1. **Matched-ID** (`flight_id` ↔ `matched_google_flight_id`). Exact
         string equality on PP's echoed `matchedGoogleFlightId`. Fires only
         when the cash side has `flight_id` (gflight backend) AND the
         provider was called with `enable_matching=True` + cash hints.
      2. **(flight#, date)** primary heuristic key.
      3. **(route, time) + carrier corroboration** codeshare fallback.
         Matrix's marketing flight# won't equal PP's operating flight#, so
         origin+dest+minute anchors the candidate — but only counts as a
         match when `same_metal` holds (identical carrier, or partners).
         Without that guard, two carriers departing the same airport pair at
         the same minute cross-contaminate: MSY→MIA 10:45 has both AA3539
         and DL1424, and Delta's price landed on the AA row.

    Hits across all three are unioned and deduped by AwardFlight identity, so
    a flight satisfying multiple keys isn't double-attached.

    Cash itineraries with no award match keep an empty `awards` list — caller
    decides whether to render them or filter to inner-join.

    `slice_index` selects which leg of each Itinerary to match against (0 for
    outbound, 1 for return on a round-trip, etc).
    """
    # Build all three award indexes in one pass. A single flight may appear
    # under multiple — per-itinerary dedup in the join loop keeps the output
    # clean.
    mid_idx: dict[str, list[AwardFlight]] = {}
    fn_idx: dict[MatchKey, list[AwardFlight]] = {}
    rt_idx: dict[RouteTimeKey, list[AwardFlight]] = {}
    for af in awards:
        if af.matched_google_flight_id:
            mid_idx.setdefault(af.matched_google_flight_id, []).append(af)
        fn_k = award_match_key(af)
        if fn_k[0]:
            fn_idx.setdefault(fn_k, []).append(af)
        rt_k = award_route_time_key(af)
        if rt_k:
            rt_idx.setdefault(rt_k, []).append(af)

    out: list[MatchedFare] = []
    for it in search.solutions:
        matched: list[AwardFlight] = []
        seen_ids: set[int] = set()

        mid_k = cash_matched_id_key(it, slice_index=slice_index)
        if mid_k and mid_k in mid_idx:
            for af in mid_idx[mid_k]:
                if id(af) in seen_ids:
                    continue
                seen_ids.add(id(af))
                matched.append(af)
        fn_k = cash_match_key(it, slice_index=slice_index)
        if fn_k and fn_k in fn_idx:
            for af in fn_idx[fn_k]:
                if id(af) in seen_ids:
                    continue
                seen_ids.add(id(af))
                matched.append(af)
        rt_k = cash_route_time_key(it, slice_index=slice_index)
        if rt_k and rt_k in rt_idx:
            cash_fn = cash_first_flight_number(it, slice_index=slice_index)
            for af in rt_idx[rt_k]:
                if id(af) in seen_ids:
                    continue
                # Same route + same minute is only a candidate; require the
                # carriers to be the same or partnered before believing it's
                # the same aircraft.
                if not same_metal(cash_fn, af.flight_number):
                    continue
                seen_ids.add(id(af))
                matched.append(af)

        out.append(MatchedFare(itinerary=it, awards=matched))
    return out
