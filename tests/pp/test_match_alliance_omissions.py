# pyright: reportCallIssue=false
# DIVERGE: the cash-side models trip reportCallIssue as in test_match.py.
"""SK and AZ left the alliance groups in `_PARTNER_GROUPS`: an omitted carrier
cannot fabricate an award price on the route+time fallback."""

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
    return SearchResult(solutions=[itin])


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


def test_join_drops_award_from_carrier_that_left_its_alliance():
    """The same route and minute under a Star carrier (LH) and SK, or a
    SkyTeam carrier (DL) and AZ, attaches no award in either direction."""
    for cash_fn, award_fn in [
        ("LH400", "SK400"),
        ("SK400", "LH400"),
        ("DL100", "AZ100"),
        ("AZ100", "DL100"),
    ]:
        matches = join(_cash(cash_fn), [_award(award_fn)])
        assert matches[0].awards == [], (cash_fn, award_fn)


def test_same_metal_rejects_sk_and_az_against_former_alliance_mates():
    assert same_metal("LH400", "SK400") is False
    assert same_metal("SK400", "UA1") is False
    assert same_metal("AF1", "AZ1") is False
    assert same_metal("AZ1", "KL2") is False


def test_same_metal_still_accepts_sk_and_az_against_themselves():
    assert same_metal("SK400", "SK916") is True
    assert same_metal("AZ100", "AZ200") is True


def test_join_still_bridges_the_rest_of_each_alliance():
    for cash_fn, award_fn in [("LH400", "UA400"), ("DL100", "AF100")]:
        matches = join(_cash(cash_fn), [_award(award_fn)])
        assert [a.flight_number for a in matches[0].awards] == [award_fn], (cash_fn, award_fn)
