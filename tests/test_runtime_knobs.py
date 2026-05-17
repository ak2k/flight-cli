# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Tests for env-var + config.toml resolution of rps/impersonate/no-cache.

Order of precedence: explicit CLI flag (None sentinel means "fall through")
> env var > config.toml > hard-coded default. These tests pin the resolver
helpers and the underlying _config functions independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from flight_cli import _config
from flight_cli.cli import (
    _resolve_impersonate,
    _resolve_no_cache,
    _resolve_rps,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture
def _config_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    monkeypatch.setenv(_config.CONFIG_DIR_ENV, str(tmp_path))
    return tmp_path


# ─────────────────────────── rps resolution ─────────────────────────────


def test_rps_default_when_nothing_set(_config_dir: Path) -> None:
    assert _resolve_rps(None) == _config.DEFAULT_RPS


def test_rps_explicit_flag_wins_over_env(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(_config.RPS_ENV, "5.0")
    assert _resolve_rps(2.0) == 2.0


def test_rps_env_wins_over_config(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    (_config_dir / "config.toml").write_text("[http]\nrps = 3.0\n")
    monkeypatch.setenv(_config.RPS_ENV, "7.5")
    assert _resolve_rps(None) == 7.5


def test_rps_config_wins_over_default(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text("[http]\nrps = 4.0\n")
    assert _resolve_rps(None) == 4.0


def test_rps_bad_env_raises(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(_config.RPS_ENV, "not-a-number")
    with pytest.raises(typer.Exit):
        _resolve_rps(None)


# ─────────────────────────── impersonate resolution ─────────────────────


def test_impersonate_default(_config_dir: Path) -> None:
    assert _resolve_impersonate(None) == _config.DEFAULT_IMPERSONATE


def test_impersonate_explicit_flag_wins(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(_config.IMPERSONATE_ENV, "firefox")
    assert _resolve_impersonate("safari") == "safari"


def test_impersonate_env_wins_over_config(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    (_config_dir / "config.toml").write_text('[http]\nimpersonate = "edge"\n')
    monkeypatch.setenv(_config.IMPERSONATE_ENV, "safari")
    assert _resolve_impersonate(None) == "safari"


def test_impersonate_config(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text('[http]\nimpersonate = "firefox"\n')
    assert _resolve_impersonate(None) == "firefox"


# ─────────────────────────── no-cache resolution ────────────────────────


def test_no_cache_default(_config_dir: Path) -> None:
    assert _resolve_no_cache(False) is False


def test_no_cache_flag_forces_true(_config_dir: Path) -> None:
    assert _resolve_no_cache(True) is True


def test_no_cache_env_truthy(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(_config.NO_CACHE_ENV, "1")
    assert _resolve_no_cache(False) is True


def test_no_cache_env_false(_config_dir: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(_config.NO_CACHE_ENV, "false")
    assert _resolve_no_cache(False) is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_no_cache_env_truthy_variants(
    _config_dir: Path, monkeypatch: MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv(_config.NO_CACHE_ENV, val)
    assert _resolve_no_cache(False) is True


def test_no_cache_config_disabled(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text("[cache]\nenabled = false\n")
    assert _resolve_no_cache(False) is True


def test_no_cache_config_enabled(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text("[cache]\nenabled = true\n")
    assert _resolve_no_cache(False) is False
