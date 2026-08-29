# pyright: reportPrivateUsage=false
"""Regressions for cli.py findings from the codex adversarial pass.

All four were the house failure class: a plausible artifact making a claim
that isn't true of the search the user asked for.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from flight_cli.cli import _pinned_solution_index, _seated_pax
from flight_cli.domain import Leg, Pax, SearchOptions, SpecificDateSearch
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    SearchResult,
    Slice,
    SliceEndpoint,
)


def _res(n: int) -> SearchResult:
    sols = [
        Itinerary(
            id=f"sol-{i}",
            displayTotal=f"USD{i:03d}.00",
            itinerary=ItineraryDetails(
                slices=[
                    Slice(
                        flights=[f"AA{i}"],
                        departure="2026-09-01T06:00",
                        origin=SliceEndpoint(code="JFK"),
                        destination=SliceEndpoint(code="LHR"),
                    ),
                ],
                carriers=[],
            ),
        )
        for i in range(1, n + 1)
    ]
    return SearchResult(solutionCount=n, solutions=sols)  # pyright: ignore[reportCallIssue]


# ───────────── --pick must not name a row the user never saw ─────────────


def test_pick_beyond_the_rendered_table_falls_back() -> None:
    """The table hardcoded 10 rows while `--pick` validated against the full
    solution list, so `-n 15 --pick 15` printed 10 rows and then emitted a
    booking link labelled "itinerary #15 pinned" — for a row never displayed,
    and with no out-of-range warning because 15 was in range for the
    unrendered list."""
    assert _pinned_solution_index(_res(15), 15, 10) == 0  # fell back to cheapest


def test_pick_within_the_rendered_table_is_honoured() -> None:
    assert _pinned_solution_index(_res(15), 8, 10) == 7


def test_pick_is_honoured_when_the_table_was_widened() -> None:
    """`-n 15` renders 15 rows, so `--pick 15` is now legitimate."""
    assert _pinned_solution_index(_res(15), 15, 15) == 14


# ───────────── every seated passenger reaches both backends ─────────────


def test_infant_in_seat_counts_toward_the_award_seat_count() -> None:
    """An infant IN SEAT buys a seat; only a LAP infant doesn't. Omitting it
    made the award query ask for fewer seats than the cash query on the same
    run, so an award with too little availability rendered as bookable."""
    p = Pax(adults=2, children=1, infants_in_seat=1, infants_in_lap=1)
    assert _seated_pax(p) == 4


def test_lap_infant_does_not_consume_a_seat() -> None:
    assert _seated_pax(Pax(adults=1, infants_in_lap=1)) == 1


def _fli_pax(**kw: Any) -> Any:
    from flight_cli.fli_bridge import to_fli_filter

    s = SpecificDateSearch(
        legs=(Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 9, 1)),),
        options=SearchOptions(pax=Pax(**kw)),
    )
    return to_fli_filter(s).passenger_info


def test_child_only_search_does_not_synthesize_an_adult() -> None:
    """`adults=(...) or 1` invented an adult for a child-only search, pricing a
    two-passenger trip nobody asked for. fli permits adults=0."""
    pi = _fli_pax(adults=0, children=1)
    assert (pi.adults, pi.children) == (0, 1)


def test_infants_reach_the_google_flights_bridge() -> None:
    """Infants were dropped entirely on this path, so an infant-in-seat search
    priced one fewer seat than the Matrix side of the same run."""
    pi = _fli_pax(adults=2, children=1, infants_in_seat=1, infants_in_lap=1)
    assert (pi.adults, pi.children, pi.infants_in_seat, pi.infants_on_lap) == (2, 1, 1, 1)


# ───────── the Google link states how it differs from the search ─────────


def test_multi_airport_and_routing_search_gets_caveats() -> None:
    """`fast_flights`' tfs= format takes one airport pair and has no routing
    field, so a multi-airport routed search silently degrades: rows flying
    EWR->LGW under `--routing AA+` sat beside a link searching JFK->LHR
    unconstrained, while the Matrix link on the same output was faithful."""
    from flight_cli.cli import _gflight_url_caveats

    s = SpecificDateSearch(
        legs=(
            Leg(
                origins=("JFK", "EWR", "LGA"),
                destinations=("LHR", "LGW"),
                date=date(2026, 9, 1),
                route_language="AA+",
                extension="f bc=J",
            ),
        ),
    )
    notes = _gflight_url_caveats(s)
    assert any("multi-airport" in n and "JFK→LHR" in n for n in notes)
    assert any("routing/extension" in n for n in notes)


def test_plain_search_gets_no_caveats() -> None:
    """A search the link CAN express must not be annotated."""
    from flight_cli.cli import _gflight_url_caveats

    s = SpecificDateSearch(
        legs=(Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 9, 1)),),
    )
    assert _gflight_url_caveats(s) == []


# ───────── an award without enough seats is flagged, not hidden ─────────


def _award(miles: int, seats: int | None) -> Any:
    from flight_cli.providers.base import AwardFlight, CabinAward

    return AwardFlight(
        origin="JFK",
        destination="LHR",
        departure="d",
        arrival="a",
        flight_number="AA100",
        num_connections=0,
        provider="Seats.aero",
        program="American Airlines",
        miles_to_cash_ratio=0.0125,
        funding_banks=["Chase"],
        cabins=[
            CabinAward(
                cabin="Business",
                miles=miles,
                tax_usd=5.6,
                tax_currency="USD",
                remaining_seats=seats,
            ),
        ],
    )


def test_award_with_fewer_seats_than_the_party_is_flagged() -> None:
    """seats.aero reports RemainingSeats and the provider discarded it, so a
    57,500-mile business award with ONE seat rendered as available to a party
    of four."""
    from flight_cli.pp.cli import _fmt_award_cell

    cell = _fmt_award_cell([_award(57_500, 1)], "Business", None, 4)
    assert "1 seat" in cell


def test_sufficient_seats_are_not_flagged() -> None:
    from flight_cli.pp.cli import _fmt_award_cell

    assert "seat" not in _fmt_award_cell([_award(57_500, 4)], "Business", None, 4)


def test_unreported_seat_count_is_not_treated_as_zero() -> None:
    """None means "not reported" — PointsPath never reports it, and
    seats.aero's 0 is usually staleness. Neither is a claim of no seats."""
    from flight_cli.pp.cli import _fmt_award_cell

    assert "seat" not in _fmt_award_cell([_award(57_500, None)], "Business", None, 4)
