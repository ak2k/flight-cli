# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) confuses basedpyright into thinking
# alias names are required kwargs. The tests rely on populate_by_name=True
# (set on _Loose); silence the rule rather than reformat every constructor.
"""Tests for the cash-itinerary ↔ award-flight matcher."""

from __future__ import annotations

import json
import pathlib

from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.pp.match import (
    award_match_key,
    cash_match_key,
    join,
)
from flight_cli.pp.models import (
    AirlineSearchResponse,
    OutboundFlight,
    PricingInfoResponse,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


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


def _award(
    fn: str,
    dep: str,
    *,
    miles: int = 47000,
    tax: float = 250.0,
    cabin: str = "Economy",
    origin: str = "EWR",
    dest: str = "LHR",
) -> OutboundFlight:
    return OutboundFlight.model_validate(
        {
            "origin": origin,
            "destination": dest,
            "localDepartureDateTime": dep,
            "localArrivalDateTime": dep,
            "firstFlightNumber": fn,
            "perCabinMilesPricing": [
                {
                    "cabinClass": cabin,
                    "perPassengerPricing": {
                        "perPassengerMilesAmount": miles,
                        "perPassengerTaxAmountUsd": tax,
                        "taxCurrencyCode": "USD",
                    },
                }
            ],
        }
    )


# ─────────────────────────── key normalization ─────────────────────────────


def test_cash_match_key_uppercases_and_strips_whitespace():
    it = _itin(("ua 146", "2026-06-09T22:00:00", "JFK", "LHR"))
    assert cash_match_key(it) == ("UA146", "2026-06-09")


def test_cash_match_key_empty_when_no_flights():
    it = Itinerary(itinerary=ItineraryDetails(slices=[Slice(flights=[])]))
    assert cash_match_key(it) is None


def test_cash_match_key_handles_space_separated_iso():
    """Some Matrix payloads return 'YYYY-MM-DD HH:MM' instead of ISO 'T'."""
    it = _itin(("UA146", "2026-06-09 22:00", "JFK", "LHR"))
    assert cash_match_key(it) == ("UA146", "2026-06-09")


def test_cash_match_key_uses_slice_index_for_return_leg():
    it = _itin(
        ("UA146", "2026-06-09T22:00:00", "JFK", "LHR"),  # outbound
        ("UA147", "2026-06-12T10:00:00", "LHR", "JFK"),  # return
    )
    assert cash_match_key(it, slice_index=0) == ("UA146", "2026-06-09")
    assert cash_match_key(it, slice_index=1) == ("UA147", "2026-06-12")


def test_cash_match_key_out_of_range_slice_returns_none():
    it = _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR"))
    assert cash_match_key(it, slice_index=5) is None


def test_award_match_key_normalizes_consistently():
    of = _award("ua 146", "2026-06-09T22:00:00")
    assert award_match_key(of) == ("UA146", "2026-06-09")


# ────────────────────────────── join semantics ─────────────────────────────


def _pricing() -> PricingInfoResponse:
    return PricingInfoResponse.model_validate(json.loads((FIX / "pricing_info.json").read_text()))


def test_join_outer_keeps_unmatched_cash():
    """A cash itinerary whose flight # has no PP award match should still
    surface in the output — with empty awards."""
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),  # will match
            _itin(("BA178", "2026-06-09T07:50:00", "JFK", "LHR")),  # no award
        ]
    )
    award_resp = AirlineSearchResponse(
        outboundFlights=[
            _award("UA146", "2026-06-09T22:00:00"),
        ]
    )
    matches = join(res, {"United": award_resp}, _pricing())
    assert len(matches) == 2
    assert matches[0].awards and matches[0].awards[0].airline == "United"
    assert matches[1].awards == []  # BA178 unmatched but preserved


def test_join_attaches_funding_banks_from_pricing():
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),
        ]
    )
    award_resp = AirlineSearchResponse(
        outboundFlights=[
            _award("UA146", "2026-06-09T22:00:00"),
        ]
    )
    matches = join(res, {"United": award_resp}, _pricing())
    [option] = matches[0].awards
    assert sorted(option.funding_banks) == ["Bilt", "Chase"]
    assert option.miles_to_cash_ratio == 0.0125


def test_join_codeshare_surfaces_multiple_airlines():
    """When two airlines (PP-side) report the same flight#xdate as their own
    award, both options should attach to the cash itinerary."""
    res = SearchResult(
        solutions=[
            _itin(("BA178", "2026-06-09T07:50:00", "JFK", "LHR")),
        ]
    )
    award_by_airline = {
        "American": AirlineSearchResponse(
            outboundFlights=[
                _award("BA178", "2026-06-09T07:50:00", miles=30000, tax=308.0),
            ]
        ),
        "British Airways": AirlineSearchResponse(
            outboundFlights=[
                _award("BA178", "2026-06-09T07:50:00", miles=50000, tax=750.0),
            ]
        ),
    }
    matches = join(res, award_by_airline, _pricing())
    airlines = {ao.airline for ao in matches[0].awards}
    assert airlines == {"American", "British Airways"}


def test_join_use_inbound_reads_inboundFlights():
    res = SearchResult(
        solutions=[
            _itin(
                ("UA146", "2026-06-09T22:00:00", "JFK", "LHR"),  # outbound
                ("UA147", "2026-06-12T10:00:00", "LHR", "JFK"),  # return
            ),
        ]
    )
    award_resp = AirlineSearchResponse(
        outboundFlights=[],  # nothing on outbound
        inboundFlights=[_award("UA147", "2026-06-12T10:00:00", origin="LHR", dest="JFK")],
    )
    # use_inbound=True flips the index source
    matches = join(res, {"United": award_resp}, _pricing(), slice_index=1, use_inbound=True)
    assert len(matches[0].awards) == 1


def test_join_empty_award_response():
    res = SearchResult(
        solutions=[
            _itin(("UA146", "2026-06-09T22:00:00", "JFK", "LHR")),
        ]
    )
    matches = join(res, {"United": AirlineSearchResponse()}, _pricing())
    assert matches[0].awards == []
