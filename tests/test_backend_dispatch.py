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
        ("routing", "LH+"),
        ("extension", "MAXCONNECT 2:00"),
        ("slice_specs", ["JFK-LHR:2026-08-15"]),
        ("depart_times", "morning"),
        ("return_times", "evening"),
        ("seniors", 1),
        ("youth", 1),
        ("inf_seat", 1),
        ("inf_lap", 1),
    ],
)
def test_auto_matrix_only_flag_picks_matrix(flag: str, value: object) -> None:
    # pyright: ignore[reportArgumentType] — `flag` is a parametrize key; types are
    # heterogeneous (str/int/list/bool). _call's kwargs accept object.
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


# PP-* flags no longer influence backend choice (work-qmx1): PP overlay runs on
# both backends, so `--pp-only` on a plain query stays on gflight (faster). The
# flags aren't in `_pick_backend`'s signature anymore — that's the test.


# ──────────────────────────────── explicit ─────────────────────────────────


def test_explicit_matrix_always_wins() -> None:
    assert _call(BACKEND_MATRIX) == BACKEND_MATRIX
    assert _call(BACKEND_MATRIX, routing="LH+") == BACKEND_MATRIX


def test_explicit_gflight_with_plain_search() -> None:
    assert _call(BACKEND_GFLIGHT) == BACKEND_GFLIGHT


def test_explicit_gflight_rejects_matrix_only_flags() -> None:
    with pytest.raises(typer.BadParameter, match="incompatible"):
        _call(BACKEND_GFLIGHT, routing="LH+")


def test_unknown_backend_rejected() -> None:
    with pytest.raises(typer.BadParameter, match="--backend must be one of"):
        _call("nope")
