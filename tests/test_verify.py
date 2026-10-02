# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_fare_rules.py follows; the constructor has no transport
# injection point.
"""`search --verify`: one Google row priced on Matrix as exactly that itinerary.

The Matrix answers below are the ones measured for JFK-LAX on 2026-10-20 with
a routing chain of the row's flights. `AS21 AS487` answered two solutions with
the same flights and departure, one landing a day later; `AA3120 AA1630
AA2038` answered three, two of them landing on 10-21 with slices identical
field for field, where only booking details tell the middle flight's day."""

from __future__ import annotations

import io
import json
import pathlib
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from fli.models import (  # pyright: ignore[reportMissingTypeStubs] — fli ships no stubs
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
)
from rich.console import Console
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import _verify as v
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli._gflight_ids import GFlightWithId
from flight_cli.client import MatrixClient
from flight_cli.domain import Cabin, Pax, SearchOptions
from flight_cli.models import BookingDetailsResult, SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

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


# ─────────────────────────────── the command ────────────────────────────────
#
# Google serves the captured JFK-LAX board re-dated to `_DEP`; Matrix is a
# MockTransport behind the real client, answering a routed search with
# `chain`, an unrouted one with `probe`, and booking details by solution id.

_DEP = date.today() + timedelta(days=45)
_URL = "https://www.google.com/travel/flights?tfs=abc"
_SEARCH = [
    "search",
    "JFK",
    "LAX",
    "--dep",
    _DEP.isoformat(),
    "--cash-only",
    "--no-google-url",
    "--no-matrix-url",
]


def _served() -> str:
    return _page(
        _answering(_ds1("ds1_jfk_lax_tfu.json"), origin=None, destination=None, date=str(_DEP))
    )


def _booked(row: Any) -> str:
    return "+".join(f"{leg.airline.name}{leg.flight_number}" for leg in row.flight.legs)


def _as_row() -> tuple[int, Any]:
    """The AS21/AS487 row of the served board and its number in the list the
    table numbers: a one-way board keeps Google's order."""
    rows = list(gfid._rows_from_page_html(PageFetch(_served(), _URL, 200)))
    n = next(i for i, r in enumerate(rows, 1) if _booked(r) == "AS21+AS487")
    return n, rows[n - 1]


def _stamp(at: datetime, offset: str) -> str:
    return f"{at.isoformat(timespec='minutes')}{offset}"


def _row_solution(sid: str, price: str, row: Any, *, lands_later: int = 0) -> dict[str, Any]:
    """A Matrix solution with the row's flights, departing when it does and
    landing `lands_later` days after it."""
    legs = row.flight.legs
    return _solution(
        sid,
        price,
        _stamp(legs[0].departure_datetime, "-05:00"),
        _stamp(legs[-1].arrival_datetime + timedelta(days=lands_later), "-08:00"),
        [f"{leg.airline.name}{leg.flight_number}" for leg in legs],
        [leg.arrival_airport.name for leg in legs[:-1]],
    )


def _chain(*solutions: dict[str, Any]) -> dict[str, Any]:
    return {
        "solutionList": {"solutions": list(solutions)},
        "solutionCount": len(solutions),
        "session": "S",
        "solutionSet": "SET",
    }


def _details_of(row: Any, *, later: dict[int, int] | None = None) -> dict[str, Any]:
    """Booking details for the row's flights in the captured shape, leg `i`
    flown `later[i]` days after the row flies it."""
    later = later or {}
    segments = [
        _segment(
            f"{leg.airline.name}{leg.flight_number}",
            leg.departure_airport.name,
            leg.arrival_airport.name,
            _stamp(leg.departure_datetime + timedelta(days=later.get(i, 0)), "-05:00"),
            _stamp(leg.arrival_datetime + timedelta(days=later.get(i, 0)), "-08:00"),
        )
        for i, leg in enumerate(row.flight.legs)
    ]
    carrier = row.flight.legs[0].airline.name
    return {
        "bookingDetails": {
            "displayTotal": "USD213.20",
            "itinerary": {"slices": [{"segments": segments}]},
            "tickets": [
                {
                    "pricings": [
                        {
                            "fares": [
                                {
                                    "key": "0/0",
                                    "carrier": carrier,
                                    "code": "QH7OAVBN",
                                    "bookingInfos": [
                                        {
                                            "segment": {
                                                "origin": s["origin"]["code"],
                                                "destination": s["destination"]["code"],
                                            },
                                            "bookingCode": "Q",
                                            "cabin": "COACH",
                                        }
                                        for s in segments
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ],
        }
    }


class _Matrix:
    """Matrix over a MockTransport. A search is answered with no more
    solutions than its page asks for, as Matrix answers it."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.chain: dict[str, Any] = _chain()
        self.probe: dict[str, Any] = _chain()
        self.details: dict[str, dict[str, Any]] = {}
        self.error: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        self.bodies.append(body)
        if self.error is not None:
            return httpx.Response(200, json=self.error)
        if request.url.path == "/v1/search":
            routed = any(s.get("routeLanguage") for s in body["inputs"]["slices"])
            answer = json.loads(json.dumps(self.chain if routed else self.probe))
            sols = answer["solutionList"]["solutions"]
            answer["solutionList"]["solutions"] = sols[: body["inputs"]["page"]["size"]]
            return httpx.Response(200, json=answer)
        if body["summarizerSet"] == "viewDetails":
            sid = body["inputs"]["solution"].split("/", 1)[1]
            return httpx.Response(200, json=self.details[sid])
        return httpx.Response(
            200,
            json=json.loads((FIXTURES / "summarize/fare_rules_jfk_lhr_rt_0_0.json").read_text()),
        )

    def searches(self) -> list[dict[str, Any]]:
        return [b for b in self.bodies if "name" in b]

    def summarized(self) -> list[tuple[str, str]]:
        return [
            (b["summarizerSet"], b["inputs"]["solution"].split("/", 1)[1])
            for b in self.bodies
            if "name" not in b
        ]


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()

    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: val for k, val in kw.items() if k == "impersonate"},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)
    return fake


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli.app, [*_SEARCH, *args])


def test_the_rows_own_itinerary_verifies_with_its_fares(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Red at the base: exit 2, no such option."""
    n, row = _as_row()
    price = f"USD{row.flight.price:.2f}"
    matrix.chain = _chain(
        _row_solution("AS-1", price, row), _row_solution("AS-2", "USD542.00", row, lands_later=1)
    )
    matrix.details = {"AS-1": _details_of(row)}
    gf_session(_served())
    result = _run("-n", "40", "--format", "json", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    listed = doc["search"][n - 1]
    assert [leg["flight_number"] for leg in listed["legs"]] == ["21", "487"]
    verdict = doc["verify"]
    assert verdict["row"] == n
    assert verdict["outcome"] == "match"
    assert verdict["reason"] is None
    assert verdict["routing"] == ["AS21 AS487"]
    google = verdict["google"]["slices"]
    assert google[0]["flights"] == ["AS21", "AS487"]
    assert google[0]["dates"] == [leg["departure_datetime"][:10] for leg in listed["legs"]]
    assert google[0]["airports"] == [["JFK", "SEA"], ["SEA", "LAX"]]
    assert verdict["google"]["price"] == price
    assert verdict["matrix"] == {"price": price, "total": "USD213.20", "slices": google}
    assert verdict["delta"] == 0.0
    assert verdict["missing_carriers"] == []
    assert [(f["fare_basis"], f["booking_code"]) for f in verdict["fares"]] == [
        ("QH7OAVBN", "Q"),
        ("QH7OAVBN", "Q"),
    ]
    assert verdict["fare_rules"]["itinerary"] == n
    assert verdict["fare_rules"]["rules"]
    # One search: one slice on the row's day and airports, routed by its
    # flights alone, in its currency and with the default page.
    (search,) = matrix.searches()
    (leg,) = search["inputs"]["slices"]
    assert (leg["origins"], leg["destinations"], leg["date"]) == (["JFK"], ["LAX"], str(_DEP))
    assert leg["routeLanguage"] == "AS21 AS487"
    assert "commandLine" not in leg
    assert search["inputs"]["page"]["size"] == SearchOptions().page_size
    assert search["inputs"]["currency"] == "USD"
    assert matrix.summarized() == [("viewDetails", "AS-1"), ("viewRules", "AS-1")]


def test_table_mode_prints_the_check_after_the_google_table(
    gf_session: Callable[..., Any], matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_enrichment(**_kw: Any) -> None:
        raise AssertionError("the table --verify numbers is Google's")

    monkeypatch.setattr(cli, "_run_enriched_path", _no_enrichment)
    n, row = _as_row()
    price = f"USD{row.flight.price:.2f}"
    matrix.chain = _chain(_row_solution("AS-1", price, row))
    matrix.details = {"AS-1": _details_of(row)}
    gf_session(_served())
    result = _run("-n", "40", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "No Matrix enrichment: --verify asks Matrix about one row instead." in result.stderr
    head = f"Verified on Matrix · itinerary #{n:d} · AS21 AS487 {_DEP}"
    assert (
        out.index("Google Flights") < out.index(head) < out.index(f"Fare rules · itinerary #{n:d}")
    )
    assert f"Matrix {price} · Google {price} · same price" in out
    assert "JFK→SEA  AS  fare basis QH7OAVBN  booking code Q  COACH" in out


def test_enrich_json_writes_the_verify_document_not_the_cross_check(
    gf_session: Callable[..., Any], matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_enrichment(**_kw: Any) -> None:
        raise AssertionError("the document --verify writes holds no cross-check")

    monkeypatch.setattr(cli, "_run_enriched_path", _no_enrichment)
    n, row = _as_row()
    matrix.chain = _chain(_row_solution("AS-1", f"USD{row.flight.price:.2f}", row))
    matrix.details = {"AS-1": _details_of(row)}
    gf_session(_served())
    result = _run("-n", "40", "--enrich", "--format", "json", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert set(doc) == {"search", "verify"}
    assert doc["verify"]["outcome"] == "match"
    assert "No Matrix enrichment: --verify asks Matrix about one row instead." in result.stderr


def test_another_days_cheaper_price_is_never_shown(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """The L2 pair, with the other trip first and cheaper: it shares the row's
    flights and departure, and lands a day later."""
    n, row = _as_row()
    price = f"USD{row.flight.price:.2f}"
    matrix.chain = _chain(
        _row_solution("AS-2", "USD150.00", row, lands_later=1), _row_solution("AS-1", price, row)
    )
    matrix.details = {"AS-1": _details_of(row)}
    for fmt in ("table", "json"):
        gf_session(_served())
        result = _run("-n", "40", "--fast", "--verify", "--pick", str(n), "--format", fmt)
        assert result.exit_code == 0, result.output
        assert "150.00" not in result.output
    assert ("viewDetails", "AS-2") not in matrix.summarized()


def _l4_google_row(day: date) -> GFlightWithId:
    """L4's trip landing the next day, its middle flight the next day too."""
    nxt = day + timedelta(days=1)

    def leg(number: str, frm: str, to: str, leaves: datetime, lands: datetime) -> FlightLeg:
        return FlightLeg(
            airline=Airline["AA"],
            flight_number=number,
            departure_airport=Airport[frm],
            arrival_airport=Airport[to],
            departure_datetime=leaves,
            arrival_datetime=lands,
            duration=120,
        )

    flight = FlightResult(
        price=900.0,
        currency="USD",
        duration=3165,
        stops=2,
        legs=[
            leg(
                "3120",
                "JFK",
                "CLT",
                datetime.combine(day, time(17, 6)),
                datetime.combine(day, time(19, 15)),
            ),
            leg(
                "1630",
                "CLT",
                "DFW",
                datetime.combine(nxt, time(19, 50)),
                datetime.combine(nxt, time(21, 45)),
            ),
            leg(
                "2038",
                "DFW",
                "LAX",
                datetime.combine(nxt, time(22, 10)),
                datetime.combine(nxt, time(23, 51)),
            ),
        ],
    )
    return GFlightWithId(flight=flight, flight_id="", amenities=[])


def test_the_l4_row_verifies_on_its_middle_flights_day_under_n_1(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two candidates share every slice field; booking details tell them
    apart. Red before the chain's page was fixed at the default: under `-n 1`
    Matrix answered only the first solution, and the row read as another
    itinerary."""
    row = _l4_google_row(_DEP)

    def _board(*_a: object, **_kw: object) -> list[Any]:
        return [row]

    monkeypatch.setattr(cli, "_gflight_results", _board)
    on_time = _l4_google_row(_DEP)
    on_time.flight.legs[1].departure_datetime -= timedelta(days=1)
    on_time.flight.legs[1].arrival_datetime -= timedelta(days=1)
    matrix.chain = _chain(
        _solution(
            "L4-1",
            "USD453.00",
            _stamp(datetime.combine(_DEP, time(17, 6)), "-04:00"),
            _stamp(datetime.combine(_DEP, time(23, 51)), "-07:00"),
            ["AA3120", "AA1630", "AA2038"],
            ["CLT", "DFW"],
        ),
        _row_solution("L4-2", "USD691.00", row),
        _row_solution("L4-3", "USD913.00", row),
    )
    matrix.details = {"L4-2": _details_of(on_time), "L4-3": _details_of(row)}
    result = _run("-n", "1", "--format", "json", "--verify")
    assert result.exit_code == 0, result.output
    verdict = json.loads(result.stdout)["verify"]
    assert verdict["outcome"] == "match"
    assert verdict["matrix"]["price"] == "USD913.00"
    assert verdict["delta"] == -13.0
    assert verdict["google"]["slices"][0]["dates"] == [
        str(_DEP),
        str(_DEP + timedelta(days=1)),
        str(_DEP + timedelta(days=1)),
    ]
    assert "691" not in result.stdout
    assert matrix.summarized() == [
        ("viewDetails", "L4-2"),
        ("viewDetails", "L4-3"),
        ("viewRules", "L4-3"),
    ]
    table = _run("-n", "1", "--fast", "--verify")
    assert table.exit_code == 0, table.output
    assert "Matrix USD913.00 · Google USD900.00 · Matrix USD13.00 dearer" in table.stdout
    assert "691" not in table.output


def test_flights_priced_only_on_another_day_show_no_matrix_price(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, row = _as_row()
    matrix.chain = _chain(_row_solution("AS-2", "USD542.00", row, lands_later=1))
    gf_session(_served(), _served())
    table = _run("-n", "40", "--fast", "--verify", "--pick", str(n))
    assert table.exit_code == 0, table.output
    assert f"Not verified on Matrix · itinerary #{n:d}: Matrix prices these flights only on 1" in (
        " ".join(table.stdout.split())
    )
    assert "542" not in table.output
    doc = json.loads(_run("-n", "40", "--verify", "--pick", str(n), "--format", "json").stdout)
    verdict = doc["verify"]
    assert verdict["outcome"] == "other-itinerary"
    assert (verdict["matrix"], verdict["delta"], verdict["fares"]) == (None, None, [])
    assert verdict["fare_rules"] is None
    assert matrix.summarized() == []


def _listing(*carriers: str) -> dict[str, Any]:
    return _probe(*carriers).raw or {}


def test_a_carrier_matrix_lists_nowhere_is_named(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, _ = _as_row()
    matrix.probe = _listing("AA", "B6", "DL", "UA")
    gf_session(_served())
    result = _run("-n", "40", "--verify", "--pick", str(n), "--format", "json")
    assert result.exit_code == 0, result.output
    verdict = json.loads(result.stdout)["verify"]
    assert verdict["outcome"] == "carrier-absent"
    assert verdict["missing_carriers"] == ["AS"]
    assert "AS" in verdict["reason"]
    assert verdict["matrix"] is None
    chain, probe = matrix.searches()
    assert chain["inputs"]["slices"][0]["routeLanguage"] == "AS21 AS487"
    (leg,) = probe["inputs"]["slices"]
    assert "routeLanguage" not in leg
    # The row stops once, so the probe admits a carrier listed only with one stop.
    assert leg["commandLine"] == "MAXSTOPS 1"


def test_a_carrier_matrix_lists_is_no_solution(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, _ = _as_row()
    matrix.probe = _listing("AA", "AS")
    gf_session(_served())
    result = _run("-n", "40", "--fast", "--verify", "--pick", str(n))
    assert result.exit_code == 0, result.output
    flat = " ".join(result.stdout.split())
    assert f"Not verified on Matrix · itinerary #{n:d}: Matrix returned no fare" in flat


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_a_matrix_error_exits_1_after_the_table(
    gf_session: Callable[..., Any], matrix: _Matrix, fmt: str
) -> None:
    n, _ = _as_row()
    matrix.error = {"error": {"message": "backend [/x] busy", "type": "INTERNAL"}}
    gf_session(_served())
    result = _run("-n", "40", "--fast", "--verify", "--pick", str(n), "--format", fmt)
    assert result.exit_code == 1, result.output
    assert "Matrix returned an error (INTERNAL): backend [/x] busy" in result.stderr
    if fmt == "json":
        doc = json.loads(result.stdout)
        assert doc["verify"] is None
        assert len(doc["search"]) == 40
    else:
        assert "Google Flights" in result.stdout
        assert "Verified" not in result.stdout


def test_booking_details_without_their_flights_fail_rather_than_claim_another_trip(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    n, row = _as_row()
    matrix.chain = _chain(_row_solution("AS-1", f"USD{row.flight.price:.2f}", row))
    matrix.details = {"AS-1": {"bookingDetails": {}}}
    gf_session(_served())
    result = _run("-n", "40", "--verify", "--pick", str(n), "--format", "json")
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["verify"] is None
    assert "cannot be checked flight by flight" in " ".join(result.stderr.split())


def _no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: object, **_kw: object) -> Any:
        raise AssertionError("refused before any request")

    for name in ("_gflight_results", "_run", "_run_matrix", "MatrixClient", "_run_enriched_path"):
        monkeypatch.setattr(cli, name, _forbidden)


@pytest.mark.parametrize(
    ("args", "said"),
    [
        pytest.param(["--cabin", "y,j"], "--cabin", id="multi-cabin"),
        pytest.param(["--awards-only"], "--awards-only", id="awards-only"),
        pytest.param(["--sellers"], "--sellers", id="sellers"),
        pytest.param(["--fare-rules"], "--fare-rules", id="fare-rules"),
        pytest.param(["--bags", "1"], "--bags", id="bags"),
        pytest.param(["--backend", "matrix"], "runs on Matrix", id="backend-matrix"),
        pytest.param(["--pick", "0"], "--pick 0", id="pick-zero"),
        pytest.param(["--pick", "11"], "--pick 11", id="pick-past-n"),
    ],
)
def test_a_search_with_no_google_row_to_check_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, args: list[str], said: str
) -> None:
    _no_request(monkeypatch)
    result = _run("--verify", *args)
    assert result.exit_code == 2, result.output
    assert said in result.stderr
    assert "Using Matrix" not in result.stderr
    assert result.stdout == ""


def test_awards_json_is_refused_naming_cash_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_request(monkeypatch)

    def _awards_on(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", str(_DEP), "--verify", "--format", "json"],
    )
    assert result.exit_code == 2, result.output
    assert "--cash-only" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--slice", f"JFK-LAX:{_DEP}"], id="slice"),
        pytest.param(["--include-unavailable"], id="matrix-only-constraint"),
    ],
)
def test_a_search_that_runs_on_matrix_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _no_request(monkeypatch)
    result = _run("--verify", *args)
    assert result.exit_code == 2, result.output
    assert "--verify needs a Google Flights row, and this search runs on Matrix." in (
        " ".join(result.stderr.split())
    )
    assert result.stdout == ""


def test_an_empty_board_exits_1_and_a_pick_past_it_2(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[Any] = []

    def _board(*_a: object, **_kw: object) -> list[Any]:
        return list(rows)

    monkeypatch.setattr(cli, "_gflight_results", _board)
    empty = _run("--fast", "--verify", "--format", "json")
    assert empty.exit_code == 1, empty.output
    assert "no itinerary to check" in empty.stderr
    assert empty.stdout == ""
    rows.append(_l4_google_row(_DEP))
    short = _run("--fast", "--verify", "--pick", "3")
    assert short.exit_code == 2, short.output
    assert "--pick 3 is out of range (1-1); --verify checks that row." in short.stderr
    assert matrix.bodies == []


def test_without_the_flag_the_search_is_the_base_and_asks_matrix_nothing(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Green at the base."""

    def _forbidden(*_a: object, **_kw: object) -> Any:
        raise AssertionError("no Matrix request without --verify")

    monkeypatch.setattr(cli, "MatrixClient", _forbidden)
    gf_session(_served(), _served())
    doc = json.loads(_run("-n", "40", "--backend", "gflight", "--format", "json").stdout)
    assert isinstance(doc, list)
    assert len(cast("list[Any]", doc)) == 40
    table = _run("-n", "40", "--fast")
    assert table.exit_code == 0, table.output
    assert "Verified" not in table.output
    assert "Matrix" not in table.stderr


# ─────────────────────────── what a run says as it waits ───────────────────────────


def _flat(text: str) -> str:
    return " ".join(text.split())


def _stderr_at_each_search(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> tuple[io.StringIO, list[tuple[bool, str]]]:
    """Stderr, and per Matrix search as it arrives: whether it is routed, and
    what stderr held when it was sent."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=200, no_color=True))
    seen: list[tuple[bool, str]] = []
    real = matrix.handler

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            body = json.loads(request.content)
            routed = any(s.get("routeLanguage") for s in body["inputs"]["slices"])
            seen.append((routed, buf.getvalue()))
        return real(request)

    monkeypatch.setattr(matrix, "handler", _handler)
    return buf, seen


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_stderr_names_the_row_before_matrix_is_asked_for_it(
    gf_session: Callable[..., Any], matrix: _Matrix, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Red at the base: stderr is empty when the chain search is sent."""
    buf, seen = _stderr_at_each_search(matrix, monkeypatch)
    n, row = _as_row()
    matrix.chain = _chain(_row_solution("AS-1", f"USD{row.flight.price:.2f}", row))
    matrix.details = {"AS-1": _details_of(row)}
    gf_session(_served())
    result = _run("-n", "40", "--fast", "--verify", "--pick", str(n), "--format", fmt)
    assert result.exit_code == 0, result.output
    ((routed, before),) = seen
    assert routed
    asking = f"Asking Matrix for itinerary #{n:d}: AS21 AS487 {_DEP}…"
    assert _flat(before) == asking
    # Booking details and fare rules take a second or two and say nothing.
    assert _flat(buf.getvalue()) == asking


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_stderr_says_why_matrix_is_asked_again_before_the_probe(
    gf_session: Callable[..., Any], matrix: _Matrix, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Red at the base: stderr is empty when each search is sent."""
    _, seen = _stderr_at_each_search(matrix, monkeypatch)
    n, _ = _as_row()
    matrix.probe = _listing("AA", "B6", "DL", "UA")
    gf_session(_served())
    result = _run("-n", "40", "--fast", "--verify", "--pick", str(n), "--format", fmt)
    assert result.exit_code == 0, result.output
    (chain, before_chain), (probe, before_probe) = seen
    assert (chain, probe) == (True, False)
    assert f"itinerary #{n:d}: AS21 AS487" in before_chain
    assert before_probe.startswith(before_chain)
    assert _flat(before_probe[len(before_chain) :]) == (
        "Matrix has no fare on those flights; asking which carriers it lists…"
    )


# ───────────────────── an empty board under --verify or --sellers ─────────────────────

_FILTERED = "Google Flights: no itinerary matched a carrier filter (AS) (7 rows filtered out)."


def _board(monkeypatch: pytest.MonkeyPatch, board: gfid.Board[Any]) -> None:
    def _served_board(*_a: object, **_kw: object) -> gfid.Board[Any]:
        return board

    monkeypatch.setattr(cli, "_gflight_results", _served_board)


@pytest.mark.parametrize(
    ("flag", "fmt", "said"),
    [
        pytest.param("--verify", "table", "the search returned no itinerary to check.", id="verify"),
        pytest.param(
            "--verify", "json", "the search returned no itinerary to check.", id="verify-json"
        ),
        pytest.param("--sellers", "table", "the search returned no itinerary to open.", id="sellers"),
        pytest.param(
            "--sellers", "json", "the search returned no itinerary to open.", id="sellers-json"
        ),
    ],
)
def test_a_filtered_board_says_why_before_the_flag_says_it_has_no_row(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch, flag: str, fmt: str, said: str
) -> None:
    """Red at the base: the flag exited first and the filter's reason never
    printed."""
    _board(monkeypatch, gfid.Board(dropped=7))
    result = _run("--routing", "AS+", "--backend", "gflight", "--fast", flag, "--format", fmt)
    assert result.exit_code == 1, result.output
    err = _flat(result.stderr)
    assert _FILTERED in err
    assert err.index(_FILTERED) < err.index(said)
    assert result.stdout == ""
    assert matrix.bodies == []


def test_a_board_empty_under_the_cap_says_so_before_verify_exits(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base: the cap line never printed."""
    _board(monkeypatch, gfid.Board())
    result = _run("--backend", "gflight", "--fast", "--max-price", "1", "--verify")
    assert result.exit_code == 1, result.output
    assert _flat(result.stdout) == "Google Flights: no fare at or under USD 1."
    assert "the search returned no itinerary to check." in _flat(result.stderr)
    json_run = _run("--backend", "gflight", "--max-price", "1", "--verify", "--format", "json")
    assert json_run.exit_code == 1, json_run.output
    assert json_run.stdout == ""
    assert matrix.bodies == []


def test_a_round_trip_names_its_pinned_outbounds_before_verify_exits(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red at the base: the pinned reason never printed."""
    _board(monkeypatch, gfid.Board(dropped=4, pinned=3))
    ret = (_DEP + timedelta(days=7)).isoformat()
    result = _run(
        "--routing", "AS+", "--backend", "gflight", "--fast", "--return", ret, "--verify"
    )
    assert result.exit_code == 1, result.output
    assert (
        "Google Flights: no round trip matched a carrier filter (AS) (4 rows filtered out; "
        "returns were searched for the 3 cheapest outbound options)."
    ) in _flat(result.stderr)
    assert matrix.bodies == []


def test_without_either_flag_an_empty_board_answers_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base."""
    _board(monkeypatch, gfid.Board(dropped=7))
    table = _run("--routing", "AS+", "--backend", "gflight", "--fast")
    assert (table.exit_code, table.stdout, _flat(table.stderr)) == (0, "", _FILTERED)
    doc = _run("--routing", "AS+", "--backend", "gflight", "--format", "json")
    assert (doc.exit_code, json.loads(doc.stdout), _flat(doc.stderr)) == (0, [], _FILTERED)
    _board(monkeypatch, gfid.Board())
    capped = _run("--backend", "gflight", "--fast", "--max-price", "1")
    assert (capped.exit_code, _flat(capped.stdout), capped.stderr) == (
        0,
        "Google Flights: no fare at or under USD 1.",
        "",
    )

