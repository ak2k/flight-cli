# pyright: reportPrivateUsage=false
"""Tests for the legroom-field extractor in _gflight_ids.

Bit positions in `data[0][2][i]` (each leg tuple in Google Flights'
/GetShoppingResults response). Empirically calibrated 2026-05 by
correlating against Google Flights' detail-panel labels — see
`research/probe_wifi_correlation.py` and `docs/memories/legroom_recipe.md`."""

from __future__ import annotations

from flight_cli._gflight_ids import (
    LegAmenities,
    _decode_power,
    _decode_video,
    _decode_wifi,
    _parse_leg_amenities,
    _parse_pitch,
)


# ─────────────────────────── pitch parsing ─────────────────────────────


def test_pitch_string_with_unit_suffix() -> None:
    assert _parse_pitch("31 in") == 31
    assert _parse_pitch("32 in") == 32
    assert _parse_pitch("28 in") == 28


def test_pitch_bare_int_passthrough() -> None:
    assert _parse_pitch(30) == 30


def test_pitch_none_on_unrecognized() -> None:
    assert _parse_pitch(None) is None
    assert _parse_pitch("") is None
    assert _parse_pitch([]) is None
    assert _parse_pitch("very tight") is None


# ─────────────────────────── wifi enum ([11]) ──────────────────────────


def test_wifi_no_wifi_enum_1() -> None:
    """F9 Frontier (no wifi): [11]=1 → None."""
    arr = [None, None, None, None, None, None, None, None, None, None, None, 1]
    assert _decode_wifi(arr) is None


def test_wifi_free_enum_2() -> None:
    """AA/B6/DL/WN/KL (free wifi): [11]=2 → "free"."""
    arr = [None, True, None, None, None, None, None, None, None, None, True, 2]
    assert _decode_wifi(arr) == "free"


def test_wifi_paid_enum_3() -> None:
    """EK/UA/AS/AC/OS (paid wifi): [11]=3 → "paid"."""
    arr = [None, True, None, None, None, None, None, None, None, True, None, 3]
    assert _decode_wifi(arr) == "paid"


def test_wifi_empty_array_returns_none() -> None:
    assert _decode_wifi([]) is None
    assert _decode_wifi(None) is None


def test_wifi_unknown_enum_value_returns_none() -> None:
    arr = [None] * 11 + [99]
    assert _decode_wifi(arr) is None


def test_wifi_robust_to_short_array() -> None:
    """Truncated amenities (length < 12) → None, no IndexError."""
    assert _decode_wifi([None, True]) is None


# ─────────────────────────── power decoder ─────────────────────────────


def test_power_plug_via_idx1() -> None:
    """Live JFK→LHR DL1 sample: position [1]=True → "plug"."""
    arr = [None, True, None, None, None, None, None, None, None, True, None, 2]
    assert _decode_power(arr) == "plug"


def test_power_plug_via_idx3() -> None:
    arr = [None, None, None, True, None, None, None, None, None, None, None, None]
    assert _decode_power(arr) == "plug"


def test_power_usb_only_via_idx5() -> None:
    """WN 1140 (737MAX, USB-only): [5]=True with [1]/[3] both False → "usb"."""
    arr = [None, None, None, None, None, True, None, None, None, None, True, 2]
    assert _decode_power(arr) == "usb"


def test_power_none_when_no_signal() -> None:
    assert _decode_power([None] * 12) is None
    assert _decode_power(None) is None


def test_power_robust_to_short_array() -> None:
    """No IndexError on truncated input."""
    assert _decode_power([True]) is None  # [1] OOB
    assert _decode_power([]) is None


# ─────────────────────────── video decoder ─────────────────────────────


def test_video_live_stream_via_idx8() -> None:
    """B6 (DirecTV seatback live TV): [8]=True → "stream". Validated against
    B6 1072 FLL→LGA where Google's UI labelled it "Live TV"."""
    arr = [None, True, None, None, None, None, None, None, True, None, None, 2]
    assert _decode_video(arr) == "stream"


def test_video_ondemand_via_idx9() -> None:
    """DL/EK/UA seatback IFE: [9]=True → "ondemand"."""
    arr = [None, True, None, None, None, None, None, None, None, True, None, 2]
    assert _decode_video(arr) == "ondemand"


def test_video_byod_via_idx10() -> None:
    """AA/WN BYOD-to-device streaming: [10]=True → "byod". Validated against
    AA 952 MIA→LGA where Google's UI labelled it "Stream media to your device"."""
    arr = [None, True, None, None, None, None, None, None, None, None, True, 2]
    assert _decode_video(arr) == "byod"


def test_video_seatback_beats_byod() -> None:
    """When multiple delivery channels are set, the most-premium seatback
    option wins — matches Google's label priority."""
    arr = [None] * 12
    arr[8] = True
    arr[10] = True
    assert _decode_video(arr) == "stream"
    arr2 = [None] * 12
    arr2[9] = True
    arr2[10] = True
    assert _decode_video(arr2) == "ondemand"


def test_video_none_when_no_signal() -> None:
    assert _decode_video([None] * 12) is None
    assert _decode_video(None) is None


def test_video_robust_to_short_array() -> None:
    assert _decode_video([True, True]) is None  # [8] OOB
    assert _decode_video([]) is None


# ─────────────────────────── full-tuple parsing ────────────────────────


def _build_leg(
    *,
    amenities: list | None = None,
    legroom_class: object = 1,
    pitch: object = "31 in",
    cabin: object = 1,
    aircraft: object = "Airbus A330",
) -> list:
    """Construct a 33-element leg tuple with the indices under test populated."""
    leg: list = [None] * 33
    leg[12] = amenities
    leg[13] = legroom_class
    leg[14] = pitch
    leg[16] = cabin
    leg[17] = aircraft
    return leg


def test_groundtruth_aa952_mia_lga() -> None:
    """AA 952 Boeing 737MAX 8, MIA→LGA on 2026-06-16.

    Google's UI shows:
        Average legroom (30 in) · Free Wi-Fi
        In-seat power & USB outlets · Stream media to your device
    """
    leg = _build_leg(
        amenities=[None, True, None, None, None, None, None, None, None, None, True, 2],
        legroom_class=1,
        pitch="30 in",
        cabin=1,
        aircraft="Boeing 737MAX 8 Passenger",
    )
    a = _parse_leg_amenities(leg)
    assert a == LegAmenities(
        aircraft="Boeing 737MAX 8 Passenger",
        pitch_inches=30,
        legroom_class="AVERAGE",
        cabin="ECONOMY",
        wifi="free",
        power="plug",
        video="byod",
    )


def test_groundtruth_b61072_fll_lga() -> None:
    """B6 1072 Airbus A320, FLL→LGA on 2026-06-16.

    Google's UI shows:
        Above average legroom (32 in) · Free Wi-Fi
        In-seat power & USB outlets · Live TV
    """
    leg = _build_leg(
        amenities=[None, True, None, None, None, None, None, None, True, None, None, 2],
        legroom_class=3,
        pitch="32 in",
        cabin=1,
        aircraft="Airbus A320",
    )
    a = _parse_leg_amenities(leg)
    assert a == LegAmenities(
        aircraft="Airbus A320",
        pitch_inches=32,
        legroom_class="ABOVE",
        cabin="ECONOMY",
        wifi="free",
        power="plug",
        video="stream",
    )


def test_frontier_no_amenities_empty_array() -> None:
    """F9 (Frontier) A320neo: t[12]=[]. No wifi/IFE/power; pitch shows BELOW."""
    leg = _build_leg(
        amenities=[],
        legroom_class=2,  # BELOW
        pitch="28 in",
        cabin=1,
        aircraft="Airbus A320neo",
    )
    a = _parse_leg_amenities(leg)
    assert a.wifi is None
    assert a.power is None
    assert a.video is None
    assert a.pitch_inches == 28
    assert a.legroom_class == "BELOW"


def test_premium_cabin_suite_paid_wifi() -> None:
    """Premium cabin (Emirates A380-style): paid wifi + on-demand seatback + suite."""
    leg = _build_leg(
        amenities=[None, True, None, None, None, None, None, None, None, True, None, 3],
        legroom_class=6,  # Suite
        pitch=78,
        cabin=3,  # BUSINESS
        aircraft="Airbus A380-800",
    )
    a = _parse_leg_amenities(leg)
    assert a.legroom_class == "Suite"
    assert a.cabin == "BUSINESS"
    assert a.pitch_inches == 78
    assert a.wifi == "paid"
    assert a.power == "plug"
    assert a.video == "ondemand"


def test_missing_indices_handled_gracefully() -> None:
    """Truncated leg: parser shouldn't raise, just returns None for absent fields."""
    short_leg: list = [None] * 13
    short_leg[12] = None
    a = _parse_leg_amenities(short_leg)
    assert a.aircraft is None
    assert a.pitch_inches is None
    assert a.cabin is None


def test_unknown_enums_become_none() -> None:
    leg = _build_leg(legroom_class=999, cabin=999)
    a = _parse_leg_amenities(leg)
    assert a.legroom_class is None
    assert a.cabin is None
