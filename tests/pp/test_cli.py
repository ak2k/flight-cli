# pyright: reportPrivateUsage=false
"""Tests for the small parsing/normalization helpers in pp.cli."""

from __future__ import annotations

import pytest

from flight_cli.pp.cli import (
    _best_award_for_cabin,
    _fmt_award_cell,
    _normalize_cabin,
    _parse_cash,
    _parse_csv,
)
from flight_cli.providers.base import AwardFlight, CabinAward

# ───────────────────────────── _parse_cash ─────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("USD530.00", 530.0),  # Matrix's normal format
        ("$1,078", 1078.0),  # Dollar prefix, comma thousand-sep
        ("1,078 USD", 1078.0),  # Currency suffix
        ("USD 530.00", 530.0),  # With space
        ("USD12345.67", 12345.67),  # Long number
        ("$0.50", 0.5),  # Sub-dollar
        ("EUR250", 250.0),  # Foreign currency
        ("530", 530.0),  # Bare number
    ],
)
def test_parse_cash_recognises_common_formats(raw: str, expected: float) -> None:
    assert _parse_cash(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "—", "-", "no price"])
def test_parse_cash_returns_none_for_empty_or_unparseable(raw: str | None) -> None:
    assert _parse_cash(raw) is None


# ──────────────────────────── _normalize_cabin ─────────────────────────────


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("y", "Economy"),
        ("Y", "Economy"),
        ("economy", "Economy"),
        ("Economy", "Economy"),
        ("coach", "Economy"),
        ("Main", "Economy"),
        ("j", "Business"),
        ("Business", "Business"),
        ("business", "Business"),
        ("BUSINESS", "Business"),
        ("first", "First"),
        ("F", "First"),
        ("premium", "Premium economy"),
        ("premium-economy", "Premium economy"),
        ("Premium Economy", "Premium economy"),
    ],
)
def test_normalize_cabin_canonicalizes_aliases(alias: str, expected: str) -> None:
    assert _normalize_cabin(alias) == expected


def test_normalize_cabin_passes_through_unknown() -> None:
    """Unknown values pass through verbatim — the API call will silently
    return 0 results rather than crashing on a typo."""
    assert _normalize_cabin("Cargo") == "Cargo"


# ──────────────────────────────── _parse_csv ───────────────────────────────


def test_parse_csv_strips_whitespace() -> None:
    assert _parse_csv(" a , b , c ", ()) == ("a", "b", "c")


def test_parse_csv_drops_empty_entries() -> None:
    assert _parse_csv("a,,b,", ()) == ("a", "b")


def test_parse_csv_returns_default_when_none() -> None:
    assert _parse_csv(None, ("default",)) == ("default",)


def test_parse_csv_returns_default_when_empty_string():
    assert _parse_csv("", ("default",)) == ("default",)


# ────────────── _best_award_for_cabin + _fmt_award_cell ────────────────────


def _award(cabin: str, miles: int, tax: float = 0.0, program: str = "Test") -> AwardFlight:
    """Minimal AwardFlight for cabin-cell tests. Other fields aren't read by
    the render helpers under test, so we leave them at sensible defaults."""
    return AwardFlight(
        provider="test",
        program=program,
        flight_number="AA100",
        origin="JFK",
        destination="LHR",
        departure="2026-08-15T09:00",
        arrival="2026-08-15T20:00",
        cabins=[CabinAward(cabin=cabin, miles=miles, tax_usd=tax, tax_currency="USD")],
        funding_banks=[],
    )


def test_best_award_for_cabin_returns_none_when_absent():
    awards = [_award("Business", 100_000)]
    assert _best_award_for_cabin(awards, "Economy") is None


def test_best_award_for_cabin_picks_cheapest_in_miles():
    awards = [
        _award("Economy", 50_000, program="Expensive"),
        _award("Economy", 30_000, program="Cheap"),
        _award("Economy", 40_000, program="Mid"),
    ]
    best = _best_award_for_cabin(awards, "Economy")
    assert best is not None
    miles, _tax, program, _banks = best
    assert (miles, program) == (30_000, "Cheap")


def test_fmt_award_cell_without_cash_omits_cpm_line():
    awards = [_award("Economy", 36_000, tax=164.0, program="VirginAtlantic")]
    cell = _fmt_award_cell(awards, "Economy")
    assert cell == "36.0k VirginAtlantic + $164"


def test_fmt_award_cell_with_cash_appends_cpm_line():
    # 36k miles + $164 tax for $794 cash → (794 - 164) / 36000 * 100 = 1.75¢/mi
    awards = [_award("Economy", 36_000, tax=164.0, program="VirginAtlantic")]
    cell = _fmt_award_cell(awards, "Economy", cash_usd=794.0)
    head, _, cpm_line = cell.partition("\n")
    assert head == "36.0k VirginAtlantic + $164"
    assert "1.8¢/mi" in cpm_line  # rounded one decimal


def test_fmt_award_cell_missing_award_returns_dash_only():
    """No award in this cabin → just '—', no ¢/mi line even if cash is known."""
    awards = [_award("Business", 100_000)]
    assert _fmt_award_cell(awards, "Economy", cash_usd=794.0) == "—"
