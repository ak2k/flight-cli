"""Tests for the small parsing/normalization helpers in pp.cli."""
from __future__ import annotations
import pytest

from flight_cli.pp.cli import _normalize_cabin, _parse_cash, _parse_csv


# ───────────────────────────── _parse_cash ─────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("USD530.00", 530.0),                # Matrix's normal format
    ("$1,078", 1078.0),                  # Dollar prefix, comma thousand-sep
    ("1,078 USD", 1078.0),               # Currency suffix
    ("USD 530.00", 530.0),               # With space
    ("USD12345.67", 12345.67),           # Long number
    ("$0.50", 0.5),                      # Sub-dollar
    ("EUR250", 250.0),                   # Foreign currency
    ("530", 530.0),                      # Bare number
])
def test_parse_cash_recognises_common_formats(raw, expected):
    assert _parse_cash(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "—", "-", "no price"])
def test_parse_cash_returns_none_for_empty_or_unparseable(raw):
    assert _parse_cash(raw) is None


# ──────────────────────────── _normalize_cabin ─────────────────────────────

@pytest.mark.parametrize("alias,expected", [
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
])
def test_normalize_cabin_canonicalizes_aliases(alias, expected):
    assert _normalize_cabin(alias) == expected


def test_normalize_cabin_passes_through_unknown():
    """Unknown values pass through verbatim — the API call will silently
    return 0 results rather than crashing on a typo."""
    assert _normalize_cabin("Cargo") == "Cargo"


# ──────────────────────────────── _parse_csv ───────────────────────────────

def test_parse_csv_strips_whitespace():
    assert _parse_csv(" a , b , c ", ()) == ("a", "b", "c")


def test_parse_csv_drops_empty_entries():
    assert _parse_csv("a,,b,", ()) == ("a", "b")


def test_parse_csv_returns_default_when_none():
    assert _parse_csv(None, ("default",)) == ("default",)


def test_parse_csv_returns_default_when_empty_string():
    assert _parse_csv("", ("default",)) == ("default",)
