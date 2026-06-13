# pyright: reportPrivateUsage=false
"""Tests for work-24i2l:

1. `--stops` must reach the gflight backend's fli filter (it was silently
   dropped, so `--stops 0` still returned/pinned connecting itineraries).
2. `--pick N` selects which itinerary the deep links pin, instead of always
   pinning the globally-cheapest row.
"""

from __future__ import annotations

from datetime import date

import pytest

from flight_cli.cli import (
    _pinned_solution_index,
    _try_pinned_gflight_url,
    _try_pinned_matrix_url,
)
from flight_cli.domain import Cabin, Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter
from flight_cli.models import SearchResult


def _search(max_extra_stops: int | None) -> SpecificDateSearch:
    return SpecificDateSearch(
        legs=(Leg.of("JFK", "LHR", date(2026, 8, 15)),),
        options=SearchOptions(cabin=Cabin.COACH, max_extra_stops=max_extra_stops),
    )


# ──────────────────────── --stops → fli MaxStops ────────────────────────────


@pytest.mark.parametrize(
    "max_extra_stops,expected_name",
    [
        (0, "NON_STOP"),
        (1, "ONE_STOP_OR_FEWER"),
        (2, "TWO_OR_FEWER_STOPS"),
        (3, "ANY"),  # fli's enum tops out at "2 or fewer"
        (None, "ANY"),  # no limit
    ],
)
def test_stops_maps_to_fli_maxstops(max_extra_stops: int | None, expected_name: str) -> None:
    f = to_fli_filter(_search(max_extra_stops))
    assert f.stops.name == expected_name


def test_nonstop_filter_is_not_dropped() -> None:
    # Regression: the gflight backend used to ignore --stops entirely, so a
    # nonstop request still asked Google Flights for everything.
    f = to_fli_filter(_search(0))
    assert f.stops.value == 1  # MaxStops.NON_STOP


# ──────────────────────── _pinned_solution_index ───────────────────────────


def _result(n: int) -> SearchResult:
    # model_validate (dict in) builds each Itinerary from defaults, matching how
    # the real code constructs results from the wire.
    return SearchResult.model_validate({"solutions": [{} for _ in range(n)]})


def test_pick_none_pins_cheapest() -> None:
    assert _pinned_solution_index(_result(3), None) == 0


def test_pick_selects_one_based_row() -> None:
    assert _pinned_solution_index(_result(3), 1) == 0
    assert _pinned_solution_index(_result(3), 2) == 1
    assert _pinned_solution_index(_result(3), 3) == 2


def test_pick_out_of_range_falls_back_to_cheapest(capsys: pytest.CaptureFixture[str]) -> None:
    assert _pinned_solution_index(_result(3), 5) == 0
    assert "out of range" in capsys.readouterr().out


def test_pick_zero_is_out_of_range(capsys: pytest.CaptureFixture[str]) -> None:
    assert _pinned_solution_index(_result(3), 0) == 0
    assert "out of range" in capsys.readouterr().out


def test_no_solutions_returns_none() -> None:
    assert _pinned_solution_index(_result(0), 2) is None
    assert _pinned_solution_index(None, 2) is None


# ──────────────── --pick flows through to the actual deep link ──────────────


def _slice_dict(flight: str) -> dict[str, object]:
    return {
        "flights": [flight],
        "departure": "2026-08-15T09:00:00",
        "arrival": "2026-08-15T12:00:00",
        "origin": {"code": "JFK"},
        "destination": {"code": "LAX"},
        "stops": [],
    }


def _oneway_search() -> SpecificDateSearch:
    return SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", date(2026, 8, 15)),),
        options=SearchOptions(cabin=Cabin.COACH),
    )


def test_pick_changes_gflight_pinned_url() -> None:
    # Two itineraries differing only by flight number — selecting #2 instead of
    # the cheapest must pin a different itinerary in the deep link.
    result = SearchResult.model_validate(
        {
            "solutions": [
                {"itinerary": {"slices": [_slice_dict("AA100")]}},
                {"itinerary": {"slices": [_slice_dict("AA200")]}},
            ]
        }
    )
    search = _oneway_search()
    url0 = _try_pinned_gflight_url(search, result, 0)
    url1 = _try_pinned_gflight_url(search, result, 1)
    assert url0 is not None
    assert url1 is not None
    assert url0 != url1


def test_pick_changes_matrix_pinned_url() -> None:
    result = SearchResult.model_validate(
        {
            "session": "sess123",
            "solutionSet": "ss456",
            "solutions": [
                {"id": "sol-cheapest", "itinerary": {"slices": [_slice_dict("AA100")]}},
                {"id": "sol-second", "itinerary": {"slices": [_slice_dict("AA200")]}},
            ],
        }
    )
    search = _oneway_search()
    url0 = _try_pinned_matrix_url(search, result, 0)
    url1 = _try_pinned_matrix_url(search, result, 1)
    assert url0 is not None
    assert url1 is not None
    assert url0 != url1
