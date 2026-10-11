"""CZ left SkyTeam on 2019-01-01, so it is in no `_PARTNER_GROUPS` alliance
group: an award on CZ cannot attach to a SkyTeam carrier's flight on the
route+time fallback. SU stays (its membership is suspended, not ended)."""

from __future__ import annotations

from flight_cli.models import Itinerary, ItineraryDetails, SearchResult, Slice, SliceEndpoint
from flight_cli.pp.match import join, same_metal
from flight_cli.providers.base import AwardFlight, CabinAward

DEP = "2026-08-15T18:40:00"


def _cash(fn: str) -> SearchResult:
    s = Slice(
        flights=[fn],
        departure=DEP,
        origin=SliceEndpoint(code="CPH"),
        destination=SliceEndpoint(code="FRA"),
    )
    itin = Itinerary(
        displayTotal="USD500.00",
        itinerary=ItineraryDetails(slices=[s], carriers=[]),
    )
    return SearchResult(solutions=[itin])  # pyright: ignore[reportCallIssue] # cash model has required wire fields


def _award(fn: str) -> AwardFlight:
    return AwardFlight(
        origin="CPH",
        destination="FRA",
        departure=DEP,
        arrival=DEP,
        flight_number=fn,
        num_connections=0,
        provider="PointsPath",
        program="United",
        miles_to_cash_ratio=0.0125,
        funding_banks=["Chase"],
        cabins=[CabinAward(cabin="Economy", miles=10000, tax_usd=50.0, tax_currency="USD")],
        matched_google_flight_id="",
        segment_flight_numbers=[],
    )


def test_same_metal_rejects_cz_against_skyteam_members():
    assert same_metal("CZ1", "DL1") is False
    assert same_metal("DL1", "CZ1") is False
    assert same_metal("CZ1", "AF1") is False


def test_join_drops_award_from_cz_against_a_skyteam_flight():
    for cash_fn, award_fn in [("DL100", "CZ100"), ("CZ100", "DL100")]:
        matches = join(_cash(cash_fn), [_award(award_fn)])
        assert matches[0].awards == [], (cash_fn, award_fn)


def test_same_metal_still_accepts_cz_against_itself_and_su_against_skyteam():
    assert same_metal("CZ3", "CZ4") is True
    assert same_metal("SU1", "AF1") is True
    assert same_metal("DL1", "AF1") is True
