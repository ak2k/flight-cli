# pyright: reportPrivateUsage=false
"""`flight explore` reads a destination whose fare is not a finite number as it
reads one with no fare: unpriced, and so left out of the priced answer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from flight_cli import _gf_explore as ge
from test_gf_explore import (
    _explore,
    _explore_body,
    _fare,
    _fixed_today_and_wide_consoles,
    _info,
    _serve,
)

if TYPE_CHECKING:
    from typing import Any

__all__ = ["_fixed_today_and_wide_consoles"]

# `inf` is what a JSON `1e400` reads as; 10**400 is an integer no float holds.
_BAD_FARES = [
    pytest.param(float("inf"), id="inf"),
    pytest.param(10**400, id="int-beyond-float"),
]


def _body(bad_fare: Any) -> str:
    return _explore_body(
        [_info("/m/1", "Austin"), _info("/m/2", "Boston")],
        [_fare("/m/1", cast("int", bad_fare)), _fare("/m/2", 150)],
    )


@pytest.mark.parametrize("bad_fare", _BAD_FARES)
def test_a_fare_that_is_not_a_finite_number_leaves_the_destination_unpriced(
    bad_fare: Any,
) -> None:
    found = ge.parse_destinations(_body(bad_fare), origin="JFK", month=None)
    assert [(d.name, d.price) for d in found] == [("Austin", None), ("Boston", 150)]
    assert [d.name for d in ge.ExploreAnswer("USD", tuple(found)).priced] == ["Boston"]


@pytest.mark.parametrize("bad_fare", _BAD_FARES)
def test_json_lists_only_the_destination_with_a_finite_fare(
    monkeypatch: pytest.MonkeyPatch, bad_fare: Any
) -> None:
    _serve(monkeypatch, _body(bad_fare))
    result = _explore("JFK", "--format", "json")
    assert result.exit_code == 0, result.output
    assert "Infinity" not in result.stdout
    assert [(row["name"], row["price"]) for row in json.loads(result.stdout)] == [("Boston", 150)]


@pytest.mark.parametrize("bad_fare", _BAD_FARES)
def test_the_table_lists_only_the_destination_with_a_finite_fare(
    monkeypatch: pytest.MonkeyPatch, bad_fare: Any
) -> None:
    _serve(monkeypatch, _body(bad_fare))
    result = _explore("JFK")
    assert result.exit_code == 0, result.output
    assert "USD150.00" in result.stdout
    assert "Austin" not in result.stdout
    assert "inf" not in result.stdout.lower()
