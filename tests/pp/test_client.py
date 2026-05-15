"""Tests for the PointsPath HTTP client's pure helpers (no live API)."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from flight_cli.pp.client import enabled_airlines
from flight_cli.pp.models import PricingInfoResponse

FIX = pathlib.Path(__file__).parent / "fixtures"


def _pricing() -> PricingInfoResponse:
    return PricingInfoResponse.model_validate(json.loads((FIX / "pricing_info.json").read_text()))


def _ext_config() -> dict[str, Any]:
    return json.loads((FIX / "extension_config.json").read_text())


# ────────────────────────── enabled_airlines() ──────────────────────────────


def test_enabled_excludes_explicitly_disabled():
    """`enableSingapore=0` and `enableIberia=0` should suppress those
    airlines even though they appear in pricing-info."""
    enabled = enabled_airlines(_pricing(), _ext_config())
    assert "Iberia" not in enabled
    assert "Singapore" not in enabled


def test_enabled_includes_always_on_airlines():
    """American has no `enable<X>` flag at all — should still be included
    (always-on tier)."""
    enabled = enabled_airlines(_pricing(), _ext_config())
    assert "American" in enabled


def test_enabled_includes_versioned_flag_match():
    """AirFrance is gated by `enableAirFranceV2=1` — the V<n> suffix should
    match without needing a separate alias map."""
    enabled = enabled_airlines(_pricing(), _ext_config())
    assert "AirFrance" in enabled


def test_enabled_ignores_subfeature_flags():
    """`enableDeltaTakeOff15` is a sub-feature flag, not an airline toggle.
    United has `enableUnitedAwardToolApi=0` — that's a sub-feature too, not
    a disable for the airline itself. Both airlines should remain enabled."""
    enabled = enabled_airlines(_pricing(), _ext_config())
    assert "Delta" in enabled
    assert "United" in enabled


def test_enabled_handles_missing_feature_flags_section():
    """Some response variants may omit featureFlags; should fall back to
    universe (assume everything is always-on)."""
    enabled = enabled_airlines(_pricing(), {"status": True})
    universe = {p.airline for p in _pricing().pricingInfos}
    assert set(enabled) == universe


def test_enabled_returns_pricing_order():
    """Order matters for deterministic CLI output and for tests; the helper
    walks pricingInfos in order, so the result preserves it."""
    enabled = enabled_airlines(_pricing(), _ext_config())
    pricing_order = [p.airline for p in _pricing().pricingInfos]
    expected = [a for a in pricing_order if a in set(enabled)]
    assert list(enabled) == expected
