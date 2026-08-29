# pyright: reportPrivateUsage=false
# DIVERGE: these pin wire-format contracts of the SPA slice builder, which is
# module-internal by design. Exporting it to satisfy the rule would widen the
# API for a test.
"""Regression tests for `matrix_deep_link()` — the SPA URL-state encoder.

Round-trip ground truth was captured 2026-05-19 by driving Matrix's SPA via
research/record_user_session.py (see fixture headers for the exact command).
If the SPA changes its URL state schema again, the byte-exact test fails
and we know to recapture.

The schema drift this set of tests locks down:
  Round-trip = ONE slice with both `departureDate` and `returnDate`
               (NOT two slices each carrying one direction's date — that
               legacy form silently misrenders today's SPA's return field)."""

from __future__ import annotations

import base64
import json
import pathlib
import urllib.parse
from datetime import date
from typing import Any, cast

from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
    SpecificDateSearch,
)
from flight_cli.links import extract_pin_segments_from_slice, matrix_deep_link, matrix_itinerary_url
from flight_cli.models import Slice, SliceEndpoint

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "matrix_url"


def _read_fixture_url(name: str) -> str:
    text = (FIXTURE_DIR / name).read_text()
    for line in text.splitlines():
        if line.startswith("https://"):
            return line.strip()
    raise AssertionError(f"no https URL line in fixture {name}")


def _decode_search(url: str) -> dict[str, Any]:
    qs = urllib.parse.urlparse(url).query
    b64 = urllib.parse.parse_qs(qs)["search"][0]
    return cast("dict[str, Any]", json.loads(base64.b64decode(b64)))


def test_round_trip_byte_exact_matches_spa_capture() -> None:
    """Generated round-trip URL is byte-identical to the SPA's own emission.

    Captured by driving the SPA with HNL→MIA, dep 2026-10-14, ret 2026-10-24,
    economy, 1 adult, no other knobs."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("HNL",), destinations=("MIA",), date=date(2026, 10, 14)),
            Leg(origins=("MIA",), destinations=("HNL",), date=date(2026, 10, 24)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    expected = _read_fixture_url("spa_round_trip_hnl_mia.txt")
    assert matrix_deep_link(s) == expected


def test_round_trip_emits_single_slice_with_return_date() -> None:
    """Structural check: round-trip emits ONE slice carrying both dates.
    Pre-2026-05 SPA accepted two slices; today's SPA renders that as a
    multi-city and corrupts the return date field."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 8, 15)),
            Leg(origins=("LHR",), destinations=("JFK",), date=date(2026, 8, 22)),
        ),
        options=SearchOptions(cabin=Cabin.BUSINESS, pax=Pax(adults=2)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "round-trip"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 1
    only = slices[0]
    assert only["origin"] == ["JFK"]
    assert only["dest"] == ["LHR"]
    dates: dict[str, Any] = only["dates"]
    assert dates["departureDate"] == "2026-08-15"
    assert dates["returnDate"] == "2026-08-22"


def test_one_way_emits_single_slice_with_empty_return_date() -> None:
    s = SpecificDateSearch(
        legs=(Leg(origins=("SFO",), destinations=("NRT",), date=date(2026, 11, 3)),),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "one-way"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 1
    only = slices[0]
    dates: dict[str, Any] = only["dates"]
    assert dates["departureDate"] == "2026-11-03"
    assert dates["returnDate"] == ""


def test_matrix_itinerary_url_byte_exact_matches_spa_capture() -> None:
    """Pinned `/itinerary` URL is byte-identical to the SPA's emission.

    Captured by driving the SPA: fill HNL->MIA round-trip 10/14-10/24,
    click Search, click the cheapest `$680` result row -> SPA navigates
    to `/itinerary?search=...` with a `solution` block carrying the
    server-generated identifiers."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("HNL",), destinations=("MIA",), date=date(2026, 10, 14)),
            Leg(origins=("MIA",), destinations=("HNL",), date=date(2026, 10, 24)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    url = matrix_itinerary_url(
        s,
        solution_id="LW2fnBXaw5TMrLyYxZfSj4001",
        session="v1K0kQfukbERvMxYF6uY3vqFf",
        solution_set="0BjvHzTSqMX6RLaj0Mc4UJ7",
    )
    expected = _read_fixture_url("spa_pinned_hnl_mia.txt")
    assert url == expected


def test_matrix_itinerary_url_rejects_calendar_search() -> None:
    """Calendar searches don't produce itinerary rows, so they can't be
    pinned; calling the builder is a programming error rather than a
    silent fallback."""
    cal = CalendarSearch(
        legs=(Leg(origins=("JFK",), destinations=("LHR",)),),
        window=CalendarWindow(
            start=date(2026, 9, 1),
            end=date(2026, 9, 15),
            duration_min=5,
            duration_max=7,
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    try:
        matrix_itinerary_url(cal, solution_id="x", session="y", solution_set="z")
    except TypeError as e:
        assert "SpecificDateSearch" in str(e)
    else:
        msg = "expected TypeError for calendar search"
        raise AssertionError(msg)


def test_multi_city_keeps_one_slice_per_leg() -> None:
    """3-leg multi-city = 3 slices each with its own departureDate.
    Captured SPA fixture confirms the SPA emits this same shape today."""
    s = SpecificDateSearch(
        legs=(
            Leg(origins=("SFO",), destinations=("JFK",), date=date(2026, 9, 1)),
            Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 9, 5)),
            Leg(origins=("LHR",), destinations=("SFO",), date=date(2026, 9, 12)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    payload = _decode_search(matrix_deep_link(s))
    assert payload["type"] == "multi-city"
    slices: list[dict[str, Any]] = payload["slices"]
    assert len(slices) == 3
    for slc, (o, d, iso) in zip(
        slices,
        [("SFO", "JFK", "2026-09-01"), ("JFK", "LHR", "2026-09-05"), ("LHR", "SFO", "2026-09-12")],
        strict=True,
    ):
        assert slc["origin"] == [o]
        assert slc["dest"] == [d]
        dates: dict[str, Any] = slc["dates"]
        assert dates["departureDate"] == iso
        # Multi-city slices keep returnDate empty — our benign superset.
        assert dates["returnDate"] == ""


# ───────── pinned segment dates: a segment is dated by when it DEPARTS ─────────


def _slice(
    flights: list[str], dep: str, arr: str, o: str, d: str, stops: list[str] | None = None
) -> Slice:
    return Slice(
        flights=flights,
        departure=dep,
        arrival=arr,
        origin=SliceEndpoint(code=o),
        destination=SliceEndpoint(code=d),
        stops=[SliceEndpoint(code=c) for c in (stops or [])],
    )


def test_overnight_nonstop_is_dated_by_departure() -> None:
    """A nonstop satisfies `i == n - 1`, so the last-segment rule dated it by
    ARRIVAL — pinning BA178 JFK->LHR (dep 2026-12-31, arr 2027-01-01) to
    2027-01-01 and sending the user to a search for the wrong day, across a
    year boundary."""
    segs = extract_pin_segments_from_slice(
        _slice(["BA178"], "2026-12-31T22:00-05:00", "2027-01-01T10:00+00:00", "JFK", "LHR"),
    )
    assert segs is not None
    assert [x["date"] for x in segs] == ["2026-12-31"]


def test_overnight_connection_still_dates_its_last_leg_by_arrival() -> None:
    """The rule the nonstop case was over-applying is real for a genuine
    connection: the second leg does depart on the following day."""
    segs = extract_pin_segments_from_slice(
        _slice(
            ["AA100", "BA200"],
            "2026-12-31T18:00-05:00",
            "2027-01-01T14:00+00:00",
            "JFK",
            "LHR",
            ["BOS"],
        ),
    )
    assert segs is not None
    assert [x["date"] for x in segs] == ["2026-12-31", "2027-01-01"]


def test_same_day_nonstop_unchanged() -> None:
    segs = extract_pin_segments_from_slice(
        _slice(["AA867"], "2026-09-09T06:00-05:00", "2026-09-09T09:04-04:00", "MSY", "MIA"),
    )
    assert segs is not None
    assert [x["date"] for x in segs] == ["2026-09-09"]


# ───────── a 2-leg search is only a round trip if it actually returns ─────────


def test_open_jaw_is_multi_city_not_a_collapsed_round_trip() -> None:
    """Round-trip's SPA encoding folds both legs into ONE slice with two dates,
    which cannot express a second route. Treating any 2-leg search as a round
    trip therefore DELETED the second leg: SFO->JFK plus LAX->HNL encoded as
    SFO->JFK with a return date, and LAX/HNL vanished entirely."""
    from flight_cli.domain import Leg
    from flight_cli.links import _spa_specific_slices  # pyright: ignore[reportPrivateUsage]

    legs = (
        Leg(origins=("SFO",), destinations=("JFK",), date=date(2026, 9, 1)),
        Leg(origins=("LAX",), destinations=("HNL",), date=date(2026, 9, 5)),
    )
    trip, slices = _spa_specific_slices(legs)
    assert trip == "multi-city"
    assert [(s["origin"][0], s["dest"][0]) for s in slices] == [("SFO", "JFK"), ("LAX", "HNL")]


def test_true_round_trip_still_folds_into_one_slice() -> None:
    from flight_cli.domain import Leg
    from flight_cli.links import _spa_specific_slices  # pyright: ignore[reportPrivateUsage]

    legs = (
        Leg(origins=("SFO",), destinations=("JFK",), date=date(2026, 9, 1)),
        Leg(origins=("JFK",), destinations=("SFO",), date=date(2026, 9, 5)),
    )
    trip, slices = _spa_specific_slices(legs)
    assert trip == "round-trip"
    assert len(slices) == 1
    assert slices[0]["dates"]["returnDate"] == "2026-09-05"


def test_half_open_jaw_is_multi_city() -> None:
    """Returns from where it landed but not to where it started."""
    from flight_cli.domain import Leg
    from flight_cli.links import _spa_specific_slices  # pyright: ignore[reportPrivateUsage]

    legs = (
        Leg(origins=("SFO",), destinations=("JFK",), date=date(2026, 9, 1)),
        Leg(origins=("JFK",), destinations=("LAX",), date=date(2026, 9, 5)),
    )
    trip, _ = _spa_specific_slices(legs)
    assert trip == "multi-city"


# ───────── routing / extension / arrival-date reach the deep link ─────────


def _decoded(s: object) -> dict[str, Any]:
    import base64
    import json
    import urllib.parse

    from flight_cli.links import matrix_deep_link

    q = urllib.parse.unquote(matrix_deep_link(s).split("search=", 1)[1])  # pyright: ignore[reportArgumentType]
    return json.loads(base64.b64decode(q + "=="))


def test_routing_and_extension_reach_the_matrix_link() -> None:
    """Field names captured from the real SPA (2026-08): the URL state calls
    these `routing` / `ext`, NOT the `routeLanguage` / `commandLine` the /batch
    API uses for the same values — see docs/memories/matrix_spa_url_state.md.

    Dropping them meant a link built from `--routing BA+ --ext "MAXSTOPS 0"`
    opened an UNCONSTRAINED search, showing itineraries the CLI had excluded.
    """
    from flight_cli.domain import Leg, SpecificDateSearch

    s = SpecificDateSearch(
        legs=(
            Leg(
                origins=("JFK",),
                destinations=("LHR",),
                date=date(2026, 9, 1),
                route_language="BA+",
                extension="MAXSTOPS 0",
            ),
        ),
    )
    sl = _decoded(s)["slices"][0]
    assert sl["routing"] == "BA+"
    assert sl["ext"] == "MAXSTOPS 0"


def test_return_leg_carries_its_own_routing_codes() -> None:
    """The SPA folds a round trip into one slice, so the inbound leg's codes
    live in the separate `routingRet` / `extRet` fields."""
    from flight_cli.domain import Leg, SpecificDateSearch

    s = SpecificDateSearch(
        legs=(
            Leg(
                origins=("JFK",),
                destinations=("LHR",),
                date=date(2026, 9, 1),
                route_language="BA+",
                extension="MAXSTOPS 0",
            ),
            Leg(
                origins=("LHR",),
                destinations=("JFK",),
                date=date(2026, 9, 8),
                route_language="AA+",
                extension="MAXCONNECT 2:00",
            ),
        ),
    )
    sl = _decoded(s)["slices"][0]
    assert (sl["routing"], sl["ext"]) == ("BA+", "MAXSTOPS 0")
    assert (sl["routingRet"], sl["extRet"]) == ("AA+", "MAXCONNECT 2:00")


def test_arrival_date_intent_survives_into_the_link() -> None:
    """The URL-state counterpart of the API's `isArrivalDate` bool is the
    string `departureDateType: "arrive"`."""
    from flight_cli.domain import Leg, SpecificDateSearch

    def date_type(is_arrival: bool) -> str:
        s = SpecificDateSearch(
            legs=(
                Leg(
                    origins=("JFK",),
                    destinations=("LHR",),
                    date=date(2026, 9, 1),
                    is_arrival_date=is_arrival,
                ),
            ),
        )
        return _decoded(s)["slices"][0]["dates"]["departureDateType"]

    assert date_type(True) == "arrive"
    assert date_type(False) == "depart"
