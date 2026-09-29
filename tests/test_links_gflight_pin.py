# pyright: reportPrivateUsage=false
# DIVERGE: these pin wire-format contracts of the pinned-tfs encoder, which is
# module-internal by design. Exporting it to satisfy the rule would widen the
# API for a test.
"""Byte-exact regression test for the Google Flights pinned-itinerary
`tfs=` protobuf encoder.

Fixture was captured by:
  uv run --script research/record_user_session.py --auto \\
    "https://www.google.com/travel/flights/search?tfs=...&hl=en&curr=USD"

The capture clicks the first outbound + first return card, recording each
URL transition. The pinned-itinerary tfs= bytes are saved to
tests/fixtures/gflight_tfs/.

If Google Flights changes the protobuf schema (field tags, marker values),
this test fails immediately and tells us to re-RE."""

from __future__ import annotations

import base64
import pathlib
from typing import Any, cast

from flight_cli.links import (
    _encode_gflight_pinned_tfs,  # pyright: ignore[reportPrivateUsage]  # test-only: lock byte-exact regression
    extract_pin_segments_from_slice,
)
from flight_cli.models import Slice, SliceEndpoint

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "gflight_tfs"


def test_pinned_tfs_aa_via_lax_byte_exact() -> None:
    """AA162/AA2777 HNL-LAX-MIA + AA2458/AA31 MIA-LAX-HNL, 3 adults, business.
    Captured 2026-05-19 from headed Chrome via Playwright."""
    raw = _encode_gflight_pinned_tfs(
        slices=[
            {
                "date": "2026-10-14",
                "origin": "HNL",
                "destination": "MIA",
                "segments": [
                    {
                        "origin": "HNL",
                        "date": "2026-10-14",
                        "destination": "LAX",
                        "carrier": "AA",
                        "flight": "162",
                    },
                    {
                        "origin": "LAX",
                        "date": "2026-10-14",
                        "destination": "MIA",
                        "carrier": "AA",
                        "flight": "2777",
                    },
                ],
            },
            {
                "date": "2026-10-24",
                "origin": "MIA",
                "destination": "HNL",
                "segments": [
                    {
                        "origin": "MIA",
                        "date": "2026-10-24",
                        "destination": "LAX",
                        "carrier": "AA",
                        "flight": "2458",
                    },
                    {
                        "origin": "LAX",
                        "date": "2026-10-25",
                        "destination": "HNL",
                        "carrier": "AA",
                        "flight": "31",
                    },
                ],
            },
        ],
        cabin=3,
        adults=3,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
    )
    expected = (FIXTURE_DIR / "aa_hnl-lax-mia_rt_biz_3pax.bin").read_bytes()
    assert raw == expected, (
        "Pinned-itinerary tfs= bytes drifted from the captured fixture. "
        "Re-capture via research/record_user_session.py --auto and inspect "
        "the diff: Google Flights may have changed the protobuf schema."
    )


# Google's own search page for JFK+LGA+EWR -> LAX, one-way, economy, 1 adult,
# read off a live page load (it served 45 rows from all three origins). It has
# no field 16: that is the pinned booking link's marker, not the search page's.
_RECON_LAX_MULTI = (
    "CBwQAhowEgoyMDI2LTExLTA0agcIARIDSkZLagcIARIDTEdBagcIARIDRVdScgcIARIDTEFYQAFIAXABmAEC"
)


def _search_page_tfs(origin: str | list[str], destination: str | list[str]) -> bytes:
    return _encode_gflight_pinned_tfs(
        slices=[
            {"date": "2026-11-04", "origin": origin, "destination": destination, "segments": []}
        ],
        cabin=1,
        adults=1,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
        pin_max_u64=False,
    )


def test_an_airport_set_encodes_byte_exact_to_googles_own_search_page() -> None:
    raw = _search_page_tfs(["JFK", "LGA", "EWR"], ["LAX"])
    assert base64.urlsafe_b64encode(raw).rstrip(b"=").decode() == _RECON_LAX_MULTI


def test_a_one_airport_sequence_encodes_exactly_as_the_bare_code() -> None:
    """A str is also a sequence of one-letter strings; the encoder must read
    "HNL" as one airport, and `["HNL"]` as the same one."""
    assert _search_page_tfs("JFK", "LAX") == _search_page_tfs(["JFK"], ["LAX"])


def test_extract_pin_segments_refuses_a_same_day_connection_with_no_flight_dates() -> None:
    """A Matrix connection states only the slice's two ends, and they do not
    date the flights between them, even when both fall on one day."""
    s = Slice(
        flights=["DL2021", "DL861"],
        departure="2026-10-24T06:50:00-04:00",
        arrival="2026-10-24T14:15:00-10:00",
        origin=SliceEndpoint(code="MIA"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="SLC")],
    )
    assert extract_pin_segments_from_slice(s) is None


def test_extract_pin_segments_refuses_an_overnight_connection_with_no_flight_dates() -> None:
    """HNL->SEA->MIA leaving Wed evening and landing Thu: the second flight may
    leave either day, so no date is written for it."""
    s = Slice(
        flights=["DL440", "DL506"],
        departure="2026-10-14T21:45:00-10:00",
        arrival="2026-10-15T17:27:00-04:00",
        origin=SliceEndpoint(code="HNL"),
        destination=SliceEndpoint(code="MIA"),
        stops=[SliceEndpoint(code="SEA")],
    )
    assert extract_pin_segments_from_slice(s) is None


def test_extract_pin_segments_refuses_a_connection_over_the_date_line() -> None:
    """NZ104 SYD-AKL, then NZ10 AKL-HNL leaving Auckland after midnight: the
    slice lands on the day it left while its second flight leaves the next."""
    s = Slice(
        flights=["NZ104", "NZ10"],
        departure="2026-10-20T18:00+11:00",
        arrival="2026-10-20T10:00-10:00",
        origin=SliceEndpoint(code="SYD"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="AKL")],
    )
    assert extract_pin_segments_from_slice(s) is None


def test_extract_pin_segments_nonstop() -> None:
    """Nonstop slice (no stops, 1 flight) → 1 segment."""
    s = Slice(
        flights=["DL422"],
        departure="2026-10-14T07:00:00-10:00",
        arrival="2026-10-14T15:42:00-07:00",
        origin=SliceEndpoint(code="HNL"),
        destination=SliceEndpoint(code="LAX"),
        stops=[],
    )
    segs = extract_pin_segments_from_slice(s)
    assert segs == [
        {
            "origin": "HNL",
            "date": "2026-10-14",
            "destination": "LAX",
            "carrier": "DL",
            "flight": "422",
        }
    ]


def test_extract_pin_segments_uses_exact_dates_when_present() -> None:
    """3-segment slice: `segment_dates` (populated by the gflight adapter)
    dates each flight, whatever days the slice leaves and lands on."""
    # MIA → ATL (Oct 24 evening) → SEA (arrive late Oct 24) → HNL (depart
    # Oct 25 morning, land Oct 25 morning HNL local).
    s = Slice(
        flights=["DL1249", "DL629", "DL419"],
        departure="2026-10-24T18:00:00-04:00",
        arrival="2026-10-25T11:00:00-10:00",
        origin=SliceEndpoint(code="MIA"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="ATL"), SliceEndpoint(code="SEA")],
        segment_dates=["2026-10-24", "2026-10-24", "2026-10-25"],
    )
    segs = extract_pin_segments_from_slice(s)
    assert segs is not None
    assert [seg["date"] for seg in segs] == ["2026-10-24", "2026-10-24", "2026-10-25"]


def test_extract_pin_segments_bails_on_segment_dates_length_mismatch() -> None:
    """segment_dates present but wrong length → bail rather than mix."""
    s = Slice(
        flights=["DL440", "DL506"],
        departure="2026-10-14T21:45:00-10:00",
        arrival="2026-10-15T17:27:00-04:00",
        origin=SliceEndpoint(code="HNL"),
        destination=SliceEndpoint(code="MIA"),
        stops=[SliceEndpoint(code="SEA")],
        segment_dates=["2026-10-14"],  # 1 entry for 2 flights
    )
    assert extract_pin_segments_from_slice(s) is None


def test_extract_pin_segments_bails_on_missing_data() -> None:
    """Slice with stops/flights length mismatch returns None — caller
    falls back to the search-only URL."""
    s = Slice(
        flights=["DL440", "DL506"],
        departure="2026-10-14T21:45:00-10:00",
        origin=SliceEndpoint(code="HNL"),
        destination=SliceEndpoint(code="MIA"),
        stops=[],  # 2 flights but 0 stops — invalid topology
    )
    assert extract_pin_segments_from_slice(s) is None


# ───────── multi-city and passenger types survive into the pinned link ─────────


def _pin_slice(origin: str, dest: str, date: str) -> dict[str, Any]:
    return {
        "date": date,
        "origin": origin,
        "destination": dest,
        "segments": [
            {
                "origin": origin,
                "date": date,
                "destination": dest,
                "carrier": "AA",
                "flight": "100",
            },
        ],
    }


def _field8_values(buf: bytes) -> list[int]:
    """Every top-level field-8 varint (tag byte 0x40) — the per-occupant types."""
    out: list[int] = []
    i = 0
    while i < len(buf):
        if buf[i] == 0x40:
            out.append(buf[i + 1])
            i += 2
        else:
            i += 1
    return out


def test_passenger_types_are_not_all_encoded_as_adults() -> None:
    """Field 8 carries each occupant's TYPE: 1 adult, 2 child, 3 an infant on a
    lap, 4 an infant in a seat. Google prices BA112 JFK-LHR at $295 for one
    adult, $324 with a 3 beside it and $589 with a 4. A bare 1 for everyone
    would search and price a child as an adult."""
    from flight_cli.links import _encode_gflight_pinned_tfs  # pyright: ignore[reportPrivateUsage]

    buf = _encode_gflight_pinned_tfs(
        slices=[_pin_slice("SFO", "JFK", "2026-09-01")],
        cabin=1,
        adults=1,
        children=1,
        infants_in_seat=1,
        infants_on_lap=1,
    )
    assert _field8_values(buf) == [1, 2, 4, 3]


def test_each_infant_kind_has_its_own_code_in_the_pinned_link() -> None:
    from flight_cli.links import _encode_gflight_pinned_tfs  # pyright: ignore[reportPrivateUsage]

    def field8(*, seat: int, lap: int) -> list[int]:
        return _field8_values(
            _encode_gflight_pinned_tfs(
                slices=[_pin_slice("JFK", "LHR", "2026-11-04")],
                cabin=1,
                adults=1,
                children=0,
                infants_in_seat=seat,
                infants_on_lap=lap,
            )
        )

    assert field8(seat=1, lap=0) == [1, 4]
    assert field8(seat=0, lap=1) == [1, 3]


def _search_url_passengers(**pax: int) -> list[int]:
    """Field 8 of `google_flights_url`'s tfs=, read with fast_flights' own schema."""
    import urllib.parse
    from datetime import date

    from fast_flights import flights_pb2  # pyright: ignore[reportMissingTypeStubs]

    from flight_cli.domain import Leg, Pax, SearchOptions, SpecificDateSearch
    from flight_cli.links import google_flights_url

    url = google_flights_url(
        SpecificDateSearch(
            legs=(Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 11, 4)),),
            options=SearchOptions(pax=Pax(**pax)),
        )
    )
    raw = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]
    # The generated module is untyped.
    info = cast("Any", flights_pb2).Info.FromString(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    return [int(kind) for kind in info.passengers]


def test_the_search_link_writes_googles_infant_codes() -> None:
    """The link calendar and every unpinned search print is built through
    fast_flights, whose enum names 3 an infant in a seat and 4 one on a lap."""
    assert _search_url_passengers(adults=1, infants_in_seat=1) == [1, 4]
    assert _search_url_passengers(adults=1, infants_in_lap=1) == [1, 3]
    assert _search_url_passengers(adults=1, children=1) == [1, 2]


def test_two_adults_still_encode_as_two_adults() -> None:
    from flight_cli.links import _encode_gflight_pinned_tfs  # pyright: ignore[reportPrivateUsage]

    buf = _encode_gflight_pinned_tfs(
        slices=[_pin_slice("SFO", "JFK", "2026-09-01")],
        cabin=1,
        adults=2,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
    )
    assert _field8_values(buf) == [1, 1]


def test_three_leg_itinerary_is_multi_city_not_round_trip() -> None:
    """`>= 2 slices` meant round-trip, so Google read only the first two and
    leg 3 vanished from a link still described as "pinned"."""
    from flight_cli.links import (  # pyright: ignore[reportPrivateUsage]
        _GF_TRIP_MULTI_CITY,
        _GF_TRIP_ONE_WAY,
        _GF_TRIP_ROUND_TRIP,
        _encode_gflight_pinned_tfs,
    )

    def trip_type(n_slices: int) -> int:
        route = [("SFO", "JFK"), ("JFK", "LHR"), ("LHR", "SFO")][:n_slices]
        buf = _encode_gflight_pinned_tfs(
            slices=[_pin_slice(o, d, "2026-09-01") for o, d in route],
            cabin=1,
            adults=1,
            children=0,
            infants_in_seat=0,
            infants_on_lap=0,
        )
        return buf[-1]  # field 19 is the last varint written

    assert trip_type(1) == _GF_TRIP_ONE_WAY
    assert trip_type(2) == _GF_TRIP_ROUND_TRIP
    assert trip_type(3) == _GF_TRIP_MULTI_CITY


def test_stop_limit_reaches_the_google_search_url() -> None:
    """`max_stops` is a TFSData-level field, not per-FlightData. Omitting it
    made a `--stops 0` link byte-identical to an unconstrained one, so a
    nonstop-only result table handed the user a page that also offered
    connections."""
    from datetime import date

    from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
    from flight_cli.links import google_flights_url

    def url(max_extra_stops: int | None) -> str:
        return google_flights_url(
            SpecificDateSearch(
                legs=(Leg(origins=("JFK",), destinations=("LHR",), date=date(2026, 9, 1)),),
                options=SearchOptions(max_extra_stops=max_extra_stops),
            ),
        )

    nonstop, one_stop, unconstrained = url(0), url(1), url(None)
    assert nonstop != unconstrained
    assert nonstop != one_stop
    assert one_stop != unconstrained
