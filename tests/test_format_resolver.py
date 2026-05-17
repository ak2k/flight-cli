# pyright: reportPrivateUsage=false
"""Tests for --format / --json resolution.

The resolver collapses the new --format and the deprecated --json into a
single output-format string. --json emits a deprecation warning but still
forwards to --format json for one release. Conflicting combos error out.
"""

from __future__ import annotations

import pytest
import typer

from flight_cli.cli import _resolve_format


def test_default_is_table() -> None:
    assert _resolve_format(fmt="table", json_flag=False) == "table"


def test_format_json() -> None:
    assert _resolve_format(fmt="json", json_flag=False) == "json"


def test_legacy_json_flag_forwards() -> None:
    assert _resolve_format(fmt="table", json_flag=True) == "json"


def test_legacy_json_with_explicit_format_json_is_ok() -> None:
    """Belt-and-suspenders: --json --format json is redundant, not a conflict."""
    assert _resolve_format(fmt="json", json_flag=True) == "json"


def test_legacy_json_conflicts_with_explicit_other_format() -> None:
    """--json + --format csv (when supported) should fail loudly."""
    # We don't yet ship csv as a valid format, but the resolver still rejects
    # the ambiguity. Use a format string that would conflict but isn't valid.
    # Since csv isn't in _VALID_FORMATS, the bad-format check fires first
    # when json_flag is False. When json_flag is True we hit the conflict
    # branch — test that path with a non-table, non-json fmt.
    with pytest.raises(typer.Exit):
        _resolve_format(fmt="csv", json_flag=True)


def test_invalid_format_rejected() -> None:
    with pytest.raises(typer.Exit):
        _resolve_format(fmt="yaml", json_flag=False)


def test_empty_format_rejected() -> None:
    with pytest.raises(typer.Exit):
        _resolve_format(fmt="", json_flag=False)
