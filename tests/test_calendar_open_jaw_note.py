# pyright: reportPrivateUsage=false
"""The fan-out note and the `--max-per-query` help name the round trips only the
combined query prices (work-h70kv.108)."""

from __future__ import annotations

import inspect
from datetime import date
from typing import Any

import pytest

from flight_cli import cli
from flight_cli.client import MatrixApiError
from flight_cli.domain import Cabin, CalendarSearch, CalendarWindow, Leg, SearchOptions
from flight_cli.models import CalendarResult
from test_calendar_split import _calendar_fast, _pair_client, _priced, _spy_renderers

_WINDOW = CalendarWindow(
    start=date(2026, 9, 7), end=date(2026, 10, 7), duration_min=5, duration_max=7
)
_EMPTY: dict[str, Any] = {"solutionCount": 0, "calendar": {"months": []}}

_ORIGIN_SIDE = "return to a different origin airport"
_DESTINATION_SIDE = "come back from a different destination airport"
_ACROSS_GROUPS = "come back from a destination airport in another group"


def _deliver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    n_split: int,
    floor_lost: bool = False,
) -> None:
    """Deliver a round-trip fan-out as JSON with the sub-queries stubbed, so only
    the stderr note is under test."""
    search = CalendarSearch(
        legs=(Leg.of(list(origins), list(dests)), Leg.of(list(dests), list(origins))),
        options=SearchOptions(cabin=Cabin.COACH),
        window=_WINDOW,
    )
    answer = CalendarResult.from_api(_EMPTY)

    def _stub(*_a: object, **_k: object) -> tuple[CalendarResult, int, bool]:
        return answer, n_split, floor_lost

    monkeypatch.setattr(cli, "_run_calendar", _stub)
    cli._run_matrix_calendar(
        search,
        origins=origins,
        dests=dests,
        sd=_WINDOW.start,
        ed=_WINDOW.end,
        dmin=5,
        dmax=7,
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
        max_per_query=1,
        max_concurrency=12,
        json_out=True,
        matrix_url=False,
        google_url=False,
    )


@pytest.mark.parametrize(
    ("origins", "dests", "n_split", "clause"),
    [
        pytest.param(("JFK",), ("LON",), 7, _DESTINATION_SIDE, id="one-origin"),
        pytest.param(("NYC",), ("LHR",), 4, _ORIGIN_SIDE, id="one-destination"),
        pytest.param(
            ("NYC",), ("LON",), 19, f"{_ORIGIN_SIDE} or {_DESTINATION_SIDE}", id="both-sides"
        ),
    ],
)
def test_the_note_names_the_side_the_combined_query_alone_prices(
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    n_split: int,
    clause: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _deliver(monkeypatch, origins=origins, dests=dests, n_split=n_split)
    note = " ".join(capsys.readouterr().err.split())
    assert f"Round trips that {clause} come only from the combined query" in note
    assert "another airport of the set" not in note


def test_a_lost_combined_query_names_the_same_side(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _deliver(monkeypatch, origins=("JFK",), dests=("LON",), n_split=6, floor_lost=True)
    note = " ".join(capsys.readouterr().err.split())
    assert f"Round trips that {_DESTINATION_SIDE} are missing: only the combined query" in note


def test_the_max_per_query_help_names_both_sides() -> None:
    option = inspect.signature(cli.calendar).parameters["max_per_query"].default
    help_text = " ".join(str(option.help).split())
    assert "another airport of the set" not in help_text
    assert (
        f"{_ORIGIN_SIDE}, or come back from a destination airport in another request," in help_text
    )
    assert _DESTINATION_SIDE not in help_text


@pytest.mark.parametrize(
    ("combined", "verb"),
    [
        pytest.param(_priced("USD500.00"), "come only from the combined query", id="merged"),
        pytest.param(
            MatrixApiError("COMBINED UNAVAILABLE", kind="internal"),
            "are missing: only the combined query",
            id="lost",
        ),
    ],
)
def test_a_grouped_fanout_names_only_the_returns_across_groups(
    combined: CalendarResult | Exception,
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A group's query asks a return from every airport of the group, so out to
    LHR and back from LGW is in the grid whether or not the combined query ran."""
    _pair_client(
        monkeypatch,
        {
            ("JFK", "LHR,LGW"): _priced("USD600.00"),
            ("JFK", "STN,LTN"): _priced("USD650.00"),
            ("JFK", "LHR,LGW,STN,LTN"): combined,
        },
    )
    _spy_renderers(monkeypatch)
    _calendar_fast(
        fast=False, fmt="json", destination="LHR,LGW,STN,LTN", one_way=False, max_per_query=2
    )
    note = " ".join(capsys.readouterr().err.split())
    assert f"Round trips that {_ACROSS_GROUPS} {verb}" in note
    assert _DESTINATION_SIDE not in note


def test_one_destination_group_leaves_only_the_origin_side(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pair_client(
        monkeypatch,
        {
            ("JFK", "LHR,LGW"): _priced("USD600.00"),
            ("EWR", "LHR,LGW"): _priced("USD650.00"),
            ("JFK,EWR", "LHR,LGW"): _priced("USD500.00"),
        },
    )
    _spy_renderers(monkeypatch)
    _calendar_fast(
        fast=False,
        fmt="json",
        origin="JFK,EWR",
        destination="LHR,LGW",
        one_way=False,
        max_per_query=2,
    )
    note = " ".join(capsys.readouterr().err.split())
    assert f"Round trips that {_ORIGIN_SIDE} come only from the combined query" in note
    assert "come back from" not in note
