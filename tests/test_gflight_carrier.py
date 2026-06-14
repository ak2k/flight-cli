# pyright: reportPrivateUsage=false
"""Tests for marketing/operating carrier resolution in _gflight_ids.

A leg tuple carries the OPERATING carrier at fl[22] and the MARKETING (selling)
carriers at fl[15]; fl[18] is truthy when the operating carrier self-markets.
The booking carrier a passenger sees (and what Matrix returns) is the marketing
carrier on operated-for regional legs, else the operating carrier. Ground-truthed
2026-06-13 against Google Flights' own headline labels (see _resolve_booking)."""

from __future__ import annotations

from typing import Any

from flight_cli._gflight_ids import (
    LegAmenities,
    _carrier_entry,
    _marketing_codes,
    _parse_leg_amenities,
    _resolve_booking,
)


def _leg(
    *,
    operating: list[Any] | None,
    marketing: list[list[Any]] | None,
    self_marketed: object,
) -> list[Any]:
    """33-element leg tuple with only the carrier-identity indices populated."""
    leg: list[Any] = [None] * 33
    leg[15] = marketing
    leg[18] = self_marketed
    leg[22] = operating
    return leg


# Ground-truth shapes captured 2026-06-13.
_EN8858 = _leg(  # Air Dolomiti operating for Lufthansa (operated-for)
    operating=["EN", "8858", None, "Air Dolomiti"],
    marketing=[["LH", "9498", None, "Lufthansa"]],
    self_marketed=None,
)
_OS36 = _leg(  # Austrian self-marketing, United codeshare
    operating=["OS", "36", None, "Austrian"],
    marketing=[["UA", "9820", None, "United"]],
    self_marketed=[True],
)
_LX39 = _leg(  # SWISS mainline, no codeshare
    operating=["LX", "39", None, "SWISS"],
    marketing=None,
    self_marketed=[True],
)
_AF83 = _leg(  # Air France self-marketing, multiple codeshares
    operating=["AF", "83", None, "Air France"],
    marketing=[["DL", "83", None, "Delta"], ["KL", "1066", None, "KLM"]],
    self_marketed=[True],
)


# ─────────────────────────── booking carrier ───────────────────────────


def test_booking_operated_for_uses_marketing() -> None:
    """Air Dolomiti operating for LH -> book as Lufthansa LH9498, not EN8858."""
    assert _resolve_booking(_EN8858) == ("LH", "9498")


def test_booking_self_marketed_uses_operating() -> None:
    """Austrian self-sells OS36 (UA is a codeshare) -> book as Austrian."""
    assert _resolve_booking(_OS36) == ("OS", "36")


def test_booking_mainline_uses_operating() -> None:
    """No codeshare -> the operating carrier is the booking carrier."""
    assert _resolve_booking(_LX39) == ("LX", "39")


def test_booking_multi_codeshare_self_marketed_uses_operating() -> None:
    """AF83 self-marketed with DL+KL codeshares -> book as Air France."""
    assert _resolve_booking(_AF83) == ("AF", "83")


def test_booking_falls_back_to_operating_on_malformed_marketing() -> None:
    leg = _leg(operating=["EN", "8858"], marketing=[["LH"]], self_marketed=None)
    assert _resolve_booking(leg) == ("EN", "8858")


def test_booking_short_tuple_no_indexerror() -> None:
    assert _resolve_booking([None, None]) == (None, None)


# ─────────────────────────── carrier extraction ────────────────────────


def test_amenities_capture_operating_and_marketing_codeshare() -> None:
    a = _parse_leg_amenities(_EN8858)
    assert a.operating_carrier == "EN"
    assert a.operating_carrier_name == "Air Dolomiti"
    assert a.marketing_carriers == ("LH",)


def test_amenities_mainline_has_no_marketing_partners() -> None:
    a = _parse_leg_amenities(_LX39)
    assert a.operating_carrier == "LX"
    assert a.operating_carrier_name == "SWISS"
    assert a.marketing_carriers == ()


def test_amenities_multi_codeshare_collects_all_marketing_codes() -> None:
    assert _parse_leg_amenities(_AF83).marketing_carriers == ("DL", "KL")


def test_amenities_carrier_fields_none_on_empty_leg() -> None:
    a = _parse_leg_amenities([None] * 33)
    assert a == LegAmenities()  # all defaults — no carrier identity, no amenities


# ─────────────────────────── helpers ───────────────────────────────────


def test_carrier_entry_full_and_malformed() -> None:
    assert _carrier_entry(["LH", "9498", None, "Lufthansa"]) == ("LH", "9498", "Lufthansa")
    assert _carrier_entry(["EN", "8858"]) == ("EN", "8858", None)
    assert _carrier_entry(["LH"]) == (None, None, None)
    assert _carrier_entry(None) == (None, None, None)


def test_marketing_codes_skips_malformed_entries() -> None:
    leg = _leg(
        operating=["AF", "83", None, "Air France"],
        marketing=[["DL", "83"], ["bad"], ["KL", "1"]],
        self_marketed=[True],
    )
    assert _marketing_codes(leg) == ("DL", "KL")
