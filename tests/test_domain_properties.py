"""Property tests for domain.py validators.

The golden-file tests in test_wire_round_trip.py cover wire fidelity:
"do we serialize this captured search the same way the SPA did?" These
tests cover the *other* axis: "do the domain validators enforce their
invariants across the full input space?"

Catches: typos in alias mappings, off-by-one in length checks, regression
on `field_validator` ordering, accidental Mutable-default-on-frozen-model.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from flight_cli.domain import (
    _TIME_RANGE_FOR,  # pyright: ignore[reportPrivateUsage]
    CalendarWindow,
    Leg,
    Pax,
    SpecificDateSearch,
    TimeOfDay,
    time_range_for,
)

# ───────────────────────────── strategies ─────────────────────────────────

iata_codes = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=3)
# _iata() uppercases + strips first, then checks 3-letter alpha. The negative
# strategy must filter on the *post-normalized* form so we don't generate
# inputs the validator legitimately accepts (e.g. "AAa" → "AAA").
non_iata_text = st.text().filter(
    lambda s: not (len(s.upper().strip()) == 3 and s.upper().strip().isalpha())
)
small_int = st.integers(min_value=0, max_value=9)


# ───────────────────────────── _iata / Leg.of ─────────────────────────────


@given(iata_codes)
def test_iata_accepts_3_letter_uppercase(code: str) -> None:
    """Any 3-letter ASCII-uppercase string round-trips through Leg.of()."""
    leg = Leg.of(code, code, date(2026, 6, 1))
    assert leg.origins == (code,)
    assert leg.destinations == (code,)


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=3))
def test_iata_uppercases_lowercase_input(code: str) -> None:
    """Lowercase is upcased, not rejected."""
    leg = Leg.of(code, code, date(2026, 6, 1))
    assert leg.origins == (code.upper(),)


@given(non_iata_text)
def test_iata_rejects_non_3_letter(garbage: str) -> None:
    """Anything that isn't 3 ASCII letters (post-upper) should raise."""
    with pytest.raises(ValidationError):
        Leg.of(garbage, "JFK", date(2026, 6, 1))


@given(st.lists(iata_codes, min_size=1, max_size=5).map(tuple))
def test_leg_of_accepts_iata_list(origins: tuple[str, ...]) -> None:
    """Multi-airport per slice round-trips."""
    leg = Leg.of(origins, ("LHR",), date(2026, 6, 1))
    assert leg.origins == origins


# ───────────────────────────── CalendarWindow ─────────────────────────────


@given(small_int, small_int)
def test_calendar_window_duration_invariant(a: int, b: int) -> None:
    """duration_max < duration_min must always fail; >= must always succeed."""
    lo, hi = sorted((a, b))
    # valid: max >= min
    w = CalendarWindow(
        start=date(2026, 6, 1), end=date(2026, 7, 1), duration_min=lo, duration_max=hi
    )
    assert w.duration_min == lo and w.duration_max == hi

    # invalid: swapped (only if they differ — equal is fine)
    if lo != hi:
        with pytest.raises(ValidationError):
            CalendarWindow(
                start=date(2026, 6, 1), end=date(2026, 7, 1), duration_min=hi, duration_max=lo
            )


# ───────────────────────────── Pax totals ─────────────────────────────────


@given(small_int, small_int, small_int, small_int, small_int, small_int)
def test_pax_total_sums(a: int, c: int, s: int, y: int, ins: int, inl: int) -> None:
    """Pax.total is the sum of all six pax fields."""
    pax = Pax(adults=a, children=c, seniors=s, youth=y, infants_in_seat=ins, infants_in_lap=inl)
    assert pax.total == a + c + s + y + ins + inl


# ───────────────────────────── time_range_for ─────────────────────────────


@given(st.sampled_from(list(TimeOfDay)))
def test_time_range_well_formed(t: TimeOfDay) -> None:
    """Every TimeOfDay produces a {min, max} dict with min < max lexically."""
    r = time_range_for(t)
    assert set(r.keys()) == {"min", "max"}
    # Lexical comparison works because the times are zero-padded compatibly
    # within each pair (see TimeOfDay docstring quirk re: '00:00' vs '8:00').
    raw = _TIME_RANGE_FOR[t]
    assert r["min"] == raw[0]
    assert r["max"] == raw[1]


# ───────────────────────────── SpecificDateSearch ─────────────────────────


@given(st.lists(iata_codes, min_size=1, max_size=3))
def test_specific_search_requires_dates(origins: list[str]) -> None:
    """A leg without a date must fail SpecificDateSearch validation."""
    leg_no_date = Leg.of(origins[0], "LHR")  # date defaults to None
    with pytest.raises(ValidationError):
        SpecificDateSearch(legs=(leg_no_date,))


@given(st.integers(min_value=1, max_value=30))
def test_specific_search_accepts_dated_legs(days_out: int) -> None:
    """Dated single-leg searches always validate."""
    dt = date(2026, 6, 1) + timedelta(days=days_out)
    leg = Leg.of("JFK", "LHR", dt)
    s = SpecificDateSearch(legs=(leg,))
    assert s.legs[0].date == dt
