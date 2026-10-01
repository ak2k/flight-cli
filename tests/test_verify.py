# pyright: reportPrivateUsage=false
"""`search --verify`: one Google row priced on Matrix as exactly that itinerary.

The Matrix answers below are the ones measured for JFK-LAX on 2026-10-20 with
a routing chain of the row's flights. `AS21 AS487` answered two solutions with
the same flights and departure, one landing a day later; `AA3120 AA1630
AA2038` answered three, two of them landing on 10-21 with slices identical
field for field, where only booking details tell the middle flight's day."""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime
from typing import Any, cast

from fli.models import (  # pyright: ignore[reportMissingTypeStubs] — fli ships no stubs
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
)

from flight_cli import _verify as v
from flight_cli._gflight_ids import GFlightWithId
from flight_cli.domain import Cabin, Pax, SearchOptions
from flight_cli.models import BookingDetailsResult, SearchResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _flight(code: str, frm: str, to: str, dep: str, arr: str) -> v.Flight:
    return v.Flight(code[:2], code[2:], frm, to, dep, arr)


def _row(*slices: tuple[v.Flight, ...], price: str | None = "USD284.00") -> v.Row:
    return v.Row(tuple(slices), price)


def _solution(
    sid: str, price: str, dep: str, arr: str, flights: list[str], stops: list[str]
) -> dict[str, Any]:
    """One `solutionList` entry in the shape Matrix sends it."""
    return {
        "id": sid,
        "ext": {"price": price},
        "displayTotal": price,
        "itinerary": {
            "slices": [
                {
                    "origin": {"code": "JFK"},
                    "destination": {"code": "LAX"},
                    "departure": dep,
                    "arrival": arr,
                    "flights": flights,
                    "stops": [{"code": s} for s in stops],
                }
            ],
            "carriers": [{"code": flights[0][:2]}],
        },
    }


def _answer(*solutions: dict[str, Any]) -> SearchResult:
    return SearchResult.from_api(
        {
            "solutionList": {"solutions": list(solutions)},
            "solutionCount": len(solutions),
            "session": "S",
            "solutionSet": "SET",
        }
    )


# ───────────────────────────── booking details ─────────────────────────────


def test_booking_details_state_every_booked_flight() -> None:
    """Red at the base: `BookingDetails` had no itinerary, and `models.Segment`
    fails on these segments (the number is an int, the aircraft an object)."""
    body = json.loads((FIXTURES / "summarize/booking_details_jfk_lhr_rt_gbp.json").read_text())
    bd = BookingDetailsResult.from_api(body).booking_details
    assert bd is not None and bd.itinerary is not None
    out, back = (s.segments for s in bd.itinerary.slices)
    seg = out[0]
    assert seg.carrier is not None and seg.carrier.code == "AA"
    assert seg.flight is not None and seg.flight.number == "142"
    assert (seg.origin and seg.origin.code, seg.destination and seg.destination.code) == (
        "JFK",
        "LHR",
    )
    assert seg.departure == "2026-10-20T09:35-04:00"
    assert [leg.departure for leg in seg.legs] == ["2026-10-20T09:35-04:00"]
    ret = back[0]
    assert ret.flight is not None and ret.flight.number == "107"
    assert ret.departure == "2026-10-27T15:35+00:00"
    assert v.booked_flights(bd.itinerary) == (
        (_flight("AA142", "JFK", "LHR", "2026-10-20T09:35", "2026-10-20T21:40"),),
        (_flight("AA107", "LHR", "JFK", "2026-10-27T15:35", "2026-10-27T19:35"),),
    )


def test_a_segment_matrix_sends_without_its_fields_still_parses() -> None:
    """`--fare-rules` reads the same body, so a field Matrix leaves out of a
    booked segment must not fail it."""
    bd = BookingDetailsResult.from_api(
        {"bookingDetails": {"itinerary": {"slices": [{"segments": [{}]}]}}}
    ).booking_details
    assert bd is not None and bd.itinerary is not None
    assert v.booked_flights(bd.itinerary) == ((v.Flight("", "", "", "", "", ""),),)


# ──────────────────────────────── the query ─────────────────────────────────


def test_a_chain_names_each_flight_once_and_folds_a_through_flight() -> None:
    assert v.routing(["XX1", "XX1"]) == "XX1"
    assert v.routing(["AS21", "AS487"]) == "AS21 AS487"
    assert v.routing(["AA1", "AA1", "AA2", "AA1"]) == "AA1 AA2 AA1"


def test_a_round_trip_asks_two_legs_each_its_own_chain() -> None:
    row = _row(
        (
            _flight("AS21", "JFK", "SEA", "2026-10-20T07:22", "2026-10-20T10:31"),
            _flight("AS487", "SEA", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
        ),
        (_flight("DL747", "LAX", "JFK", "2026-10-27T23:59", "2026-10-28T08:10"),),
        price="EUR250.00",
    )
    legs = v.matrix_legs(row)
    assert [(lg.origins, lg.destinations, lg.date, lg.route_language) for lg in legs] == [
        (("JFK",), ("LAX",), date(2026, 10, 20), "AS21 AS487"),
        (("LAX",), ("JFK",), date(2026, 10, 27), "DL747"),
    ]
    assert all(lg.extension is None and not lg.time_ranges for lg in legs)
    asked = SearchOptions(
        cabin=Cabin.BUSINESS,
        pax=Pax(adults=2),
        max_extra_stops=0,
        page_size=1,
        max_price=900,
        currency="USD",
    )
    opts = v.matrix_options(row, asked)
    # The search's cabin and travelers in the row's currency; no cap, bags or
    # stop limit, and the default page so the chain's answer is read whole.
    assert opts == SearchOptions(cabin=Cabin.BUSINESS, pax=Pax(adults=2), currency="EUR")
    assert opts.page_size == SearchOptions().page_size
    assert [lg.route_language for lg in v.matrix_legs(row, routed=False)] == [None, None]


def test_the_google_row_is_read_from_fli_on_the_airports_wall_clock() -> None:
    day = date(2026, 10, 20)

    def leg(number: str, frm: str, to: str, leaves: str, lands: str) -> FlightLeg:
        return FlightLeg(
            airline=Airline["AS"],
            flight_number=number,
            departure_airport=Airport[frm],
            arrival_airport=Airport[to],
            departure_datetime=datetime.fromisoformat(f"{day}T{leaves}"),
            arrival_datetime=datetime.fromisoformat(f"{day}T{lands}"),
            duration=60,
        )

    flight = FlightResult(
        price=284.0,
        currency="USD",
        duration=612,
        stops=1,
        legs=[
            leg("21", "JFK", "SEA", "07:22", "10:31"),
            leg("487", "SEA", "LAX", "12:05", "14:34"),
        ],
    )
    row = v.google_row(GFlightWithId(flight=flight, flight_id="", amenities=[]))
    assert row == _row(
        (
            _flight("AS21", "JFK", "SEA", "2026-10-20T07:22", "2026-10-20T10:31"),
            _flight("AS487", "SEA", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
        )
    )


# ─────────────────────────── candidates (L2, L4) ────────────────────────────

_L2 = _answer(
    _solution(
        "L2-1",
        "USD284.00",
        "2026-10-20T07:22-04:00",
        "2026-10-20T14:34-07:00",
        ["AS21", "AS487"],
        ["SEA"],
    ),
    _solution(
        "L2-2",
        "USD542.00",
        "2026-10-20T07:22-04:00",
        "2026-10-21T14:34-07:00",
        ["AS21", "AS487"],
        ["SEA"],
    ),
)


def test_l2_each_landing_day_has_its_own_solution() -> None:
    same_day = _row(
        (
            _flight("AS21", "JFK", "SEA", "2026-10-20T07:22", "2026-10-20T10:31"),
            _flight("AS487", "SEA", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
        )
    )
    next_day = _row(
        (
            _flight("AS21", "JFK", "SEA", "2026-10-20T07:22", "2026-10-20T10:31"),
            _flight("AS487", "SEA", "LAX", "2026-10-21T12:05", "2026-10-21T14:34"),
        ),
        price="USD542.00",
    )
    assert v.candidates(same_day, _L2) == [0]
    assert v.candidates(next_day, _L2) == [1]


def test_a_solution_differing_in_a_stop_or_an_end_is_no_candidate() -> None:
    row = _row(
        (
            _flight("AS21", "JFK", "PDX", "2026-10-20T07:22", "2026-10-20T10:31"),
            _flight("AS487", "PDX", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
        )
    )
    assert v.candidates(row, _L2) == []
    late = _row(
        (
            _flight("AS21", "JFK", "SEA", "2026-10-20T07:23", "2026-10-20T10:31"),
            _flight("AS487", "SEA", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
        )
    )
    assert v.candidates(late, _L2) == []


_L4 = _answer(
    _solution(
        "L4-1",
        "USD453.00",
        "2026-10-20T17:06-04:00",
        "2026-10-20T23:51-07:00",
        ["AA3120", "AA1630", "AA2038"],
        ["CLT", "DFW"],
    ),
    _solution(
        "L4-2",
        "USD691.00",
        "2026-10-20T17:06-04:00",
        "2026-10-21T23:51-07:00",
        ["AA3120", "AA1630", "AA2038"],
        ["CLT", "DFW"],
    ),
    _solution(
        "L4-3",
        "USD913.00",
        "2026-10-20T17:06-04:00",
        "2026-10-21T23:51-07:00",
        ["AA3120", "AA1630", "AA2038"],
        ["CLT", "DFW"],
    ),
)


def _segment(code: str, frm: str, to: str, dep: str, arr: str) -> dict[str, Any]:
    """A booked segment in the shape the captured booking details send."""
    leg = {
        "origin": {"code": frm},
        "destination": {"code": to},
        "departure": dep,
        "arrival": arr,
        "aircraft": {"shortName": "Airbus A321"},
    }
    return {
        "carrier": {"code": code[:2]},
        "flight": {"number": int(code[2:])},
        "origin": {"code": frm},
        "destination": {"code": to},
        "departure": dep,
        "arrival": arr,
        "legs": [leg],
        "bookingInfos": [{"bookingCode": "O", "cabin": "COACH"}],
    }


def _l4_details(middle_day: str) -> BookingDetailsResult:
    """L4's 10-21 trip, its middle flight on `middle_day`."""
    return BookingDetailsResult.from_api(
        {
            "bookingDetails": {
                "itinerary": {
                    "slices": [
                        {
                            "segments": [
                                _segment(
                                    "AA3120",
                                    "JFK",
                                    "CLT",
                                    "2026-10-20T17:06-04:00",
                                    "2026-10-20T19:15-04:00",
                                ),
                                _segment(
                                    "AA1630",
                                    "CLT",
                                    "DFW",
                                    f"{middle_day}T19:50-04:00",
                                    f"{middle_day}T21:45-05:00",
                                ),
                                _segment(
                                    "AA2038",
                                    "DFW",
                                    "LAX",
                                    "2026-10-21T22:10-05:00",
                                    "2026-10-21T23:51-07:00",
                                ),
                            ]
                        }
                    ]
                }
            }
        }
    )


def _l4_row(middle_day: str) -> v.Row:
    return _row(
        (
            _flight("AA3120", "JFK", "CLT", "2026-10-20T17:06", "2026-10-20T19:15"),
            _flight("AA1630", "CLT", "DFW", f"{middle_day}T19:50", f"{middle_day}T21:45"),
            _flight("AA2038", "DFW", "LAX", "2026-10-21T22:10", "2026-10-21T23:51"),
        ),
        price="USD913.00",
    )


def test_l4_two_candidates_and_the_flight_check_keeps_the_one_with_the_rows_middle_day() -> None:
    row = _l4_row("2026-10-21")
    assert v.candidates(row, _L4) == [1, 2]
    over_dfw = _l4_details("2026-10-20").booking_details
    over_clt = _l4_details("2026-10-21").booking_details
    assert over_dfw is not None and over_dfw.itinerary is not None
    assert over_clt is not None and over_clt.itinerary is not None
    assert not v.same_flights(row, over_dfw.itinerary)
    assert v.same_flights(row, over_clt.itinerary)
    assert v.same_flights(_l4_row("2026-10-20"), over_dfw.itinerary)


def test_the_flight_check_reads_each_flights_minute_and_airports() -> None:
    row = _l4_row("2026-10-21")
    details = _l4_details("2026-10-21").booking_details
    assert details is not None and details.itinerary is not None
    itinerary = details.itinerary
    seg = itinerary.slices[0].segments[1]
    moved = itinerary.model_copy(deep=True)
    moved.slices[0].segments[1].legs[0].departure = "2026-10-21T19:55-04:00"
    assert not v.same_flights(row, moved)
    elsewhere = itinerary.model_copy(deep=True)
    elsewhere.slices[0].segments[1].legs[0].origin = None
    assert not v.same_flights(row, elsewhere)
    assert seg.flight is not None and seg.flight.number == "1630"


def test_a_through_flight_matches_whichever_side_splits_it() -> None:
    whole = _row((_flight("XX1", "JFK", "LAX", "2026-10-20T08:00", "2026-10-20T13:00"),))
    split = _row(
        (
            _flight("XX1", "JFK", "DEN", "2026-10-20T08:00", "2026-10-20T10:00"),
            _flight("XX1", "DEN", "LAX", "2026-10-20T11:00", "2026-10-20T13:00"),
        )
    )
    seg = _segment("XX1", "JFK", "LAX", "2026-10-20T08:00-04:00", "2026-10-20T13:00-07:00")
    seg["legs"] = [
        {
            "origin": {"code": "JFK"},
            "destination": {"code": "DEN"},
            "departure": "2026-10-20T08:00-04:00",
            "arrival": "2026-10-20T10:00-06:00",
        },
        {
            "origin": {"code": "DEN"},
            "destination": {"code": "LAX"},
            "departure": "2026-10-20T11:00-06:00",
            "arrival": "2026-10-20T13:00-07:00",
        },
    ]
    details = BookingDetailsResult.from_api(
        {"bookingDetails": {"itinerary": {"slices": [{"segments": [seg]}]}}}
    ).booking_details
    assert details is not None and details.itinerary is not None
    assert v.same_flights(whole, details.itinerary)
    assert v.same_flights(split, details.itinerary)
    later = _row(
        (
            _flight("XX1", "JFK", "DEN", "2026-10-20T08:00", "2026-10-20T10:00"),
            _flight("XX1", "DEN", "LAX", "2026-10-21T11:00", "2026-10-21T13:00"),
        )
    )
    assert not v.same_flights(later, details.itinerary)
    assert v.routings(split) == ["XX1"]


# ────────────────────────── no fare: the probe ─────────────────────────────


def _probe(*carriers: str) -> SearchResult:
    """An unrouted answer listing `carriers`, in the L3 shape: carrier-stop
    matrix columns, the carrier filter, and one itinerary per carrier."""
    return SearchResult.from_api(
        {
            "carrierStopMatrix": {"columns": [{"label": {"code": c}} for c in carriers]},
            "itineraryCarrierList": {"groups": [{"label": {"code": c}} for c in carriers]},
            "solutionList": {
                "solutions": [
                    _solution(
                        f"P{i}",
                        "USD99.00",
                        "2026-10-20T07:00-07:00",
                        "2026-10-20T08:10-07:00",
                        [f"{c}100"],
                        [],
                    )
                    for i, c in enumerate(carriers)
                ]
            },
        }
    )


_WN = _row(
    (_flight("WN1234", "LAS", "LAX", "2026-10-20T07:00", "2026-10-20T08:10"),), price="USD99.00"
)


def test_a_carrier_the_route_lists_nowhere_is_named_absent() -> None:
    verdict = v.unpriced(_WN, _probe("AA", "DL", "F9", "XE", "UA"))
    assert verdict.outcome == "carrier-absent"
    assert verdict.missing_carriers == ("WN",)
    assert verdict.reason is not None
    assert "WN" in verdict.reason
    assert "AA, DL, F9, UA, XE" in verdict.reason
    assert "at most 0 stops" in verdict.reason


def test_a_carrier_the_route_lists_is_no_solution() -> None:
    verdict = v.unpriced(_WN, _probe("AA", "WN"))
    assert verdict.outcome == "no-solution"
    assert verdict.missing_carriers == ()


def test_an_empty_probe_proves_no_carrier_absent() -> None:
    verdict = v.unpriced(_WN, SearchResult.from_api({"solutionList": {"solutions": []}}))
    assert verdict.outcome == "no-solution"
    assert verdict.missing_carriers == ()


def test_the_probe_admits_the_rows_most_stops() -> None:
    row = _l4_row("2026-10-20")
    assert v.most_stops(row) == 2
    opts = v.matrix_options(row, SearchOptions(), max_stops=v.most_stops(row))
    assert opts.max_extra_stops == 2


# ─────────────────────────────── the document ───────────────────────────────


def test_no_match_carries_no_matrix_price() -> None:
    doc = v.document(3, _l4_row("2026-10-21"), v.other_itinerary(2), None)
    assert doc["outcome"] == "other-itinerary"
    assert doc["matrix"] is None
    assert doc["delta"] is None
    assert doc["fares"] == []
    assert doc["fare_rules"] is None
    assert "691" not in json.dumps(doc)
    assert doc["google"]["slices"][0]["dates"] == ["2026-10-20", "2026-10-21", "2026-10-21"]


def test_a_match_states_both_sides_and_google_minus_matrix() -> None:
    row = _l4_row("2026-10-21")
    details = _l4_details("2026-10-21").booking_details
    verdict = v.Verdict("match", solution=_L4.solutions[2], details=details)
    doc = v.document(2, row, verdict, {"rules": []})
    assert doc["row"] == 2
    assert doc["routing"] == ["AA3120 AA1630 AA2038"]
    matrix = cast("dict[str, Any]", doc["matrix"])
    assert matrix["price"] == "USD913.00"
    assert matrix["slices"] == doc["google"]["slices"]
    assert matrix["slices"][0]["airports"] == [["JFK", "CLT"], ["CLT", "DFW"], ["DFW", "LAX"]]
    assert doc["delta"] == 0.0
    assert doc["fares"] == []
    assert doc["fare_rules"] == {"rules": []}


def test_the_delta_is_google_minus_matrix_and_none_across_currencies() -> None:
    assert v.delta("USD300.00", "USD284.00") == 16.0
    assert v.delta("USD284.00", "USD300.00") == -16.0
    assert v.delta("EUR284.00", "USD284.00") is None
    assert v.delta(None, "USD284.00") is None
