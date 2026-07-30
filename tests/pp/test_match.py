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


def _itin(*slices_data: tuple[str, str, str, str]) -> Itinerary:
    """Build an Itinerary with the given slices. Each tuple is
    (flight_number, departure_iso, origin_iata, destination_iata)."""
    slcs = [
        Slice(
            flights=[fn],
            departure=dep,
            origin=SliceEndpoint(code=o),
            destination=SliceEndpoint(code=d),
        )
        for fn, dep, o, d in slices_data
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
) -> AwardFlight:
    return AwardFlight(
        origin=origin,
        destination=dest,
        departure=dep,
        arrival=dep,
        flight_number=fn,
        num_connections=num_connections,
        provider="PointsPath",
        program=program,
        miles_to_cash_ratio=miles_to_cash_ratio,
        funding_banks=funding_banks or ["Chase", "Bilt"],
        cabins=[CabinAward(cabin=cabin, miles=miles, tax_usd=tax, tax_currency="USD")],
        matched_google_flight_id=matched_id,
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


def test_join_exact_carrier_award_suppresses_partner_in_same_bucket():
    """Characterization of a deliberate fail-closed choice.

    Cash AA6939 with two awards in the same route+time bucket: BA174 (the
    operating carrier of the codeshare) and AA6939 (the marketing number).
    The old behavior attached both, on the premise that they describe one
    aircraft.

    We can no longer assume that. This bucket is structurally IDENTICAL to
    the collision case — cash AA118 with awards AA118 (American) and AS17
    (Alaska) — where the two awards are different aircraft that merely share
    a departure minute. From (route, time, flight number) alone the two are
    indistinguishable, so resolving them differently is not possible here.

    We keep the exact-carrier award and drop the partner. The cost is a
    hidden second booking option for one physical seat; the alternative cost
    is rendering another aircraft's price on this row. Only `operating_carrier`
    ground truth (models.py:105, gflight backend only) can tell these apart —
    when the join learns to read it, this test should flip back.
    """
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
    programs = {ao.program for ao in matches[0].awards}
    assert programs == {"VirginAtlantic"}


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
