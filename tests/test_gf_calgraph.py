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
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_calgraph as cg
from flight_cli import cli
from flight_cli._gf_browser import CapturedResponse
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfBrowserUnavailableError, GfConsentError, GfThrottledError
from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
    TimeOfDay,
)
from flight_cli.models import CalendarResult
from test_gf_airport_sets import _tfs
from test_links_search_tfs import _decode, _slices

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
    extension_ret: str | None = None,
    options: SearchOptions | None = None,
    times: tuple[TimeOfDay, ...] = (),
    ret_times: tuple[TimeOfDay, ...] = (),
) -> CalendarSearch:
    out = Leg.of("JFK", "LAX", route_language=routing, extension=extension, time_ranges=times)
    legs = (out,)
    if nights is not None:
        back = Leg.of(
            "LAX",
            "JFK",
            route_language=routing_ret or routing,
            extension=extension_ret or extension,
            time_ranges=ret_times,
        )
        legs += (back,)
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


# What the wall check is handed on a results page that loaded: no rows it can
# find, which the check lets through.
_LOADED = PageFetch(
    html="<html>no rows</html>", final_url="https://www.google.com/travel/flights", status_code=200
)
_SORRY = PageFetch(
    html="<html>unusual traffic</html>",
    final_url="https://www.google.com/sorry/index?continue=x",
    status_code=200,
)
_CONSENT = PageFetch(
    html="<html>before you continue</html>",
    final_url="https://consent.google.com/ml?continue=x",
    status_code=200,
)
_CLICK_TIMEOUT = (
    "Chrome could not click 'Price graph' on Google Flights' page: "
    "Locator.click: Timeout 20000ms exceeded."
)


class _Unloaded(NamedTuple):
    """A capture that fails before there is a page to check."""

    error: Exception


_Answer = tuple[int, str] | PageFetch | Exception | _Unloaded


class _FakeSession:
    """Answers each capture with the next answer, recording what was asked.

    A `(status, body)` or an exception comes from a page that loaded, so the
    wall check sees `_LOADED` first; a `PageFetch` is the page the check sees,
    and has to be refused there."""

    def __init__(self, answers: list[_Answer]) -> None:
        self._answers = answers
        self.calls: list[tuple[str, gfb.Control | None]] = []

    def capture(
        self,
        url: str,
        wanted: Callable[[str], bool],
        *,
        click: gfb.Control | None = None,
        check_page: Callable[[PageFetch], object] | None = None,
    ) -> CapturedResponse:
        assert wanted(_GRAPH_URL)
        assert not wanted("https://www.google.com/_/FlightsFrontendUi/data/x/GetCalendarGrid")
        self.calls.append((url, click))
        answer = self._answers[len(self.calls) - 1]
        if isinstance(answer, _Unloaded):
            raise answer.error
        assert check_page is not None
        check_page(answer if isinstance(answer, PageFetch) else _LOADED)
        if isinstance(answer, PageFetch):
            raise AssertionError(f"the wall check let {answer.final_url} through")
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return CapturedResponse(url=_GRAPH_URL, status=status, body=body)


def _serve(monkeypatch: pytest.MonkeyPatch, *bodies: str | _Answer) -> _FakeSession:
    fake = _FakeSession([(200, b) if isinstance(b, str) else b for b in bodies])

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
    with pytest.raises(cg.GfPriceGraphError, match=f"more than {cg._MAX_PAGES}") as e:
        cg.price_graph(_search(start=start, end=start + timedelta(days=60)), headed=False)
    assert len(fake.calls) == cg._MAX_PAGES
    # No reload and the whole budget: the window is what ran out.
    assert not isinstance(e.value, cg.GfGraphBudgetError)
    assert str(e.value) == "the window needs more than 8 price-graph pages; narrow --start/--end"


def test_a_smaller_budget_caps_the_loads_and_names_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loads left by other trip lengths ran out, not the window's own eight:
    narrowing the window is not the remedy."""
    start = date(2026, 10, 20)
    one_day_each = [_body(_cells(start + timedelta(days=i), 1)) for i in range(3)]
    fake = _serve(monkeypatch, *one_day_each)
    with pytest.raises(cg.GfGraphBudgetError) as e:
        cg.price_graph(_search(start=start, end=start + timedelta(days=60)), headed=False, pages=3)
    assert str(e.value) == "no price-graph load of the 8 was left for the rest of the window"
    assert e.value.loads == 3
    assert len(fake.calls) == 3


def test_a_graph_counts_the_loads_it_took(monkeypatch: pytest.MonkeyPatch) -> None:
    second = _body(_cells(date(2026, 11, 13), 45, price=300.0))
    _serve(monkeypatch, _fixture("ow_jfk_lax.body"), second)
    search = _search(start=date(2026, 10, 20), end=date(2026, 12, 10))
    assert cg.price_graph(search, headed=False).loads == 2
    _serve(monkeypatch, _fixture("ow_jfk_lax.body"))
    search = _search(start=date(2026, 10, 20), end=date(2026, 11, 2))
    assert cg.price_graph(search, headed=False).loads == 1


# ───────────────────────────── a page that draws no graph ────────────────────


def test_a_page_that_drew_no_graph_is_loaded_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    miss = GfBrowserUnavailableError(_CLICK_TIMEOUT)
    fake = _serve(monkeypatch, miss, _fixture("ow_jfk_lax.body"))
    search = _search(start=date(2026, 10, 20), end=date(2026, 11, 2))
    graph = cg.price_graph(search, headed=False)
    assert [url for url, _ in fake.calls] == ["page:2026-10-20", "page:2026-10-20"]
    assert graph.loads == 2
    assert len(graph.cells) == 14


def test_a_second_miss_on_the_same_page_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    miss = GfBrowserUnavailableError(_CLICK_TIMEOUT)
    fake = _serve(monkeypatch, miss, miss, _fixture("ow_jfk_lax.body"))
    with pytest.raises(cg.GfGraphStalledError) as e:
        cg.price_graph(_search(), headed=False)
    assert e.value.reason == _CLICK_TIMEOUT
    assert e.value.loads == 2
    assert len(fake.calls) == 2


def test_each_page_of_a_window_gets_its_own_second_load(monkeypatch: pytest.MonkeyPatch) -> None:
    miss = GfBrowserUnavailableError(_CLICK_TIMEOUT)
    second = _body(_cells(date(2026, 11, 13), 45, price=300.0))
    fake = _serve(monkeypatch, miss, _fixture("ow_jfk_lax.body"), miss, second)
    graph = cg.price_graph(_search(start=date(2026, 10, 20), end=date(2026, 12, 10)), headed=False)
    assert [url for url, _ in fake.calls] == [
        "page:2026-10-20",
        "page:2026-10-20",
        "page:2026-11-20",
        "page:2026-11-20",
    ]
    assert graph.loads == 4


def test_the_second_load_is_spent_from_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    miss = GfBrowserUnavailableError(_CLICK_TIMEOUT)
    fake = _serve(monkeypatch, miss, _fixture("ow_jfk_lax.body"))
    with pytest.raises(cg.GfGraphStalledError) as e:
        cg.price_graph(_search(), headed=False, pages=1)
    assert e.value.loads == 1
    assert len(fake.calls) == 1
    fake = _serve(monkeypatch, miss, _fixture("ow_jfk_lax.body"), _fixture("ow_jfk_lax.body"))
    window = _search(start=date(2026, 10, 20), end=date(2026, 12, 10))
    with pytest.raises(cg.GfGraphBudgetError) as budget:
        cg.price_graph(window, headed=False, pages=2)
    assert str(budget.value) == (
        "no price-graph load of the 8 was left for the rest of the window "
        "(a page that drew no graph was loaded again)"
    )
    assert budget.value.loads == 2
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (_SORRY, GfThrottledError),
        (_CONSENT, GfConsentError),
        ((429, ""), cg.GfPriceGraphError),
        ((500, "oops"), cg.GfPriceGraphError),
        ("<html>not an envelope</html>", cg.GfPriceGraphError),
        (_fixture("error13.body"), cg.GfPriceGraphError),
        (
            _Unloaded(GfBrowserUnavailableError("Chrome could not load Google Flights' page: x.")),
            GfBrowserUnavailableError,
        ),
    ],
    ids=["throttle", "consent", "http-429", "http-500", "unreadable", "error-13", "not-loaded"],
)
def test_a_wall_an_answer_or_a_page_that_never_loaded_is_not_loaded_again(
    answer: str | _Answer, error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading a wall again spends the budget that put it there; an answer the
    graph came back with is Google's, not a page that failed to draw; and a
    navigation that failed says nothing a second one would not."""
    fake = _serve(monkeypatch, answer, _fixture("ow_jfk_lax.body"))
    with pytest.raises(error) as e:
        cg.price_graph(_search(start=date(2026, 10, 20), end=date(2026, 11, 2)), headed=False)
    assert not isinstance(e.value, cg.GfGraphStalledError)
    assert len(fake.calls) == 1


def test_one_graph_per_trip_length_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-way is one graph with no trip length; a range is one per length, each
    asked with the loads the lengths before it left."""
    asked: list[tuple[int | None, int]] = []

    def _price_graph(search: CalendarSearch, *, headed: bool, pages: int) -> cg.PriceGraph:
        del headed
        nights = search.window.duration_min if len(search.legs) > 1 else None
        asked.append((nights, pages))
        return cg.PriceGraph(nights, (), 4)

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    one_way = _search()
    assert [g.trip_length for g in cg.price_graphs(one_way, headed=False).graphs] == [None]
    assert asked == [(None, cg._MAX_PAGES)]
    asked.clear()
    got = cg.price_graphs(_ranged(), headed=False)
    assert asked == [(5, 8), (6, 4)]
    assert [g.trip_length for g in got.graphs] == [5, 6]
    assert _lost(got) == [(7, "no price-graph load of the 8 was left")]


def _ranged() -> CalendarSearch:
    window = _search().window.model_copy(update={"duration_min": 5, "duration_max": 7})
    return _search(nights=5).model_copy(update={"window": window})


def _lost(got: cg.GraphRange) -> list[tuple[int | None, str]]:
    return [(lost.nights, str(lost.cause)) for lost in got.lost]


def _lengths_answer(
    monkeypatch: pytest.MonkeyPatch, answers: dict[int, cg.PriceGraph | Exception]
) -> list[tuple[int, int]]:
    """Stand in for each trip length's graph, recording the loads it was given."""
    asked: list[tuple[int, int]] = []

    def _price_graph(search: CalendarSearch, *, headed: bool, pages: int) -> cg.PriceGraph:
        del headed
        asked.append((search.window.duration_min, pages))
        answer = answers[search.window.duration_min]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    return asked


def _missed(loads: int) -> cg.GfGraphStalledError:
    return cg.GfGraphStalledError(GfBrowserUnavailableError(_CLICK_TIMEOUT), loads=loads)


def test_a_length_whose_page_drew_no_graph_is_lost_and_the_next_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loads the lost length spent are spent: the next length has what is left."""
    asked = _lengths_answer(
        monkeypatch, {5: cg.PriceGraph(5, (), 1), 6: _missed(3), 7: cg.PriceGraph(7, (), 1)}
    )
    got = cg.price_graphs(_ranged(), headed=False)
    assert asked == [(5, 8), (6, 7), (7, 4)]
    assert [g.trip_length for g in got.graphs] == [5, 7]
    assert _lost(got) == [(6, str(_missed(3)))]


def test_any_other_failure_loses_its_length_and_the_ones_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall, an error row or a Chrome that could not load the page would meet
    the next length the same way, and asking it would spend a load to learn so."""
    asked = _lengths_answer(
        monkeypatch,
        {
            5: cg.PriceGraph(5, (), 1),
            6: cg.GfPriceGraphError("error 13", code=13),
            7: cg.PriceGraph(7, (), 1),
        },
    )
    got = cg.price_graphs(_ranged(), headed=False)
    assert [n for n, _ in asked] == [5, 6]
    assert [g.trip_length for g in got.graphs] == [5]
    assert _lost(got) == [(6, "error 13"), (7, "not asked after 6-night trips failed")]


def test_a_length_that_ran_out_of_loads_counts_them_and_the_rest_name_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lengths after it were not lost to a failure the next page would meet:
    no load was left for them."""
    asked = _lengths_answer(
        monkeypatch,
        {
            5: cg.PriceGraph(5, (), 7),
            6: cg.GfGraphBudgetError(loads=1, reloaded=False),
            7: cg.PriceGraph(7, (), 1),
        },
    )
    got = cg.price_graphs(_ranged(), headed=False)
    assert asked == [(5, 8), (6, 1)]
    assert [g.trip_length for g in got.graphs] == [5]
    assert _lost(got) == [
        (6, "no price-graph load of the 8 was left for the rest of the window"),
        (7, "no price-graph load of the 8 was left"),
    ]


@pytest.mark.parametrize(
    ("answers", "asked", "raised", "message"),
    [
        (
            {5: cg.GfPriceGraphError("error 13", code=13)},
            [5],
            cg.GfPriceGraphError,
            "5-night trips: error 13",
        ),
        (
            {5: GfBrowserUnavailableError("Chrome could not load Google Flights' page: x.")},
            [5],
            GfBrowserUnavailableError,
            "Chrome could not load Google Flights' page: x.",
        ),
        (
            {5: _missed(2), 6: _missed(2), 7: GfThrottledError("rate-limited")},
            [5, 6, 7],
            cg.GfGraphStalledError,
            _CLICK_TIMEOUT,
        ),
    ],
    ids=["error-row", "chrome", "every-length"],
)
def test_a_range_that_priced_no_length_raises_its_first_failure_as_before(
    answers: dict[int, cg.PriceGraph | Exception],
    asked: list[int],
    raised: type[Exception],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph's own error is named with its length; a browser's is its own line."""
    seen = _lengths_answer(monkeypatch, answers)
    with pytest.raises(raised) as e:
        cg.price_graphs(_ranged(), headed=False)
    assert [n for n, _ in seen] == asked
    shown = e.value.reason if isinstance(e.value, GfBrowserUnavailableError) else str(e.value)
    assert shown == message
    if isinstance(e.value, cg.GfPriceGraphError):
        assert e.value.code == 13


def test_a_range_that_priced_no_length_can_return_every_lost_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `--fast` asks for: every length is its answer, so none is dropped."""
    _lengths_answer(monkeypatch, {5: _missed(2), 6: _missed(2), 7: cg.GfPriceGraphError("e13")})
    got = cg.price_graphs(_ranged(), headed=False, raise_unpriced=False)
    assert got.graphs == []
    assert _lost(got) == [(5, str(_missed(2))), (6, str(_missed(2))), (7, "e13")]


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


# ───────────────────────────── the graph's own gate ──────────────────────────

_T = TimeOfDay


@pytest.mark.parametrize(
    "search",
    [
        _search(),
        _search(routing="AA+"),
        _search(extension="AIRLINES AA DL"),
        _search(extension="ALLIANCE skyteam"),
        _search(extension="ALLIANCE oneworld|skyteam"),
        _search(extension="MAXDUR 6:20"),
        _search(extension="MINCONNECT 2:00"),
        _search(extension="MINCONNECT 0:00"),
        _search(extension="MAXCONNECT 2:00"),
        _search(extension="MINCONNECT 1:00; MAXCONNECT 2:00"),
        _search(extension="MAXDUR 9:00; MAXDUR 9:00"),
        _search(routing="AA+", extension="AIRLINES AA"),
        _search(routing="N:AA", options=SearchOptions(max_extra_stops=1)),
        _search(times=(_T.NIGHT,)),
        _search(times=(_T.EVENING, _T.NIGHT)),
        _search(times=(_T.AFTERNOON, _T.EVENING, _T.NIGHT)),
        _search(times=tuple(_T)),
        _search(nights=7, routing="AA+"),
        _search(
            nights=7, extension="ALLIANCE skyteam; MAXDUR 9:00; MINCONNECT 1:00; MAXCONNECT 3:00"
        ),
    ],
    ids=[
        "plain",
        "carrier",
        "carrier-list",
        "alliance",
        "alliance-list",
        "maxdur",
        "minconnect",
        "minconnect-zero",
        "maxconnect",
        "layover-range",
        "maxdur-twice",
        "same-carrier-twice",
        "nonstop-carrier",
        "night",
        "evening-night",
        "afternoon-to-night",
        "whole-day",
        "round-trip-carrier",
        "round-trip-bounds",
    ],
)
def test_the_graph_takes_what_google_applies_from_its_url(search: CalendarSearch) -> None:
    assert cg.graph_blocker(search) is None


@pytest.mark.parametrize(
    ("search", "reason"),
    [
        # What the base refused, in its words.
        (_search(routing="~BA+"), "Tier-2 routing"),
        (_search(routing="O:AA+"), "Tier-2 routing"),
        (_search(extension="-CODESHARE"), "a Tier-2 extension code"),
        (_search(extension="MINCONNECT 3:00; -CODESHARE"), "Tier-2 extension codes"),
        (_search(routing="F* X:ORD F*"), "a connecting-airport filter (ORD)"),
        (_search(routing="BQ+"), "a carrier Google Flights has no code for (BQ)"),
        (_search(extension="MAXDUR 0:00"), "a maximum trip duration of 0 minutes"),
        (_search(extension="MAXCONNECT 0:00"), "a maximum layover of 0 minutes"),
        (_search(options=SearchOptions(max_extra_stops=3)), "a stop ceiling above 2 (3)"),
        (_search(extension="MAXSTOPS 3"), "a stop ceiling above 2 (3)"),
        (_search(options=SearchOptions(pax=Pax(children=1))), "a passenger type other than adults"),
        (
            _search(options=SearchOptions(allow_airport_changes=False)),
            "an airport-change exclusion (--no-airport-changes)",
        ),
        (_search(times=(_T.MORNING,)), "a departure-time window"),
        (_search(times=(_T.EARLY_MORNING,)), "a departure-time window"),
        (_search(times=(_T.MIDDAY, _T.NIGHT)), "a departure-time window"),
        (_search(nights=7, times=(_T.NIGHT,)), "a departure-time window"),
        (_search(nights=7, ret_times=(_T.NIGHT,)), "a return-time window"),
        (
            _search(nights=7, routing="AA+", routing_ret="N"),
            "different routing or extension codes on the outbound and the return",
        ),
        # Which reason wins, as at the base.
        (
            _search(extension="MAXDUR 0:00", options=SearchOptions(max_extra_stops=3)),
            "a maximum trip duration of 0 minutes",
        ),
        (_search(routing="AA+", times=(_T.MORNING,)), "a departure-time window"),
        (
            _search(routing="F* X:ORD F*", options=SearchOptions(pax=Pax(children=1))),
            "a passenger type other than adults",
        ),
        (
            _search(nights=7, extension="-CODESHARE", extension_ret="MAXDUR 0:00"),
            "a Tier-2 extension code",
        ),
        (
            _search(nights=7, extension="MINCONNECT 3:00", extension_ret="MAXDUR 0:00"),
            "a maximum trip duration of 0 minutes on the return leg",
        ),
        # A combination the URL would write wider than asked.
        (
            _search(routing="AA+", extension="AIRLINES DL"),
            "a carrier filter combined with another carrier filter",
        ),
        (
            _search(routing="N:AA", extension="AIRLINES DL"),
            "a carrier filter combined with another carrier filter",
        ),
        (
            _search(routing="AA+", extension="ALLIANCE skyteam"),
            "an alliance filter combined with another carrier or alliance filter",
        ),
        (
            _search(extension="ALLIANCE oneworld; ALLIANCE skyteam"),
            "an alliance filter combined with another carrier or alliance filter",
        ),
        (
            _search(extension="MINCONNECT 3:00; MAXCONNECT 1:00"),
            "a minimum layover (180 min) above the maximum (60 min)",
        ),
        (
            _search(extension="MAXDUR 6:00; MAXDUR 9:00"),
            "more than one maximum trip duration (360 min, 540 min)",
        ),
        (
            _search(extension="MAXCONNECT 3:00; MAXCONNECT 1:00"),
            "more than one maximum layover (60 min, 180 min)",
        ),
        (
            _search(nights=7, routing="AA+", extension="AIRLINES DL", routing_ret="N"),
            "a carrier filter combined with another carrier filter",
        ),
        (
            _search(nights=7, routing="AA+", extension_ret="AIRLINES DL"),
            "a carrier filter combined with another carrier filter on the return leg",
        ),
        (
            _search(
                nights=7,
                extension="MAXCONNECT 1:00",
                extension_ret="MINCONNECT 3:00; MAXCONNECT 1:00",
            ),
            "a minimum layover (180 min) above the maximum (60 min) on the return leg",
        ),
    ],
    ids=[
        "operating-exclude",
        "operating",
        "codeshare",
        "minconnect-and-codeshare",
        "connect-at",
        "unmapped-carrier",
        "maxdur-zero",
        "maxconnect-zero",
        "stops-3",
        "maxstops-3",
        "children",
        "airport-changes",
        "morning",
        "early",
        "midday-and-night",
        "round-trip-departure-window",
        "round-trip-return-window",
        "legs-differ",
        "legs-before-stops",
        "window-before-carrier",
        "options-before-connect-at",
        "return-tier-2-after-outbound",
        "return-leg-zero",
        "two-carriers",
        "nonstop-carrier-and-another",
        "carrier-and-alliance",
        "two-alliances",
        "minimum-above-maximum",
        "two-durations",
        "two-maximum-layovers",
        "wider-before-legs-differ",
        "return-leg-two-carriers",
        "return-leg-minimum-above-maximum",
    ],
)
def test_the_graph_refuses_the_rest_by_name(search: CalendarSearch, reason: str) -> None:
    assert cg.graph_blocker(search) == reason


def test_the_graph_refuses_a_cabin_requirement_even_for_its_own_cabin() -> None:
    """Green at the base and the tip: the graph has no rows to hold it to."""
    search = _search(extension="+CABIN 2", options=SearchOptions(cabin=Cabin.BUSINESS))
    assert cg.graph_blocker(search) is not None


# At 6fce7b1, for the shapes its gate admitted: the graph asks the same page.
_FAR = date(2099, 6, 1)
_BASE_PAGES = {
    "one-way": "CBwQAhoeEgoyMDk5LTA2LTAxagcIARIDSkZLcgcIARIDTEFYQAFIAXABmAEC",
    "round-trip-7": (
        "CBwQAhoeEgoyMDk5LTA2LTAxagcIARIDSkZLcgcIARIDTEFYGh4SCjIwOTktMDYtMDhqBwgBEgNMQVhyBwgB"
        "EgNKRktAAUgBcAGYAQE"
    ),
    "nonstop": "CBwQAhogEgoyMDk5LTA2LTAxKABqBwgBEgNKRktyBwgBEgNMQVhAAUgBcAGYAQI",
    "nonstop-round-trip-7": (
        "CBwQAhogEgoyMDk5LTA2LTAxKABqBwgBEgNKRktyBwgBEgNMQVgaIBIKMjA5OS0wNi0wOCgAagcIARIDTEFY"
        "cgcIARIDSkZLQAFIAXABmAEB"
    ),
    "stops-1": "CBwQAhogEgoyMDk5LTA2LTAxKAFqBwgBEgNKRktyBwgBEgNMQVhAAUgBcAGYAQI",
    "business": "CBwQAhoeEgoyMDk5LTA2LTAxagcIARIDSkZLcgcIARIDTEFYQAFIA3ABmAEC",
}


_BASE_SHAPES = {
    "one-way": _search(start=_FAR),
    "round-trip-7": _search(start=_FAR, nights=7),
    "nonstop": _search(start=_FAR, routing="N"),
    "nonstop-round-trip-7": _search(start=_FAR, nights=7, routing="N"),
    "stops-1": _search(start=_FAR, options=SearchOptions(max_extra_stops=1)),
    "business": _search(start=_FAR, options=SearchOptions(cabin=Cabin.BUSINESS)),
}


@pytest.mark.parametrize("shape", list(_BASE_SHAPES))
def test_what_the_base_admitted_asks_the_same_page(shape: str) -> None:
    tfs = _BASE_PAGES[shape]
    assert cg.page_url(_BASE_SHAPES[shape], _FAR) == (
        f"https://www.google.com/travel/flights?tfs={tfs}&hl=en&gl=US&curr=USD&tfu=EgQIABABIgA"
    )


def test_a_round_trip_asks_for_each_carrier_once_on_each_slice() -> None:
    out, back = _slices(_tfs(cg.page_url(_search(nights=7, routing="AA+"), _START)))
    assert (out[6], back[6]) == ([b"AA"], [b"AA"])
    same = _search(routing="AA+", extension="AIRLINES AA")
    (only,) = _slices(_tfs(cg.page_url(same, _START)))
    assert only[6] == [b"AA"]


@pytest.mark.parametrize(
    ("search", "field", "value"),
    [
        (_search(extension="ALLIANCE skyteam"), 6, [b"SKYTEAM"]),
        (_search(extension="MAXDUR 9:00"), 12, [540]),
        (_search(extension="MINCONNECT 3:00"), 17, [180]),
        (_search(extension="MAXCONNECT 1:00"), 18, [60]),
        (_search(times=(_T.EVENING, _T.NIGHT)), 8, [17]),
        (_search(times=(_T.EVENING, _T.NIGHT)), 9, [23]),
    ],
    ids=["alliance", "maxdur", "minconnect", "maxconnect", "earliest-hour", "latest-hour"],
)
def test_what_the_graph_takes_is_on_its_page(
    search: CalendarSearch, field: int, value: list[Any]
) -> None:
    assert cg.graph_blocker(search) is None
    (only,) = _slices(_tfs(cg.page_url(search, _START)))
    assert only[field] == value


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

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        del pages
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


# Five airports and six: one leg of 11, the page's bound; `_TWELVE` is one past it.
_ELEVEN = ("JFK,EWR,LGA,BOS,PHL", "LHR,CDG,FRA,AMS,MAD,BCN")
_TWELVE = (_ELEVEN[0], f"{_ELEVEN[1]},FCO")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"origin": "YTO", "destination": "LHR"}, "a city code rather than an airport (YTO)"),
        ({"origin": "NYC", "destination": "JFK"}, "an airport at both ends of a leg (JFK)"),
        (
            {"origin": _TWELVE[0], "destination": _TWELVE[1]},
            "12 airports on one leg (its limit is 11)",
        ),
        ({"routing": "BQ+"}, "a carrier Google Flights has no code for (BQ)"),
        ({"extension": "AIRLINES AA BQ"}, "a carrier Google Flights has no code for (BQ)"),
        ({"routing": "F* X:QQQ F*"}, "a connecting airport Google Flights has no code for (QQQ)"),
        ({"extension": "MAXDUR 0:00"}, "a maximum trip duration of 0 minutes"),
        ({"extension": "MAXCONNECT 0:00"}, "a maximum layover of 0 minutes"),
        ({"depart_times": "morning"}, "a departure-time window"),
        ({"allow_airport_changes": False}, "--no-airport-changes"),
        ({"only_available": False}, "--include-unavailable"),
        ({"stops": 3}, "a stop ceiling above 2 (3)"),
        ({"extension": "MAXSTOPS 3"}, "a stop ceiling above 2 (3)"),
        ({"children": 1}, "a passenger type other than adults"),
        ({"one_way": False, "duration": "7", "routing_return": "N"}, "different routing"),
        (
            {"one_way": False, "duration": "5-7", "gf_transport": "http"},
            "a trip-length range (5-7 nights)",
        ),
    ],
    ids=[
        "unknown-code",
        "airport-at-both-ends",
        "twelve-airports",
        "unmapped-carrier",
        "unmapped-carrier-in-a-list",
        "unmapped-connect-at",
        "maxdur-zero",
        "maxconnect-zero",
        "times",
        "airport-changes",
        "unavailable",
        "stops-3",
        "maxstops-3",
        "children",
        "legs-differ",
        "length-range-over-http",
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


# What Google was measured applying from the page URL, as the command takes it.
_TAKEN: list[dict[str, Any]] = [
    {"routing": "AA+"},
    {"extension": "ALLIANCE skyteam"},
    {"extension": "MAXDUR 9:00"},
    {"extension": "MINCONNECT 3:00"},
    {"extension": "MAXCONNECT 1:00"},
    {"depart_times": "evening,night"},
]
_TAKEN_IDS = ["carrier", "alliance", "maxdur", "minconnect", "maxconnect", "evening-night"]


@pytest.mark.parametrize("overrides", _TAKEN, ids=_TAKEN_IDS)
def test_fast_asks_the_graph_for_what_google_applies_from_its_url(
    overrides: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    _calendar(fmt="json", **overrides)
    cap = capsys.readouterr()
    assert len(json.loads(cap.out)["grid"]) == 2
    assert len(seen) == 1
    leg = seen[0][0].legs[0]
    asked = leg.route_language or leg.extension or ",".join(t.value for t in leg.time_ranges)
    assert asked == next(iter(overrides.values()))
    assert "Run without --fast" not in cap.err


@pytest.mark.parametrize("overrides", _TAKEN, ids=_TAKEN_IDS)
def test_the_default_calendar_asks_the_graph_for_them_after_matrix(
    overrides: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = _matrix_answers(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    _calendar(fast=False, **overrides)
    cap = capsys.readouterr()
    assert (len(asked), len(seen)) == (1, 1)
    assert "not asked" not in cap.err
    assert "lowest fare per departure day (Google Flights)" in " ".join(cap.out.split())


def test_the_help_names_what_the_graph_takes_and_what_http_keeps() -> None:
    """`--help` makes the gate's claim: the graph takes what Google applies from
    its URL, and the http grid still takes only cabin, adults and stops. A help
    text naming the narrower set sends a user with a carrier filter to Matrix."""
    result = CliRunner().invoke(cli.app, ["calendar", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    flat = " ".join(result.output.replace("│", " ").split())
    assert "stops up to 2, one carrier or alliance include, MAXDUR, MINCONNECT," in flat
    assert "a --depart-times window that runs to midnight" in flat
    assert "over --gf-transport http only cabin, adults and stops up to 2" in flat


def _run_on(keyword: str, raw: str) -> str:
    """How every calendar gate names a code given more arguments than it takes."""
    return f"a Matrix-only extension code ({keyword} with more arguments than it takes ('{raw}'))"


_KEPT = [
    ({"routing": "~BA+"}, "Tier-2 routing"),
    ({"routing": "O:AA+"}, "Tier-2 routing"),
    ({"extension": "-CODESHARE"}, "a Tier-2 extension code"),
    ({"routing": "F* X:ORD F*"}, "a connecting-airport filter (ORD)"),
    ({"children": 1}, "a passenger type other than adults"),
    ({"extension": "MAXDUR 0:00"}, "a maximum trip duration of 0 minutes"),
    ({"depart_times": "morning"}, "a departure-time window"),
    (
        {"extension": "MAXDUR 9:00 MAXCONNECT 1:00"},
        _run_on("MAXDUR", "MAXDUR 9:00 MAXCONNECT 1:00"),
    ),
    (
        {"extension": "MAXCONNECT 1:00 MINCONNECT 3:00"},
        _run_on("MAXCONNECT", "MAXCONNECT 1:00 MINCONNECT 3:00"),
    ),
    (
        {"extension": "MINCONNECT 3:00 MAXCONNECT 1:00"},
        _run_on("MINCONNECT", "MINCONNECT 3:00 MAXCONNECT 1:00"),
    ),
]
_KEPT_IDS = [
    "operating-exclude",
    "operating",
    "codeshare",
    "connect-at",
    "children",
    "maxdur-0",
    "morning",
    "maxdur-runs-into-maxconnect",
    "maxconnect-runs-into-minconnect",
    "minconnect-runs-into-maxconnect",
]


@pytest.mark.parametrize(("overrides", "reason"), _KEPT, ids=_KEPT_IDS)
def test_what_the_graph_still_refuses_keeps_the_bases_words_under_fast(
    overrides: dict[str, Any],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    with pytest.raises(typer.Exit):
        _calendar(**overrides)
    err = " ".join(capsys.readouterr().err.split())
    assert f"search page can carry; this is {reason}. Run without --fast for Matrix." in err
    assert seen == []


@pytest.mark.parametrize(("overrides", "reason"), _KEPT, ids=_KEPT_IDS)
def test_what_the_graph_still_refuses_keeps_the_bases_words_beside_matrix(
    overrides: dict[str, Any],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked = _matrix_answers(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    _calendar(fast=False, **overrides)
    err = " ".join(capsys.readouterr().err.split())
    assert err.count("Google Flights price graph not asked:") == 1
    assert f"Google Flights price graph not asked: this is {reason}." in err
    assert (len(asked), seen) == (1, [])


# Codes run together without `;`. A page that read the first code alone would
# ask without the words after it, so each is refused by name, beside a
# constraint the graph takes too.
_RUNS_ON: list[tuple[tuple[str, ...], CalendarSearch, str]] = [
    (
        ("--one-way", "--ext", "MINCONNECT 2:00; MAXSTOPS 1 MAXDUR 0:00"),
        _search(extension="MINCONNECT 2:00; MAXSTOPS 1 MAXDUR 0:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXDUR 0:00"),
    ),
    (
        ("--one-way", "--ext", "MINCONNECT 3:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _search(extension="MINCONNECT 3:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXCONNECT 1:00"),
    ),
    (
        ("--one-way", "--ext", "MINCONNECT 3:00; MAXSTOPS 1 -CODESHARE"),
        _search(extension="MINCONNECT 3:00; MAXSTOPS 1 -CODESHARE"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 -CODESHARE"),
    ),
    (
        ("--one-way", "--routing", "AA+", "--ext", "MAXSTOPS 1 MAXDUR 9:00"),
        _search(routing="AA+", extension="MAXSTOPS 1 MAXDUR 9:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXDUR 9:00"),
    ),
    (
        ("--one-way", "--depart-times", "night", "--ext", "MAXSTOPS 1 MAXDUR 9:00"),
        _search(extension="MAXSTOPS 1 MAXDUR 9:00", times=(_T.NIGHT,)),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXDUR 9:00"),
    ),
    (
        ("--one-way", "--ext", "MAXDUR 9:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _search(extension="MAXDUR 9:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXCONNECT 1:00"),
    ),
    (
        ("-d", "7", "--ext", "MINCONNECT 3:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _search(nights=7, extension="MINCONNECT 3:00; MAXSTOPS 1 MAXCONNECT 1:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXCONNECT 1:00"),
    ),
    (
        ("-d", "7", "--routing", "AA+", "--ext", "MAXSTOPS 1 MAXDUR 9:00"),
        _search(nights=7, routing="AA+", extension="MAXSTOPS 1 MAXDUR 9:00"),
        _run_on("MAXSTOPS", "MAXSTOPS 1 MAXDUR 9:00"),
    ),
]
_RUNS_ON_IDS = [
    "minconnect-maxstops-runs-into-maxdur-zero",
    "minconnect-maxstops-runs-into-maxconnect",
    "minconnect-maxstops-runs-into-codeshare",
    "carrier-maxstops-runs-into-maxdur",
    "night-maxstops-runs-into-maxdur",
    "maxdur-maxstops-runs-into-maxconnect",
    "round-trip-minconnect-maxstops-runs-into-maxconnect",
    "round-trip-carrier-maxstops-runs-into-maxdur",
]


def _graph_gate(search: CalendarSearch) -> str | None:
    """`_grid_branch_blocker` as `--fast` over the browser calls it."""
    return cli._grid_branch_blocker(
        search,
        json_out=False,
        one_way=len(search.legs) == 1,
        origins=("JFK",),
        dests=("LAX",),
        fast=True,
        graph=True,
    )


@pytest.mark.parametrize(
    ("search", "reason"),
    [
        *((search, reason) for _, search, reason in _RUNS_ON),
        (
            _search(extension="MAXDUR 9:00 MAXCONNECT 1:00"),
            _run_on("MAXDUR", "MAXDUR 9:00 MAXCONNECT 1:00"),
        ),
        (_search(extension="MAXDUR 9:00 -CODESHARE"), _run_on("MAXDUR", "MAXDUR 9:00 -CODESHARE")),
        (
            _search(extension="MAXCONNECT 1:00 MINCONNECT 3:00"),
            _run_on("MAXCONNECT", "MAXCONNECT 1:00 MINCONNECT 3:00"),
        ),
        (_search(extension="MAXCONNECT 1:00 2:00"), _run_on("MAXCONNECT", "MAXCONNECT 1:00 2:00")),
        (
            _search(extension="MINCONNECT 3:00 MAXCONNECT 1:00"),
            _run_on("MINCONNECT", "MINCONNECT 3:00 MAXCONNECT 1:00"),
        ),
        (
            _search(nights=7, extension="MAXDUR 9:00", extension_ret="MAXDUR 9:00 MAXCONNECT 1:00"),
            f"{_run_on('MAXDUR', 'MAXDUR 9:00 MAXCONNECT 1:00')} on the return leg",
        ),
        (
            _search(nights=7, extension="MAXDUR 9:00 MAXCONNECT 1:00", extension_ret="MAXDUR 9:00"),
            _run_on("MAXDUR", "MAXDUR 9:00 MAXCONNECT 1:00"),
        ),
    ],
    ids=[
        *_RUNS_ON_IDS,
        "maxdur-runs-into-maxconnect",
        "maxdur-runs-into-codeshare",
        "maxconnect-runs-into-minconnect",
        "maxconnect-two-arguments",
        "minconnect-runs-into-maxconnect",
        "return-leg-runs-on",
        "outbound-runs-on",
    ],
)
def test_a_code_run_into_the_next_is_refused_by_name_at_the_gate(
    search: CalendarSearch, reason: str
) -> None:
    assert _graph_gate(search) == reason


def test_a_carrier_list_that_repeats_a_code_is_asked_as_the_list_without_it() -> None:
    """Every word of a carrier list is read, so a repeated code is the same
    include, on the same page, as the list naming it once."""
    once, twice = _search(extension="AIRLINES AA"), _search(extension="AIRLINES AA AA")
    assert (_graph_gate(once), _graph_gate(twice)) == (None, None)
    assert cg.page_url(twice, _START) == cg.page_url(once, _START)


_FAST_JSON = ("--fast", "--gf-transport", "browser", "--format", "json")


def _runs_on_cli(*extra: str) -> tuple[int, str, str]:
    """`calendar` through the CLI parser; stderr with its line breaks flattened."""
    end = _START + timedelta(days=13)
    args = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", end.isoformat()]
    result = CliRunner().invoke(cli.app, [*args, "--no-cache", *extra])
    return result.exit_code, result.stdout, " ".join(result.stderr.split())


@pytest.mark.parametrize(("args", "search", "reason"), _RUNS_ON, ids=_RUNS_ON_IDS)
def test_a_code_run_into_the_next_is_refused_by_name_under_fast(
    args: tuple[str, ...], search: CalendarSearch, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    del search
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    code, out, err = _runs_on_cli(*args, *_FAST_JSON)
    assert (code, out, seen) == (1, "", [])
    assert f"search page can carry; this is {reason}. Run without --fast for Matrix." in err


@pytest.mark.parametrize(("args", "search", "reason"), _RUNS_ON, ids=_RUNS_ON_IDS)
def test_a_code_run_into_the_next_is_named_beside_matrix(
    args: tuple[str, ...], search: CalendarSearch, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    del search
    asked = _matrix_answers(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    code, _, err = _runs_on_cli(*args, "--gf-transport", "browser")
    assert (code, len(asked), seen) == (0, 1, [])
    assert err.count("Google Flights price graph not asked:") == 1
    assert f"Google Flights price graph not asked: this is {reason}." in err


_STOPS_RUN_ON = ("--ext", "MAXSTOPS 1 MAXDUR 9:00")
_STOPS_RUN_ON_REASON = _run_on("MAXSTOPS", "MAXSTOPS 1 MAXDUR 9:00")


@pytest.mark.parametrize("trip", [("--one-way",), ("-d", "7")], ids=["one-way", "round-trip"])
def test_a_stop_ceiling_run_into_a_bound_is_refused_not_asked_alone(
    trip: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page would carry the stop ceiling alone and price a wider question
    than the one typed."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT if len(trip) > 1 else _OW)
    code, out, err = _runs_on_cli(*trip, *_STOPS_RUN_ON, *_FAST_JSON)
    assert (code, out, seen) == (1, "", [])
    assert f"this is {_STOPS_RUN_ON_REASON}. Run without --fast for Matrix." in err


@pytest.mark.parametrize("trip", [("--one-way",), ("-d", "7")], ids=["one-way", "round-trip"])
def test_fast_over_http_refuses_a_stop_ceiling_run_into_a_bound_by_name(
    trip: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RPC grid would be asked the stop ceiling alone, as the page would."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    asked: list[CalendarSearch] = []

    def _grid(search: CalendarSearch, **_kwargs: object) -> None:
        asked.append(search)

    monkeypatch.setattr(cli, "_run_fast_calendar_grid", _grid)
    code, out, err = _runs_on_cli(*trip, *_STOPS_RUN_ON, "--fast", "--gf-transport", "http")
    assert (code, out, asked, seen) == (1, "", [], [])
    assert f"this is {_STOPS_RUN_ON_REASON}. Run without --fast for Matrix." in err


def test_the_default_calendar_over_http_runs_matrix_alone_for_a_code_run_into_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The weave would paint a grid asked the stop ceiling alone above
    Matrix's answer."""
    asked = _matrix_answers(monkeypatch)
    woven: list[CalendarSearch] = []

    def _weave(search: CalendarSearch, **_kwargs: object) -> None:
        woven.append(search)

    monkeypatch.setattr(cli, "_run_calendar_enriched", _weave)
    seen = _graph_is(monkeypatch, _OW)
    code, _, _ = _runs_on_cli("--one-way", *_STOPS_RUN_ON, "--gf-transport", "http")
    assert (code, len(asked), woven, seen) == (0, 1, [], [])


@pytest.mark.parametrize(
    ("ext", "reason"),
    [
        ("MINCONNECT 2:00; MAXSTOPS 1; MAXDUR 0:00", "a maximum trip duration of 0 minutes"),
        (
            "MINCONNECT 3:00; MAXSTOPS 1; MAXCONNECT 1:00",
            "a minimum layover (180 min) above the maximum (60 min)",
        ),
    ],
    ids=["maxdur-zero", "minimum-above-maximum"],
)
def test_the_same_codes_with_their_separators_are_refused_by_the_graph(
    ext: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    code, _, err = _runs_on_cli("--one-way", "--ext", ext, *_FAST_JSON)
    assert (code, seen) == (1, [])
    assert f"this is {reason}." in err


# `--fast --gf-transport http`'s stderr for each, as 6fce7b1 printed it.
_HTTP_HEAD = (
    "--fast applies only to calendars one-way or of one trip length, between airports\n"
    "or metro codes Google Flights can ask for (up to 11 airports a leg), whose every\n"
    "filter its search page can carry; "
)
_HTTP_TAIL = {
    "carrier": "this is a carrier filter (AA). Run without \n--fast for Matrix.\n",
    "alliance": "this is an alliance filter (skyteam). Run \nwithout --fast for Matrix.\n",
    "maxdur": "this is a maximum trip duration (540 min). Run\nwithout --fast for Matrix.\n",
    "minconnect": "this is a Tier-2 extension code. Run without \n--fast for Matrix.\n",
    "maxconnect": "this is a layover-time bound. Run without \n--fast for Matrix.\n",
    "evening-night": "this is a departure-time window. Run without \n--fast for Matrix.\n",
}
_HTTP_ARGS = {
    "carrier": ("--routing", "AA+"),
    "alliance": ("--ext", "ALLIANCE skyteam"),
    "maxdur": ("--ext", "MAXDUR 9:00"),
    "minconnect": ("--ext", "MINCONNECT 3:00"),
    "maxconnect": ("--ext", "MAXCONNECT 1:00"),
    "evening-night": ("--depart-times", "evening,night"),
}


@pytest.mark.parametrize("trip", [(), ("-d", "7")], ids=["one-way", "round-trip"])
@pytest.mark.parametrize("taken", list(_HTTP_ARGS))
def test_fast_over_http_refuses_them_byte_for_byte_as_before(
    taken: str, trip: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RPC grid's admission is not the graph's: nothing it printed moves."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(cli, "err", Console(stderr=True))
    end = _START + timedelta(days=13)
    args = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", end.isoformat()]
    args += [*(trip or ("--one-way",)), "--fast", "--gf-transport", "http", *_HTTP_ARGS[taken]]
    result = CliRunner().invoke(cli.app, args)
    assert (result.exit_code, result.stdout) == (1, "")
    assert result.stderr == _HTTP_HEAD + _HTTP_TAIL[taken]
    assert seen == []


def test_a_reload_that_spends_the_eighth_load_blames_the_budget_not_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 248-day window fits in eight loads; one of them went to a page loaded
    again, so the last page's dates had no load left."""
    _no_matrix(monkeypatch)
    start = date(2026, 10, 20)
    miss = GfBrowserUnavailableError(_CLICK_TIMEOUT)
    pages = [_body(_cells(start + timedelta(days=31 * i - 7), 38)) for i in range(7)]
    fake = _serve(monkeypatch, miss, *pages)
    end = start + timedelta(days=247)
    with pytest.raises(typer.Exit):
        _calendar(start=start.isoformat(), end=end.isoformat(), fmt="json")
    err = " ".join(capsys.readouterr().err.split())
    assert (
        "date grid failed: no price-graph load of the 8 was left for the rest of the window "
        "(a page that drew no graph was loaded again)"
    ) in err
    assert "narrow" not in err
    assert "price-graph pages" not in err
    assert len(fake.calls) == cg._MAX_PAGES


def _round_trip_page(first: date, nights: int) -> str:
    """38 priced departures from `first`, each returning `nights` later."""
    return _body(
        [
            [
                (first + timedelta(days=i)).isoformat(),
                (first + timedelta(days=i + nights)).isoformat(),
                [[None, 400.0 + i], ""],
                1,
            ]
            for i in range(38)
        ]
    )


def test_a_reload_on_one_trip_length_leaves_the_last_one_out_of_loads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four lengths of two loads each fill the budget; a reload on the first
    leaves the last one load, which is not a window that needs one page."""
    asked = _matrix_answers(monkeypatch)
    start = date(2026, 10, 20)
    answers: list[str | _Answer] = [GfBrowserUnavailableError(_CLICK_TIMEOUT)]
    for nights in (5, 6, 7, 8):
        answers += [
            _round_trip_page(start - timedelta(days=7), nights),
            _round_trip_page(start + timedelta(days=24), nights),
        ]
    fake = _serve(monkeypatch, *answers)
    _calendar(fast=False, one_way=False, duration="5-8", end="2026-12-10")
    cap = capsys.readouterr()
    err = " ".join(cap.err.split())
    assert len(asked) == 1
    assert len(fake.calls) == cg._MAX_PAGES
    assert (
        "Google Flights price graph not shown: 8-night trips: no price-graph load of the 8 "
        "was left for the rest of the window."
    ) in err
    assert "narrow" not in err
    assert "price-graph pages" not in err
    assert all(f"{nights}n" in cap.out for nights in (5, 6, 7))
    assert "8n" not in cap.out


# ───────────────────────────── airport sets ─────────────────────────────────


def _gate(origin: str, destination: str, *, fast: bool) -> str | None:
    """`_grid_branch_blocker` on a one-way calendar, as the command builds it."""
    origins, dests = cli._parse_iata_list(origin), cli._parse_iata_list(destination)
    window = CalendarWindow(
        start=_START, end=_START + timedelta(days=13), duration_min=0, duration_max=0
    )
    search = CalendarSearch(legs=(Leg.of(origins, dests),), window=window)
    return cli._grid_branch_blocker(
        search, json_out=False, one_way=True, origins=origins, dests=dests, fast=fast
    )


@pytest.mark.parametrize(
    ("origin", "destination", "fast", "matrix"),
    [
        ("NYC", "LON", None, "a city code rather than an airport (NYC, LON)"),
        ("JFK,EWR", "LHR", None, "a multi-airport route"),
        ("QSF", "JFK", None, "a city code rather than an airport (QSF)"),
        (*_ELEVEN, None, "a multi-airport route"),
        ("YTO", "LHR", *["a city code rather than an airport (YTO)"] * 2),
        (
            "NYC",
            "JFK",
            "an airport at both ends of a leg (JFK)",
            "a city code rather than an airport (NYC)",
        ),
        (*_TWELVE, "12 airports on one leg (its limit is 11)", "a multi-airport route"),
    ],
    ids=["metro", "list", "qsf", "eleven", "unknown-code", "both-ends", "twelve"],
)
def test_fast_takes_the_airport_sets_a_search_takes(
    origin: str, destination: str, fast: str | None, matrix: str
) -> None:
    """Under `--fast` the page asks for every airport, so the airports are
    checked expanded, against fli's table and one page's per-leg bound: a grid
    is one page, where a single-cabin search over the bound is several. Without
    it a set keeps the reason that sends it to Matrix's fan-out, because the
    weave's Matrix half is one query."""
    assert _gate(origin, destination, fast=True) == fast
    assert _gate(origin, destination, fast=False) == matrix


@pytest.mark.parametrize(
    ("origin", "destination"),
    [("NYC", "LON"), ("JFK,EWR", "LHR"), ("QSF", "JFK"), _ELEVEN],
    ids=["metro", "list", "qsf", "eleven"],
)
def test_a_set_is_one_page_and_the_document_names_it(
    origin: str,
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The page's graph already prices each date at the set's cheapest airport,
    so one graph answers, and the document names what was asked, as the table
    title does."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    _calendar(origin=origin, destination=destination, fmt="json")
    doc = json.loads(capsys.readouterr().out)
    assert (doc["origin"], doc["destination"]) == (origin, destination)
    assert len(seen) == 1
    leg = seen[0][0].legs[0]
    assert (",".join(leg.origins), ",".join(leg.destinations)) == (origin, destination)


def test_a_round_trip_over_a_set_names_it_and_its_trip_length(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT)
    _calendar(origin="JFK,EWR", destination="LHR", fmt="json", one_way=False, duration="7")
    doc = json.loads(capsys.readouterr().out)
    assert (doc["origin"], doc["destination"], doc["trip_length"]) == ("JFK,EWR", "LHR", 7)
    assert len(seen) == 1


def test_a_single_airport_document_is_byte_for_byte_what_it_was(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_matrix(monkeypatch)
    _graph_is(monkeypatch, _OW)
    _calendar(fmt="json")
    assert capsys.readouterr().out == (
        "{\n"
        '  "origin": "JFK",\n'
        '  "destination": "LAX",\n'
        '  "currency": "USD",\n'
        '  "trip_length": null,\n'
        '  "grid": [\n'
        "    {\n"
        '      "departure": "2026-10-20",\n'
        '      "price": 204\n'
        "    },\n"
        "    {\n"
        '      "departure": "2026-10-21",\n'
        '      "price": 214.5\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def _airports(entries: list[Any]) -> list[str]:
    """The codes of a decoded slice's airport field, in the order written."""
    return [_decode(entry)[2][0].decode() for entry in entries]


def test_the_page_asks_for_every_airport_of_both_sets() -> None:
    nyc, london = ["JFK", "LGA", "EWR"], ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"]

    def _window(nights: int) -> CalendarWindow:
        end = _START + timedelta(days=13)
        return CalendarWindow(start=_START, end=end, duration_min=nights, duration_max=nights)

    one_way = CalendarSearch(legs=(Leg.of("NYC", "LON"),), window=_window(0))
    (out,) = _slices(_tfs(cg.page_url(one_way, _START)))
    assert (_airports(out[13]), _airports(out[14])) == (nyc, london)

    legs = (Leg.of("NYC", "LON"), Leg.of("LON", "NYC"))
    out, back = _slices(_tfs(cg.page_url(CalendarSearch(legs=legs, window=_window(7)), _START)))
    assert (_airports(out[13]), _airports(out[14])) == (nyc, london)
    assert (_airports(back[13]), _airports(back[14])) == (london, nyc)


@pytest.mark.parametrize(
    ("origin", "destination"),
    [(",", "LHR"), ("LHR", ","), (" , ", "LHR"), ("", "LHR")],
    ids=["origin-comma", "destination-comma", "origin-blanks", "origin-empty"],
)
@pytest.mark.parametrize(
    "extra",
    [
        ("--one-way", "--fast"),
        ("-d", "7", "--fast"),
        ("--one-way", "--fast", "--format", "json"),
        ("--one-way", "--fast", "--gf-transport", "http"),
        ("--one-way",),
        (),
    ],
    ids=["fast", "fast-rt", "fast-json", "fast-http", "one-way", "round-trip"],
)
def test_a_blank_airport_side_is_an_input_error_on_every_calendar_path(
    origin: str, destination: str, extra: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_parse_iata_list` drops blank entries, so each of these is an empty
    airport set, and nothing past the parse refuses one as the input it is: the
    page gate finds no airport over its bound or outside fli's table, the page's
    encoder then fails as though Google had, and without `--fast` it is a Matrix
    query with no airports."""
    reached: list[str] = []

    def _unreached(name: str) -> Callable[..., NoReturn]:
        def _run(*_a: object, **_k: object) -> NoReturn:
            reached.append(name)
            raise AssertionError(f"{name} ran on a calendar with no airports")

        return _run

    for name in ("_run_calendar", "_run_calendar_enriched", "_http_date_grid"):
        monkeypatch.setattr(cli, name, _unreached(name))
    monkeypatch.setattr(cg, "price_graph", _unreached("price_graph"))
    end = _START + timedelta(days=13)
    window = ["--start", _START.isoformat(), "--end", end.isoformat()]
    result = CliRunner().invoke(cli.app, ["calendar", origin, destination, *window, *extra])
    assert (result.exit_code, reached) == (2, []), result.output
    assert "origin and destination are required" in result.stderr
    assert result.stdout == ""


def test_a_headed_window_over_http_without_fast_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """http opens no Chrome, so a flag that shows its window would be honored
    nowhere."""
    _no_matrix(monkeypatch)
    with pytest.raises(typer.BadParameter, match="--gf-transport http opens none") as e:
        _calendar(fast=False, gf_transport="http", gf_headed=True)
    assert e.value.param_hint == "--gf-headed"


def _matrix_answers(monkeypatch: pytest.MonkeyPatch) -> list[CalendarSearch]:
    """Stand in for the Matrix calendar with an empty answer, recording each ask."""
    asked: list[CalendarSearch] = []

    def _matrix(search: CalendarSearch, **_kw: object) -> tuple[CalendarResult, int, bool]:
        asked.append(search)
        return CalendarResult.from_api({"solutionCount": 0}), 0, False

    def _no_weave(*_a: object, **_k: object) -> object:
        raise AssertionError("the http weave ran on the browser transport")

    monkeypatch.setattr(cli, "_run_calendar", _matrix)
    monkeypatch.setattr(cli, "_run_calendar_enriched", _no_weave)
    return asked


@pytest.mark.parametrize("transport", ["browser", "auto"])
def test_without_fast_the_browser_transports_read_the_graph_beside_matrix(
    transport: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = _matrix_answers(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    _calendar(fast=False, gf_transport=transport)
    assert len(asked) == 1
    assert len(seen) == 1
    assert "lowest fare per departure day (Google Flights)" in " ".join(
        capsys.readouterr().out.split()
    )


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


def test_without_fast_an_http_transport_runs_the_weave_and_never_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[CalendarSearch] = []

    def _weave(search: CalendarSearch, **_kwargs: object) -> None:
        ran.append(search)

    monkeypatch.setattr(cli, "_run_calendar_enriched", _weave)
    seen = _graph_is(monkeypatch, _OW)
    result = _calendar_cli("--gf-transport", "http")
    assert result.exit_code == 0, result.stderr
    assert len(ran) == 1
    assert seen == []


@pytest.mark.parametrize(
    ("extra", "headed"),
    [
        ((), False),
        (("--gf-transport", "browser"), False),
        (("--gf-transport", "auto"), False),
        (("--gf-headed",), True),
    ],
    ids=["unset", "browser", "auto", "headed"],
)
def test_without_fast_an_unset_or_browser_transport_reads_the_graph_beside_matrix(
    extra: tuple[str, ...], headed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _matrix_answers(monkeypatch)
    seen = _graph_is(monkeypatch, _OW)
    result = _calendar_cli(*extra)
    assert result.exit_code == 0, result.stderr
    assert len(asked) == 1
    assert [s[1] for s in seen] == [headed]
    assert "lowest fare per departure day (Google Flights)" in " ".join(result.stdout.split())


def test_without_fast_a_headed_window_over_http_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_matrix(monkeypatch)
    result = _calendar_cli("--gf-headed", "--gf-transport", "http")
    err = " ".join(result.stderr.split())
    assert result.exit_code == 2
    assert "--gf-headed" in err
    assert "--gf-transport http opens none" in err


# ──────────────────── --fast over a trip-length range ─────────────────────


def _graph(nights: int, *prices: float) -> cg.PriceGraph:
    start = date(2026, 10, 20)
    return cg.PriceGraph(
        nights,
        tuple(
            cg.GraphCell(start + timedelta(days=i), start + timedelta(days=i + nights), price)
            for i, price in enumerate(prices)
        ),
    )


_RANGE = [_graph(5, 301.0, 311.0), _graph(6, 302.0, 312.0), _graph(7, 303.0, 313.0)]


def _graphs_are(
    monkeypatch: pytest.MonkeyPatch, answer: cg.GraphRange | BaseException
) -> list[CalendarSearch]:
    """Stand in for the range's page loads, recording the search each was asked."""
    seen: list[CalendarSearch] = []

    def _price_graphs(
        search: CalendarSearch, *, headed: bool, raise_unpriced: bool = True
    ) -> cg.GraphRange:
        del headed, raise_unpriced
        seen.append(search)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cg, "price_graphs", _price_graphs)
    return seen


def test_fast_prints_one_column_per_trip_length(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red at the base: `--fast -d 5-7` was refused before any load."""
    _no_matrix(monkeypatch)
    seen = _graphs_are(monkeypatch, cg.GraphRange(_RANGE, []))
    _calendar(one_way=False, duration="5-7")
    cap = capsys.readouterr()
    assert len(seen) == 1
    for column in ("5n", "6n", "7n"):
        assert column in cap.out, cap.out
    assert "5-7-night round trips" in " ".join(cap.out.split())
    assert "Run without --fast" not in cap.err


def test_fast_writes_the_range_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red at the base: refused. The shape follows what was asked: every length,
    a graph per priced one, and the lost ones by name."""
    _no_matrix(monkeypatch)
    _graphs_are(monkeypatch, cg.GraphRange(_RANGE, []))
    _calendar(one_way=False, duration="5-7", fmt="json")
    doc = json.loads(capsys.readouterr().out)
    assert doc == {
        "origin": "JFK",
        "destination": "LAX",
        "currency": "USD",
        "trip_lengths": [5, 6, 7],
        "graphs": [cg.document(g, origin="JFK", destination="LAX") for g in _RANGE],
        "lost": [],
    }


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_a_lost_length_is_named_on_stderr_and_in_the_document(
    fmt: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red at the base: refused."""
    _no_matrix(monkeypatch)
    lost = cg.LostLength(6, cg.GfPriceGraphError("Google Flights' price graph priced no date"))
    _graphs_are(monkeypatch, cg.GraphRange([_RANGE[0], _RANGE[2]], [lost]))
    _calendar(one_way=False, duration="5-7", fmt=fmt)
    cap = capsys.readouterr()
    assert "6-night trips: Google Flights' price graph priced no date." in " ".join(
        cap.err.split()
    ), cap.err
    if fmt == "json":
        doc = json.loads(cap.out)
        assert [g["trip_length"] for g in doc["graphs"]] == [5, 7]
        assert doc["trip_lengths"] == [5, 6, 7]
        assert doc["lost"] == [
            {"trip_length": 6, "reason": "Google Flights' price graph priced no date."}
        ]
    else:
        assert "5n" in cap.out and "7n" in cap.out and "6n" not in cap.out


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_every_length_lost_is_named_by_its_nights_before_the_no_grid_line(
    fmt: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red at the base: refused with the range sentence, before any load. A
    length that spent its loads is named, and so is the one a Chrome failure
    left unasked: each is a length the user asked for and got nothing back."""
    _no_matrix(monkeypatch)
    asked = _lengths_answer(
        monkeypatch,
        {
            5: _missed(2),
            6: GfBrowserUnavailableError("Chrome could not load Google Flights' page: x."),
            7: _RANGE[2],
        },
    )
    with pytest.raises(typer.Exit) as e:
        _calendar(one_way=False, duration="5-7", fmt=fmt)
    cap = capsys.readouterr()
    assert e.value.exit_code == 1
    assert cap.out == ""
    assert [n for n, _ in asked] == [5, 6]
    err = " ".join(cap.err.split())
    named = [
        f"5-night trips: {_CLICK_TIMEOUT}",
        "6-night trips: Chrome could not load Google Flights' page: x.",
        "7-night trips: not asked after 6-night trips failed.",
        "No Google Flights grid; drop --fast for Matrix.",
    ]
    assert all(line in err for line in named), err
    assert [err.find(line) for line in named] == sorted(err.find(line) for line in named), err


def test_a_range_over_the_load_budget_is_refused_with_its_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three lengths over 100 days is 3 x 4 = 12 loads, past eight."""
    _no_matrix(monkeypatch)
    seen = _graphs_are(monkeypatch, cg.GraphRange(_RANGE, []))
    end = date(2026, 10, 20) + timedelta(days=99)
    with pytest.raises(typer.Exit) as e:
        _calendar(one_way=False, duration="5-7", end=end.isoformat())
    cap = capsys.readouterr()
    assert e.value.exit_code == 1
    assert seen == []
    err = " ".join(cap.err.split())
    assert (
        "this is a window and trip-length range needing 12 price-graph loads (at most 8). "
        "Run without --fast for Matrix." in err
    ), err


def test_the_browser_refusal_bounds_the_loads_of_a_range_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One trip length is never refused for its loads: a window longer than the
    budget is asked and cut short. A refusal on other grounds must not say it was."""
    _no_matrix(monkeypatch)
    seen = _graph_is(monkeypatch, _RT)
    with pytest.raises(typer.Exit) as e:
        _calendar(
            origin="JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA,FLL,CLT", one_way=False, duration="7"
        )
    assert e.value.exit_code == 1
    assert seen == []
    err = " ".join(capsys.readouterr().err.split())
    assert (
        "--fast applies only to calendars one-way, of one trip length, or of a trip-length "
        "range within the price graph's page-load budget, between airports or metro codes "
        "Google Flights can ask for (up to 11 airports a leg), whose every filter its "
        "search page can carry; this is 13 airports on one leg (its limit is 11)." in err
    ), err


def test_fast_over_http_still_refuses_a_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Green at the base: the RPC grid prices one trip length, in the base's words."""
    _no_matrix(monkeypatch)
    seen = _graphs_are(monkeypatch, cg.GraphRange(_RANGE, []))
    with pytest.raises(typer.Exit):
        _calendar(one_way=False, duration="5-7", gf_transport="http")
    err = " ".join(capsys.readouterr().err.split())
    assert (
        "--fast applies only to calendars one-way or of one trip length, between airports "
        "or metro codes Google Flights can ask for (up to 11 airports a leg), whose every "
        "filter its search page can carry; this is a trip-length range (5-7 nights). "
        "Run without --fast for Matrix." in err
    ), err
    assert seen == []


@pytest.mark.parametrize(
    ("overrides", "graph"),
    [({"one_way": False, "duration": "7"}, _RT), ({}, _OW)],
    ids=["one-length", "one-way"],
)
@pytest.mark.parametrize("fmt", ["table", "json"])
def test_one_length_and_a_one_way_write_what_they_wrote(
    overrides: dict[str, Any],
    graph: cg.PriceGraph,
    fmt: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Green at the base: one graph, one load path, the single document."""
    _no_matrix(monkeypatch)
    ranged = _graphs_are(monkeypatch, AssertionError("one length is never a range"))
    seen = _graph_is(monkeypatch, graph)
    _calendar(fmt=fmt, **overrides)
    cap = capsys.readouterr()
    assert ranged == []
    assert len(seen) == 1
    if fmt == "json":
        assert json.loads(cap.out) == cg.document(graph, origin="JFK", destination="LAX")
    else:
        assert "and trip length" not in cap.out  # the range table's title
        assert f"{min(c.price for c in graph.cells):.0f}" in cap.out
