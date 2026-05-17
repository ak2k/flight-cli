# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Tests for the provider-selection resolver in cli.py.

Covers:
  - New surface: --providers / --cash-only / --awards-only / --provider-opt
  - Deprecated alias forwarding: --no-pp / --pp-only / --pp-airlines / --pp-cabin
  - Conflict detection across old + new surface
  - Config.toml merging with CLI overrides
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from flight_cli import _config
from flight_cli.cli import _resolve_providers

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture
def _config_dir(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    monkeypatch.setenv(_config.CONFIG_DIR_ENV, str(tmp_path))
    return tmp_path


def test_default_resolution_no_flags(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
    )
    assert sel.provider_filter is None
    assert sel.cash_only is False
    assert sel.awards_only is False
    assert sel.provider_opts == {}


def test_providers_csv_parsing(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers="pp,seats",
        cash_only=False,
        awards_only=False,
        provider_opt=(),
    )
    assert sel.provider_filter == ("pp", "seats")


def test_providers_empty_string_rejected(_config_dir: Path) -> None:
    with pytest.raises(typer.Exit):
        _resolve_providers(
            providers=",,",
            cash_only=False,
            awards_only=False,
            provider_opt=(),
        )


def test_cash_only_and_awards_only_conflict(_config_dir: Path) -> None:
    with pytest.raises(typer.Exit):
        _resolve_providers(
            providers=None,
            cash_only=True,
            awards_only=True,
            provider_opt=(),
        )


def test_legacy_no_pp_forwards_to_cash_only(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
        legacy_no_pp=True,
    )
    assert sel.cash_only is True


def test_legacy_pp_only_forwards_to_awards_only(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
        legacy_pp_only=True,
    )
    assert sel.awards_only is True


def test_legacy_pp_airlines_forwards_to_provider_opts(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
        legacy_pp_airlines="United,Delta",
    )
    assert sel.provider_opts == {"pp": {"airlines": ["United", "Delta"]}}
    assert sel.pp_airlines() == "United,Delta"


def test_legacy_pp_cabin_forwards_to_provider_opts(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
        legacy_pp_cabin="Economy,Business",
    )
    assert sel.provider_opts == {"pp": {"cabins": ["Economy", "Business"]}}
    assert sel.pp_cabins() == "Economy,Business"


def test_legacy_no_pp_conflicts_with_new_surface(_config_dir: Path) -> None:
    with pytest.raises(typer.Exit):
        _resolve_providers(
            providers=None,
            cash_only=True,
            awards_only=False,
            provider_opt=(),
            legacy_no_pp=True,
        )


def test_legacy_pp_only_conflicts_with_providers(_config_dir: Path) -> None:
    with pytest.raises(typer.Exit):
        _resolve_providers(
            providers="pp",
            cash_only=False,
            awards_only=False,
            provider_opt=(),
            legacy_pp_only=True,
        )


def test_provider_opt_cli_only(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=("pp.airlines=United,Delta",),
    )
    assert sel.provider_opts == {"pp": {"airlines": ["United", "Delta"]}}


def test_config_toml_seeds_provider_opts(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text(
        """\
[providers.pp]
airlines = ["United"]
cabins = ["Economy"]
"""
    )
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
    )
    assert sel.provider_opts == {"pp": {"airlines": ["United"], "cabins": ["Economy"]}}


def test_cli_override_wins_over_config(_config_dir: Path) -> None:
    (_config_dir / "config.toml").write_text('[providers.pp]\nairlines = ["United"]\n')
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=("pp.airlines=Delta",),
    )
    assert sel.provider_opts["pp"]["airlines"] == "Delta"


def test_cli_provider_opt_preempts_legacy_pp_airlines(_config_dir: Path) -> None:
    """If both --provider-opt and --pp-airlines are set, --provider-opt wins.

    Rationale: the new surface is preferred; legacy is only for unmigrated
    scripts. Erroring would break the migration path; silent wins-old would
    surprise the user who reached for the new flag."""
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=("pp.airlines=Delta",),
        legacy_pp_airlines="United,JetBlue",
    )
    assert sel.provider_opts["pp"]["airlines"] == "Delta"


def test_pp_airlines_helper_handles_list(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=("pp.airlines=United,Delta",),
    )
    assert sel.pp_airlines() == "United,Delta"


def test_pp_cabins_helper_handles_scalar(_config_dir: Path) -> None:
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=("pp.cabins=Economy",),
    )
    assert sel.pp_cabins() == "Economy"


def test_invalid_provider_opt_format(_config_dir: Path) -> None:
    with pytest.raises(typer.Exit):
        _resolve_providers(
            providers=None,
            cash_only=False,
            awards_only=False,
            provider_opt=("pp.airlines",),  # missing =
        )
