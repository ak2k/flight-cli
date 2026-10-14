# pyright: reportPrivateUsage=false
"""`flight watch run` prices each saved watch once from Matrix's calendar and prints
a line per watch. Matrix is the stub of `test_calendar_formats`, which serves a live
JFK-LAX answer whose lowest fare is USD407 on 2026-10-31."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from conftest import LITERAL_DATES_NOW
from flight_cli import _watch, cli
from flight_cli._envelope import CalendarRow
from flight_cli.domain import Cabin
from flight_cli.models import CalendarResult
from test_calendar_formats import _BODY, _serve
from test_calendar_split import _pair_client

if TYPE_CHECKING:
    from click.testing import Result

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_WINDOW = ("--from", "2026-10-20", "--to", "2026-11-16")
_HEAD = "JFK-LAX  2026-10-20..2026-11-16  economy"


def _watch_cmd(*args: str) -> Result:
    return CliRunner().invoke(cli.app, ["watch", *args])


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    assert _watch.watches_path().is_relative_to(tmp_path)


def test_a_watch_run_prints_the_lowest_date_against_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _serve(monkeypatch, _BODY)
    for ceiling in ("500", "407", "300"):
        assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--below", ceiling).exit_code == 0
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [
        f"1  {_HEAD}  below 500: lowest USD 407 on 2026-10-31, under the ceiling",
        f"2  {_HEAD}  below 407: lowest USD 407 on 2026-10-31, over the ceiling",
        f"3  {_HEAD}  below 300: lowest USD 407 on 2026-10-31, over the ceiling",
    ]
    assert len(asked) == 3


def test_a_watch_with_award_or_no_dates_is_skipped_without_a_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _serve(monkeypatch, _BODY)
    assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--award").exit_code == 0
    assert _watch_cmd("add", "JFK", "LAX").exit_code == 0
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [
        f"1  {_HEAD}  award: skipped, award watches are not polled yet",
        "2  JFK-LAX  any date  economy: skipped, a watch with no dates is not polled yet",
    ]
    assert asked == []


def test_a_tie_goes_to_the_earlier_date_and_the_line_is_stripped_of_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _row(day: int) -> CalendarRow:
        return CalendarRow.model_validate(
            {"price": 300.0, "currency": "U\x1b\nSD", "row": {}, "departure": date(2026, 10, day)}
            | {"return": None}
        )

    def _rows(*_a: object, **_kw: object) -> list[CalendarRow]:
        return [_row(22), _row(21), _row(23)]

    monkeypatch.setattr(cli, "lowest_fare_rows", _rows)
    add = _watch_cmd("add", "JFK", "LAX", "--from", "2026-10-20", "--to", "2026-10-25")
    assert add.exit_code == 0, add.output
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [
        "1  JFK-LAX  2026-10-20..2026-10-25  economy: lowest U SD 300 on 2026-10-21"
    ]


@pytest.mark.parametrize(("price", "shown"), [(1221407.0, "1221407"), (1221407.5, "1221407.50")])
def test_a_fare_of_a_million_or_more_prints_every_digit(
    monkeypatch: pytest.MonkeyPatch, price: float, shown: str
) -> None:
    def _rows(*_a: object, **_kw: object) -> list[CalendarRow]:
        return [
            CalendarRow.model_validate(
                {"price": price, "currency": "KRW", "row": {}, "departure": date(2026, 10, 31)}
                | {"return": None}
            )
        ]

    monkeypatch.setattr(cli, "lowest_fare_rows", _rows)
    assert _watch_cmd("add", "ICN", "LAX", *_WINDOW).exit_code == 0
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [
        f"1  ICN-LAX  2026-10-20..2026-11-16  economy: lowest KRW {shown} on 2026-10-31"
    ]


def test_a_window_with_no_priced_day_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = _serve(monkeypatch, {"solutionCount": 0, "calendar": {"months": []}})
    assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--below", "500").exit_code == 0
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [f"1  {_HEAD}  below 500: no priced date"]


def _watch_calendar_calls(
    monkeypatch: pytest.MonkeyPatch, *, fail_first: bool = False
) -> list[object]:
    """Records the `no_cache` flag of each calendar query; the first one raises when asked."""
    real = cli._run_calendar
    seen: list[object] = []

    def _run(search: cli.CalendarSearch, **kw: float | str | bool) -> object:
        seen.append(kw["no_cache"])
        if fail_first and len(seen) == 1:
            raise type("Down\x1b\nextra record", (RuntimeError,), {})("down")
        return real(search, **kw)  # pyright: ignore[reportArgumentType] - forwards the caller's keywords

    monkeypatch.setattr(cli, "_run_calendar", _run)
    return seen


def test_a_watch_is_priced_as_an_uncached_one_way_calendar_of_its_cabin_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _serve(monkeypatch, _BODY)
    no_cache = _watch_calendar_calls(monkeypatch)
    assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--cabin", "business").exit_code == 0
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert [
        (s.legs[0].origins, s.legs[0].destinations, len(s.legs), s.window.start, s.window.end)
        for s in asked
    ] == [(("JFK",), ("LAX",), 1, date(2026, 10, 20), date(2026, 11, 16))]
    assert [s.options.cabin for s in asked] == [Cabin.BUSINESS]
    assert no_cache == [True]


def test_a_failed_lookup_is_one_line_and_the_other_watches_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = _serve(monkeypatch, _BODY)
    _ = _watch_calendar_calls(monkeypatch, fail_first=True)
    assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--below", "500").exit_code == 0
    assert _watch_cmd("add", "JFK", "LAX", *_WINDOW, "--below", "500").exit_code == 0
    stored = (tmp_path / "watches.json").stat()
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [
        f"1  {_HEAD}  below 500: lookup failed, Down extra record",
        f"2  {_HEAD}  below 500: lowest USD 407 on 2026-10-31, under the ceiling",
    ]
    kept = (tmp_path / "watches.json").stat()
    assert (kept.st_ino, kept.st_mtime_ns) == (stored.st_ino, stored.st_mtime_ns)


def test_a_metro_watch_that_lost_an_airport_pair_reports_a_failed_lookup_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _answer() -> CalendarResult:
        return CalendarResult.from_api(copy.deepcopy(_BODY))

    grids: dict[tuple[str, str], CalendarResult | Exception] = {
        ("JFK", "LAX"): RuntimeError("down"),
        ("LGA", "LAX"): _answer(),
        ("EWR", "LAX"): _answer(),
    }
    _ = _pair_client(monkeypatch, grids)
    assert _watch_cmd("add", "NYC", "LAX", *_WINDOW, "--below", "300").exit_code == 0
    head = "1  NYC-LAX  2026-10-20..2026-11-16  economy  below 300"
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == [f"{head}: lookup failed, Exit"]
    assert "1 of 3 sub-queries failed" in " ".join(ran.stderr.split())
    grids[("JFK", "LAX")] = _answer()
    ran = _watch_cmd("run")
    assert ran.stdout.splitlines() == [f"{head}: lowest USD 407 on 2026-10-31, over the ceiling"]


def test_a_run_with_no_watches_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _serve(monkeypatch, _BODY)
    ran = _watch_cmd("run")
    assert ran.exit_code == 0, ran.output
    assert ran.stdout.splitlines() == ["No watches saved."]
    assert asked == []


def test_the_help_text_and_the_readme_say_run_polls() -> None:
    root = Path(__file__).resolve().parent.parent
    helped = _watch_cmd("--help").stdout
    texts = [helped, cli.__doc__ or "", _watch.__doc__ or "", (root / "README.md").read_text()]
    assert "run" in helped
    assert all("nothing polls" not in t.lower() for t in texts)
    assert "`flight watch run`" in texts[-1]
