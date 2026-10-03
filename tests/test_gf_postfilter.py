# pyright: reportCallIssue=false, reportPrivateUsage=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs even though populate_by_name=True is
# set. Same posture as tests/pp/test_match.py + pp/gflight_adapter.py.
"""Tests for the Tier-2 Google Flights post-filter."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import FlightLeg, FlightResult  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]

from conftest import _ds1
from flight_cli import _gflight_ids as gfid
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

    from flight_cli.domain import CalendarSearch


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


def _flies(pred: SpecificFlightPred, *flights: str, legs: int | None = None) -> bool:
    """Whether a slice of `flights` passes `pred`; `legs` states more legs than
    flights when the row leaves a flight number out."""
    slc = _slice([(f, "XX", ["XX"]) for f in flights])
    if legs is not None:
        slc = slc.model_copy(update={"legs": [LegInfo(operating_carrier="XX")] * legs})
    return bool(apply_postfilter(_result(slc), [[pred]]).solutions)


def test_one_flight_is_every_leg_under_its_number() -> None:
    """Matrix's flight is every leg under one number: bare AS21 answered
    JFK-LAX with no solutions where Google listed AS21 connecting to AS487."""
    one = SpecificFlightPred("XX", 1, 1)
    assert _flies(one, "XX1")
    assert _flies(one, "XX1", "XX1")
    assert not _flies(one, "XX1", "XX2")
    assert not _flies(one, "YY9", "XX1")
    assert not _flies(SpecificFlightPred("XX", 1, 1, quantifier="+"), "XX1", "XX2")


def test_a_one_flight_range_is_one_number_in_it() -> None:
    assert _flies(SpecificFlightPred("XX", 1, 9), "XX5", "XX5")
    assert not _flies(SpecificFlightPred("XX", 1, 9), "XX1", "XX2")
    assert _flies(SpecificFlightPred("XX", 1, 9, quantifier="+"), "XX1", "XX2")
    assert not _flies(SpecificFlightPred("XX", 1, 9, quantifier="+"), "XX1", "XX12")


def test_a_leg_with_no_flight_number_fails_a_flight_number() -> None:
    one = SpecificFlightPred("XX", 1, 1)
    assert not _flies(one, "XX1", "XX")
    assert not _flies(one, "XX1", legs=2)
    assert not _flies(one)


def _lax_kept(routing: str) -> list[str]:
    """The JFK-LAX capture's rows a routing keeps, as booked flight numbers."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_tfu.json"))
    keep = routing_keep([classify(routing, None).predicates])
    assert keep is not None
    rows = [gfid._parse_flight_with_id(raw) for raw in gfid._rows_from_ds1(payload).rows]
    return [
        "+".join(f"{leg.airline.name}{leg.flight_number}" for leg in row.flight.legs)
        for row in rows
        if keep(0, row)
    ]


def test_on_the_lax_board_a_flight_number_keeps_that_flight_alone() -> None:
    assert sorted(r for r in _lax_kept("AS+") if r.startswith("AS21+")) == [
        "AS21+AS1793",
        "AS21+AS487",
        "AS21+AS600",
    ]
    assert _lax_kept("AS21") == []
    assert _lax_kept("AS21+") == []
    assert _lax_kept("AA1") == ["AA1"]
    assert _lax_kept("DL747") == ["DL747"]
    served = _lax_kept("AA1-3000")
    assert served and all(r.startswith("AA") and "+" not in r for r in served)


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
    assert can_postfilter(ExcludeRedeyesPred())


def test_the_search_page_serves_encodable_and_post_filterable_predicates() -> None:
    assert search_page_reasons(classify("O:LH+", "-CODESHARE; MAXSTOPS 1").predicates) == []
    assert search_page_reasons(classify("~BA+", "-AIRLINES AF").predicates) == []


def test_every_other_predicate_keeps_its_own_reason() -> None:
    """One reason per predicate the page can't serve, and none for the ones it
    can: the user reads which constraint sent the search to Matrix."""
    # Each row's legs carry their own local clocks, so the night checks ride.
    reasons = search_page_reasons(
        classify("~BA+", "MINCONNECT 1:00; -REDEYES; -OVERNIGHTS").predicates
    )
    assert reasons == []
    assert search_page_reasons(classify("~BA+", "-REDEYES; F bc=y").predicates) == [
        "extension 'F bc=y' not expressible on GF"
    ]
    assert search_page_reasons(classify("LH+", "F bc=y").predicates)  # include, Tier 3
    # Evaluable here, but not with Matrix's meaning: one connection not at DUB,
    # and a range that may be several flights.
    assert search_page_reasons(classify("F* ~DUB F*", None).predicates)
    assert search_page_reasons(classify("AA1-3000+", None).predicates) == [
        "a flight-number range (AA1-3000+)"
    ]
    assert search_page_reasons(classify("AS21", None).predicates) == []


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


def test_a_zero_maximum_duration_goes_to_matrix_naming_it() -> None:
    """fli's duration maximum is positive, and assigning it skips that check,
    so the page would be sent 3.12=0."""
    assert search_page_reasons(classify(None, "MAXDUR 0:00").predicates) == [
        "a maximum trip duration (0 min)"
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


def _seattle_connection(
    day: list[int], arrival: list[int], departure: list[int], stated: int, at: str = "SEA"
) -> GFlightWithId:
    """The JFK-LAX capture's row through SEA with its connection moved to `day`:
    landing at `arrival` and leaving at `departure`, [hour, minute] on SEA's
    clocks, the page stating a layover of `stated` minutes at `at`."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_tfu.json"))
    raw = next(r for r in gfid._rows_from_ds1(payload).rows if r[0][2][0][6] == "SEA")
    inbound, outbound = raw[0][2]
    inbound[21], inbound[10] = day, arrival
    outbound[20], outbound[8] = day, departure
    raw[0][13][0][0:3] = [stated, at, at]
    return gfid._parse_flight_with_id(raw)


def test_a_layover_across_a_clock_change_is_measured_as_the_page_states_it() -> None:
    """Leg times are clock readings at each airport. Landing 01:50 and leaving
    01:20 once the clocks fall back is 30 minutes on the ground, and so is 01:50
    to 03:20 across the spring change."""
    fall_back = _seattle_connection([2026, 11, 1], [1, 50], [1, 20], stated=30)
    assert _keeps(fall_back, None, "MINCONNECT 0:30")
    assert not _keeps(fall_back, None, "MINCONNECT 0:45")
    spring_forward = _seattle_connection([2027, 3, 14], [1, 50], [3, 20], stated=30)
    assert _keeps(spring_forward, None, "MAXCONNECT 1:00")
    assert not _keeps(spring_forward, None, "MINCONNECT 1:00")


def test_a_layover_stated_at_another_airport_is_not_that_connections() -> None:
    assert _seattle_connection([2026, 11, 4], [10, 23], [11, 30], stated=67).layovers == (67,)
    assert _seattle_connection([2026, 11, 4], [10, 23], [11, 30], 67, at="PDX").layovers == (None,)


def test_a_layover_the_clocks_cannot_place_is_not_held_against_the_row() -> None:
    """With no layover stated, leaving before landing on the clocks is a
    fall-back change, and the row gives nothing to measure the gap by."""
    fall_back = _row(
        ("AA", "LGA", "ORD", _h(0), _h(1) + 50),
        ("AA", "ORD", "LAX", _h(1) + 20, _h(4)),
        duration=330,
    )
    assert _keeps(fall_back, None, "MINCONNECT 0:30")
    assert _keeps(fall_back, None, "MAXCONNECT 1:00")


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
    assert routing_keep([[], []], [(), ()], max_stops=-1) is None


# ─────────────────────────── the stop ceiling ──────────────────────────────


def _one_stop() -> GFlightWithId:
    return _row(
        ("AA", "JFK", "ORD", _h(7), _h(9)), ("AA", "ORD", "LAX", _h(10), _h(12)), duration=420
    )


def _stop_keep(max_stops: int | None, extension: str | None = None) -> Any:
    keep = routing_keep([classify(None, extension).predicates] * 2, max_stops=max_stops)
    assert keep is not None
    return keep


def test_a_row_over_the_stop_limit_is_dropped() -> None:
    """Google has ignored a field it was sent, so the page's stop filter is
    checked on the row as well."""
    assert not _stop_keep(0)(0, _one_stop())
    assert _stop_keep(1)(0, _one_stop())
    assert _stop_keep(0)(0, _row(("AA", "JFK", "LAX", _h(8), _h(11)), duration=360))


def test_the_extensions_and_routings_own_ceiling_is_checked_on_the_row() -> None:
    assert not _keeps(_one_stop(), None, "MAXSTOPS 0")
    assert _keeps(_one_stop(), None, "MAXSTOPS 1")
    assert not _keeps(_one_stop(), "N", None)


def test_the_strictest_ceiling_holds() -> None:
    assert not _stop_keep(1, "MAXSTOPS 0")(0, _one_stop())
    assert not _stop_keep(0, "MAXSTOPS 2")(0, _one_stop())


def test_a_return_board_is_held_to_the_ceiling() -> None:
    assert not _stop_keep(0)(1, _one_stop())
    keep = routing_keep([[], classify(None, "MAXSTOPS 0").predicates])
    assert keep is not None
    assert keep(0, _one_stop())
    assert not keep(1, _one_stop())


def test_a_negative_limit_is_no_limit() -> None:
    assert _stop_keep(-1, "MAXDUR 9:00")(0, _one_stop())


def test_the_checks_are_named_in_the_users_words() -> None:
    preds = classify("AA+", "MAXDUR 6:20; MINCONNECT 2:00; ALLIANCE oneworld; MAXSTOPS 1")
    names = row_check_names(
        [preds.predicates, preds.predicates], [(TimeOfDay.MORNING,), (TimeOfDay.EVENING,)]
    )
    assert names == [
        "a carrier filter (AA)",
        "a maximum trip duration (380 min)",
        "a minimum layover (120 min)",
        "a stop ceiling of 1",
        "a departure-time window (morning)",
        "a return-time window (evening)",
    ]


def test_the_strictest_stop_ceiling_is_named_once() -> None:
    stops = classify(None, "MAXSTOPS 2").predicates
    assert row_check_names([stops, stops], max_stops=1) == ["a stop ceiling of 1"]
    assert row_check_names([stops, stops], max_stops=3) == ["a stop ceiling of 2"]
    assert row_check_names([[], []], max_stops=0) == ["a stop ceiling of 0"]
    assert row_check_names([[], []], max_stops=-1) == []


def _priced(price: float | None, currency: str | None = "USD") -> GFlightWithId:
    row = _row(("AA", "JFK", "LAX", _h(8), _h(11)), duration=360)
    return replace(row, flight=row.flight.model_copy(update={"price": price, "currency": currency}))


def test_a_price_cap_keeps_only_rows_priced_at_or_under_it_in_its_currency() -> None:
    """A row over the cap, a row Google did not price and a row priced in
    another currency are each dropped: none of them is shown to be under it."""
    keep = routing_keep([[], []], max_price=250, currency="USD")
    assert keep is not None
    rows = [_priced(250.0), _priced(204.0), _priced(250.01), _priced(None), _priced(200.0, "EUR")]
    assert [keep(0, r) for r in rows] == [True, True, False, False, False]


def test_a_price_cap_holds_every_board_of_a_round_trip() -> None:
    keep = routing_keep([[], []], max_price=500, currency="USD")
    assert keep is not None
    assert not keep(1, _priced(501.0))
    assert keep(1, _priced(442.0))


def test_a_price_cap_is_held_beside_the_routing() -> None:
    keep = routing_keep([classify("AA+", None).predicates], max_price=250)
    assert keep is not None
    assert keep(0, _priced(249.0))
    assert not keep(0, _priced(251.0))


def test_a_price_cap_is_named_with_its_currency() -> None:
    assert row_check_names([[]], max_price=250, currency="EUR") == ["a price cap of EUR 250"]


# ─────────────────────────── the date grids ────────────────────────────────

_SEARCH_ONLY = [
    ("AA+", None, (), 0),
    (None, "ALLIANCE oneworld", (), 0),
    (None, "MAXDUR 6:20", (), 0),
    (None, "MINCONNECT 2:00", (), 0),
    (None, "MAXCONNECT 2:00", (), 0),
    (None, None, (TimeOfDay.MORNING,), 0),
    (None, None, (), 1),
]
_SEARCH_ONLY_IDS = [
    "carrier",
    "alliance",
    "maxdur",
    "minconnect",
    "maxconnect",
    "morning",
    "children",
]


def _one_way_calendar(
    routing: str | None, extension: str | None, times: tuple[TimeOfDay, ...], children: int
) -> CalendarSearch:
    from datetime import date

    from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, Pax, SearchOptions

    start = date.today() + timedelta(days=45)
    return CalendarSearch(
        legs=(
            Leg.of("JFK", "LAX", route_language=routing, extension=extension, time_ranges=times),
        ),
        window=CalendarWindow(
            start=start, end=start + timedelta(days=13), duration_min=0, duration_max=0
        ),
        options=SearchOptions(pax=Pax(children=children)),
    )


@pytest.mark.parametrize(
    ("routing", "extension", "times", "children"), _SEARCH_ONLY, ids=_SEARCH_ONLY_IDS
)
def test_the_http_grid_gate_still_refuses_what_only_a_search_can_check(
    routing: str | None, extension: str | None, times: tuple[TimeOfDay, ...], children: int
) -> None:
    """The search page serves these because it has rows to check them on;
    `page_blocker`, the `--fast --gf-transport http` gate, still refuses them."""
    from flight_cli._gf_calgraph import page_blocker

    search = _one_way_calendar(routing, extension, times, children)
    assert page_blocker(search) is not None
    assert search_page_reasons(classify(routing, extension).predicates) == []


@pytest.mark.parametrize(
    ("routing", "extension", "times", "children"), _SEARCH_ONLY, ids=_SEARCH_ONLY_IDS
)
def test_the_price_graph_takes_the_bounds_google_applies_and_not_the_rest(
    routing: str | None, extension: str | None, times: tuple[TimeOfDay, ...], children: int
) -> None:
    """The Chrome price graph has no rows either, but Google was measured
    applying the includes and the bounds from its URL. A morning window is
    written to 11:59 and a child is not asked for."""
    from flight_cli._gf_calgraph import graph_blocker

    blocker = graph_blocker(_one_way_calendar(routing, extension, times, children))
    assert (blocker is None) == (not times and not children)


def test_the_rpc_grid_still_refuses_a_minimum_layover() -> None:
    from datetime import date

    from flight_cli._gf_dategrid import grid_can_serve
    from flight_cli.domain import CalendarSearch, CalendarWindow, Leg

    start = date.today() + timedelta(days=45)
    search = CalendarSearch(
        legs=(Leg.of("JFK", "LAX", extension="MINCONNECT 2:00"),),
        window=CalendarWindow(
            start=start, end=start + timedelta(days=13), duration_min=0, duration_max=0
        ),
    )
    assert not grid_can_serve(search)
