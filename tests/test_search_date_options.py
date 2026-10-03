# pyright: reportPrivateUsage=false
"""Matrix's two date options a slice: a flexible date (`--flex`, the form's "Or
day before", "Or day after", "+/- 1 day" and "+/- 2 days") and an arrival date
(`--arrive` in place of `--dep`). Google Flights takes neither, so each sends the
search to Matrix with its reason named."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Bags, ClockWindow, Leg, SpecificDateSearch, TimeOfDay
from flight_cli.links import google_flights_pinned_url
from flight_cli.models import SearchResult
from flight_cli.wire import to_wire
from test_backend_dispatch import _call


def _dep() -> date:
    return date.today() + timedelta(days=45)


def test_leg_of_takes_the_date_options() -> None:
    leg = Leg.of("JFK", "LHR", _dep(), date_minus=2, date_plus=2, is_arrival_date=True)
    assert (leg.date_minus, leg.date_plus, leg.is_arrival_date) == (2, 2, True)
    plain = Leg.of("JFK", "LHR", _dep())
    assert (plain.date_minus, plain.date_plus, plain.is_arrival_date) == (0, 0, False)


# ──────────────────────────────── the backend ───────────────────────────────


@pytest.mark.parametrize(
    ("option", "reason"),
    [
        ({"flex": (1, 0)}, "a flexible outbound date (or day before)"),
        ({"flex": (0, 1)}, "a flexible outbound date (or day after)"),
        ({"flex": (1, 1)}, "a flexible outbound date (+/- 1 day)"),
        ({"flex": (2, 2)}, "a flexible outbound date (+/- 2 days)"),
        ({"arrive": True}, "an outbound arrival date"),
        ({"return_flex": (0, 1)}, "a flexible return date (or day after)"),
        ({"return_arrive": True}, "a return arrival date"),
    ],
)
def test_auto_names_the_date_option_and_uses_matrix(
    option: dict[str, object], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _call(cli.BACKEND_AUTO, **option) == cli.BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert f"Using Matrix: Google Flights can't serve {reason}." in printed, printed


def test_auto_names_every_date_option_in_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        _call(flex=(1, 1), arrive=True, return_flex=(0, 1), return_arrive=True)
        == cli.BACKEND_MATRIX
    )
    printed = " ".join(capsys.readouterr().err.split())
    assert (
        "Using Matrix: Google Flights can't serve a flexible outbound date (+/- 1 day), an "
        "outbound arrival date, a flexible return date (or day after) and a return arrival "
        "date." in printed
    ), printed


def test_gflight_refuses_a_date_option_naming_it() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(cli.BACKEND_GFLIGHT, flex=(2, 2))
    assert str(excinfo.value) == (
        "--backend gflight can't serve this request: a flexible outbound date (+/- 2 days). "
        "Drop it, or use --backend matrix."
    )


def test_bags_refuse_a_date_option_naming_it() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(bags=Bags(checked=1), return_arrive=True)
    assert str(excinfo.value) == (
        "--bags needs Google Flights, which can't serve a return arrival date. Drop it, or "
        "drop --bags to search Matrix, which prices no bags."
    )


# ──────────────────────────────── the flags ─────────────────────────────────

_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url", "--format", "json"]


def _ret() -> date:
    return _dep() + timedelta(days=7)


def _sent(monkeypatch: pytest.MonkeyPatch, *args: str) -> list[dict[str, Any]]:
    """The slices of the one body Matrix is sent for `flight search <args>`."""
    asked: list[SpecificDateSearch] = []

    def _matrix(search: SpecificDateSearch, *_a: object) -> SearchResult:
        asked.append(search)
        return SearchResult.from_api({})

    monkeypatch.setattr(cli, "_run", _matrix)
    result = CliRunner().invoke(cli.app, [*_SEARCH, *args], env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    (search,) = asked
    return to_wire(search).as_json()["inputs"]["slices"]


def test_flex_and_return_flex_send_the_spas_date_modifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ret = _sent(
        monkeypatch,
        "JFK",
        "LHR",
        "--dep",
        _dep().isoformat(),
        "--return",
        _ret().isoformat(),
        "--flex",
        "2",
        "--return-flex",
        "after",
    )
    assert (out["date"], out["dateModifier"], out["isArrivalDate"]) == (
        _dep().isoformat(),
        {"minus": 2, "plus": 2},
        False,
    )
    assert (ret["date"], ret["dateModifier"], ret["isArrivalDate"]) == (
        _ret().isoformat(),
        {"minus": 0, "plus": 1},
        False,
    )


@pytest.mark.parametrize(
    ("value", "days"), [("before", (1, 0)), ("AFTER", (0, 1)), ("1", (1, 1)), (" 2 ", (2, 2))]
)
def test_each_flex_value_sends_its_days(
    value: str, days: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    (out,) = _sent(monkeypatch, "JFK", "LHR", "--dep", _dep().isoformat(), "--flex", value)
    assert (out["dateModifier"]["minus"], out["dateModifier"]["plus"]) == days


def test_auto_names_the_flex_and_searches_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _matrix(*_a: object) -> SearchResult:
        return SearchResult.from_api({})

    monkeypatch.setattr(cli, "_run", _matrix)
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, "JFK", "LHR", "--dep", _dep().isoformat(), "--flex", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "Using Matrix: Google Flights can't serve a flexible outbound date (+/- 1 day)." in (
        " ".join(result.stderr.split())
    )


def test_arrive_sends_an_arrival_date(monkeypatch: pytest.MonkeyPatch) -> None:
    (out,) = _sent(monkeypatch, "JFK", "LHR", "--arrive", _dep().isoformat())
    assert (out["date"], out["dateModifier"], out["isArrivalDate"]) == (
        _dep().isoformat(),
        {"minus": 0, "plus": 0},
        True,
    )
    assert "timeRanges" not in out


def test_return_arrive_dates_the_return_by_its_arrival(monkeypatch: pytest.MonkeyPatch) -> None:
    out, ret = _sent(
        monkeypatch,
        "JFK",
        "LHR",
        "--dep",
        _dep().isoformat(),
        "--return-arrive",
        _ret().isoformat(),
        "--return-flex",
        "before",
    )
    assert (out["isArrivalDate"], ret["isArrivalDate"]) == (False, True)
    assert (ret["origins"], ret["date"], ret["dateModifier"]) == (
        ["LHR"],
        _ret().isoformat(),
        {"minus": 1, "plus": 0},
    )


@pytest.mark.parametrize(
    ("times", "ranges"),
    [
        ("evening", [{"min": "17:00", "max": "21:00"}]),
        ("18:00-21:30", [{"min": "18:00", "max": "21:30"}]),
        ("morning,evening", [{"min": "8:00", "max": "11:00"}, {"min": "17:00", "max": "21:00"}]),
    ],
)
def test_arrive_times_beside_arrive_go_out_as_the_slices_time_ranges(
    times: str, ranges: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix holds an arrival-date slice's `timeRanges` to the arrival: live,
    JFK-LHR arriving 2026-10-21 under evening answered only rows landing
    19:45-20:45."""
    (out,) = _sent(
        monkeypatch, "JFK", "LHR", "--arrive", _dep().isoformat(), "--arrive-times", times
    )
    assert out["isArrivalDate"] is True
    assert out["timeRanges"] == ranges


def test_return_arrive_times_beside_return_arrive_go_out_on_the_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ret = _sent(
        monkeypatch,
        "JFK",
        "LHR",
        "--dep",
        _dep().isoformat(),
        "--return-arrive",
        _ret().isoformat(),
        "--return-arrive-times",
        "9:30-13:45",
    )
    assert "timeRanges" not in out
    assert ret["timeRanges"] == [{"min": "9:30", "max": "13:45"}]


def test_arrive_times_beside_arrive_compare_several_cabins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Leg, ...]] = []

    def _multi(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        seen.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _multi)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LHR",
            "--arrive",
            _dep().isoformat(),
            "--arrive-times",
            "evening",
            "--cabin",
            "economy,business",
        ],
    )
    assert result.exit_code == 0, result.output
    ((leg,),) = seen
    assert (leg.is_arrival_date, leg.time_ranges, leg.arrival_ranges) == (
        True,
        (TimeOfDay.EVENING,),
        (),
    )


# ─────────────────────────────── the refusals ───────────────────────────────


def _refused(monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    def _no_request(*_a: object, **_kw: object) -> None:
        pytest.fail("a refused search reached a backend")

    monkeypatch.setattr(cli, "_run", _no_request)
    monkeypatch.setattr(cli, "_gflight_results", _no_request)
    # Wide, so typer's error box wraps no sentence these tests read.
    result = CliRunner().invoke(cli.app, [*_SEARCH, *args], env={"COLUMNS": "400"})
    assert result.exit_code == 2, result.output
    return " ".join(result.output.replace("│", " ").split())


def test_a_departure_window_beside_arrive_is_exit_2_naming_the_arrival_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _refused(
        monkeypatch, "JFK", "LHR", "--arrive", _dep().isoformat(), "--depart-times", "morning"
    )
    assert (
        "--depart-times holds when the outbound leaves, and --arrive dates when it lands: "
        "Matrix holds an arrival-date slice's times to its arrival. Give them as "
        "--arrive-times, or date the departure with --dep." in printed
    ), printed


def test_a_return_window_beside_return_arrive_is_exit_2_naming_the_arrival_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _refused(
        monkeypatch,
        "JFK",
        "LHR",
        "--dep",
        _dep().isoformat(),
        "--return-arrive",
        _ret().isoformat(),
        "--return-times",
        "morning",
    )
    assert "--return-times holds when the return leaves, and --return-arrive dates" in printed
    assert "Give them as --return-arrive-times, or date the departure with --return." in printed


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("--dep", "D", "--arrive", "D"), "--dep and --arrive both date the outbound: give --dep"),
        (
            ("--dep", "D", "--return", "R", "--return-arrive", "R"),
            "--return and --return-arrive both date the return: give --return",
        ),
        (
            ("--dep", "D", "--return-flex", "after"),
            "--return-flex widens the return's date, and needs a --return or --return-arrive.",
        ),
        (
            ("--dep", "D", "--flex", "3"),
            "bad --flex '3': choose before (or day before), after (or day after), "
            "1 (+/- 1 day) or 2 (+/- 2 days)",
        ),
        (
            ("--dep", "D", "--return", "R", "--return-flex", "week"),
            "bad --return-flex 'week': choose before",
        ),
        (
            ("--arrive", "D", "--backend", "gflight"),
            "--backend gflight can't serve this request: an outbound arrival date.",
        ),
        (
            ("--dep", "D", "--flex", "1", "--bags", "1"),
            "--bags needs Google Flights, which can't serve a flexible outbound date (+/- 1 day).",
        ),
        (
            ("--dep", "D", "--return-arrive", "R", "--arrive-times", "18:00-21:30"),
            "--arrive-times needs Google Flights, which can't serve a return arrival date.",
        ),
    ],
)
def test_a_date_option_that_reads_two_ways_is_exit_2_before_any_request(
    args: tuple[str, ...], said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = {"D": _dep().isoformat(), "R": _ret().isoformat()}
    printed = _refused(monkeypatch, "JFK", "LHR", *(days.get(a, a) for a in args))
    assert said in printed, printed


@pytest.mark.parametrize("flag", ["--flex", "--return-flex", "--arrive", "--return-arrive"])
def test_a_date_option_beside_a_slice_is_exit_2_naming_the_slice_keys(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "1" if flag.endswith("flex") else _dep().isoformat()
    printed = _refused(monkeypatch, "--slice", f"JFK-LHR:{_dep().isoformat()}", flag, value)
    assert (
        f"{flag} dates a search given by origin and destination. A --slice takes its own in "
        "its f= and d=arrive fields." in printed
    ), printed


def test_verify_beside_a_date_option_is_refused_before_matrix_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed = _refused(
        monkeypatch, "JFK", "LHR", "--dep", _dep().isoformat(), "--flex", "1", "--verify"
    )
    assert "--verify needs a Google Flights row, and this search runs on Matrix." in printed
    assert "Using Matrix" not in printed


# ─────────────────────────────── --slice keys ───────────────────────────────


def test_a_slice_takes_its_flex_and_arrival_date() -> None:
    leg = cli._parse_slice_spec("JFK-LHR:2026-10-20:r=BA+:f=1:d=arrive:e=MAXCONNECT 2:00")
    assert (leg.date_minus, leg.date_plus, leg.is_arrival_date) == (1, 1, True)
    assert (leg.route_language, leg.extension) == ("BA+", "MAXCONNECT 2:00")
    plain = cli._parse_slice_spec("JFK-LHR:2026-10-20:e=MAXCONNECT 2:00")
    assert (plain.date_minus, plain.date_plus, plain.is_arrival_date) == (0, 0, False)


@pytest.mark.parametrize(
    ("spec", "said"),
    [
        ("JFK-LHR:2026-10-20:f=3", "f= takes before, after, 1 or 2"),
        ("JFK-LHR:2026-10-20:d=depart", "d= takes arrive"),
        ("JFK-LHR:2026-10-20:x=1", "valid keys are r=ROUTING, e=EXTENSION, f=FLEX and d=arrive"),
    ],
)
def test_a_bad_slice_date_key_names_the_slice(spec: str, said: str) -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        cli._parse_slice_spec(spec)
    assert said in str(excinfo.value)
    assert repr(spec) in str(excinfo.value)


def test_slices_send_their_own_date_options(monkeypatch: pytest.MonkeyPatch) -> None:
    later = _dep() + timedelta(days=4)
    first, second = _sent(
        monkeypatch,
        "--slice",
        f"JFK-LHR:{_dep().isoformat()}:f=before",
        "--slice",
        f"LHR-CDG:{later.isoformat()}:f=2:d=arrive",
    )
    assert (first["dateModifier"], first["isArrivalDate"]) == ({"minus": 1, "plus": 0}, False)
    assert (second["dateModifier"], second["isArrivalDate"]) == ({"minus": 2, "plus": 2}, True)


def test_fare_takes_the_slice_date_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Leg, ...]] = []

    def _matrix(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        seen.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app, ["fare", "--no-pp", "--slice", f"JFK-LHR:{_dep().isoformat()}:f=after:d=arrive"]
    )
    assert result.exit_code == 0, result.output
    ((leg,),) = seen
    assert (leg.date_minus, leg.date_plus, leg.is_arrival_date) == (0, 1, True)


def test_the_matrix_link_names_an_arrival_window_it_leaves_out_as_an_arrival() -> None:
    windowed = SpecificDateSearch(
        legs=(
            Leg.of(
                "JFK",
                "LHR",
                _dep(),
                is_arrival_date=True,
                time_ranges=(ClockWindow(first=18 * 60, last=21 * 60 + 30),),
            ),
        )
    )
    assert cli._matrix_link_caveats(windowed) == [
        "Matrix's page takes only times-of-day for an arrival, so the link leaves out 18:00-21:30"
    ]


# ──────────────────────────── the Google surfaces ───────────────────────────


def _nonstop_on(day: date, origin: str, dest: str) -> list[dict[str, str]]:
    return [
        {
            "origin": origin,
            "date": day.isoformat(),
            "destination": dest,
            "carrier": "BA",
            "flight": "112",
        }
    ]


def test_the_google_search_link_says_it_drops_the_date_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flexed = SpecificDateSearch(
        legs=(
            Leg.of("JFK", "LHR", _dep(), date_minus=1, date_plus=1),
            Leg.of("LHR", "JFK", _ret(), is_arrival_date=True),
        )
    )
    cli._emit_urls(flexed, matrix_url=False, google_url=True)
    printed = " ".join(capsys.readouterr().out.split())
    assert (
        f"note: the link searches {_dep().isoformat()} and {_ret().isoformat()} as departure "
        "dates only: Google's link takes no flexible or arrival date" in printed
    ), printed
    plain = SpecificDateSearch(legs=(Leg.of("JFK", "LHR", _dep()),))
    assert cli._gflight_url_caveats(plain) == []


def test_a_pinned_google_link_opens_on_the_pinned_flights_own_day() -> None:
    """Under +/- 1 day the cheapest row can leave the day before the date
    typed, and the pinned link opens that row's own day."""
    flexed = SpecificDateSearch(
        legs=(
            Leg.of("JFK", "LHR", _dep(), date_minus=1, date_plus=1),
            Leg.of("LHR", "JFK", _ret(), date_minus=1, date_plus=1),
        )
    )
    day_before, day_after = _dep() - timedelta(days=1), _ret() + timedelta(days=1)
    on_their_days = SpecificDateSearch(
        legs=(Leg.of("JFK", "LHR", day_before), Leg.of("LHR", "JFK", day_after))
    )
    out, ret = _nonstop_on(day_before, "JFK", "LHR"), _nonstop_on(day_after, "LHR", "JFK")
    assert google_flights_pinned_url(
        flexed, outbound_segments=out, return_segments=ret
    ) == google_flights_pinned_url(on_their_days, outbound_segments=out, return_segments=ret)


def test_awards_on_a_moved_date_say_which_day_they_were_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flexed = (Leg.of("JFK", "LHR", _dep(), is_arrival_date=True),)
    (query,) = cli._build_pp_legs(flexed)
    assert query.date == _dep().isoformat()
    printed = " ".join(capsys.readouterr().err.split())
    assert (
        f"Award providers were asked for departures on {_dep().isoformat()} only: they take no "
        "flexible or arrival date." in printed
    ), printed
    cli._build_pp_legs((Leg.of("JFK", "LHR", _dep()),))
    assert capsys.readouterr().err == ""
