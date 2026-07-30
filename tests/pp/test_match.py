# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) confuses basedpyright into thinking
# alias names are required kwargs. The tests rely on populate_by_name=True
# (set on _Loose); silence the rule rather than reformat every constructor.
"""Tests for the cash-itinerary ↔ award-flight matcher."""

from __future__ import annotations

from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.pp.match import (
    award_match_key,
    award_route_time_key,
    cash_match_key,
    cash_route_time_key,
    join,
    same_metal,
)
from flight_cli.providers.base import AwardFlight, CabinAward


def _itin(*slices_data: tuple[str, ...]) -> Itinerary:
    """Build an Itinerary with the given slices. Each tuple is
    (flight_number, departure_iso, origin_iata, destination_iata) with an
    optional 5th element for arrival_iso."""
    slcs = [
        Slice(
            flights=[t[0]],
            departure=t[1],
            origin=SliceEndpoint(code=t[2]),
            destination=SliceEndpoint(code=t[3]),
            arrival=(t[4] if len(t) > 4 else None),
        )
        for t in slices_data
    ]
    return Itinerary(
        displayTotal="USD500.00",
        itinerary=ItineraryDetails(slices=slcs, carriers=[]),
    )


def _itin_with_id(fn: str, dep: str, o: str, d: str, flight_id: str) -> Itinerary:
    """Like _itin() but populates `Slice.flight_id` — what the gflight
    backend sets from Google's data[0][17]."""
    s = Slice(
        flights=[fn],
        departure=dep,
        origin=SliceEndpoint(code=o),
        destination=SliceEndpoint(code=d),
        flight_id=flight_id,
    )
    return Itinerary(
        displayTotal="USD500.00",
        itinerary=ItineraryDetails(slices=[s], carriers=[]),
    )


def _award(
    fn: str,
    dep: str,
    *,
    program: str = "United",
    miles: int = 47000,
    tax: float = 250.0,
    cabin: str = "Economy",
    origin: str = "JFK",
    dest: str = "LHR",
    funding_banks: list[str] | None = None,
    miles_to_cash_ratio: float = 0.0125,
    matched_id: str = "",
    num_connections: int = 0,
    arrival: str | None = None,
    segment_flight_numbers: list[str] | None = None,
) -> AwardFlight:
    return AwardFlight(
        origin=origin,
        destination=dest,
        departure=dep,
        arrival=arrival if arrival is not None else dep,
        flight_number=fn,
        num_connections=num_connections,
        provider="PointsPath",
        program=program,
        miles_to_cash_ratio=miles_to_cash_ratio,
        funding_banks=funding_banks or ["Chase", "Bilt"],
        cabins=[CabinAward(cabin=cabin, miles=miles, tax_usd=tax, tax_currency="USD")],
        matched_google_flight_id=matched_id,
        segment_flight_numbers=segment_flight_numbers or [],
    )


# ─────────────────────────── key normalization ─────────────────────────────


def test_cash_match_key_uppercases_and_strips_whitespace():
    it = _itin(("ua 146", "2026-06-09T22:00:00", "JFK", "LHR"))
    assert cash_match_key(it) == ("UA146", "2026-06-09", "JFK", "LHR")


def test_cash_match_key_empty_when_no_flights():
    it = Itinerary(itinerary=ItineraryDetails(slices=[Slice(flights=[])]))
    assert cash_match_key(it) is None


def test_cash_match_key_handles_space_separated_iso():
    """Some Matrix payloads return 'YYYY-MM-DD HH:MM' instead of ISO 'T'."""
    it = _itin(("UA146", "2026-06-09 22:00", "JFK", "LHR"))
    assert cash_match_key(it) == ("UA146", "2026-06-09", "JFK", "LHR")


def test_cash_match_key_uses_slice_index_for_return_leg():
    it = _itin(
        ("UA146", "2026-06-09T22:00:00", "JFK", "LHR"),  # outbound
        ("UA147", "2026-06-12T10:00:00", "LHR", "JFK"),  # return
    )
    assert cash_match_key(it, slice_index=0) == ("UA146", "2026-06-09", "JFK", "LHR")
    assert cash_match_key(it, slice_index=1) == ("UA147", "2026-06-12", "LHR", "JFK")


def test_cash_match_key_out_of_range_slice_returns_none():
    it = _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR"))
    assert cash_match_key(it, slice_index=5) is None


def test_award_match_key_normalizes_consistently():
    af = _award("ua 146", "2026-06-09T22:00:00")
    assert award_match_key(af) == ("UA146", "2026-06-09", "JFK", "LHR")


# ───────────────────────── route+time key ──────────────────────────────────


def test_cash_route_time_key_includes_origin_dest_minute():
    it = _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR"))
    assert cash_route_time_key(it) == ("JFK", "LHR", "2026-08-15T18:40")


def test_cash_route_time_key_uppercases_airports():
    """IATA codes are case-insensitive on the wire; canonicalize so a malformed
    'jfk' on one side still matches a proper 'JFK' on the other."""
    it = _itin(("AA6939", "2026-08-15T18:40:00", "jfk", "lhr"))
    assert cash_route_time_key(it) == ("JFK", "LHR", "2026-08-15T18:40")


def test_cash_route_time_key_handles_space_separated_iso():
    it = _itin(("AA6939", "2026-08-15 18:40", "JFK", "LHR"))
    assert cash_route_time_key(it) == ("JFK", "LHR", "2026-08-15T18:40")


def test_cash_route_time_key_out_of_range_slice_returns_none():
    it = _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR"))
    assert cash_route_time_key(it, slice_index=5) is None


def test_cash_route_time_key_works_when_flights_list_is_empty():
    """Route+time doesn't need flight numbers — it should still produce a
    key for a slice that has origin/dest/departure but no flights[]."""
    it = Itinerary(
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=[],
                    departure="2026-08-15T18:40:00",
                    origin=SliceEndpoint(code="JFK"),
                    destination=SliceEndpoint(code="LHR"),
                ),
            ],
        ),
    )
    assert cash_route_time_key(it) == ("JFK", "LHR", "2026-08-15T18:40")


def test_award_route_time_key_normalizes_consistently():
    af = _award("BA174", "2026-08-15T18:40:00", origin="JFK", dest="LHR")
    assert award_route_time_key(af) == ("JFK", "LHR", "2026-08-15T18:40")


# ───────────────── codeshare fallback (the work-22az fix) ──────────────────


def test_join_codeshare_via_route_time_when_flight_numbers_differ():
    """Matrix returns the marketing flight number; PP returns the operating
    flight number for the same physical aircraft. The (flight#, date) key
    can't bridge them, but the route+time fallback does.

    Real example from a live probe: AA6939 (AA-marketed) ↔ BA174 (BA-operated).
    """
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    # PP's American airline-search returns BA-operated codeshares under the
    # OPERATING flight number — this is what we observed in the probe.
    awards = [
        _award("BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "BA174"


def test_join_does_not_double_attach_when_both_keys_match():
    """The non-codeshare case: Matrix and PP both report UA146 at the same
    time, so both the flight# key AND the route+time key fire. Dedup must
    keep it to one award per AwardFlight."""
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("UA146", "2026-06-09T22:00:00", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_join_route_time_fallback_does_not_match_different_route():
    """Sanity: a JFK→LHR cash flight at 18:40 must not match a JFK→ORD flight
    at 18:40, even though times agree."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="ORD"),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_route_time_fallback_requires_minute_precision():
    """A 5-minute offset must not fall back. (We can broaden to a tolerance
    window later if real traffic shows minor schedule-source drift.)"""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA174", "2026-08-15T18:45:00", program="American", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_arrival_time_resolves_codeshare_and_partner_in_one_bucket():
    """Arrival time is the real identity, so the codeshare bucket resolves
    rather than failing closed.

    Cash AA6939 arrives 06:30. The BA174 award is the same physical aircraft
    (same arrival) and attaches; an AS99 award sharing only the departure
    minute is a different aircraft (different arrival) and does not. Keying
    on route+departure alone could not tell these apart — on a live MSY
    payload that key left 3 multi-carrier buckets of 41, and adding arrival
    left 0 of 91.
    """
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", "2026-08-16T06:30:00")),
        ]
    )
    awards = [
        _award(
            "BA174",
            "2026-08-15T18:40:00",
            program="American",
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:30:00",
        ),
        _award(
            "AS99",
            "2026-08-15T18:40:00",
            program="Alaska",
            miles=7500,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T07:55:00",
        ),
    ]
    matches = join(res, awards)
    assert [a.flight_number for a in matches[0].awards] == ["BA174"]


def test_join_falls_back_to_carrier_logic_when_arrival_missing():
    """Arrival is optional on both sides (`Slice.arrival` is nullable and a
    provider may omit it), so the carrier resolution stays the backstop. With
    no arrival anywhere, cash AA6939 + a BA174 and an AA6939 award is
    unresolvable by time, and same-carrier-wins keeps only AA6939."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR"),
        _award("AA6939", "2026-08-15T18:40:00", program="VirginAtlantic", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert {a.program for a in matches[0].awards} == {"VirginAtlantic"}


def test_join_arrival_filter_does_not_wipe_bucket_when_awards_omit_arrival():
    """A cash arrival paired with awards that carry none must not zero the
    bucket — the filter only applies when it actually matched something."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", "2026-08-16T06:30:00")),
        ]
    )
    awards = [
        _award(
            "BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR", arrival=""
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "BA174"


# ────────────────────────────── join semantics ─────────────────────────────


def test_join_outer_keeps_unmatched_cash():
    """A cash itinerary whose flight # has no award match should still
    surface in the output — with empty awards."""
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),  # will match
            _itin(("BA178", "2026-06-09T07:50:00", "JFK", "LHR")),  # no award
        ]
    )
    awards = [_award("UA146", "2026-06-09T22:00:00")]
    matches = join(res, awards)
    assert len(matches) == 2
    assert matches[0].awards and matches[0].awards[0].program == "United"
    assert matches[1].awards == []  # BA178 unmatched but preserved


def test_join_preserves_funding_banks_and_ratio_from_award():
    """Funding banks and miles_to_cash_ratio travel with the AwardFlight
    (set by the provider during conversion). The matcher attaches the
    award unchanged."""
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award(
            "UA146",
            "2026-06-09T22:00:00",
            funding_banks=["Chase", "Bilt"],
            miles_to_cash_ratio=0.0125,
        ),
    ]
    matches = join(res, awards)
    [matched] = matches[0].awards
    assert sorted(matched.funding_banks) == ["Bilt", "Chase"]
    assert matched.miles_to_cash_ratio == 0.0125


def test_join_codeshare_surfaces_multiple_airlines():
    """When two airlines report the same flight#xdate as their own award,
    both options should attach to the cash itinerary."""
    res = SearchResult(
        solutions=[
            _itin(("BA178", "2026-06-09T07:50:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA178", "2026-06-09T07:50:00", program="American", miles=30000, tax=308.0),
        _award("BA178", "2026-06-09T07:50:00", program="British Airways", miles=50000, tax=750.0),
    ]
    matches = join(res, awards)
    programs = {ao.program for ao in matches[0].awards}
    assert programs == {"American", "British Airways"}


def test_join_empty_award_list():
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),
        ]
    )
    matches = join(res, [])
    assert matches[0].awards == []


# ──────────────── matched-id join (work-?? matched-id upgrade) ──────────────


def test_matched_id_key_overrides_when_present():
    """When the cash slice carries a flight_id AND an award has the same
    matched_google_flight_id, that's the primary key — fires regardless of
    whether flight#+date or route+time would also match."""
    res = SearchResult(
        solutions=[
            _itin_with_id("UA146", "2026-06-09T22:00:00", "JFK", "LHR", flight_id="MWRvrf"),
        ]
    )
    awards = [
        _award(
            "UA146",
            "2026-06-09T22:00:00",
            origin="JFK",
            dest="LHR",
            matched_id="MWRvrf",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].matched_google_flight_id == "MWRvrf"


def test_matched_id_key_joins_when_flight_number_disagrees():
    """The matched-id key bridges flight-number mismatches the heuristic
    keys would miss — e.g. cash side has a marketed flight#, award side
    reports the operating flight# but the underlying flight_id agrees."""
    res = SearchResult(
        solutions=[
            _itin_with_id("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", flight_id="x9z2"),
        ]
    )
    # Different flight#, different time → no flight#+date or route+time match.
    # Only the matched-id key can bridge.
    awards = [
        _award(
            "BA174",
            "2026-08-15T19:15:00",
            program="American",
            origin="JFK",
            dest="LHR",
            matched_id="x9z2",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "BA174"


def test_matched_id_dedup_with_heuristic_keys():
    """When the same AwardFlight matches via matched-id AND a heuristic key,
    it must attach exactly once."""
    res = SearchResult(
        solutions=[
            _itin_with_id("UA146", "2026-06-09T22:00:00", "JFK", "LHR", flight_id="MWRvrf"),
        ]
    )
    awards = [
        _award(
            "UA146",
            "2026-06-09T22:00:00",
            origin="JFK",
            dest="LHR",
            matched_id="MWRvrf",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_award_without_matched_id_still_joins_via_heuristics():
    """An award flight without matched_google_flight_id populated must
    still join via flight#+date when applicable — backwards compat with
    enable_matching=False providers / responses."""
    res = SearchResult(
        solutions=[
            _itin_with_id("UA146", "2026-06-09T22:00:00", "JFK", "LHR", flight_id="MWRvrf"),
        ]
    )
    # Award doesn't carry matched_id (empty default), so falls through to
    # the (flight#, date) primary heuristic key.
    awards = [_award("UA146", "2026-06-09T22:00:00", origin="JFK", dest="LHR")]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_cash_without_flight_id_skips_matched_id_path():
    """Matrix backend: slices have no flight_id; the matched-id key is
    never evaluated and the heuristics carry the join."""
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),  # no flight_id
        ]
    )
    awards = [
        _award(
            "UA146",
            "2026-06-09T22:00:00",
            origin="JFK",
            dest="LHR",
            matched_id="MWRvrf",  # would match if cash had flight_id="MWRvrf"
        ),
    ]
    matches = join(res, awards)
    # Joins via flight#+date heuristic, not matched-id.
    assert len(matches[0].awards) == 1


# ────────────── carrier corroboration on the route+time fallback ──────────────
#
# Regression: a live MSY→MIA search attached a 9.1k Delta SkyMiles price to
# American's AA3539. Delta cannot ticket AA metal — no interline award
# agreement — and the two are genuinely different aircraft that happen to
# push back at the same minute. Route+time alone can't tell them apart.


def test_join_route_time_rejects_different_carrier_at_same_minute():
    """The bug, reduced. AA3539 and DL1424 both depart MSY→MIA at 10:45 on
    2026-09-09. Only the American award may attach."""
    res = SearchResult(
        solutions=[
            _itin(("AA3539", "2026-09-09T10:45:00", "MSY", "MIA")),
        ]
    )
    awards = [
        _award(
            "DL1424",
            "2026-09-09T10:45:00",
            program="Delta",
            miles=9100,
            origin="MSY",
            dest="MIA",
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_route_time_keeps_same_carrier_at_same_minute():
    """The other half of the collision: the AA-programmed award on the same
    route+minute still attaches."""
    res = SearchResult(
        solutions=[
            _itin(("AA3539", "2026-09-09T10:45:00", "MSY", "MIA")),
        ]
    )
    awards = [
        _award(
            "AA3539",
            "2026-09-09T10:45:00",
            program="American",
            origin="MSY",
            dest="MIA",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].program == "American"


def test_join_route_time_partitions_a_real_collision():
    """Both flights present at once, as the live payload had them: each cash
    itinerary keeps only its own carrier's award."""
    res = SearchResult(
        solutions=[
            _itin(("AA3539", "2026-09-09T10:45:00", "MSY", "MIA")),
            _itin(("DL1424", "2026-09-09T10:45:00", "MSY", "MIA")),
        ]
    )
    awards = [
        _award("DL1424", "2026-09-09T10:45:00", program="Delta", origin="MSY", dest="MIA"),
        _award("AA3539", "2026-09-09T10:45:00", program="American", origin="MSY", dest="MIA"),
    ]
    matches = join(res, awards)
    assert [a.program for a in matches[0].awards] == ["American"]
    assert [a.program for a in matches[1].awards] == ["Delta"]


def test_join_route_time_still_bridges_oneworld_codeshare():
    """The fallback's reason for existing must survive the guard: AA-marketed
    / BA-operated is a partner pair, so it still joins."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "BA174"


def test_join_route_time_rejects_cross_alliance_pair():
    """United (Star) must not pick up an American (oneworld) award, even on
    an identical route+minute."""
    res = SearchResult(
        solutions=[
            _itin(("UA1122", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("AA100", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_route_time_allows_non_alliance_bilateral():
    """Delta/Virgin Atlantic codeshare across alliance lines via the JV."""
    res = SearchResult(
        solutions=[
            _itin(("DL4321", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("VS26", "2026-08-15T18:40:00", program="Delta", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_join_route_time_skipped_when_cash_slice_has_no_flight_number():
    """No cash flight number ⇒ no carrier to corroborate against ⇒ the
    fallback can't fire. Fails closed: a wrong join invents an unbookable
    price, a missed join just shows no award."""
    res = SearchResult(
        solutions=[
            Itinerary(
                displayTotal="USD500.00",
                itinerary=ItineraryDetails(
                    slices=[
                        Slice(
                            flights=[],
                            departure="2026-09-09T10:45:00",
                            origin=SliceEndpoint(code="MSY"),
                            destination=SliceEndpoint(code="MIA"),
                        ),
                    ],
                    carriers=[],
                ),
            ),
        ]
    )
    awards = [
        _award("AA3539", "2026-09-09T10:45:00", program="American", origin="MSY", dest="MIA"),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_flight_number_key_needs_no_carrier_corroboration():
    """The guard is scoped to the route+time fallback. An exact (flight#,
    date) hit is self-corroborating and must be untouched — including when
    the award's *program* differs from the operating carrier, which is the
    normal partner-redemption case (Alaska miles on an AA flight)."""
    res = SearchResult(
        solutions=[
            _itin(("AA867", "2026-09-09T06:00:00", "MSY", "MIA")),
        ]
    )
    awards = [
        _award(
            "AA867",
            "2026-09-09T06:00:00",
            program="Alaska",
            miles=4500,
            origin="MSY",
            dest="MIA",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].program == "Alaska"


def test_same_metal_helper_rejects_unparseable_carrier():
    assert same_metal("AA3539", "AA3539") is True
    assert same_metal("AA3539", "DL1424") is False
    assert same_metal("", "AA3539") is False
    assert same_metal("AA", "AA3539") is False  # too short to carry a number


# ───────── review round 2: bucket resolution, regionals, stop count ─────────
#
# The first fix gated the route+time fallback on `same_metal` and claimed the
# collision class was closed. It wasn't: same-alliance carriers compete on the
# same route at the same minute more often than cross-alliance ones do, so the
# guard relocated the bug instead of removing it — and made it less visible
# (an Alaska price on an AA row reads plausible; a Delta price did not).


def test_join_rejects_intra_alliance_collision_via_exact_carrier_preference():
    """The relocated bug. Cash AA118 JFK->LAX 10:45 with two awards in the
    bucket: the real AA118 award and an AS17 award (different aircraft, same
    minute, both oneworld). Only the American award may attach — otherwise
    the renderer's lowest-miles pick shows 7.5k Alaska on the AA row."""
    res = SearchResult(
        solutions=[
            _itin(("AA118", "2026-08-15T10:45:00", "JFK", "LAX")),
        ]
    )
    awards = [
        _award(
            "AA118",
            "2026-08-15T10:45:00",
            program="American",
            miles=25000,
            origin="JFK",
            dest="LAX",
        ),
        _award(
            "AS17", "2026-08-15T10:45:00", program="Alaska", miles=7500, origin="JFK", dest="LAX"
        ),
    ]
    matches = join(res, awards)
    assert [a.program for a in matches[0].awards] == ["American"]


def test_join_rejects_ambiguous_multi_partner_bucket():
    """No exact-carrier award, and two DIFFERENT partner carriers share the
    bucket. We cannot tell which is the cash flight's metal, so neither
    attaches — guessing would be a coin flip on a price the user might book."""
    res = SearchResult(
        solutions=[
            _itin(("AA118", "2026-08-15T10:45:00", "JFK", "LAX")),
        ]
    )
    awards = [
        _award("AS17", "2026-08-15T10:45:00", program="Alaska", origin="JFK", dest="LAX"),
        _award("BA99", "2026-08-15T10:45:00", program="British Airways", origin="JFK", dest="LAX"),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_still_bridges_unambiguous_codeshare():
    """The fallback's reason for existing survives: a single partner carrier
    alone in the bucket (no competing exact-carrier award) still joins."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award("BA174", "2026-08-15T18:40:00", program="American", origin="JFK", dest="LHR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "BA174"


def test_join_bridges_mainline_marketed_regional_operated():
    """The dominant real codeshare shape, and the repo's own documented
    example (docs/memories/gf_routing_and_carriers.md): LH9498 marketed,
    EN8858 (Air Dolomiti) operated. An alliance-only table rejected this —
    a feeder is not an alliance member."""
    res = SearchResult(
        solutions=[
            _itin(("LH9498", "2026-08-15T09:15:00", "FRA", "FLR")),
        ]
    )
    awards = [
        _award("EN8858", "2026-08-15T09:15:00", program="United", origin="FRA", dest="FLR"),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1
    assert matches[0].awards[0].flight_number == "EN8858"


def test_regional_operator_mapping_is_directional_not_symmetric():
    """MQ and OH are both American regionals but have no relationship with
    each other. A symmetric table would wrongly pair them."""
    assert same_metal("AA3539", "MQ3539") is True  # mainline -> its regional
    assert same_metal("MQ3539", "OH1234") is False  # two regionals of the same mainline
    assert same_metal("MQ3539", "DL1424") is False  # regional -> unrelated mainline


def test_join_rejects_connecting_award_on_nonstop_cash_row():
    """A 1-stop award is cheaper than the nonstop it would render beside, so
    it wins the lowest-miles pick — and the row still prints 'nonstop',
    because stops come from the cash slice. Connection count must agree."""
    res = SearchResult(
        solutions=[
            _itin(("AA100", "2026-08-15T18:30:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award(
            "AA100",
            "2026-08-15T18:30:00",
            program="American",
            miles=12000,
            origin="JFK",
            dest="LHR",
            num_connections=1,
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_join_accepts_award_whose_stop_count_agrees():
    """The other side of the stop-count check: a genuine nonstop award on a
    nonstop cash row still attaches."""
    res = SearchResult(
        solutions=[
            _itin(("AA100", "2026-08-15T18:30:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award(
            "AA100",
            "2026-08-15T18:30:00",
            program="American",
            origin="JFK",
            dest="LHR",
            num_connections=0,
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_join_flight_number_key_requires_matching_route():
    """Flight numbers repeat across a carrier's daily rotations, so
    (flight#, date) alone is not an identity: an AA100 JFK->LHR cash row
    used to attach an AA100 MIA->DFW award."""
    res = SearchResult(
        solutions=[
            _itin(("AA100", "2026-08-15T18:00:00", "JFK", "LHR")),
        ]
    )
    awards = [
        _award(
            "AA100", "2026-08-15T06:30:00", program="American", miles=7500, origin="MIA", dest="DFW"
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_partner_groups_have_no_unintended_transitivity():
    """Membership is tested per-group, so overlapping groups (DL is in both
    SkyTeam and the DL/VS bilateral) must not chain: VS must not reach AF."""
    assert same_metal("DL1", "VS2") is True  # direct bilateral
    assert same_metal("VS1", "AF2") is False  # would require chaining through DL
    assert same_metal("B61", "AA2") is False  # via AS: B6-AS bilateral, AS-AA oneworld
    assert same_metal("EK1", "AA2") is False  # via QF: EK-QF bilateral, QF-AA oneworld


def test_partner_group_codes_are_well_formed():
    """Structural pin: a malformed entry would silently never match, since
    `_carrier` only ever produces two-character prefixes."""
    # DIVERGE: reportPrivateUsage — these are module-internal reference
    # tables, not API. A structural test is exactly the case where reaching
    # in is correct; exporting them publicly to satisfy the rule would widen
    # the surface for a test's benefit.
    from flight_cli.pp.match import (
        _PARTNER_GROUPS,  # pyright: ignore[reportPrivateUsage]
        _REGIONAL_OPERATORS,  # pyright: ignore[reportPrivateUsage]
    )

    for group in _PARTNER_GROUPS:
        assert len(group) >= 2, f"degenerate group: {group}"
        for code in group:
            assert len(code) == 2, f"not a 2-char IATA code: {code!r}"
    for mainline, regionals in _REGIONAL_OPERATORS.items():
        assert len(mainline) == 2, f"not a 2-char IATA code: {mainline!r}"
        for code in regionals:
            assert len(code) == 2, f"not a 2-char IATA code: {code!r}"
            assert code != mainline, f"{mainline} lists itself as its own regional"


def test_same_metal_rejects_award_side_empty_flight_number():
    """Fails closed on an unparseable award-side carrier, mirroring the
    cash-side case. A missed match shows no award; a wrong one invents a
    price the user might try to book."""
    assert same_metal("AA3539", "") is False
    assert same_metal("AA3539", None) is False


# ─────────── multi-stop slices + wire-format timestamps (review r2) ───────────


def _itin_multi(
    flights: list[str],
    dep: str,
    o: str,
    d: str,
    stops: list[str],
    arrival: str | None = None,
) -> Itinerary:
    """A connecting itinerary: N flights, N-1 intermediate stops."""
    s = Slice(
        flights=flights,
        departure=dep,
        arrival=arrival,
        origin=SliceEndpoint(code=o),
        destination=SliceEndpoint(code=d),
        stops=[SliceEndpoint(code=c) for c in stops],
    )
    return Itinerary(
        displayTotal="USD500.00",
        itinerary=ItineraryDetails(slices=[s], carriers=[]),
    )


def test_join_matches_one_stop_award_to_one_stop_cash():
    """The stop-count check is an equality, not a nonstop-only filter: a
    connecting cash slice must still match its own connecting award."""
    res = SearchResult(
        solutions=[
            _itin_multi(["DL2542", "DL719"], "2026-09-09T13:00:00", "MSY", "MIA", ["ATL"]),
        ]
    )
    awards = [
        _award(
            "DL2542",
            "2026-09-09T13:00:00",
            program="Delta",
            origin="MSY",
            dest="MIA",
            num_connections=1,
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_join_rejects_nonstop_award_on_connecting_cash_row():
    """The other direction: a nonstop award is a different (better) product
    than the 1-stop cash itinerary it would render against."""
    res = SearchResult(
        solutions=[
            _itin_multi(["DL2542", "DL719"], "2026-09-09T13:00:00", "MSY", "MIA", ["ATL"]),
        ]
    )
    awards = [
        _award(
            "DL2542",
            "2026-09-09T13:00:00",
            program="Delta",
            origin="MSY",
            dest="MIA",
            num_connections=0,
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_slice_stop_count_falls_back_to_segment_count():
    """`stops` is authoritative when Matrix populates it; the gflight adapter
    may not, so a 2-segment slice with no `stops` still counts as 1."""
    from flight_cli.pp.match import _slice_stop_count  # pyright: ignore[reportPrivateUsage]

    populated = Slice(flights=["DL1", "DL2"], stops=[SliceEndpoint(code="ATL")])
    assert _slice_stop_count(populated) == 1
    derived = Slice(flights=["DL1", "DL2"])  # no stops[]
    assert _slice_stop_count(derived) == 1
    nonstop = Slice(flights=["DL1"])
    assert _slice_stop_count(nonstop) == 0


def test_arrival_match_survives_matrix_utc_offset_wire_format():
    """Matrix emits offset-aware local times ('2026-09-09T09:04-04:00') whose
    offset can even differ from the departure's ('...T06:00-05:00' for the same
    flight), while PointsPath emits naive local ('2026-09-09T09:04:00'). Both
    are local-at-the-airport and `_iso_minute` truncates before the offset, so
    they compare equal — this pins that, since a format drift here would
    silently turn the arrival discriminator into a no-op."""
    res = SearchResult(
        solutions=[
            _itin(("AA867", "2026-09-09T06:00-05:00", "MSY", "MIA", "2026-09-09T09:04-04:00")),
        ]
    )
    awards = [
        _award(
            "AA867",
            "2026-09-09T06:00:00",
            program="American",
            origin="MSY",
            dest="MIA",
            arrival="2026-09-09T09:04:00",
        ),
        # Same route+departure, different aircraft (later arrival) — must not win.
        _award(
            "AA999",
            "2026-09-09T06:00:00",
            program="Alaska",
            miles=1000,
            origin="MSY",
            dest="MIA",
            arrival="2026-09-09T10:30:00",
        ),
    ]
    matches = join(res, awards)
    assert [a.flight_number for a in matches[0].awards] == ["AA867"]


def test_cash_first_flight_number_direct():
    """Direct coverage of the guard branches, matching its siblings."""
    from flight_cli.pp.match import cash_first_flight_number

    it = _itin(("ua 146", "2026-06-09T22:00:00", "JFK", "LHR"))
    assert cash_first_flight_number(it) == "UA146"
    assert cash_first_flight_number(it, slice_index=5) == ""
    no_flights = Itinerary(itinerary=ItineraryDetails(slices=[Slice(flights=[])]))
    assert cash_first_flight_number(no_flights) == ""


def test_match_keys_require_complete_route():
    """A slice or award missing origin/destination yields no key rather than a
    partial one that could collide with an unrelated flight."""
    partial = Itinerary(
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=["AA100"],
                    departure="2026-08-15T18:00:00",
                    origin=SliceEndpoint(code="JFK"),
                    destination=None,
                ),
            ],
        ),
    )
    assert cash_match_key(partial) is None
    assert award_match_key(_award("AA100", "2026-08-15T18:00:00", origin="", dest="LHR")) is None
    assert award_match_key(_award("AA100", "", origin="JFK", dest="LHR")) is None


# ───────── arrival must narrow WITHIN carrier stages, not ahead of them ─────────
#
# The arrival discriminator was first applied to the whole bucket before the
# carrier rules ran. That let a wrong-carrier award whose arrival happened to
# match survive while the CORRECT same-carrier award — one that merely omitted
# its arrival — was deleted before the exact-carrier rule ever saw it, putting
# another aircraft's price on the row. Ordering here is load-bearing.


def test_arrival_filter_does_not_delete_correct_metal_in_mixed_bucket():
    """The P0. Cash AA6939 arrives 06:30. BA174 is the right metal but omits
    its arrival; AS99 is different metal that happens to arrive 06:30.
    Filtering by arrival first left only AS99 and rendered 7.5k Alaska on the
    American row."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", "2026-08-16T06:30:00")),
        ]
    )
    awards = [
        _award(
            "BA174",
            "2026-08-15T18:40:00",
            program="American",
            miles=60000,
            origin="JFK",
            dest="LHR",
            arrival="",
        ),
        _award(
            "AS99",
            "2026-08-15T18:40:00",
            program="Alaska",
            miles=7500,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:30:00",
        ),
    ]
    matches = join(res, awards)
    assert [a.program for a in matches[0].awards] != ["Alaska"]
    # Two distinct partner carriers with incomplete arrival data is genuinely
    # unresolvable, so the safe answer is no award at all.
    assert matches[0].awards == []


def test_arrival_filter_abstains_on_partial_coverage_keeping_exact_carrier():
    """Same shape, but the same-carrier award is the one missing an arrival.
    It must still win — partial arrival coverage is not evidence."""
    res = SearchResult(
        solutions=[
            _itin(("AA118", "2026-08-15T10:45:00", "JFK", "LAX", "2026-08-15T14:05:00")),
        ]
    )
    awards = [
        _award(
            "AA118",
            "2026-08-15T10:45:00",
            program="American",
            miles=25000,
            origin="JFK",
            dest="LAX",
            arrival="",
        ),
        _award(
            "AS17",
            "2026-08-15T10:45:00",
            program="Alaska",
            miles=7500,
            origin="JFK",
            dest="LAX",
            arrival="2026-08-15T14:05:00",
        ),
    ]
    matches = join(res, awards)
    assert [a.program for a in matches[0].awards] == ["American"]


def test_arrival_separates_same_carrier_rotations():
    """What arrival is actually for: two AA flights the carrier rules cannot
    tell apart, sharing a departure minute. Only the matching arrival wins."""
    res = SearchResult(
        solutions=[
            _itin(("AA867", "2026-09-09T06:00-05:00", "MSY", "MIA", "2026-09-09T09:04-04:00")),
        ]
    )
    awards = [
        _award(
            "AA867",
            "2026-09-09T06:00:00",
            program="American",
            miles=25000,
            origin="MSY",
            dest="MIA",
            arrival="2026-09-09T09:04:00",
        ),
        _award(
            "AA999",
            "2026-09-09T06:00:00",
            program="American",
            miles=9000,
            origin="MSY",
            dest="MIA",
            arrival="2026-09-09T10:30:00",
        ),
    ]
    matches = join(res, awards)
    assert [a.flight_number for a in matches[0].awards] == ["AA867"]


def test_arrival_disagreement_drops_the_award_even_if_it_is_the_only_one():
    """Disagreeing arrival is evidence of different metal, and we act on it
    even when that leaves the row with no award.

    Real schedule sources do drift: a cross-check of the seats.aero fixture
    against the live Matrix cache found AA106 differing by 5 minutes (19:20 vs
    19:15). So this WILL cost some legitimate matches. That trade is deliberate
    — a dropped award shows an empty cell the user can investigate, while a
    re-admitted one prints a confident price for another aircraft. Absence of
    evidence (no arrival at all) is still admitted; only contradiction is not.

    If drift turns out to be common in practice, the fix is a tolerance window
    here, not restoring a wholesale fallback.
    """
    res = SearchResult(
        solutions=[
            _itin(("AA106", "2026-08-15T19:20:00", "JFK", "LHR", "2026-08-16T07:30:00")),
        ]
    )
    awards = [
        _award(
            "AA106",
            "2026-08-15T19:20:00",
            program="American",
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T07:25:00",
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_arrival_missing_on_award_is_still_admitted():
    """Absence of evidence is not evidence: an award that omits its arrival
    stays eligible and is resolved by the carrier rules."""
    res = SearchResult(
        solutions=[
            _itin(("AA106", "2026-08-15T19:20:00", "JFK", "LHR", "2026-08-16T07:30:00")),
        ]
    )
    awards = [
        _award(
            "AA106", "2026-08-15T19:20:00", program="American", origin="JFK", dest="LHR", arrival=""
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_primary_key_resolves_sibling_journeys_by_arrival():
    """The second P0. (flight#, date, origin, dest) keys on the FIRST segment,
    so every connecting journey starting on that flight collapses onto one
    key. The repo's seats.aero fixture holds three distinct AA1444 JFK->LHR
    journeys (arriving 06:55, 09:05, 12:50) — unresolved, the renderer's
    lowest-miles pick quotes the 12:50 journey's fare on the 06:55 row."""
    res = SearchResult(
        solutions=[
            _itin_multi(
                ["AA1444", "AA200"],
                "2026-08-15T18:00:00",
                "JFK",
                "LHR",
                ["BOS"],
                arrival="2026-08-16T06:55:00",
            ),
        ]
    )
    awards = [
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=115500,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:55:00",
            num_connections=1,
        ),
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=104000,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T12:50:00",
            num_connections=1,
        ),
    ]
    matches = join(res, awards)
    assert [a.cabins[0].miles for a in matches[0].awards] == [115500]


def test_matched_id_path_requires_carrier_corroboration():
    """The third P0. PP mints matchedGoogleFlightId from a hint we supply and
    its matcher is documented as loose, so the echoed ID is a claim, not proof.
    A Delta award must not ride it onto an American row."""
    res = SearchResult(
        solutions=[
            _itin_with_id("AA3539", "2026-09-09T10:45:00", "MSY", "MIA", flight_id="XyZ123"),
        ]
    )
    awards = [
        _award(
            "DL1424",
            "2026-09-09T10:45:00",
            program="Delta",
            miles=9100,
            origin="MSY",
            dest="MIA",
            matched_id="XyZ123",
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []


def test_matched_id_path_still_bridges_a_real_codeshare():
    """...but the path keeps working for the codeshare it exists to serve."""
    res = SearchResult(
        solutions=[
            _itin_with_id("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", flight_id="XyZ123"),
        ]
    )
    awards = [
        _award(
            "BA174",
            "2026-08-15T18:40:00",
            program="American",
            origin="JFK",
            dest="LHR",
            matched_id="XyZ123",
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


# ─────────── codex adversarial pass: buckets on the other two keys ───────────


def test_matched_id_bucket_is_resolved_not_just_filtered_on_return_leg():
    """Codex P0. Several awards can share one matchedGoogleFlightId. Filtering
    that bucket with `same_metal` alone still admits a partner beside the true
    same-carrier award, and the renderer's lowest-miles pick then shows the
    partner's 7.5k Alaska price on an American row. Return leg, because
    slice_index=1 had almost no coverage."""
    it = Itinerary(
        displayTotal="USD500.00",
        itinerary=ItineraryDetails(
            slices=[
                Slice(
                    flights=["AA100"],
                    departure="2026-08-15T18:00:00",
                    origin=SliceEndpoint(code="JFK"),
                    destination=SliceEndpoint(code="LAX"),
                ),
                Slice(
                    flights=["AA117"],
                    departure="2026-08-20T10:00:00",
                    origin=SliceEndpoint(code="LAX"),
                    destination=SliceEndpoint(code="JFK"),
                    flight_id="RET1",
                ),
            ],
            carriers=[],
        ),
    )
    awards = [
        _award(
            "AA117",
            "2026-08-20T10:00:00",
            program="American",
            miles=25000,
            origin="LAX",
            dest="JFK",
            matched_id="RET1",
        ),
        _award(
            "AS19",
            "2026-08-20T10:00:00",
            program="Alaska",
            miles=7500,
            origin="LAX",
            dest="JFK",
            matched_id="RET1",
        ),
    ]
    matches = join(SearchResult(solutions=[it]), awards, slice_index=1)
    assert [a.program for a in matches[0].awards] == ["American"]


def test_segment_disagreement_separates_journeys_sharing_first_flight():
    """Codex P0. seats.aero returns both "AA1444, BA216" and "AA1444, AA100"
    on one JFK->LHR date. Every key here identifies a journey by segment 0, so
    without the full segment list they collapse and the cheaper journey's fare
    prints on the other's row."""
    res = SearchResult(
        solutions=[
            _itin_multi(
                ["AA1444", "BA216"],
                "2026-08-15T18:00:00",
                "JFK",
                "LHR",
                ["BOS"],
                arrival="2026-08-16T06:55:00",
            ),
        ]
    )
    awards = [
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=60000,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:55:00",
            num_connections=1,
            segment_flight_numbers=["AA1444", "BA216"],
        ),
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=12000,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:55:00",
            num_connections=1,
            segment_flight_numbers=["AA1444", "AA100"],
        ),
    ]
    matches = join(res, awards)
    assert [a.cabins[0].miles for a in matches[0].awards] == [60000]


def test_segment_check_admits_providers_that_omit_segments():
    """PointsPath sends only the first flight number. An empty segment list is
    absence of evidence, so it must not be read as disagreement."""
    res = SearchResult(
        solutions=[
            _itin_multi(
                ["AA1444", "BA216"],
                "2026-08-15T18:00:00",
                "JFK",
                "LHR",
                ["BOS"],
                arrival="2026-08-16T06:55:00",
            ),
        ]
    )
    awards = [
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American",
            miles=60000,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:55:00",
            num_connections=1,
        ),
    ]
    matches = join(res, awards)
    assert len(matches[0].awards) == 1


def test_exact_arrival_match_beats_cheaper_sibling_with_no_arrival():
    """Codex P0. A no-arrival sibling used to sit beside the award that
    positively confirmed the cash flight, and its lower price won the cell —
    silently preferring the unverified candidate over the verified one."""
    res = SearchResult(
        solutions=[
            _itin_multi(
                ["AA1444", "BA216"],
                "2026-08-15T18:00:00",
                "JFK",
                "LHR",
                ["BOS"],
                arrival="2026-08-16T06:55:00",
            ),
        ]
    )
    awards = [
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=60000,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:55:00",
            num_connections=1,
        ),
        _award(
            "AA1444",
            "2026-08-15T18:00:00",
            program="American Airlines",
            miles=10000,
            origin="JFK",
            dest="LHR",
            arrival="",
            num_connections=1,
        ),
    ]
    matches = join(res, awards)
    assert [a.cabins[0].miles for a in matches[0].awards] == [60000]


def test_arrival_never_picks_between_carriers():
    """The inverse trap. Arrival separates two rotations of the SAME carrier;
    it must not choose between carriers, or a partner that merely omits its
    arrival loses to an unrelated carrier that happens to publish a matching
    one. Cash AA6939 + BA174 (no arrival) + AS99 (matching arrival) is
    unresolvable, not an Alaska match."""
    res = SearchResult(
        solutions=[
            _itin(("AA6939", "2026-08-15T18:40:00", "JFK", "LHR", "2026-08-16T06:30:00")),
        ]
    )
    awards = [
        _award(
            "BA174",
            "2026-08-15T18:40:00",
            program="American",
            miles=60000,
            origin="JFK",
            dest="LHR",
            arrival="",
        ),
        _award(
            "AS99",
            "2026-08-15T18:40:00",
            program="Alaska",
            miles=7500,
            origin="JFK",
            dest="LHR",
            arrival="2026-08-16T06:30:00",
        ),
    ]
    matches = join(res, awards)
    assert matches[0].awards == []
