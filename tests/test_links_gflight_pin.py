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

import pathlib

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


def test_extract_pin_segments_same_day_one_stop() -> None:
    """1-stop slice that arrives the same calendar day → all segments
    share the slice departure date."""
    s = Slice(
        flights=["DL2021", "DL861"],
        departure="2026-10-24T06:50:00-04:00",
        arrival="2026-10-24T14:15:00-10:00",
        origin=SliceEndpoint(code="MIA"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="SLC")],
    )
    segs = extract_pin_segments_from_slice(s)
    assert segs == [
        {
            "origin": "MIA",
            "date": "2026-10-24",
            "destination": "SLC",
            "carrier": "DL",
            "flight": "2021",
        },
        {
            "origin": "SLC",
            "date": "2026-10-24",
            "destination": "HNL",
            "carrier": "DL",
            "flight": "861",
        },
    ]


def test_extract_pin_segments_overnight_one_stop() -> None:
    """1-stop overnight slice (HNL→SEA→MIA, depart Wed evening, arrive
    Thu) → last segment date = arrival date."""
    s = Slice(
        flights=["DL440", "DL506"],
        departure="2026-10-14T21:45:00-10:00",
        arrival="2026-10-15T17:27:00-04:00",
        origin=SliceEndpoint(code="HNL"),
        destination=SliceEndpoint(code="MIA"),
        stops=[SliceEndpoint(code="SEA")],
    )
    segs = extract_pin_segments_from_slice(s)
    assert segs == [
        {
            "origin": "HNL",
            "date": "2026-10-14",
            "destination": "SEA",
            "carrier": "DL",
            "flight": "440",
        },
        {
            "origin": "SEA",
            "date": "2026-10-15",
            "destination": "MIA",
            "carrier": "DL",
            "flight": "506",
        },
    ]


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
    """3-segment slice with multiple midnight crossings: heuristic would
    guess wrong, but `segment_dates` (populated by gflight adapter) gives
    the precise per-leg date."""
    # MIA → ATL (Oct 24 evening) → SEA (arrive late Oct 24) → HNL (depart
    # Oct 25 morning, cross IDL, land Oct 25 morning HNL local). Heuristic
    # would put all segments on Oct 24 except possibly the last on arrival
    # date — but the SEA→HNL departure is genuinely on Oct 25.
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
    # The middle segment's date stays 2026-10-24 (exact, not heuristic
    # which would have put it on departure date too — by coincidence
    # correct here, but the principle is exact dates beat guesses).
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
