# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Tests for the user-level config.toml loader.

The loader powers the new --providers / --provider-opt surface (work-4byx)
and is the seed of the broader surface-hygiene config story (work-4uls).
Round-trip tests pin: missing file → {}, partial sections → preserved verbatim,
provider lookups by name, CLI override parsing, merge semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from flight_cli import _config

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture
def _config_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    monkeypatch.setenv(_config.CONFIG_DIR_ENV, str(tmp_path))
    return tmp_path


def test_load_missing_file_returns_empty(_config_dir: Path) -> None:
    assert _config.load() == {}


def test_load_round_trip(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text(
        """\
[providers.pp]
airlines = ["United", "Delta"]
cabins = ["Economy", "Business"]

[providers.seats]
api_key = "secret"
"""
    )
    cfg = _config.load()
    assert cfg["providers"]["pp"]["airlines"] == ["United", "Delta"]
    assert cfg["providers"]["pp"]["cabins"] == ["Economy", "Business"]
    assert cfg["providers"]["seats"]["api_key"] == "secret"


def test_provider_options_named_lookup(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text(
        """\
[providers.pp]
airlines = ["United"]
"""
    )
    assert _config.provider_options("pp") == {"airlines": ["United"]}
    assert _config.provider_options("seats") == {}


def test_provider_options_missing_providers_section(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text("[http]\nrps = 2.0\n")
    assert _config.provider_options("pp") == {}


def test_parse_provider_opt_csv_value() -> None:
    parsed = _config.parse_provider_opt_overrides(
        ["pp.airlines=United,Delta"],
    )
    assert parsed == {"pp": {"airlines": ["United", "Delta"]}}


def test_parse_provider_opt_scalar_value() -> None:
    parsed = _config.parse_provider_opt_overrides(["pp.api_key=secret"])
    assert parsed == {"pp": {"api_key": "secret"}}


def test_parse_provider_opt_multiple_providers() -> None:
    parsed = _config.parse_provider_opt_overrides(
        ["pp.airlines=United", "seats.api_key=k"],
    )
    assert parsed == {"pp": {"airlines": "United"}, "seats": {"api_key": "k"}}


def test_parse_provider_opt_rejects_missing_eq() -> None:
    with pytest.raises(ValueError, match="missing '='"):
        _config.parse_provider_opt_overrides(["pp.airlines"])


def test_parse_provider_opt_rejects_missing_dot() -> None:
    with pytest.raises(ValueError, match=r"missing '\.'"):
        _config.parse_provider_opt_overrides(["airlines=United"])


def test_parse_provider_opt_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        _config.parse_provider_opt_overrides([".airlines=United"])


def test_merge_provider_options_inner_keys() -> None:
    base = {"pp": {"airlines": ["A"], "cabins": ["Economy"]}}
    override = {"pp": {"airlines": ["B", "C"]}}
    merged = _config.merge_provider_options(base, override)
    assert merged == {
        "pp": {"airlines": ["B", "C"], "cabins": ["Economy"]},
    }


def test_merge_provider_options_adds_new_provider() -> None:
    base = {"pp": {"airlines": ["A"]}}
    override = {"seats": {"api_key": "k"}}
    merged = _config.merge_provider_options(base, override)
    assert merged == {
        "pp": {"airlines": ["A"]},
        "seats": {"api_key": "k"},
    }


# ─────────────────────────── canonical / aliases ──────────────────────────


def test_canonical_provider_pp_aliases() -> None:
    assert _config.canonical_provider("pp") == "pp"
    assert _config.canonical_provider("PP") == "pp"
    assert _config.canonical_provider("pointspath") == "pp"
    assert _config.canonical_provider("Points-Path") == "pp"


def test_canonical_provider_seats_aliases() -> None:
    assert _config.canonical_provider("seats-aero") == "seats-aero"
    assert _config.canonical_provider("sa") == "seats-aero"
    assert _config.canonical_provider("seatsaero") == "seats-aero"
    assert _config.canonical_provider("seats.aero") == "seats-aero"
    assert _config.canonical_provider("SA") == "seats-aero"


def test_canonical_provider_unknown_passes_through_lowercased() -> None:
    """Unknown names lowercase but otherwise pass through, so callers can
    surface a clear error naming exactly what the user typed."""
    assert _config.canonical_provider("MysteryProvider") == "mysteryprovider"


def test_parse_provider_opt_normalizes_alias(_config_dir: Path) -> None:
    parsed = _config.parse_provider_opt_overrides(["sa.sources=american,united"])
    assert parsed == {"seats-aero": {"sources": ["american", "united"]}}


def test_known_canonical_providers_lists_both() -> None:
    canonicals = _config.known_canonical_providers()
    assert "pp" in canonicals
    assert "seats-aero" in canonicals
