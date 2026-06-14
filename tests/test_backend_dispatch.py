# pyright: reportPrivateUsage=false
"""Tests for `flight search`'s backend selection logic.

`_pick_backend` is the routing decision: which backend handles a given mix
of user-facing CLI flags. The set of "Matrix-only" flags is the load-
bearing knowledge — get it wrong and either Matrix is invoked when it
needn't be (slow) or gflight is invoked for inexpressible queries (errors
deep in fli)."""

from __future__ import annotations

import pytest
import typer

from flight_cli.cli import (
    BACKEND_AUTO,
    BACKEND_GFLIGHT,
    BACKEND_MATRIX,
    _pick_backend,
)


def _call(backend: str = BACKEND_AUTO, **overrides: object) -> str:
    """Defaults match a plain `flight search JFK LHR --dep 2026-08-15`."""
    defaults: dict[str, object] = {
        "routing": None,
        "extension": None,
        "slice_specs": None,
        "depart_times": None,
        "return_times": None,
        "seniors": 0,
        "youth": 0,
        "inf_seat": 0,
        "inf_lap": 0,
    }
    defaults.update(overrides)
    return _pick_backend(backend=backend, **defaults)  # type: ignore[arg-type]


# ───────────────────────────────── auto ────────────────────────────────────


def test_auto_plain_search_picks_gflight() -> None:
    assert _call() == BACKEND_GFLIGHT


@pytest.mark.parametrize(
    "flag,value",
    [
        ("slice_specs", ["JFK-LHR:2026-08-15"]),  # multi-city
        ("depart_times", "morning"),
        ("return_times", "evening"),
        ("seniors", 1),
        ("youth", 1),
        ("inf_seat", 1),
        ("inf_lap", 1),
    ],
)
def test_auto_hard_matrix_flag_picks_matrix(flag: str, value: object) -> None:
    """Flags the GF bridge can't map at all always force Matrix."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("routing", "LH+"),  # marketing carrier (native)
        ("routing", "F* X:FRA F*"),  # via airport (native)
        ("routing", "O:LH+"),  # operating carrier (Tier-2 post-filter)
        ("extension", "MAXCONNECT 2:00"),  # layover max (native)
        ("extension", "ALLIANCE star-alliance; MAXSTOPS 1"),  # native
        ("extension", "-CODESHARE"),  # Tier-2 post-filter
    ],
)
def test_auto_gf_serveable_routing_picks_gflight(flag: str, value: object) -> None:
    """Routing/extension GF can honor (native + post-filter) stays on gflight."""
    assert _call(**{flag: value}) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("extension", "F bc=y"),  # fare basis (Tier 3)
        ("extension", "MAXMILES 8000"),  # mileage (Tier 3)
        ("routing", "BA AA"),  # ordered carrier chain — not GF-expressible
        ("extension", "MINCONNECT 1:00"),  # min layover — unsupported Tier-2
        ("extension", "-REDEYES"),  # red-eyes — unsupported Tier-2 (no per-seg times)
    ],
)
def test_auto_non_serveable_routing_picks_matrix(flag: str, value: object) -> None:
    """Routing GF can't honor (fare construction, ordered chains, unsupported
    Tier-2) falls back to Matrix."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


# PP-* flags no longer influence backend choice (work-qmx1): PP overlay runs on
# both backends, so `--pp-only` on a plain query stays on gflight (faster). The
# flags aren't in `_pick_backend`'s signature anymore — that's the test.


# ──────────────────────────────── explicit ─────────────────────────────────


def test_explicit_matrix_always_wins() -> None:
    assert _call(BACKEND_MATRIX) == BACKEND_MATRIX
    assert _call(BACKEND_MATRIX, routing="LH+") == BACKEND_MATRIX
    assert _call(BACKEND_MATRIX, extension="F bc=y") == BACKEND_MATRIX


def test_explicit_gflight_with_plain_search() -> None:
    assert _call(BACKEND_GFLIGHT) == BACKEND_GFLIGHT


def test_explicit_gflight_allows_gf_serveable_routing() -> None:
    assert _call(BACKEND_GFLIGHT, routing="LH+") == BACKEND_GFLIGHT
    assert _call(BACKEND_GFLIGHT, extension="MAXCONNECT 2:00") == BACKEND_GFLIGHT


def test_explicit_gflight_rejects_unserveable_request() -> None:
    with pytest.raises(typer.BadParameter, match="can't serve"):
        _call(BACKEND_GFLIGHT, extension="F bc=y")
    with pytest.raises(typer.BadParameter, match="can't serve"):
        _call(BACKEND_GFLIGHT, slice_specs=["JFK-LHR:2026-08-15"])


def test_unknown_backend_rejected() -> None:
    with pytest.raises(typer.BadParameter, match="--backend must be one of"):
        _call("nope")
