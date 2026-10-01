# pyright: reportPrivateUsage=false
"""`-REDEYES` and `-OVERNIGHTS` checked on Google Flights rows.

Each leg of a row carries its own airports' local clocks, which is all the two
checks need: a red-eye lands on a later local date than it took off, takes off
00:00-04:59, or crosses the date line; an overnight stop is a connection whose
next leg leaves on a later date than the arrival, or that begins 00:00-04:59.
The date grids have no rows to check, so they still refuse both."""

from __future__ import annotations

import itertools
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import FlightLeg, FlightResult  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_postfilter import routing_keep, row_check_names, search_page_reasons
from flight_cli._gflight_ids import GFlightWithId, LegAmenities
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg
from flight_cli.routing_predicates import classify
from test_gf_postfilter import _DAY, _h, _row

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_BOTH = ["-REDEYES", "-OVERNIGHTS"]


def _keep(extension: str) -> Callable[[int, Any], bool]:
    keep = routing_keep([classify(None, extension).predicates])
    assert keep is not None
    return keep


def _board(name: str) -> list[GFlightWithId]:
    payload: list[Any] = json.loads(_ds1(name))
    return [gfid._parse_flight_with_id(raw) for raw in gfid._rows_from_ds1(payload).rows]


def _night_leg(leg: Any) -> bool:
    """The red-eye rule as stated, for a leg that does not cross the date line."""
    off, on = leg.departure_datetime, leg.arrival_datetime
    return on.date() > off.date() or off.hour < 5


# ─────────────────────────── the JFK-LHR board ─────────────────────────────


def test_on_the_lhr_board_red_eyes_drop_exactly_the_rows_with_a_night_leg() -> None:
    rows = _board("ds1_jfk_lhr_tfu.json")
    legs = [leg for r in rows for leg in r.flight.legs]
    # No leg of this board crosses the date line: its clocks and its duration
    # differ by the zone difference alone.
    assert all(
        abs((lg.arrival_datetime - lg.departure_datetime).total_seconds() // 60 - lg.duration)
        < 12 * 60
        for lg in legs
    )
    late = [lg for lg in legs if lg.arrival_datetime.date() > lg.departure_datetime.date()]
    early = [lg for lg in legs if lg.departure_datetime.hour < 5]
    assert (len(rows), len(legs), len(late), len(early)) == (101, 185, 92, 4)
    keep = _keep("-REDEYES")
    kept = [r for r in rows if keep(0, r)]
    assert kept == [r for r in rows if not any(map(_night_leg, r.flight.legs))]
    assert len(kept) == 5


def test_on_the_lax_board_each_check_drops_its_own_rows() -> None:
    rows = _board("ds1_jfk_lax_tfu.json")
    assert sum(_keep("-REDEYES")(0, r) for r in rows) == 82
    assert sum(_keep("-OVERNIGHTS")(0, r) for r in rows) == 70


# ─────────────────────────────── the arms ──────────────────────────────────


@pytest.mark.parametrize(
    ("takeoff", "kept"), [(_h(0), False), (_h(4) + 59, False), (_h(5), True), (_h(18), True)]
)
def test_a_leg_taking_off_before_five_is_a_red_eye(takeoff: int, kept: bool) -> None:
    row = _row(("AA", "JFK", "BOS", takeoff, takeoff + 75), duration=75)
    assert _keep("-REDEYES")(0, row) is kept


def test_a_leg_landing_on_a_later_date_is_a_red_eye() -> None:
    assert not _keep("-REDEYES")(0, _row(("AA", "LAX", "JFK", _h(22), _h(30.5)), duration=330))
    assert _keep("-REDEYES")(0, _row(("AA", "LAX", "JFK", _h(8), _h(16.5)), duration=330))


def _transpacific(depart: int, arrive: int, duration: int) -> GFlightWithId:
    leg = FlightLeg(
        airline=Airline["AA"],
        flight_number="170",
        departure_airport=Airport["NRT"],
        arrival_airport=Airport["LAX"],
        departure_datetime=_DAY + timedelta(minutes=depart),
        arrival_datetime=_DAY + timedelta(minutes=arrive),
        duration=duration,
    )
    flight = FlightResult(price=900.0, currency="USD", duration=duration, stops=0, legs=[leg])
    return GFlightWithId(
        flight=flight, flight_id="", amenities=[LegAmenities(marketing_carriers=("AA",))]
    )


def test_a_night_across_the_date_line_is_a_red_eye_though_it_lands_on_its_takeoff_date() -> None:
    """Tokyo 17:00 to Los Angeles 10:00 the same date, ten hours in the air:
    its clocks run seven hours backwards, seventeen off its duration."""
    assert not _keep("-REDEYES")(0, _transpacific(_h(17), _h(10), 600))


def test_overnight_stops_are_read_on_the_connecting_airports_clock() -> None:
    keep = _keep("-OVERNIGHTS")
    next_morning = _row(
        ("AA", "LAX", "ORD", _h(15), _h(21)), ("AA", "ORD", "JFK", _h(30), _h(33)), duration=900
    )
    small_hours = _row(
        ("AA", "LAX", "ORD", _h(19), _h(25)), ("AA", "ORD", "JFK", _h(30), _h(33)), duration=840
    )
    same_evening = _row(
        ("AA", "LAX", "ORD", _h(13), _h(19)), ("AA", "ORD", "JFK", _h(20.5), _h(23.5)), duration=630
    )
    assert not keep(0, next_morning)
    assert not keep(0, small_hours)
    assert keep(0, same_evening)


def test_each_check_drops_only_what_it_names() -> None:
    """A day-flight connection with a night at the hub is no red-eye, and a
    red-eye nonstop makes no stop."""
    hub_night = _row(
        ("AA", "LAX", "ORD", _h(15), _h(21)), ("AA", "ORD", "JFK", _h(32), _h(35)), duration=1020
    )
    red_eye = _row(("AA", "LAX", "JFK", _h(22), _h(30.5)), duration=330)
    assert _keep("-REDEYES")(0, hub_night)
    assert not _keep("-OVERNIGHTS")(0, hub_night)
    assert _keep("-OVERNIGHTS")(0, red_eye)
    assert not _keep("-REDEYES")(0, red_eye)


# ───────────────────────────── the gates ───────────────────────────────────


@pytest.mark.parametrize("code", _BOTH)
def test_the_search_page_serves_both(code: str) -> None:
    assert search_page_reasons(classify(None, code).predicates) == []


def test_both_are_named_when_they_empty_a_board() -> None:
    assert row_check_names([classify(None, "-REDEYES; -OVERNIGHTS").predicates]) == [
        "a red-eye exclusion",
        "an overnight-stop exclusion",
    ]


@pytest.mark.parametrize("code", _BOTH)
def test_the_date_grids_still_refuse_both(code: str) -> None:
    from flight_cli._gf_calgraph import graph_blocker, page_blocker
    from flight_cli._gf_dategrid import grid_can_serve

    start = date.today() + timedelta(days=45)
    search = CalendarSearch(
        legs=(Leg.of("JFK", "LAX", extension=code),),
        window=CalendarWindow(
            start=start, end=start + timedelta(days=13), duration_min=0, duration_max=0
        ),
    )
    assert page_blocker(search) is not None
    assert graph_blocker(search) is not None
    assert not grid_can_serve(search)


# ───────────────────────────── the search ──────────────────────────────────


@pytest.mark.parametrize(("code", "rows"), [("-REDEYES", 82), ("-OVERNIGHTS", 70)])
def test_auto_answers_on_google_with_no_night_row(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, code: str, rows: int
) -> None:
    gf_session(
        _page(
            _answering(
                _ds1("ds1_jfk_lax_tfu.json"), origin=None, destination=None, date=_DEP.isoformat()
            )
        )
    )

    def _no_matrix(**_kw: object) -> None:
        pytest.fail("the search went to Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            *("JFK", "LAX", "--dep", _DEP.isoformat(), "--ext", code),
            *("--fast", "--format", "json", "-n", "100"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Using Matrix" not in result.stderr
    doc: list[dict[str, Any]] = json.loads(result.stdout)
    assert len(doc) == rows
    for member in doc:
        stamps = [(lg["departure_datetime"], lg["arrival_datetime"]) for lg in member["legs"]]
        if code == "-REDEYES":
            assert all(off[:10] == on[:10] and off[11:13] >= "05" for off, on in stamps), stamps
        else:
            assert all(
                b[0][:10] == a[1][:10] and a[1][11:13] >= "05"
                for a, b in itertools.pairwise(stamps)
            ), stamps
