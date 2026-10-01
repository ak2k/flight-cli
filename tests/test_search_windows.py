# pyright: reportPrivateUsage=false
"""Time windows on `flight search`: one window to the minute on a departure,
and an arrival window, which only Google Flights can be asked for.

Google takes whole hours, so the page is asked for the hours around the window
and every row is held to its minutes. Matrix takes a departure window to the
minute in `timeRanges` and no arrival time at all, so an arrival window keeps
the search on Google as `--bags` does, and is never handed to Matrix later."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_postfilter import routing_keep, row_check_names
from flight_cli.domain import (
    ClockWindow,
    Leg,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
    time_bounds,
    time_range_for,
    window_label,
)
from flight_cli.links import matrix_deep_link
from flight_cli.wire import to_wire
from test_gf_full_board import _tfs
from test_gf_postfilter import _row
from test_links_search_tfs import _slices

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_LAX = "ds1_jfk_lax_tfu.json"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_EVENING = ClockWindow(first=18 * 60, last=21 * 60 + 30)


def _served(name: str = _LAX) -> str:
    return _page(_answering(_ds1(name), origin=None, destination=None, date=_DEP.isoformat()))


def _search(*extra: str) -> list[str]:
    return [*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), *extra]


def _no_matrix(**_kw: object) -> None:
    pytest.fail("the search went to Matrix")


def _lands(member: dict[str, Any]) -> str:
    return member["legs"][-1]["arrival_datetime"][11:16]


# ─────────────────────────────── the parser ────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "first", "last"),
    [
        ("9:30-13:45", 570, 825),
        ("09:30-13:45", 570, 825),
        (" 18:00-21:30 ", 1080, 1290),
        ("00:00-23:59", 0, 1439),
        ("0:00-0:01", 0, 1),
    ],
)
def test_one_window_parses_to_its_first_and_last_minute(raw: str, first: int, last: int) -> None:
    assert cli._parse_search_times(raw, "--depart-times") == (ClockWindow(first=first, last=last),)


def test_the_bucket_names_parse_as_they_always_have() -> None:
    assert cli._parse_search_times("morning,noon", "--depart-times") == cli._parse_times(
        "morning,noon"
    )
    assert cli._parse_search_times(None, "--depart-times") == ()


@pytest.mark.parametrize(
    "raw",
    [
        "13:45-9:30",  # last before first
        "9:30-9:30",  # no minute between
        "24:00-25:00",
        "9:60-10:00",
        "9:30-11:00,14:00-16:00",  # one window, not a list
        "9-13",
        "9:30",
        "morning,9:30-11:00",
    ],
)
def test_a_window_that_is_not_one_window_of_the_day_is_exit_2(
    raw: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        cli._parse_search_times(raw, "--depart-times")
    assert excinfo.value.exit_code == 2
    assert "bad --depart-times" in " ".join(capsys.readouterr().err.split())


def test_an_arrival_list_that_is_not_one_window_is_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._parse_arrival_times("morning,midday", "--arrive-times") == (
        TimeOfDay.MORNING,
        TimeOfDay.MIDDAY,
    )
    with pytest.raises(typer.Exit) as excinfo:
        cli._parse_arrival_times("morning,evening", "--arrive-times")
    assert excinfo.value.exit_code == 2
    printed = " ".join(capsys.readouterr().err.split())
    assert "--arrive-times takes one window, and morning, evening are not one" in printed


# ─────────────────────────────── the domain ────────────────────────────────


def test_a_clock_window_reads_as_matrix_and_the_user_write_it() -> None:
    window = ClockWindow(first=570, last=825)
    assert time_range_for(window) == {"min": "9:30", "max": "13:45"}
    assert time_bounds(window) == (570, 825)
    assert window_label(window) == "09:30-13:45"
    assert time_range_for(TimeOfDay.EARLY_MORNING) == {"min": "00:00", "max": "8:00"}


@pytest.mark.parametrize(("first", "last"), [(825, 570), (600, 600), (-1, 10), (0, 1440)])
def test_a_clock_window_refuses_what_is_not_one(first: int, last: int) -> None:
    with pytest.raises(ValueError, match="validation error"):
        ClockWindow(first=first, last=last)


def test_a_leg_built_with_of_carries_its_arrival_window() -> None:
    leg = Leg.of("JFK", "LAX", _DEP, arrival_ranges=(_EVENING,))
    assert leg.arrival_ranges == (_EVENING,)
    assert Leg.of("JFK", "LAX", _DEP).arrival_ranges == ()


# ───────────────────────────── Google: the page ────────────────────────────


def test_an_arrival_window_asks_the_page_for_its_whole_hours(
    gf_session: Callable[..., Any],
) -> None:
    fake = gf_session(_served())
    result = CliRunner().invoke(
        cli.app,
        _search(
            "--arrive-times", "18:00-21:30", "--backend", "gflight", "--fast", "--format", "json"
        ),
    )
    assert result.exit_code == 0, result.output
    outbound = _slices(_tfs(fake.gets[0]))[0]
    # 3.8-3.9 the departure hours, left open; 3.10-3.11 the arrival hours.
    assert (outbound[8], outbound[9], outbound[10], outbound[11]) == ([0], [23], [18], [21])


def test_a_minute_departure_window_asks_the_page_for_its_whole_hours(
    gf_session: Callable[..., Any],
) -> None:
    fake = gf_session(_served())
    result = CliRunner().invoke(
        cli.app,
        _search(
            "--depart-times", "9:30-13:45", "--backend", "gflight", "--fast", "--format", "json"
        ),
    )
    assert result.exit_code == 0, result.output
    outbound = _slices(_tfs(fake.gets[0]))[0]
    assert (outbound[8], outbound[9], outbound[10], outbound[11]) == ([9], [13], [0], [23])
    departs = [m["legs"][0]["departure_datetime"][11:16] for m in json.loads(result.stdout)]
    assert departs and all("09:30" <= d <= "13:45" for d in departs), departs


# ──────────────────────────── Google: the rows ─────────────────────────────


def _lax_rows() -> list[gfid.GFlightWithId]:
    payload: list[Any] = json.loads(_ds1(_LAX))
    return [gfid._parse_flight_with_id(raw) for raw in gfid._rows_from_ds1(payload).rows]


def _clock(stamp: datetime) -> str:
    return f"{stamp:%H:%M}"


def test_the_lax_board_keeps_exactly_the_rows_landing_inside_the_window() -> None:
    """Google's latest hour 21 answers up to 21:59; the capture has two rows
    landing in that gap (21:34 and 21:59), and the row check drops both."""
    keep = routing_keep([[]], per_slice_arrivals=[(_EVENING,)])
    assert keep is not None
    rows = _lax_rows()
    kept = [_clock(r.flight.legs[-1].arrival_datetime) for r in rows if keep(0, r)]
    expected = [
        _clock(stamp)
        for r in rows
        if "18:00" <= _clock(stamp := r.flight.legs[-1].arrival_datetime) <= "21:30"
    ]
    assert kept == expected
    assert len(kept) == 16
    late = [_clock(r.flight.legs[-1].arrival_datetime) for r in rows]
    assert {"21:34", "21:59"} <= set(late)
    assert not {"21:34", "21:59"} & set(kept)


def _landing(hour: int, minute: int) -> gfid.GFlightWithId:
    lands = hour * 60 + minute
    return _row(("AA", "JFK", "LAX", lands - 360, lands), duration=360)


def test_an_arrival_window_includes_both_bounds_to_the_minute() -> None:
    keep = routing_keep([[]], per_slice_arrivals=[(_EVENING,)])
    assert keep is not None
    assert [keep(0, _landing(h, m)) for h, m in ((17, 59), (18, 0), (21, 30), (21, 31))] == [
        False,
        True,
        True,
        False,
    ]


def test_the_arrival_held_is_the_last_legs() -> None:
    connection = _row(
        ("AA", "JFK", "ORD", 8 * 60, 10 * 60), ("AA", "ORD", "LAX", 11 * 60, 19 * 60), duration=660
    )
    keep = routing_keep([[]], per_slice_arrivals=[(_EVENING,)])
    assert keep is not None
    assert keep(0, connection)


def test_a_return_arrival_window_holds_the_return_alone() -> None:
    keep = routing_keep([[], []], per_slice_arrivals=[(), (_EVENING,)])
    assert keep is not None
    assert keep(0, _landing(12, 0))
    assert not keep(1, _landing(12, 0))
    assert keep(1, _landing(19, 0))


def test_a_minute_departure_window_holds_the_rows_to_the_minute() -> None:
    keep = routing_keep([[]], [(ClockWindow(first=570, last=825),)])
    assert keep is not None
    departing = [_row(("AA", "JFK", "LAX", m, m + 360), duration=360) for m in (569, 570, 825, 826)]
    assert [keep(0, r) for r in departing] == [False, True, True, False]


def test_the_windows_are_named_as_the_user_wrote_them() -> None:
    names = row_check_names(
        [[], []],
        [(ClockWindow(first=570, last=825),), ()],
        per_slice_arrivals=[(_EVENING,), (TimeOfDay.EVENING,)],
    )
    assert names == [
        "a departure-time window (09:30-13:45)",
        "an arrival-time window (18:00-21:30)",
        "a return arrival-time window (evening)",
    ]


def test_every_printed_row_lands_inside_the_window(gf_session: Callable[..., Any]) -> None:
    gf_session(_served())
    result = CliRunner().invoke(
        cli.app,
        _search(
            "--arrive-times",
            "18:00-21:30",
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
            "-n",
            "40",
        ),
    )
    assert result.exit_code == 0, result.output
    landed = [_lands(m) for m in json.loads(result.stdout)]
    assert len(landed) == 16
    assert all("18:00" <= t <= "21:30" for t in landed), landed


# ───────────────────────────── Matrix: the body ────────────────────────────


def test_a_minute_departure_window_goes_to_matrix_in_time_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[SpecificDateSearch] = []

    def _matrix(*, legs: tuple[Leg, ...], opts: SearchOptions, **_kw: object) -> None:
        seen.append(SpecificDateSearch(legs=legs, options=opts))

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app, _search("--depart-times", "09:30-13:45", "--backend", "matrix", "--format", "json")
    )
    assert result.exit_code == 0, result.output
    body = to_wire(seen[0]).as_json()
    assert body["inputs"]["slices"][0]["timeRanges"] == [{"min": "9:30", "max": "13:45"}]


def test_auto_serves_a_minute_departure_window_on_google() -> None:
    assert (
        cli._pick_backend(
            backend=cli.BACKEND_AUTO,
            routing=None,
            extension=None,
            slice_specs=None,
            depart_times="09:30-13:45",
            return_times="18:00-21:30",
            stops=None,
            children=0,
            seniors=0,
            youth=0,
            inf_seat=0,
            inf_lap=0,
            origin="JFK",
            destination="LAX",
            allow_airport_changes=True,
            show_only_available=True,
        )
        == cli.BACKEND_GFLIGHT
    )


def test_matrix_never_takes_an_arrival_window() -> None:
    leg = Leg.of("JFK", "LAX", _DEP, arrival_ranges=(_EVENING,))
    with pytest.raises(ValueError, match="arrival"):
        to_wire(SpecificDateSearch(legs=(leg,)))


def test_the_matrix_link_names_the_windows_it_leaves_out() -> None:
    windowed = SpecificDateSearch(
        legs=(
            Leg.of(
                "JFK",
                "LAX",
                _DEP,
                time_ranges=(ClockWindow(first=570, last=825),),
                arrival_ranges=(_EVENING,),
            ),
        )
    )
    plain = SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _DEP),))
    assert matrix_deep_link(windowed) == matrix_deep_link(plain)
    notes = cli._matrix_link_caveats(windowed)
    assert len(notes) == 1
    assert "09:30-13:45, 18:00-21:30" in notes[0]
    bucketed = SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP, time_ranges=(TimeOfDay.MORNING,)),)
    )
    assert cli._matrix_link_caveats(bucketed) == []


# ─────────────────────────────── the refusals ──────────────────────────────


def _refused(*extra: str) -> str:
    # Wide, so typer's error box wraps no sentence these tests read.
    result = CliRunner().invoke(cli.app, _search(*extra), env={"COLUMNS": "400"})
    assert result.exit_code == 2, result.output
    return " ".join(result.output.replace("│", " ").split())


def test_a_return_arrival_window_without_a_return_is_exit_2() -> None:
    printed = _refused("--return-arrive-times", "18:00-21:30")
    assert "--return-arrive-times sets when the return lands, and needs a --return" in printed


def test_an_arrival_list_on_the_command_line_is_exit_2() -> None:
    printed = _refused("--arrive-times", "morning,evening")
    assert "--arrive-times takes one window" in printed


def test_an_arrival_window_on_matrix_is_exit_2() -> None:
    printed = _refused("--arrive-times", "18:00-21:30", "--backend", "matrix")
    assert "--arrive-times needs Google Flights: Matrix takes no arrival time" in printed


def test_an_arrival_window_beside_a_matrix_reason_is_exit_2_naming_it() -> None:
    printed = _refused("--arrive-times", "18:00-21:30", "--routing", "BA AA")
    assert "--arrive-times needs Google Flights, which can't serve routing 'BA AA'" in printed
    assert "Using Matrix" not in printed


def test_a_return_arrival_window_on_matrix_names_its_own_flag() -> None:
    printed = _refused(
        "--return", _RET.isoformat(), "--return-arrive-times", "18:00-21:30", "--backend", "matrix"
    )
    assert "--return-arrive-times needs Google Flights" in printed


def test_an_arrival_window_beside_several_cabins_is_exit_2() -> None:
    printed = _refused("--arrive-times", "18:00-21:30", "--cabin", "economy,business")
    assert "--arrive-times takes one --cabin" in printed


# ───────────────────────── no Matrix after the fact ────────────────────────


def test_a_board_the_arrival_window_empties_is_answered_on_google(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under auto a board the routing empties goes to Matrix; one an arrival
    window empties cannot, since Matrix would answer it without the window."""
    gf_session(_served())
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(
        cli.app, _search("--arrive-times", "3:00-3:30", "--fast", "--format", "json")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    printed = " ".join(result.stderr.split())
    assert "no itinerary matched an arrival-time window (03:00-03:30)" in printed, printed
    assert "Using Matrix" not in printed


def test_a_failed_google_query_with_an_arrival_window_is_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fails(*_a: object, **_kw: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_gflight_results", _fails)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(
        cli.app, _search("--arrive-times", "18:00-21:30", "--format", "json")
    )
    assert (result.exit_code, result.stdout) == (1, "")
    assert "Using Matrix" not in result.stderr


def test_the_default_table_is_not_enriched_against_matrix(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served())

    def _no_weave(**_kw: object) -> None:
        pytest.fail("the table was enriched against Matrix")

    monkeypatch.setattr(cli, "_run_enriched_path", _no_weave)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search("--arrive-times", "18:00-21:30"))
    assert result.exit_code == 0, result.output
    assert "No Matrix enrichment: Matrix takes no arrival time." in " ".join(result.stderr.split())
