# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs even though populate_by_name=True is
# set. Same posture as tests/pp/test_match.py + pp/gflight_adapter.py.
"""Tests for the Tier-2 Google Flights post-filter."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fli.models import FlightLeg, FlightResult  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]

from flight_cli._gf_postfilter import (
    apply_postfilter,
    can_postfilter,
    routing_keep,
    row_check_names,
    search_page_reasons,
)
from flight_cli._gflight_ids import GFlightWithId, LegAmenities
from flight_cli.domain import TimeOfDay
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    LegInfo,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.routing_predicates import (
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    ExcludeCodesharePred,
    ExcludeRedeyesPred,
    Predicate,
    SpecificFlightPred,
    classify,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _slice(legs: Sequence[tuple[str, str | None, list[str]]], stops: Sequence[str] = ()) -> Slice:
    """legs = [(flight_number, operating_carrier, marketing_carriers)]."""
    return Slice(
        flights=[f for f, _, _ in legs],
        stops=[SliceEndpoint(code=s) for s in stops],
        legs=[LegInfo(operating_carrier=op, marketing_carriers=list(mk)) for _, op, mk in legs],
    )


def _result(*slices: Slice) -> SearchResult:
    sols = [
        Itinerary(ext=ItineraryExt(price="USD100.00"), itinerary=ItineraryDetails(slices=[s]))
        for s in slices
    ]
    return SearchResult(solutionCount=len(sols), solutions=sols)


def _filter(result: SearchResult, *preds: Predicate) -> list[str]:
    """Apply preds to slice 0 and return the surviving first-flight numbers."""
    out = apply_postfilter(result, [list(preds)])
    flights: list[str] = []
    for it in out.solutions:
        itn = it.itinerary
        if itn is not None:
            flights.append(itn.slices[0].flights[0])
    return flights


# ─────────────────────────── operating carrier ─────────────────────────


def test_operating_include_keeps_only_all_matching() -> None:
    res = _result(
        _slice([("LH400", "LH", ["LH"])]),  # operated by LH
        _slice([("UA100", "UA", ["UA"])]),  # operated by UA
    )
    kept = _filter(res, CarrierPred(frozenset({"LH"}), exclude=False, operating=True))
    assert kept == ["LH400"]


def test_operating_exclude_drops_matching() -> None:
    res = _result(_slice([("LH400", "LH", ["LH"])]), _slice([("UA100", "UA", ["UA"])]))
    kept = _filter(res, CarrierPred(frozenset({"UA"}), exclude=True, operating=True))
    assert kept == ["LH400"]


# ─────────────────────────── marketing exclude ─────────────────────────


def test_marketing_exclude_drops_by_the_booking_carrier_only() -> None:
    """Matrix's `~UA+` keeps a fare booked under another carrier even when UA
    also sells the flight, so the sellers listed beside the booking carrier
    are not the row's fare and do not exclude it."""
    res = _result(
        _slice([("UA100", "UA", ["UA"])]),  # booked UA -> excluded
        _slice([("LH9498", "EN", ["LH"])]),  # LH/Air Dolomiti, no UA -> kept
        _slice([("LH900", "LH", ["UA"])]),  # booked LH, UA also sells it -> kept
        _slice([("UA9000", "LH", ["UA", "LH"])]),  # booked under UA's number -> excluded
    )
    kept = _filter(res, CarrierPred(frozenset({"UA"}), exclude=True, operating=False))
    assert kept == ["LH9498", "LH900"]


# ─────────────────────────── connection airport ────────────────────────


def test_connection_exclude_drops_via_airport() -> None:
    res = _result(
        _slice([("AA1", "AA", ["AA"]), ("AA2", "AA", ["AA"])], stops=["DFW"]),
        _slice([("AA3", "AA", ["AA"]), ("AA4", "AA", ["AA"])], stops=["ORD"]),
    )
    kept = _filter(res, ConnectionAirportPred(frozenset({"DFW"}), exclude=True))
    assert kept == ["AA3"]


# ─────────────────────────── codeshare ─────────────────────────────────


def test_codeshare_exclude_drops_operated_for_legs() -> None:
    res = _result(
        _slice([("LH9498", "EN", ["LH"])]),  # LH marketed, EN operated -> codeshare
        _slice([("LH400", "LH", ["LH"])]),  # LH on LH metal -> not codeshare
    )
    kept = _filter(res, ExcludeCodesharePred())
    assert kept == ["LH400"]


# ─────────────────────────── specific flight ───────────────────────────


def test_specific_flight_number_and_range() -> None:
    res = _result(_slice([("UA882", "UA", ["UA"])]), _slice([("UA999", "UA", ["UA"])]))
    assert _filter(res, SpecificFlightPred("UA", 882, 882)) == ["UA882"]
    res2 = _result(_slice([("UA882", "UA", ["UA"])]), _slice([("UA3000", "UA", ["UA"])]))
    assert _filter(res2, SpecificFlightPred("UA", 1000, 2000)) == []


# ─────────────────────────── per-slice scoping ─────────────────────────


def test_predicates_apply_only_to_their_slice() -> None:
    """Outbound `~UA` must not filter on the return slice."""
    out_clean_ret_ua = Itinerary(
        ext=ItineraryExt(price="USD1.00"),
        itinerary=ItineraryDetails(
            slices=[_slice([("LH1", "LH", ["LH"])]), _slice([("UA9", "UA", ["UA"])])]
        ),
    )
    out_ua = Itinerary(
        ext=ItineraryExt(price="USD2.00"),
        itinerary=ItineraryDetails(
            slices=[_slice([("UA1", "UA", ["UA"])]), _slice([("LH9", "LH", ["LH"])])]
        ),
    )
    res = SearchResult(solutionCount=2, solutions=[out_clean_ret_ua, out_ua])
    out = apply_postfilter(res, [[CarrierPred(frozenset({"UA"}), exclude=True, operating=False)]])
    # only the itinerary with UA on the OUTBOUND slice is dropped
    assert len(out.solutions) == 1
    assert out.solution_count == 1
    itn = out.solutions[0].itinerary
    assert itn is not None
    assert itn.slices[0].flights == ["LH1"]


# ─────────────────────────── gate helpers ──────────────────────────────


def test_can_postfilter_supported_vs_unsupported() -> None:
    assert can_postfilter(CarrierPred(frozenset({"LH"}), exclude=False, operating=True))
    assert can_postfilter(ExcludeCodesharePred())
    assert not can_postfilter(ConnectTimePred(min_minutes=60, max_minutes=None))  # min layover
    assert not can_postfilter(ExcludeRedeyesPred())


def test_the_search_page_serves_encodable_and_post_filterable_predicates() -> None:
    assert search_page_reasons(classify("O:LH+", "-CODESHARE; MAXSTOPS 1").predicates) == []
    assert search_page_reasons(classify("~BA+", "-AIRLINES AF").predicates) == []


def test_every_other_predicate_keeps_its_own_reason() -> None:
    """One reason per predicate the page can't serve, and none for the ones it
    can: the user reads which constraint sent the search to Matrix."""
    reasons = search_page_reasons(
        classify("~BA+", "MINCONNECT 1:00; -REDEYES; -OVERNIGHTS").predicates
    )
    assert reasons == ["a red-eye exclusion", "an overnight-stop exclusion"]
    assert search_page_reasons(classify("LH+", "F bc=y").predicates)  # include, Tier 3
    # Evaluable here, but Matrix reads both positionally and the filter does not.
    assert search_page_reasons(classify("AS21", None).predicates)
    assert search_page_reasons(classify("F* ~DUB F*", None).predicates)


def test_apply_postfilter_no_predicates_is_noop() -> None:
    res = _result(_slice([("UA1", "UA", ["UA"])]))
    out = apply_postfilter(res, [[]])
    assert out.solution_count == 1


@pytest.mark.parametrize(
    "routing,extension",
    [
        ("AA+", None),
        (None, "AIRLINES AA DL"),
        (None, "ALLIANCE oneworld"),
        (None, "ALLIANCE oneworld|star-alliance"),
        (None, "MAXDUR 6:20"),
        (None, "MINCONNECT 2:00; MAXCONNECT 5:00"),
        ("AA+", "AIRLINES DL; MAXSTOPS 1"),
    ],
)
def test_the_search_page_serves_what_its_tfs_encodes(
    routing: str | None, extension: str | None
) -> None:
    assert search_page_reasons(classify(routing, extension).predicates) == []


@pytest.mark.parametrize(
    "routing,extension",
    [("AA+", "ALLIANCE oneworld"), (None, "ALLIANCE oneworld; ALLIANCE skyteam")],
)
def test_an_alliance_beside_another_include_goes_to_matrix(
    routing: str | None, extension: str | None
) -> None:
    """3.6 is one list, so Google would answer either, and nothing narrows an
    alliance back on the rows."""
    assert search_page_reasons(classify(routing, extension).predicates) == [
        "an alliance filter combined with another carrier or alliance filter"
    ]


def test_a_zero_maximum_layover_goes_to_matrix_naming_it() -> None:
    assert search_page_reasons(classify(None, "MAXCONNECT 0:00").predicates) == [
        "a maximum layover of 0 min"
    ]


# ─────────────────────────── checks on the raw row ─────────────────────────

_DAY = datetime(2026, 11, 4)


def _row(
    *legs: tuple[str, str, str, int, int],
    duration: int,
    marketing: tuple[tuple[str, ...], ...] = (),
) -> GFlightWithId:
    """legs = (booking carrier, from, to, departure minute of the day, arrival
    minute of the day), both local to their own airport."""
    fli_legs = [
        FlightLeg(
            airline=Airline[carrier],
            flight_number=str(100 + i),
            departure_airport=Airport[frm],
            arrival_airport=Airport[to],
            departure_datetime=_DAY + timedelta(minutes=dep),
            arrival_datetime=_DAY + timedelta(minutes=arr),
            duration=max(arr - dep, 1),
        )
        for i, (carrier, frm, to, dep, arr) in enumerate(legs)
    ]
    amenities = [
        LegAmenities(marketing_carriers=marketing[i] if i < len(marketing) else (carrier,))
        for i, (carrier, *_rest) in enumerate(legs)
    ]
    flight = FlightResult(
        price=300.0, currency="USD", duration=duration, stops=len(legs) - 1, legs=fli_legs
    )
    return GFlightWithId(flight=flight, flight_id="", amenities=amenities)


def _keeps(row: GFlightWithId, routing: str | None, extension: str | None, leg: int = 0) -> bool:
    keep = routing_keep([classify(routing, extension).predicates] * 2)
    assert keep is not None
    return keep(leg, row)


def _h(hours: float) -> int:
    return int(hours * 60)


def test_the_duration_check_reads_googles_total() -> None:
    westbound = _row(("AA", "JFK", "LAX", _h(8), _h(11.25)), duration=375)
    assert _keeps(westbound, None, "MAXDUR 6:20")
    long_way = _row(("AA", "JFK", "LAX", _h(8), _h(11.5)), duration=390)
    assert not _keeps(long_way, None, "MAXDUR 6:20")


def test_an_eastbound_row_inside_the_limit_is_kept_though_its_local_times_are_not() -> None:
    """LAX 08:00 to JFK 16:30 local is 8h30 on the clocks and 5h30 in the air:
    leg datetimes are local to each airport, so their difference is off by the
    zone offset."""
    eastbound = _row(("AA", "LAX", "JFK", _h(8), _h(16.5)), duration=330)
    assert _keeps(eastbound, None, "MAXDUR 6:20", leg=1)


def test_layover_bounds_hold_every_connection() -> None:
    def connecting(gap: int) -> GFlightWithId:
        return _row(
            ("AA", "LGA", "ORD", _h(7), _h(9)),
            ("AA", "ORD", "LAX", _h(9) + gap, _h(12) + gap),
            duration=420 + gap,
        )

    assert not _keeps(connecting(119), None, "MINCONNECT 2:00")
    assert _keeps(connecting(120), None, "MINCONNECT 2:00")
    assert _keeps(connecting(60), None, "MAXCONNECT 1:00")
    assert not _keeps(connecting(61), None, "MAXCONNECT 1:00")
    nonstop = _row(("AA", "LGA", "LAX", _h(7), _h(10)), duration=360)
    assert _keeps(nonstop, None, "MINCONNECT 2:00")


def test_a_carrier_include_keeps_a_leg_any_allowed_carrier_sells() -> None:
    """Matrix's marketing reading of `AA+`: a leg sold as AA passes whatever
    carrier it is booked under."""
    assert _keeps(_row(("AA", "JFK", "LAX", _h(8), _h(11)), duration=360), "AA+", None)
    assert not _keeps(_row(("DL", "JFK", "LAX", _h(8), _h(11)), duration=360), "AA+", None)
    codeshare = _row(("B6", "JFK", "LAX", _h(8), _h(11)), duration=360, marketing=(("B6", "AA"),))
    assert _keeps(codeshare, "AA+", None)


def _departing(hour: int, minute: int) -> GFlightWithId:
    start = hour * 60 + minute
    return _row(("AA", "JFK", "LAX", start, start + 200), duration=380)


def test_a_departure_window_holds_the_rows_to_the_minute_on_its_own() -> None:
    """Google's window is whole hours (11 answers up to 11:59); Matrix's morning
    ends at 11:00. A time window alone still builds a filter."""
    keep = routing_keep([[]], [(TimeOfDay.MORNING,)])
    assert keep is not None
    assert [keep(0, _departing(h, m)) for h, m in ((7, 59), (8, 0), (11, 0), (11, 1))] == [
        False,
        True,
        True,
        False,
    ]


def test_each_leg_is_held_to_its_own_window() -> None:
    keep = routing_keep([[], []], [(TimeOfDay.MORNING,), (TimeOfDay.EVENING, TimeOfDay.NIGHT)])
    assert keep is not None
    assert keep(0, _departing(9, 0))
    assert not keep(1, _departing(9, 0))
    assert keep(1, _departing(23, 30))


def test_no_predicate_and_no_window_is_no_filter() -> None:
    assert routing_keep([[], []], [(), ()]) is None


def test_the_checks_are_named_in_the_users_words() -> None:
    preds = classify("AA+", "MAXDUR 6:20; MINCONNECT 2:00; ALLIANCE oneworld; MAXSTOPS 1")
    names = row_check_names(
        [preds.predicates, preds.predicates], [(TimeOfDay.MORNING,), (TimeOfDay.EVENING,)]
    )
    assert names == [
        "a carrier filter (AA)",
        "a maximum trip duration (380 min)",
        "a minimum layover (120 min)",
        "a departure-time window (morning)",
        "a return-time window (evening)",
    ]
