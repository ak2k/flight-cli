# pyright: reportPrivateUsage=false
"""The `--fast` price grid, read from the search page's own `GetCalendarGraph`.

The envelopes under `fixtures/gf_calgraph/` are responses the page received in a
real Chrome (cell tokens and the metadata block trimmed; the parser reads
neither), and `error13.body` is an error row verbatim. No test here loads a page:
the browser session is replaced wherever the path would reach one.
"""

from __future__ import annotations

import json
import pathlib
import signal
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_calgraph as cg
from flight_cli import cli
from flight_cli._gf_browser import CapturedResponse
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfBrowserUnavailableError, GfThrottledError
from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
    TimeOfDay,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "gf_calgraph"
_GRAPH_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGraph?f.sid=1&rt=c"
)
# Far enough out that fli's travel-date validation never sees the past.
_START = date.today() + timedelta(days=60)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _body(cells: list[list[Any]]) -> str:
    """A `GetCalendarGraph` answer in the captured envelope's shape."""
    inner = json.dumps([None, cells], separators=(",", ":"))
    chunk = json.dumps([["wrb.fr", None, inner]], separators=(",", ":"))
    tail = json.dumps([["di", 70]], separators=(",", ":"))
    return f")]}}'\n\n{len(chunk) + 2}\n{chunk}\n{len(tail) + 2}\n{tail}\n"


def _cells(first: date, days: int, price: float = 200.0) -> list[list[Any]]:
    return [
        [(first + timedelta(days=i)).isoformat(), None, [[None, price + i], ""], 1]
        for i in range(days)
    ]


def _search(
    *,
    start: date = _START,
    end: date | None = None,
    nights: int | None = None,
    routing: str | None = None,
    extension: str | None = None,
    routing_ret: str | None = None,
    options: SearchOptions | None = None,
    times: tuple[TimeOfDay, ...] = (),
) -> CalendarSearch:
    out = Leg.of("JFK", "LAX", route_language=routing, extension=extension, time_ranges=times)
    legs = (out,)
    if nights is not None:
        legs += (Leg.of("LAX", "JFK", route_language=routing_ret or routing, extension=extension),)
    n = nights or 0
    window = CalendarWindow(
        start=start, end=end or start + timedelta(days=13), duration_min=n, duration_max=n
    )
    return CalendarSearch(legs=legs, options=options or SearchOptions(), window=window)


# ───────────────────────────── the parser, on real envelopes ─────────────────


def test_the_one_way_capture_parses_every_cell() -> None:
    page = cg.parse_graph(_fixture("ow_jfk_lax.body"), trip_length=None)
    assert len(page.cells) == 38
    assert page.cells[0] == cg.GraphCell(date(2026, 10, 13), None, 249.0)
    assert page.cells[-1] == cg.GraphCell(date(2026, 11, 19), None, 184.0)
    assert page.last == date(2026, 11, 19)


def test_the_round_trip_capture_carries_each_cells_return_date() -> None:
    page = cg.parse_graph(_fixture("rt_jfk_lax_7n.body"), trip_length=7)
    assert len(page.cells) == 38
    assert page.cells[0] == cg.GraphCell(date(2026, 10, 13), date(2026, 10, 20), 432.0)
    assert all(c.return_date == c.departure + timedelta(days=7) for c in page.cells)


def test_a_graph_of_another_trip_length_is_refused_not_relabelled() -> None:
    with pytest.raises(cg.GfPriceGraphError, match=r"7-night trip .* not the 5 nights"):
        cg.parse_graph(_fixture("rt_jfk_lax_7n.body"), trip_length=5)


def test_error_13_is_a_refusal_and_not_a_throttle() -> None:
    """The real error envelope: an empty payload, the code at `row[5][0]`. It is
    session state on the page's own request, so it must not read as a rate limit
    — which is what routing it through the throttle check would print."""
    with pytest.raises(cg.GfPriceGraphError) as e:
        cg.parse_graph(_fixture("error13.body"), trip_length=None)
    assert e.value.code == 13
    assert "error 13" in str(e.value)
    assert "rate" not in str(e.value).replace("rather than a rate limit", "")
    assert not isinstance(e.value, GfThrottledError)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (')]}\'\n\n20\n[["wrb.fr",null,null]]\n', "empty result"),
        ("<html>not an envelope</html>", "could not be read"),
        (')]}\'\n\n9\n[["di",7]]\n', "could not be read"),
        (_body([]), "no dates"),
        (_body([[]]), "cannot read"),
        (_body([["not a date", None, [[None, 5], ""], 1]]), "cannot read"),
    ],
    ids=["empty-payload", "not-json", "no-result-row", "no-cells", "bad-cell", "bad-date"],
)
def test_an_empty_or_unreadable_graph_is_a_refusal(body: str, expected: str) -> None:
    with pytest.raises(cg.GfPriceGraphError, match=expected):
        cg.parse_graph(body, trip_length=None)


def test_an_unpriced_date_counts_toward_coverage_but_is_not_a_fare() -> None:
    body = _body([["2026-10-13", None, [[None, 249], ""], 1], ["2026-10-14", None, None, 1]])
    page = cg.parse_graph(body, trip_length=None)
    assert page.last == date(2026, 10, 14)
    assert [c.departure for c in page.cells] == [date(2026, 10, 13)]


# ───────────────────────────── paging across a window ─────────────────────────


class _FakeSession:
    """Answers each capture with the next body, recording what was asked."""

    def __init__(self, answers: list[tuple[int, str]]) -> None:
        self._answers = answers
        self.calls: list[tuple[str, gfb.Control | None]] = []
        self.checks: list[Callable[..., object] | None] = []

    def capture(
        self,
        url: str,
        wanted: Callable[[str], bool],
        *,
        click: gfb.Control | None = None,
        check_page: Callable[..., object] | None = None,
    ) -> CapturedResponse:
        assert wanted(_GRAPH_URL)
        assert not wanted("https://www.google.com/_/FlightsFrontendUi/data/x/GetCalendarGrid")
        self.calls.append((url, click))
        self.checks.append(check_page)
        status, body = self._answers[len(self.calls) - 1]
        return CapturedResponse(url=_GRAPH_URL, status=status, body=body)


def _serve(monkeypatch: pytest.MonkeyPatch, *bodies: str | tuple[int, str]) -> _FakeSession:
    fake = _FakeSession([b if isinstance(b, tuple) else (200, b) for b in bodies])

    def _session(*, headed: bool) -> _FakeSession:
        del headed
        return fake

    def _page_url(_search: CalendarSearch, departure: date) -> str:
        return f"page:{departure}"

    monkeypatch.setattr(gfb, "session", _session)
    monkeypatch.setattr(cg, "page_url", _page_url)
    return fake


def test_one_load_covers_a_window_inside_the_graphs_span(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _serve(monkeypatch, _fixture("ow_jfk_lax.body"))
    search = _search(start=date(2026, 10, 20), end=date(2026, 11, 2))
    graph = cg.price_graph(search, headed=False)
    assert [url for url, _ in fake.calls] == ["page:2026-10-20"]
    assert fake.calls[0][1] == gfb.Control("button", "Price graph")
    assert fake.checks[0] is cg._refuse_a_wall
    # Clipped to the window: the graph opened seven days early and ran past it.
    assert graph.cells[0].departure == date(2026, 10, 20)
    assert graph.cells[-1].departure == date(2026, 11, 2)
    assert len(graph.cells) == 14
    assert graph.trip_length is None


def test_a_longer_window_pages_from_the_first_uncovered_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two loads, clipped and merged: the second opens the day after the first
    graph's last date, which is read from the response rather than assumed."""
    second = _body(_cells(date(2026, 11, 13), 45, price=300.0))
    fake = _serve(monkeypatch, _fixture("ow_jfk_lax.body"), second)
    search = _search(start=date(2026, 10, 20), end=date(2026, 12, 10))
    graph = cg.price_graph(search, headed=False)
    assert [url for url, _ in fake.calls] == ["page:2026-10-20", "page:2026-11-20"]
    days = [c.departure for c in graph.cells]
    assert days == [date(2026, 10, 20) + timedelta(days=i) for i in range(52)]
    by_day = {c.departure: c.price for c in graph.cells}
    # Where the pages overlap the first answer stands; past it, the second's.
    assert by_day[date(2026, 11, 19)] == 184.0
    assert by_day[date(2026, 11, 20)] == 307.0


def test_a_graph_that_covers_nothing_new_stops_the_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled = _body(_cells(date(2026, 11, 1), 10))
    _serve(monkeypatch, _fixture("ow_jfk_lax.body"), stalled)
    search = _search(start=date(2026, 10, 20), end=date(2026, 12, 10))
    with pytest.raises(cg.GfPriceGraphError, match="ends at 2026-11-10, before 2026-11-20"):
        cg.price_graph(search, headed=False)


def test_the_number_of_loads_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    start = date(2026, 10, 20)
    one_day_each = [_body(_cells(start + timedelta(days=i), 1)) for i in range(cg._MAX_PAGES)]
    fake = _serve(monkeypatch, *one_day_each)
    with pytest.raises(cg.GfPriceGraphError, match=f"more than {cg._MAX_PAGES}"):
        cg.price_graph(_search(start=start, end=start + timedelta(days=60)), headed=False)
    assert len(fake.calls) == cg._MAX_PAGES


def test_a_graph_with_no_fare_in_the_window_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Priced dates exist, all outside the window: never an empty grid."""
    _serve(monkeypatch, _fixture("ow_jfk_lax.body"))
    search = _search(start=date(2026, 11, 19), end=date(2026, 11, 19))
    assert cg.price_graph(search, headed=False).cells  # the last day is priced
    _serve(monkeypatch, _body([["2026-11-19", None, None, 1]]))
    with pytest.raises(cg.GfPriceGraphError, match="priced no date in the window"):
        cg.price_graph(search, headed=False)


def test_a_non_2xx_graph_response_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, (500, "oops"))
    with pytest.raises(cg.GfPriceGraphError, match="HTTP 500"):
        cg.price_graph(_search(), headed=False)


def test_a_round_trip_graph_carries_its_trip_length(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _fixture("rt_jfk_lax_7n.body"))
    search = _search(start=date(2026, 10, 20), end=date(2026, 11, 2), nights=7)
    graph = cg.price_graph(search, headed=False)
    assert graph.trip_length == 7
    assert graph.cells[0] == cg.GraphCell(date(2026, 10, 20), date(2026, 10, 27), 387.0)
    assert len(graph.cells) == 14


def test_the_wall_check_names_a_throttle_and_tolerates_moved_rows() -> None:
    """The navigation goes through the search path's parser: a `/sorry/` page is
    a throttle before any click, while a page whose rows the parser cannot find
    can still draw a graph."""
    sorry = PageFetch(
        html="<html>unusual traffic</html>",
        final_url="https://www.google.com/sorry/index?continue=x",
        status_code=200,
    )
    with pytest.raises(GfThrottledError):
        cg._refuse_a_wall(sorry)
    cg._refuse_a_wall(
        PageFetch(
            html="<html>no rows</html>",
            final_url="https://www.google.com/travel/flights",
            status_code=200,
        )
    )


# ───────────────────────────── the page URL carries every constraint ─────────


def test_every_spelling_of_nonstop_lands_on_the_page() -> None:
    """`--routing N` and `MAXSTOPS 0` pass the gate as stop limits, and the bridge
    reads only `--stops`: each has to reach the URL as the `--stops 0` one does,
    and `MAXSTOPS 2` beside `--stops 0` must not widen it."""
    nonstop = cg.page_url(_search(options=SearchOptions(max_extra_stops=0)), _START)
    assert nonstop != cg.page_url(_search(), _START)
    assert cg.page_url(_search(routing="N"), _START) == nonstop
    assert cg.page_url(_search(extension="MAXSTOPS 0"), _START) == nonstop
    widened = _search(extension="MAXSTOPS 2", options=SearchOptions(max_extra_stops=0))
    assert cg.page_url(widened, _START) == nonstop


def test_the_page_opens_on_the_date_asked_and_returns_a_trip_length_later() -> None:
    later = _START + timedelta(days=30)
    assert cg.page_url(_search(), _START) != cg.page_url(_search(), later)
    one_way = cg.page_url(_search(), _START)
    round_trip = cg.page_url(_search(nights=7), _START)
    assert one_way != round_trip
    assert round_trip != cg.page_url(_search(nights=8), _START)


@pytest.mark.parametrize(
    ("search", "reason"),
    [
        (_search(times=(TimeOfDay.MORNING,)), "a departure-time window"),
        (_search(options=SearchOptions(pax=Pax(children=1))), "a passenger type other than adults"),
        (_search(options=SearchOptions(pax=Pax(seniors=1))), "a passenger type other than adults"),
        (_search(options=SearchOptions(allow_airport_changes=False)), "--no-airport-changes"),
        (_search(options=SearchOptions(show_only_available=False)), "--include-unavailable"),
        (_search(options=SearchOptions(max_extra_stops=3)), "a stop ceiling above 2 (3)"),
        (_search(routing="AA+"), "a carrier filter (AA)"),
        (_search(extension="MAXDUR 6:00"), "a maximum trip duration (360 min)"),
        (_search(nights=7, routing_ret="N"), "different routing or extension codes"),
    ],
    ids=[
        "times",
        "children",
        "seniors",
        "airport-changes",
        "unavailable",
        "stops-3",
        "carrier",
        "maxdur",
        "legs-differ",
    ],
)
def test_a_constraint_the_page_cannot_carry_is_named(search: CalendarSearch, reason: str) -> None:
    blocker = cg.page_blocker(search)
    assert blocker is not None
    assert reason in blocker


def test_what_the_page_carries_is_admitted() -> None:
    assert cg.page_blocker(_search()) is None
    assert cg.page_blocker(_search(routing="N", nights=7)) is None
    assert (
        cg.page_blocker(_search(options=SearchOptions(max_extra_stops=1, cabin=Cabin.BUSINESS)))
        is None
    )


# ───────────────────────────── the command ───────────────────────────────────


def _calendar(**overrides: Any) -> None:
    """Drive `calendar` directly; every option is passed, as typer would."""
    kwargs: dict[str, Any] = {
        "origin": "JFK",
        "destination": "LAX",
        "start": "2026-10-20",
        "end": "2026-11-02",
        "duration": "5-7",
        "one_way": True,
        "cabin": "economy",
        "adults": 1,
        "children": 0,
        "seniors": 0,
        "youth": 0,
        "routing": None,
        "extension": None,
        "routing_return": None,
        "extension_return": None,
        "depart_times": None,
        "return_times": None,
        "stops": None,
        "allow_airport_changes": True,
        "only_available": True,
        "rps": 10.0,
        "impersonate": "chrome",
        "fmt": "table",
        "json_out": False,
        "matrix_url": True,
        "google_url": False,
        "no_cache": True,
        "fast": True,
        "gf_transport": "browser",
        "gf_headed": False,
        "max_per_query": 1,
        "max_concurrency": 12,
    }
    kwargs.update(overrides)
    cli.calendar(**kwargs)


_OW = cg.PriceGraph(
    None,
    (
        cg.GraphCell(date(2026, 10, 20), None, 204.0),
        cg.GraphCell(date(2026, 10, 21), None, 214.5),
    ),
)
_RT = cg.PriceGraph(
    7,
    (
        cg.GraphCell(date(2026, 10, 20), date(2026, 10, 27), 409.0),
        cg.GraphCell(date(2026, 10, 21), date(2026, 10, 28), 399.0),
    ),
)


def _graph_is(monkeypatch: pytest.MonkeyPatch, answer: cg.PriceGraph | BaseException) -> list[Any]:
    """Stand in for the page loads, recording the search and the transport's state."""
    seen: list[Any] = []

    def _price_graph(search: CalendarSearch, *, headed: bool) -> cg.PriceGraph:
        seen.append((search, headed, gfb._scope_depth.n, signal.getsignal(signal.SIGINT)))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    return seen


def _no_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_a: object, **_k: object) -> object:
        raise AssertionError("--fast must never run the Matrix calendar")

    monkeypatch.setattr(cli, "_run_calendar", _refuse)
    monkeypatch.setattr(cli, "_run_calendar_enriched", _refuse)


def test_json_is_the_document_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under `--format json` stdout is the document and nothing else: no URL
    lines, although `--matrix-url` is on by default."""
    _no_matrix(monkeypatch)
    _graph_is(monkeypatch, _OW)
    _calendar(fmt="json")
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    assert doc == {
        "origin": "JFK",
        "destination": "LAX",
        "currency": "USD",
        "trip_length": None,
        "grid": [
            {"departure": "2026-10-20", "price": 204},
            {"departure": "2026-10-21", "price": 214.5},
        ],
    }
    assert all(next(iter(row)) == "departure" for row in doc["grid"])


def test_a_round_trip_document_carries_the_return_and_the_trip_length(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT)
    _calendar(fmt="json", one_way=False, duration="7")
    doc = json.loads(capsys.readouterr().out)
    assert doc["trip_length"] == 7
    assert doc["grid"][0] == {"departure": "2026-10-20", "return": "2026-10-27", "price": 409}
    search = seen[0][0]
    assert len(search.legs) == 2
    assert (search.window.duration_min, search.window.duration_max) == (7, 7)


def test_the_table_names_the_trip_length_and_keeps_the_links(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    _graph_is(monkeypatch, _RT)
    _calendar(one_way=False, duration="7")
    out = " ".join(capsys.readouterr().out.split())
    assert out.startswith("2 priced days · 7-night round trip · cheapest: 399 (USD)")
    assert "lowest fare per departure day (Google Flights)" in out
    assert "Matrix deep-link:" in out


def test_the_browser_run_is_guarded_and_scoped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C reaches Chrome and one scope closes it, as on search's fast arm;
    `auto` is the browser under `--fast`, and `--gf-headed` reaches the launch."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    before = signal.getsignal(signal.SIGINT)
    _calendar(gf_transport="auto", gf_headed=True)
    _, headed, depth, handler = seen[0]
    assert headed is True
    assert depth == 1
    assert handler is not before
    assert signal.getsignal(signal.SIGINT) is before
    _ = capsys.readouterr()


@pytest.mark.parametrize(
    ("failure", "said", "not_said"),
    [
        (
            cg.GfPriceGraphError("Google Flights answered the price graph with error 13", code=13),
            "date grid failed: Google Flights answered the price graph with error 13",
            "rate-limited",
        ),
        (GfThrottledError("x"), "rate-limited the browser rung", "date grid failed"),
        (
            GfBrowserUnavailableError("Chrome could not load Google Flights' page: boom."),
            "Chrome could not load Google Flights' page: boom.",
            "--gf-transport http",
        ),
    ],
    ids=["error-13", "throttle", "chrome"],
)
def test_no_grid_is_a_refusal_never_an_empty_answer(
    failure: BaseException,
    said: str,
    not_said: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_matrix(monkeypatch)
    _graph_is(monkeypatch, failure)
    with pytest.raises(typer.Exit) as e:
        _calendar(fmt="json")
    cap = capsys.readouterr()
    assert e.value.exit_code == 1
    err = " ".join(cap.err.split())
    assert said in err
    assert not_said not in err
    assert err.count("No Google Flights grid; drop --fast for Matrix.") == 1
    assert cap.out == ""


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"origin": "NYC"}, "a city code rather than an airport (NYC)"),
        ({"origin": "QSF", "destination": "JFK"}, "a city code rather than an airport (QSF)"),
        ({"depart_times": "morning"}, "a departure-time window"),
        ({"allow_airport_changes": False}, "--no-airport-changes"),
        ({"only_available": False}, "--include-unavailable"),
        ({"routing": "AA+"}, "a carrier filter (AA)"),
        ({"extension": "MAXDUR 6:00"}, "a maximum trip duration"),
        ({"stops": 3}, "a stop ceiling above 2 (3)"),
        ({"children": 1}, "a passenger type other than adults"),
        ({"one_way": False, "duration": "7", "routing_return": "N"}, "different routing"),
        ({"one_way": False, "duration": "5-7"}, "a trip-length range (5-7 nights)"),
        ({"origin": "JFK,EWR"}, "a multi-airport route"),
    ],
    ids=[
        "nyc",
        "qsf",
        "times",
        "airport-changes",
        "unavailable",
        "carrier",
        "maxdur",
        "stops-3",
        "children",
        "legs-differ",
        "length-range",
        "multi-airport",
    ],
)
def test_a_calendar_the_page_cannot_ask_is_refused_before_any_load(
    overrides: dict[str, Any],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    with pytest.raises(typer.Exit) as e:
        _calendar(**overrides)
    cap = capsys.readouterr()
    assert e.value.exit_code == 1
    err = " ".join(cap.err.split())
    assert reason in err
    assert "Run without --fast for Matrix" in err
    assert seen == []
    assert cap.out == ""


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        ({"gf_transport": "browser"}, "--gf-transport"),
        ({"gf_transport": "auto"}, "--gf-transport"),
        ({"gf_transport": "http", "gf_headed": True}, "--gf-headed"),
    ],
)
def test_a_transport_flag_without_fast_is_a_usage_error(
    overrides: dict[str, Any], flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `--fast` reaches Google Flights; honoring the flag nowhere would run
    Matrix as though it had been."""
    _no_matrix(monkeypatch)
    with pytest.raises(typer.BadParameter, match="--fast") as e:
        _calendar(fast=False, **overrides)
    assert e.value.param_hint == flag


# ───────────────────── the transport a bare `--fast` takes ───────────────────


def _calendar_cli(*extra: str) -> Result:
    """`calendar` through the CLI parser: a direct call cannot leave an option
    out, because the omitted parameter would be typer's `OptionInfo` rather than
    the option's default."""
    end = _START + timedelta(days=13)
    args = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", end.isoformat()]
    return CliRunner().invoke(cli.app, [*args, "--one-way", *extra])


class _NoChrome:
    """A driver that starts, in front of a Chrome that is not installed."""

    def __init__(self) -> None:
        self.chromium = self

    def start(self) -> _NoChrome:
        return self

    def launch_persistent_context(self, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Chromium distribution 'chrome' is not found.")

    def stop(self) -> None:
        return None


def test_a_bare_fast_reads_the_price_graph_through_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_matrix(monkeypatch)
    fake = _serve(monkeypatch, _body(_cells(_START - timedelta(days=7), 38)))
    result = _calendar_cli("--fast", "--format", "json")
    assert result.exit_code == 0, result.stderr
    assert [url for url, _ in fake.calls] == [f"page:{_START}"]
    assert len(json.loads(result.stdout)["grid"]) == 14


@pytest.mark.gf_browser  # the real launcher seam, over an import that cannot succeed
def test_a_bare_fast_without_patchright_names_the_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _no_matrix(monkeypatch)
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    result = _calendar_cli("--fast")
    err = " ".join(result.stderr.split())
    assert result.exit_code == 1
    assert "needs patchright" in err
    assert "uv pip install 'flight-cli[browser]'" in err
    assert err.count("No Google Flights grid; drop --fast for Matrix.") == 1
    assert result.stdout == ""


def test_a_bare_fast_without_chrome_names_the_chrome_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The install hint for patchright also names the Chrome install, so the
    launch failure is told apart by what it leaves out."""
    _no_matrix(monkeypatch)
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv(gfb._BROWSER_BIN_ENV, raising=False)

    def _factory() -> type[_NoChrome]:
        return _NoChrome

    monkeypatch.setattr(gfb, "_playwright_factory", _factory)
    result = _calendar_cli("--fast")
    err = " ".join(result.stderr.split())
    assert result.exit_code == 1
    assert "Chromium distribution 'chrome' is not found." in err
    assert "patchright install chrome" in err
    assert "flight-cli[browser]" not in err
    assert "--gf-transport http" not in err  # no grid there to offer
    assert err.count("No Google Flights grid; drop --fast for Matrix.") == 1
    assert result.stdout == ""


@pytest.mark.parametrize("extra", [(), ("--gf-transport", "http")], ids=["unset", "http"])
def test_without_fast_an_unset_or_http_transport_runs_matrix(
    extra: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[CalendarSearch] = []

    def _weave(search: CalendarSearch, **_kwargs: object) -> None:
        ran.append(search)

    monkeypatch.setattr(cli, "_run_calendar_enriched", _weave)
    result = _calendar_cli(*extra)
    assert result.exit_code == 0, result.stderr
    assert len(ran) == 1


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (("--gf-transport", "browser"), "--gf-transport"),
        (("--gf-transport", "auto"), "--gf-transport"),
        (("--gf-headed",), "--gf-headed"),
    ],
    ids=["browser", "auto", "headed"],
)
def test_without_fast_a_browser_flag_is_still_a_usage_error(
    extra: tuple[str, ...], flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_matrix(monkeypatch)
    result = _calendar_cli(*extra)
    err = " ".join(result.stderr.split())
    assert result.exit_code == 2
    assert flag in err
    assert "applies only with --fast" in err
