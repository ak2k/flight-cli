# pyright: reportPrivateUsage=false
"""Tests for _parse_slice_spec error diagnostics.

The original generic message ("should be ORIGIN-DEST:DATE[:r=...:e=...]")
was fine for missing-date but useless for the most common typo: writing
`r-LH+` instead of `r=LH+`. The lookahead split silently ignores anything
that doesn't start with `r=` or `e=`, so the routing was just dropped.

These tests pin the specific-error-per-failure-mode behavior so future
parser changes don't silently regress.
"""

from __future__ import annotations

import pytest
import typer

from flight_cli.cli import _parse_slice_spec


def test_valid_slice() -> None:
    leg = _parse_slice_spec("JFK-LHR:2026-08-15")
    assert leg.origins == ("JFK",)
    assert leg.destinations == ("LHR",)


def test_valid_slice_with_routing() -> None:
    leg = _parse_slice_spec("JFK-LHR:2026-08-15:r=LH+")
    assert leg.route_language == "LH+"


def test_valid_slice_with_extension_containing_colon() -> None:
    leg = _parse_slice_spec("JFK-LHR:2026-08-15:r=LH+:e=MAXCONNECT 2:00")
    assert leg.route_language == "LH+"
    assert leg.extension == "MAXCONNECT 2:00"


def test_missing_date() -> None:
    with pytest.raises(typer.BadParameter, match="missing date"):
        _parse_slice_spec("JFK-LHR")


def test_missing_dash_between_origin_dest() -> None:
    with pytest.raises(typer.BadParameter, match="missing '-' between origin"):
        _parse_slice_spec("JFKLHR:2026-08-15")


def test_empty_origin() -> None:
    with pytest.raises(typer.BadParameter, match="must both be non-empty"):
        _parse_slice_spec("-LHR:2026-08-15")


def test_empty_destination() -> None:
    with pytest.raises(typer.BadParameter, match="must both be non-empty"):
        _parse_slice_spec("JFK-:2026-08-15")


def test_invalid_date() -> None:
    with pytest.raises(typer.BadParameter, match="invalid date"):
        _parse_slice_spec("JFK-LHR:2026-13-99")


def test_unknown_key_prefix_dash_instead_of_eq() -> None:
    """The common typo: `r-LH+` instead of `r=LH+`. Before this fix the
    parser silently dropped it; now it errors loudly."""
    with pytest.raises(typer.BadParameter, match="unknown key prefix"):
        _parse_slice_spec("JFK-LHR:2026-08-15:r-LH+")


def test_unknown_key_prefix_x_eq() -> None:
    with pytest.raises(typer.BadParameter, match="unknown key prefix"):
        _parse_slice_spec("JFK-LHR:2026-08-15:x=foo")
