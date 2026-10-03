"""Golden-file regression net.

For each captured SPA request body, build the equivalent domain Search,
serialize via `to_wire()`, and diff against the captured JSON. If Matrix
changes the wire shape, this test fails immediately and tells us which
field drifted.

To capture more fixtures: drive a search in the recording harness
(research/record_user_session.py) and drop the resulting req_*.json into
tests/fixtures/."""

from __future__ import annotations

import json
import pathlib
from datetime import date
from typing import Any, cast

import pytest

from flight_cli.domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
)
from flight_cli.wire import booking_details_body, fare_rules_body, to_wire

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

Json = dict[str, Any]


def _load(name: str) -> Json:
    return cast("Json", json.loads((FIXTURES / name).read_text()))


def _strip(d: Json, *keys: str) -> Json:
    """Drop top-level keys we don't reproduce (bgProgramResponse: the
    anti-abuse token; the SPA sends it but the server doesn't validate.
    session/id: server context, not part of the user-facing request)."""
    return {
        k: v
        for k, v in d.items()
        if k not in keys and k not in ("bgProgramResponse", "session", "id")
    }


# ─────────────────────────────── fixtures ──────────────────────────────────
# Each fixture is a captured SPA POST /v1/search body. We reconstruct the
# equivalent domain search by hand and assert that `to_wire().as_json()`
# matches the captured body byte-for-byte (modulo bgProgramResponse).


def test_calendar_round_trip_multi_airport():
    """NYC → [MUC, FRA] round-trip, 5-7 nights, MAXCONNECT 2:00 outbound."""
    captured = _strip(_load("calendar_nyc_munich_frankfurt.json"), "bgProgramResponse")

    search = CalendarSearch(
        legs=(
            Leg.of("NYC", ["MUC", "FRA"], route_language="LH+", extension="MAXCONNECT 2:00"),
            Leg.of(["MUC", "FRA"], "NYC"),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=captured["inputs"]["layover"]["min"],
            duration_max=captured["inputs"]["layover"]["max"],
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_calendarFollowup_after_pick():
    """Same NYC → [MUC,FRA] search but with picked dates 6/7 → 6/11."""
    captured = _strip(_load("followup_nyc_munich_frankfurt.json"), "bgProgramResponse")

    search = CalendarFollowup(
        legs=(
            Leg.of(
                "NYC",
                ["MUC", "FRA"],
                date.fromisoformat("2026-06-07"),
                route_language="LH+",
                extension="MAXCONNECT 2:00",
            ),
            Leg.of(["MUC", "FRA"], "NYC", date.fromisoformat("2026-06-11")),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=captured["inputs"]["layover"]["min"],
            duration_max=captured["inputs"]["layover"]["max"],
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_specific_with_time_ranges():
    """JFK → LHR round-trip with Early Morning + Evening time filters
    selected on the outbound."""
    captured = _strip(_load("specific_jfk_lhr_timeofday.json"), "bgProgramResponse")
    out_slice: Json = captured["inputs"]["slices"][0]
    # Reconstruct: from captured timeRanges, identify which TimeOfDay
    # values were selected.
    times: list[TimeOfDay] = []
    from flight_cli.domain import (
        _TIME_RANGE_FOR,  # pyright: ignore[reportPrivateUsage]
    )

    for tr in out_slice.get("timeRanges", []):
        key = (tr["min"], tr["max"])
        for t in TimeOfDay:
            if _TIME_RANGE_FOR[t] == key:
                times.append(t)
                break

    return_date: str = captured["inputs"]["slices"][1]["date"]
    search = SpecificDateSearch(
        legs=(
            Leg.of("JFK", "LHR", date.fromisoformat(out_slice["date"]), time_ranges=tuple(times)),
            Leg.of("LHR", "JFK", date.fromisoformat(return_date)),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_specific_with_flexible_dates():
    """JFK → LHR round trip, the outbound "+/- 2 days" and the return "Or day
    after" in the SPA's form."""
    captured = _strip(_load("specific_jfk_lhr_rt_flex.json"))
    out_slice, ret_slice = captured["inputs"]["slices"]
    search = SpecificDateSearch(
        legs=(
            Leg.of(
                "JFK", "LHR", date.fromisoformat(out_slice["date"]), date_minus=2, date_plus=2
            ),
            Leg.of("LHR", "JFK", date.fromisoformat(ret_slice["date"]), date_plus=1),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_specific_with_an_arrival_date():
    """JFK → LHR one-way, the date "Arrival" in the SPA's form."""
    captured = _strip(_load("specific_jfk_lhr_ow_arrive.json"))
    (only,) = captured["inputs"]["slices"]
    search = SpecificDateSearch(
        legs=(Leg.of("JFK", "LHR", date.fromisoformat(only["date"]), is_arrival_date=True),),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


# ─────────────────────────────── helpers ───────────────────────────────────


def _diff(expected: Any, actual: Any, path: str = "") -> str:
    """Return a human-readable diff between two dicts, recursively."""
    lines: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        exp = cast("Json", expected)
        act = cast("Json", actual)
        for k in sorted(set(exp.keys()) | set(act.keys())):
            sub = f"{path}.{k}" if path else k
            if k not in exp:
                lines.append(f"  + {sub}: {act[k]!r}")
            elif k not in act:
                lines.append(f"  - {sub}: {exp[k]!r}")
            elif exp[k] != act[k]:
                lines.extend(_diff(exp[k], act[k], sub).splitlines())
    elif expected != actual:
        lines.append(f"  ≠ {path}: expected={expected!r} actual={actual!r}")
    return "Differences:\n" + "\n".join(lines) if lines else ""


# ─────────────────── one-way calendars carry no trip-length ────────────────
# `inputs.layover` is the range of NIGHTS between the outbound and the return,
# so a one-way body has nothing to measure. Matrix does not ignore it: it answers
# HTTP 200 + "Internal server error" (verified live 2026-09-02, work-h70kv.7).


def _oneway_calendar() -> CalendarSearch:
    return CalendarSearch(
        legs=(Leg.of("JFK", "LAX"),),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=CalendarWindow(
            start=date(2026, 10, 10), end=date(2026, 10, 20), duration_min=3, duration_max=5
        ),
    )


def test_one_way_calendar_omits_trip_length():
    body = to_wire(_oneway_calendar()).as_json()
    assert "layover" not in body["inputs"]
    assert body["summarizerSet"] == "calendarOneWay"  # still the one-way summarizer set


def test_round_trip_calendar_keeps_trip_length():
    captured = _strip(_load("calendar_nyc_munich_frankfurt.json"), "bgProgramResponse")
    search = CalendarSearch(
        legs=(
            Leg.of("NYC", ["MUC", "FRA"], route_language="LH+", extension="MAXCONNECT 2:00"),
            Leg.of(["MUC", "FRA"], "NYC"),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=5,
            duration_max=7,
        ),
    )
    body = to_wire(search).as_json()
    assert body["inputs"]["layover"] == {"min": 5, "max": 7}


def test_one_way_followup_omits_trip_length():
    search = CalendarFollowup(
        legs=(Leg.of("JFK", "LAX", date(2026, 10, 10)),),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
        window=CalendarWindow(
            start=date(2026, 10, 10), end=date(2026, 10, 20), duration_min=3, duration_max=5
        ),
    )
    assert "layover" not in to_wire(search).as_json()["inputs"]


# ─────────────────────────────── currency ──────────────────────────────────
# Our own bodies plus `inputs.currency`, each sent live and answered in the
# asked currency: the GBP round trip priced 181 fares GBP and none USD, the
# calendar 23 and none. The SPA fixtures above carry no currency, and must not.


def test_round_trip_carries_the_asked_currency():
    captured = _load("matrix_currency/specific_jfk_lhr_rt_gbp_body.json")
    search = SpecificDateSearch(
        legs=(
            Leg.of("JFK", "LHR", date(2026, 10, 20)),
            Leg.of("LHR", "JFK", date(2026, 10, 27)),
        ),
        options=SearchOptions(currency="GBP"),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_one_way_calendar_carries_the_asked_currency():
    captured = _load("matrix_currency/calendar_jfk_lhr_ow_gbp_body.json")
    search = CalendarSearch(
        legs=(Leg.of("JFK", "LHR"),),
        options=SearchOptions(currency="GBP"),
        window=CalendarWindow(
            start=date(2026, 10, 20), end=date(2026, 10, 26), duration_min=0, duration_max=0
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)


def test_followup_carries_the_asked_currency():
    search = CalendarFollowup(
        legs=(Leg.of("JFK", "LHR", date(2026, 10, 20)),),
        options=SearchOptions(currency="EUR"),
        window=CalendarWindow(
            start=date(2026, 10, 20), end=date(2026, 10, 26), duration_min=0, duration_max=0
        ),
    )
    assert to_wire(search).as_json()["inputs"]["currency"] == "EUR"


def test_no_currency_leaves_the_key_out():
    search = SpecificDateSearch(legs=(Leg.of("JFK", "LHR", date(2026, 10, 20)),))
    assert "currency" not in to_wire(search).as_json()["inputs"]


# ─────────────────────────────── summarize ─────────────────────────────────
# `/v1/summarize` bodies as sent live: each got its answer back.


def test_booking_details_body_matches_the_sent_one():
    captured = _load("summarize/booking_details_body.json")
    solution_set, solution_id = captured["inputs"]["solution"].split("/")
    ours = booking_details_body(
        session=captured["session"], solution_set=solution_set, solution_id=solution_id
    ).as_json()
    assert ours == captured, _diff(captured, ours)


def test_fare_rules_body_matches_the_sent_one():
    captured = _load("summarize/fare_rules_body.json")
    solution_set, solution_id = captured["inputs"]["solution"].split("/")
    ours = fare_rules_body(
        session=captured["session"],
        solution_set=solution_set,
        solution_id=solution_id,
        fare_key=captured["inputs"]["fareKeys"],
    ).as_json()
    assert ours == captured, _diff(captured, ours)


# ─────────────────────────────── stop limit ────────────────────────────────
# `maxLegsRelativeToMin` counts legs beyond the route's own minimum, so on a
# route with no nonstop `--stops 0` still answers one-stop trips. `MAXSTOPS N`
# in each slice's commandLine is absolute: JFK-BKK under `MAXSTOPS 0` answered
# no solution, and beside an earlier `MAXSTOPS 2` a later `MAXSTOPS 0` held.


def _stopped(*extensions: str | None, stops: int | None) -> Json:
    dates = (date(2026, 11, 4), date(2026, 11, 11))
    legs = tuple(
        Leg.of(*(("JFK", "BKK") if i == 0 else ("BKK", "JFK")), dates[i], extension=ext)
        for i, ext in enumerate(extensions)
    )
    search = SpecificDateSearch(legs=legs, options=SearchOptions(max_extra_stops=stops))
    return to_wire(search).as_json()


def _command_lines(body: Json) -> list[str | None]:
    return [s.get("commandLine") for s in body["inputs"]["slices"]]


def test_a_stop_limit_rides_every_slice_as_maxstops() -> None:
    one_way = _stopped(None, stops=0)
    assert _command_lines(one_way) == ["MAXSTOPS 0"]
    assert one_way["inputs"]["maxLegsRelativeToMin"] == 0
    round_trip = _stopped(None, None, stops=0)
    assert _command_lines(round_trip) == ["MAXSTOPS 0", "MAXSTOPS 0"]
    assert round_trip["inputs"]["maxLegsRelativeToMin"] == 0


def test_the_stop_limit_follows_the_users_own_codes() -> None:
    assert _command_lines(_stopped("MAXCONNECT 2:00", stops=1)) == ["MAXCONNECT 2:00; MAXSTOPS 1"]
    assert _command_lines(_stopped("MAXDUR 9:00;", stops=0)) == ["MAXDUR 9:00; MAXSTOPS 0"]
    assert _command_lines(_stopped("MAXDUR 9:00 ; ", stops=0)) == ["MAXDUR 9:00; MAXSTOPS 0"]
    assert _command_lines(_stopped("   ", stops=0)) == ["MAXSTOPS 0"]


def test_a_looser_maxstops_is_followed_by_the_limit() -> None:
    assert _command_lines(_stopped("MAXSTOPS 2", stops=0)) == ["MAXSTOPS 2; MAXSTOPS 0"]
    # Only a later, stricter code was measured to hold beside a looser one.
    assert _command_lines(_stopped("MAXSTOPS 0; MAXSTOPS 2", stops=1)) == [
        "MAXSTOPS 0; MAXSTOPS 2; MAXSTOPS 1"
    ]


def test_a_maxstops_within_the_limit_is_sent_as_typed() -> None:
    assert _command_lines(_stopped("MAXSTOPS 0", stops=1)) == ["MAXSTOPS 0"]
    assert _command_lines(_stopped("maxstops 1", stops=1)) == ["maxstops 1"]
    assert _command_lines(_stopped("MAXSTOPS 1 ;", stops=1)) == ["MAXSTOPS 1 ;"]


def test_each_slice_is_held_to_the_limit_on_its_own() -> None:
    assert _command_lines(_stopped("MAXSTOPS 0", None, stops=1)) == ["MAXSTOPS 0", "MAXSTOPS 1"]


def test_maxlegs_carries_the_limit_itself() -> None:
    assert _stopped(None, stops=2)["inputs"]["maxLegsRelativeToMin"] == 2


def test_a_calendar_and_its_followup_carry_the_limit() -> None:
    window = CalendarWindow(
        start=date(2026, 10, 20), end=date(2026, 11, 2), duration_min=0, duration_max=0
    )
    opts = SearchOptions(max_extra_stops=0)
    calendar = to_wire(CalendarSearch(legs=(Leg.of("LGA", "LAX"),), options=opts, window=window))
    assert _command_lines(calendar.as_json()) == ["MAXSTOPS 0"]
    followup = to_wire(
        CalendarFollowup(
            legs=(Leg.of("LGA", "LAX", date(2026, 10, 20), extension="-REDEYES"),),
            options=opts,
            window=window,
        )
    )
    assert _command_lines(followup.as_json()) == ["-REDEYES; MAXSTOPS 0"]


@pytest.mark.parametrize("stops", [None, -1])
def test_no_stop_limit_sends_the_body_as_typed(stops: int | None) -> None:
    assert _command_lines(_stopped("MAXDUR 9:00;", None, stops=stops)) == ["MAXDUR 9:00;", None]
    assert _stopped(None, stops=stops)["inputs"]["maxLegsRelativeToMin"] == 1
    captured = _strip(_load("calendar_nyc_munich_frankfurt.json"), "bgProgramResponse")
    search = CalendarSearch(
        legs=(
            Leg.of("NYC", ["MUC", "FRA"], route_language="LH+", extension="MAXCONNECT 2:00"),
            Leg.of(["MUC", "FRA"], "NYC"),
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1), max_extra_stops=stops),
        window=CalendarWindow(
            start=date.fromisoformat(captured["inputs"]["startDate"]),
            end=date.fromisoformat(captured["inputs"]["endDate"]),
            duration_min=captured["inputs"]["layover"]["min"],
            duration_max=captured["inputs"]["layover"]["max"],
        ),
    )
    ours = to_wire(search).as_json()
    assert ours == captured, _diff(captured, ours)
