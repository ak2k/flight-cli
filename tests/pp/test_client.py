"""Tests for the PointsPath HTTP client's pure helpers (no live API)."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from flight_cli.pp.client import (
    enabled_airlines,
    is_unsupported_airline_response,
    load_unsupported_airlines,
    remember_unsupported_airline,
)
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


# ───────────── unsupported-airline negative cache (400 spam fix) ─────────────


def test_is_unsupported_airline_response_matches_the_real_body() -> None:
    """The exact shape PP returns for an airline it doesn't serve."""
    assert is_unsupported_airline_response(400, '{"error":"unsupported airline"}') is True
    assert is_unsupported_airline_response(400, '{"error":"Unsupported Airline"}') is True


def test_is_unsupported_airline_response_ignores_other_failures() -> None:
    """Must stay narrow: a transient or auth failure would otherwise
    permanently blacklist a working airline."""
    assert is_unsupported_airline_response(400, '{"error":"bad route"}') is False
    assert is_unsupported_airline_response(429, '{"error":"unsupported airline"}') is False
    assert is_unsupported_airline_response(500, "") is False
    assert is_unsupported_airline_response(503, '{"error":"unsupported airline"}') is False


def test_unsupported_cache_roundtrips(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    assert load_unsupported_airlines() == frozenset()
    remember_unsupported_airline("ThaiAirways")
    remember_unsupported_airline("ANA")
    remember_unsupported_airline("ThaiAirways")  # idempotent
    assert load_unsupported_airlines() == frozenset({"ANA", "ThaiAirways"})
    written: Any = json.loads(cache.read_text())
    assert sorted(written) == ["ANA", "ThaiAirways"]


def test_unsupported_cache_expires(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Past the TTL the note is ignored, so PP re-adding an airline (or a tier
    change) heals without the user clearing anything."""
    cache = tmp_path / "unsupported.json"
    cache.write_text(json.dumps({"ANA": time.time()}))
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_TTL_SECS", -1)
    assert load_unsupported_airlines() == frozenset()


def test_unsupported_entries_expire_independently(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Each entry ages on its own clock.

    Keying the TTL off the file mtime meant that learning ANY new airline
    refreshed every existing entry. Since a run that learns one airline
    rewrites the file, in steady state nothing ever expired and the cache
    could not self-heal.
    """
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_TTL_SECS", 100)
    now = time.time()
    cache.write_text(json.dumps({"ANA": now - 500}))  # already stale
    remember_unsupported_airline("Finnair")  # rewrites the file, mtime = now
    assert load_unsupported_airlines() == frozenset({"Finnair"})


def test_unsupported_cache_reads_legacy_list_format(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """The first version wrote a flat list. Upgrading must not re-query every
    unsupported airline; the entries are treated as learned now."""
    cache = tmp_path / "unsupported.json"
    cache.write_text(json.dumps(["ANA", "Southwest"]))
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    assert load_unsupported_airlines() == frozenset({"ANA", "Southwest"})


def test_remember_preserves_existing_timestamps(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Adding an entry must not restamp its siblings."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    original = time.time() - 42
    cache.write_text(json.dumps({"ANA": original}))
    remember_unsupported_airline("Finnair")
    written: Any = json.loads(cache.read_text())
    assert written["ANA"] == original


def test_unsupported_cache_tolerates_corrupt_file(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Fails open — a bad cache costs a wasted request, never a hidden award."""
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", cache)
    cache.write_text("not json{")
    assert load_unsupported_airlines() == frozenset()
    cache.write_text("[1, 2, 3]")  # right container, wrong element type
    assert load_unsupported_airlines() == frozenset()
    cache.write_text('["ANA", 42, null]')  # legacy list, mixed types
    assert load_unsupported_airlines() == frozenset({"ANA"})
    cache.write_text('{"ANA": "yesterday"}')  # non-numeric timestamp
    assert load_unsupported_airlines() == frozenset()
    cache.write_text('{"ANA": true}')  # bool is not a timestamp
    assert load_unsupported_airlines() == frozenset()


def test_remember_survives_unwritable_cache(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """A cache-write failure is swallowed: the run continues and simply
    re-queries that airline next time."""
    unwritable = tmp_path / "nodir" / "sub" / "unsupported.json"
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", unwritable)

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr("flight_cli.pp.client.Path.mkdir", _boom)
    remember_unsupported_airline("ANA")  # must not raise
    assert load_unsupported_airlines() == frozenset()
