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

import structlog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..models import Itinerary, SearchResult, Slice
    from ..providers.base import AwardFlight

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

# (FLIGHT_NUMBER_UPPER_NOSPACE, "YYYY-MM-DD", ORIGIN_UPPER, DEST_UPPER)
MatchKey = tuple[str, str, str, str]
# (ORIGIN_UPPER, DEST_UPPER, "YYYY-MM-DDTHH:MM") — carrier is checked
# separately (see `same_metal`), not folded into the key, because a
# codeshare's two carriers differ by design and a dict key can't express
# "equal OR partnered".
RouteTimeKey = tuple[str, str, str]

_ISO_MINUTE_LEN = 16  # "YYYY-MM-DDTHH:MM"
_IATA_PREFIX_LEN = 2
_MIN_MULTI_SEGMENT = 2  # a slice needs 2+ flights before later segments can disagree

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

# Mainline → the regional carriers that operate its metal under its flight
# numbers. This is the DOMINANT real codeshare shape (LH9498 marketed /
# EN8858 operated is the repo's own documented example — see
# docs/memories/gf_routing_and_carriers.md), and an alliance table misses it
# entirely: a feeder is not an alliance member.
#
# Directional and asymmetric ON PURPOSE. Folding these into the symmetric
# `_PARTNER_GROUPS` would make MQ and OH (two unrelated AA regionals) match
# each other. Lookup is `award_carrier in _REGIONAL_OPERATORS[cash_carrier]`,
# so mainline-marketed / regional-operated joins and nothing else does.
# Last verified: 2026-07.
_REGIONAL_OPERATORS: dict[str, frozenset[str]] = {
    "AA": frozenset({"MQ", "OH", "YX", "PT", "ZW", "G7", "9K"}),
    "DL": frozenset({"9E", "OO", "YX", "G7", "EM"}),
    "UA": frozenset({"OO", "YX", "ZW", "C5", "AX", "G7", "EM"}),
    "AS": frozenset({"QX", "OO"}),
    "LH": frozenset({"EN", "CL", "WK", "EW", "VL"}),
    "AF": frozenset({"A5", "XK"}),
    "BA": frozenset({"CJ", "SN"}),
    "AC": frozenset({"QK", "RV", "ZX", "8P"}),
}


def _norm_fn(fn: str | None) -> str:
    return (fn or "").upper().replace(" ", "")


def _carrier(fn: str | None) -> str:
    """IATA carrier prefix of a flight number ('AA3539' → 'AA').

    Returns '' when the input is missing or too short to carry one, which
    `same_metal` treats as unknown-and-therefore-not-joinable.
    """
    n = _norm_fn(fn)
    return n[:_IATA_PREFIX_LEN] if len(n) > _IATA_PREFIX_LEN else ""


def same_carrier(cash_fn: str | None, award_fn: str | None) -> bool:
    """Exact IATA-prefix equality, with '' (unparseable) never matching."""
    c, a = _carrier(cash_fn), _carrier(award_fn)
    return bool(c) and c == a


def same_metal(cash_fn: str | None, award_fn: str | None) -> bool:
    """Could these two flight numbers denote the same physical aircraft?

    True when the carriers are identical (the ordinary case — both sides
    reporting the same flight), when one is the other's regional operator
    (`_REGIONAL_OPERATORS` — the dominant real codeshare shape), or when they
    are co-members of `_PARTNER_GROUPS`. False when either carrier is
    unparseable: an unknown carrier can't be corroborated, and a wrong join
    fabricates an unbookable award price.

    NOTE: this is a *candidate* predicate, deliberately loose. Partner
    agreement does not mean same aircraft — two oneworld carriers routinely
    fly the same route at the same minute on different metal. `_pick_metal`
    is what resolves a bucket of candidates down to the right one; calling
    `same_metal` alone as an accept/reject test reintroduces the collision
    (cash AA118 + award AS17 at the same minute are both "same metal" here).
    """
    c, a = _carrier(cash_fn), _carrier(award_fn)
    if not c or not a:
        return False
    if c == a:
        return True
    if a in _REGIONAL_OPERATORS.get(c, frozenset()):
        return True
    return any(c in g and a in g for g in _PARTNER_GROUPS)


def _slice_stop_count(s: Slice) -> int:
    """Number of connections on a cash slice. `stops` is authoritative when
    populated; otherwise derive from the segment count."""
    if s.stops:
        return len(s.stops)
    return max(len(s.flights) - 1, 0)


def _pick_metal(
    cash_fn: str,
    cash_arrival: str,
    candidates: list[AwardFlight],
) -> list[AwardFlight]:
    """Resolve one route+time bucket to the awards that are the cash flight's
    own metal.

    A bucket keyed on (origin, destination, departure minute) can hold several
    genuinely different aircraft — same-alliance carriers compete head-to-head
    on dense routes, so AA118 and AS17 can both leave JFK for LAX at 10:45.
    Accepting every partner in the bucket is what let a 7.5k Alaska price
    render on the American row.

    Resolution, in order:
      1. **Same carrier wins.** A same-carrier award at this route+minute IS
         the flight; partners beside it are different metal.
      2. **Single partner.** Otherwise the bucket is codeshare-shaped (the
         fallback's reason for existing: marketing AA6939 ↔ operating BA174).
         Accept only when the survivors resolve to ONE carrier — two distinct
         partner carriers means we cannot tell which is the metal.

    Arrival time narrows WITHIN each stage rather than ahead of them (see
    `_narrow`). Ordering matters and getting it wrong is not a near-miss:
    filtering the whole bucket by arrival first lets a wrong-carrier award
    whose arrival happens to match survive while the correct same-carrier
    award — one that merely omits its arrival — is deleted before the
    carrier rule ever sees it. That renders another aircraft's price on the
    row, which is the exact defect this function exists to prevent.
    """
    # Stage 1 requires the FULL flight number, not just the carrier prefix.
    # `same_carrier` only compares the IATA prefix, so treating that as "this
    # IS the flight" let AA999 claim exact-match priority over cash AA867 and
    # skip straight to `_narrow`, which admits a missing arrival when nothing
    # matched exactly. A different number on the same carrier is a different
    # flight; it belongs in the partner stage where ambiguity is judged.
    exact = [af for af in candidates if _norm_fn(af.flight_number) == _norm_fn(cash_fn)]
    if exact:
        return _narrow(exact, cash_arrival)
    partners = [af for af in candidates if same_metal(cash_fn, af.flight_number)]
    # The hand-curated tables are the weakest evidence in this module, and by
    # construction they only decide a match when nothing stronger applied.
    # That makes a stale entry fail SILENTLY — the wrong price just appears.
    # Log every table-arbitrated match so the failure mode is greppable, and
    # so we can tell from real traffic whether these tables still earn their
    # keep (measured 2026-07: 0 of 471 live awards lacked an arrival, i.e.
    # this path effectively never fires today).
    for af in partners:
        if not same_carrier(cash_fn, af.flight_number):
            log.debug(
                "award_match_via_partner_table",
                cash_flight=cash_fn,
                award_flight=af.flight_number,
                program=af.program,
            )
    # A DIFFERENT number on the SAME carrier is simply a different flight —
    # an airline does not sell one departure under two of its own numbers.
    # Only a genuine codeshare (different carrier) explains a number mismatch,
    # so a same-carrier candidate that reached this stage must prove itself
    # with a matching arrival; absence of evidence is not enough for it.
    partners = [
        af
        for af in partners
        if not same_carrier(cash_fn, af.flight_number)
        or (cash_arrival and _iso_minute(af.arrival) == cash_arrival)
    ]
    # Arrival is used in two distinct ways here, and conflating them is a bug.
    # Eliminating a candidate whose arrival CONTRADICTS the cash flight is
    # sound — that is positive evidence of different metal. Letting arrival
    # *choose* between carriers is not: a partner that merely omits its
    # arrival would lose to an unrelated carrier that happens to publish a
    # matching one. So contradicted candidates go first, then ambiguity is
    # judged among the survivors.
    partners = _drop_contradicted(partners, cash_arrival)
    if not partners:
        return []
    if len({_carrier(af.flight_number) for af in partners}) > 1:
        return []
    return _narrow(partners, cash_arrival)


def _by_journey_shape(
    candidates: list[AwardFlight],
    cash_flights: list[str],
    cash_stop_airports: list[str],
) -> list[AwardFlight]:
    """Drop candidates whose journey shape contradicts the cash slice's.

    Every key in this module identifies a journey by its FIRST segment, so two
    journeys that share a first flight and diverge afterwards collapse
    together. Both are real in live data: seats.aero returns "AA1444, BA216"
    and "AA1444, AA100" on one JFK→LHR date, and PointsPath returns four
    distinct AA1650 MSY→LHR journeys sharing a 12:22 departure and one stop,
    separable only by where they connect and when they land.

    Two independent signals, because no single one is available everywhere:

    * **Connection airports** — Matrix fills `Slice.stops` and BOTH award
      providers supply the equivalent, so this is the cross-provider check.
      Only compared when the cash side is a connection and the candidate says
      something; empty on the award side is "no evidence", not agreement.
    * **Segment flight numbers** — stronger (it pins each leg, not just the
      hub) but seats.aero-only; PointsPath sends the first number alone.

    Each is skipped when its evidence is absent, so a provider that supplies
    neither is judged by the carrier and arrival rules as before — no match is
    lost that used to succeed.
    """
    if len(cash_flights) < _MIN_MULTI_SEGMENT and not cash_stop_airports:
        return candidates  # nonstop: nothing past segment 0 to contradict
    want_stops = [c.upper() for c in cash_stop_airports]
    want_flights = [_norm_fn(f) for f in cash_flights]
    out: list[AwardFlight] = []
    for af in candidates:
        if want_stops and af.stop_airports and [c.upper() for c in af.stop_airports] != want_stops:
            continue
        if (
            len(want_flights) >= _MIN_MULTI_SEGMENT
            and af.segment_flight_numbers
            and [_norm_fn(f) for f in af.segment_flight_numbers] != want_flights
        ):
            continue
        out.append(af)
    return out


def _drop_contradicted(candidates: list[AwardFlight], cash_arrival: str) -> list[AwardFlight]:
    """Remove candidates whose arrival positively disagrees with the cash
    flight's. Missing arrivals survive — absence of evidence is not
    contradiction. Unlike `_narrow` this never *selects* a winner, so it is
    safe to run before carrier ambiguity is judged."""
    if not cash_arrival:
        return candidates
    return [af for af in candidates if _iso_minute(af.arrival) in ("", cash_arrival)]


def _narrow(candidates: list[AwardFlight], cash_arrival: str) -> list[AwardFlight]:
    """Keep only the candidates arriving at the cash flight's arrival minute.

    Two aircraft sharing a departure minute on the same route essentially never
    share an arrival minute too, so this separates same-carrier rotations the
    carrier rules cannot. Measured on a live MSY→MIA/FLL payload: route+departure
    alone left 3 multi-carrier buckets of 41; adding arrival left 0 of 91.

    Per-candidate, not all-or-nothing. A *disagreeing* arrival is evidence of
    different metal and is always dropped, even when that empties the bucket.
    Judging each candidate on its own data avoids two failure modes an
    all-or-nothing filter has — one member missing an arrival disabling the
    filter for the whole bucket, and a bucket where every arrival disagrees
    (the strongest possible evidence) being restored wholesale.

    Candidates with NO arrival are the awkward middle: absence of evidence, not
    evidence of absence. They are admitted only when nothing in the bucket
    matched exactly. Otherwise a cheaper no-arrival sibling would sit beside
    the award that positively confirmed the cash flight, and the renderer's
    lowest-miles pick would show the sibling's price — silently preferring the
    unverified candidate over the verified one.

    Note both sides express local time at the airport: Matrix sends an offset
    ('...T09:04-04:00'), PointsPath naive ('...T09:04:00'), seats.aero a
    misleading 'Z' on what is also local. `_iso_minute` truncates all three to
    the same wall-clock key. If a caller ever starts genuinely parsing these
    as instants, seats.aero's fake 'Z' becomes a real ~offset-sized bug.
    """
    if not cash_arrival:
        return candidates
    exact = [af for af in candidates if _iso_minute(af.arrival) == cash_arrival]
    if exact:
        return exact
    return [af for af in candidates if not _iso_minute(af.arrival)]


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


def _cash_slice(it: Itinerary, slice_index: int) -> Slice | None:
    """Bounds-checked slice access — the guard every cash_* key repeats."""
    itn = it.itinerary
    if not itn or not itn.slices or slice_index >= len(itn.slices):
        return None
    return itn.slices[slice_index]


def cash_match_key(it: Itinerary, slice_index: int = 0) -> MatchKey | None:
    """Build the match key from a Matrix itinerary's slice's first flight.

    Default slice_index=0 = outbound leg. For round-trips pass 1 to match the
    return leg; for multi-city pass 2, 3, etc.

    Route is part of the key: a flight number is only unique *per route* on a
    given date. Airlines reuse numbers across the day's rotations, so without
    origin/dest an `AA100 JFK→LHR 18:00` cash row matched an `AA100 MIA→DFW
    06:30` award — same number, same date, different flight.
    """
    s = _cash_slice(it, slice_index)
    if s is None:
        return None
    flights = s.flights or []
    if not flights:
        return None
    fn = _norm_fn(flights[0])
    dep = _iso_date(s.departure)
    o = ((s.origin.code if s.origin else None) or "").upper()
    d = ((s.destination.code if s.destination else None) or "").upper()
    if not fn or not dep or not o or not d:
        return None
    return (fn, dep, o, d)


def award_match_key(af: AwardFlight) -> MatchKey | None:
    o = (af.origin or "").upper()
    d = (af.destination or "").upper()
    fn = _norm_fn(af.flight_number)
    dep = _iso_date(af.departure)
    if not fn or not dep or not o or not d:
        return None
    return (fn, dep, o, d)


def cash_route_time_key(it: Itinerary, slice_index: int = 0) -> RouteTimeKey | None:
    """Codeshare-fallback key: origin, destination, minute-precision departure.

    Doesn't require `slice.flights` to be populated — origin/dest/departure
    are enough to anchor a candidate. The carrier corroboration that makes
    the candidate a *match* is applied separately in `join` (`same_metal`),
    which does need `flights[0]`."""
    s = _cash_slice(it, slice_index)
    if s is None:
        return None
    o = ((s.origin.code if s.origin else None) or "").upper()
    d = ((s.destination.code if s.destination else None) or "").upper()
    t = _iso_minute(s.departure)
    if not (o and d and t):
        return None
    return (o, d, t)


def cash_first_flight_number(it: Itinerary, slice_index: int = 0) -> str:
    """First marketing flight number on the slice, '' when absent."""
    s = _cash_slice(it, slice_index)
    if s is None:
        return ""
    flights = s.flights or []
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


def join(
    search: SearchResult,
    awards: list[AwardFlight],
    *,
    slice_index: int = 0,
) -> list[MatchedFare]:
    """Outer-join cash itineraries onto award flights.

    Match strategy. Keys 1 and 2 are exact identities; key 3 is a heuristic
    that must be resolved, not merely filtered:
      1. **Matched-ID** (`flight_id` ↔ `matched_google_flight_id`). Exact
         string equality on PP's echoed `matchedGoogleFlightId`. Fires only
         when the cash side has `flight_id` (gflight backend) AND the
         provider was called with `enable_matching=True` + cash hints.
      2. **(flight#, date, origin, dest)** primary key. Route is part of the
         identity — flight numbers are only unique per route per day.
      3. **(route, time) → `_pick_metal`** codeshare fallback. Matrix's
         marketing flight# won't equal PP's operating flight#, so
         origin+dest+minute anchors the *bucket* — but a bucket can hold
         several genuinely different aircraft (MSY→MIA 10:45 held both
         AA3539 and DL1424; JFK→LAX 10:45 holds both AA118 and AS17).
         `_pick_metal` resolves the bucket: same-carrier wins outright, and
         an ambiguous multi-partner bucket resolves to nothing.

    Hits across all three are unioned and deduped by AwardFlight identity, so
    a flight satisfying multiple keys isn't double-attached. Every attached
    award must also agree with the cash slice on connection count — a 1-stop
    award is cheaper than the nonstop it would be rendered beside, so it wins
    the renderer's lowest-miles pick and prints under a "nonstop" label.

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
        if fn_k:
            fn_idx.setdefault(fn_k, []).append(af)
        rt_k = award_route_time_key(af)
        if rt_k:
            rt_idx.setdefault(rt_k, []).append(af)

    out: list[MatchedFare] = []
    for it in search.solutions:
        s = _cash_slice(it, slice_index)
        cash_stops = _slice_stop_count(s) if s else 0

        cash_fn = cash_first_flight_number(it, slice_index=slice_index)
        cash_arr = _iso_minute(s.arrival) if s else ""
        cash_flights = list(s.flights or []) if s else []
        cash_stop_airports = [(e.code or "") for e in (s.stops or [])] if s else []

        # Gather the raw candidates from every applicable key FIRST, then run
        # one resolution pass over the union.
        #
        # Resolving each bucket separately and unioning the winners is wrong:
        # the resolver's job is to decide among competing claims, so a bucket
        # that only sees part of the field decides on partial information. A
        # concrete failure — cash AA6939 with a BA174 award (route+time) and an
        # AS99 award (matched-ID): route+time sees two different partner
        # carriers, correctly calls it ambiguous and yields nothing, but the
        # matched-ID bucket sees AS99 alone, calls it an unambiguous single
        # partner, and admits it. The union then renders 7.5k Alaska on an
        # American row — precisely the rejection one bucket had just made.
        #
        # The keys are *discovery* mechanisms; resolution is a single judgment
        # over everything they found.
        # `mid`: PP mints this ID from a hint we supplied and its own matcher
        # is documented as loose, so an echoed ID is a claim, not proof.
        # `fn`: (flight#, date, route) keys on segment 0 only, so connecting
        # journeys sharing a first flight collapse together.
        # `rt`: route+departure-minute; two carriers can share both.
        # None of the three is an identity on its own.
        hits: list[Sequence[AwardFlight]] = []
        mid_k = cash_matched_id_key(it, slice_index=slice_index)
        if mid_k:
            hits.append(mid_idx.get(mid_k, ()))
        fn_k = cash_match_key(it, slice_index=slice_index)
        if fn_k:
            hits.append(fn_idx.get(fn_k, ()))
        rt_k = cash_route_time_key(it, slice_index=slice_index)
        if rt_k:
            hits.append(rt_idx.get(rt_k, ()))

        raw: list[AwardFlight] = []
        seen_raw: set[int] = set()
        for hit in hits:
            for af in hit:
                if id(af) not in seen_raw:
                    seen_raw.add(id(af))
                    raw.append(af)

        # Connection count is an objective property of the journey, so drop
        # mismatches BEFORE resolution — otherwise an ineligible candidate can
        # make the field look ambiguous and suppress a valid codeshare that
        # would have won on its own.
        raw = [af for af in raw if af.num_connections == cash_stops]

        matched = _pick_metal(
            cash_fn,
            cash_arr,
            _by_journey_shape(raw, cash_flights, cash_stop_airports),
        )
        out.append(MatchedFare(itinerary=it, awards=matched))
    return out
