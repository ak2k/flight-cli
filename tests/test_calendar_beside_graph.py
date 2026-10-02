# pyright: reportPrivateUsage=false
"""The calendar without `--fast`: Matrix's answer, then Google's price graph.

Google's half runs on a worker thread while Matrix runs, and is printed after
Matrix's output. No test here loads a page: `price_graph` is stubbed, or the
launcher seam is pointed at a fake, and the conftest guard stays on.
"""

from __future__ import annotations

import io
import shlex
import signal
import threading
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, NoReturn, override

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_calgraph as cg
from flight_cli import cli
from flight_cli._gf_errors import GfBrowserUnavailableError, GfConsentError, GfThrottledError
from flight_cli.client import MatrixApiError
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg
from test_calendar_split import _result

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

    from click.testing import Result

    from flight_cli.domain import CalendarFollowup
    from flight_cli.models import CalendarResult

# Far enough out that fli's travel-date validation never sees the past.
_START = date.today() + timedelta(days=60)
_END = _START + timedelta(days=13)
_NOT_SHOWN = "Google Flights price graph not shown:"
_NOT_ASKED = "Google Flights price graph not asked: this is"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _run(*args: str, end: date = _END) -> Result:
    """`calendar` through the CLI parser, over the test window."""
    window = ["--start", _START.isoformat(), "--end", end.isoformat()]
    return CliRunner().invoke(cli.app, ["calendar", *args, *window, "--no-cache"])


class _Matrix:
    """A Matrix client that prices one day of every calendar it is asked."""

    def __init__(self, **_kw: object) -> None: ...

    async def __aenter__(self) -> _Matrix:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
        del search, cache
        return _result({10: {21: ("USD500.00", 3, {5: "USD500.00", 7: "USD520.00"})}})


class _DeadMatrix(_Matrix):
    @override
    async def execute(self, search: CalendarSearch, *, cache: bool = True) -> NoReturn:
        del search, cache
        raise MatrixApiError("matrix is unreachable", kind="server")


def _graph(nights: int | None, *prices: tuple[int, float], loads: int = 1) -> cg.PriceGraph:
    """A graph pricing `_START + offset` at each `(offset, price)`."""
    cells = tuple(
        cg.GraphCell(
            _START + timedelta(days=offset),
            None if nights is None else _START + timedelta(days=offset + nights),
            price,
        )
        for offset, price in prices
    )
    return cg.PriceGraph(nights, cells, loads)


_OW = _graph(None, (0, 204.0), (1, 214.0))


def _graphs_are(
    monkeypatch: pytest.MonkeyPatch, answers: dict[int | None, cg.PriceGraph | BaseException]
) -> list[dict[str, Any]]:
    """Stand in for the page loads, one answer per trip length, recording each
    call and the state of the thread it ran on."""
    seen: list[dict[str, Any]] = []

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        nights = search.window.duration_min if len(search.legs) > 1 else None
        seen.append(
            {
                "nights": nights,
                "headed": headed,
                "pages": pages,
                "depth": gfb._scope_depth.n,
                "handler": signal.getsignal(signal.SIGINT),
                "thread": threading.get_ident(),
            }
        )
        answer = answers[nights]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    return seen


def _matrix_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the two Matrix entry points, letting both run."""
    calls = {"calendar": 0, "weave": 0}
    real_calendar, real_weave = cli._run_calendar, cli._run_calendar_enriched

    def _calendar(*a: Any, **kw: Any) -> tuple[CalendarResult, int, bool]:
        calls["calendar"] += 1
        return real_calendar(*a, **kw)

    def _weave(*a: Any, **kw: Any) -> None:
        calls["weave"] += 1
        real_weave(*a, **kw)

    monkeypatch.setattr(cli, "_run_calendar", _calendar)
    monkeypatch.setattr(cli, "_run_calendar_enriched", _weave)
    return calls


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _Matrix)


@pytest.fixture
def keep_sigint() -> Iterator[None]:
    """Restore SIGINT after a test whose interrupt leaves it ignored for good."""
    previous = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, previous)


# ───────────────────────────── what is asked ─────────────────────────────────


@pytest.mark.parametrize(
    ("route", "shape", "lengths"),
    [
        pytest.param(route, shape, lengths, id=" ".join((*route, *shape)))
        for route in (("NYC", "LON"), ("JFK,EWR", "LHR"), ("JFK", "LHR"))
        for shape, lengths in (
            (("--one-way",), [None]),
            (("-d", "7"), [7]),
            (("-d", "5-7"), [5, 6, 7]),
        )
    ],
)
def test_airports_sets_and_metro_codes_are_asked_one_way_and_round_trip(
    route: tuple[str, str],
    shape: tuple[str, ...],
    lengths: list[int | None],
    monkeypatch: pytest.MonkeyPatch,
    matrix: None,
) -> None:
    calls = _matrix_calls(monkeypatch)
    seen = _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0)) for n in lengths})
    result = _run(*route, *shape)
    assert result.exit_code == 0, result.output
    assert [s["nights"] for s in seen] == lengths
    assert calls == {"calendar": 1, "weave": 0}
    assert _NOT_ASKED not in _flat(result.stderr)


@pytest.mark.parametrize(
    ("args", "end", "reason"),
    [
        (("JFK", "LHR", "--one-way", "--currency", "EUR"), _END, "a non-USD currency."),
        (("JFK", "LHR", "--one-way", "--routing", "~BA+"), _END, "Tier-2 routing"),
        (
            ("NYC", "LON", "--one-way", "--depart-times", "morning"),
            _END,
            "a departure-time window.",
        ),
        (
            ("JFK", "LHR", "-d", "3-12"),
            _START + timedelta(days=30),
            "needing 10 price-graph loads (at most 8).",
        ),
        (
            ("NYC", "LON", "--one-way"),
            _START + timedelta(days=258),
            f"{_NOT_ASKED} a window needing 9 price-graph loads (at most 8).",
        ),
    ],
    ids=["currency", "tier-2", "times", "ten-loads", "one-way-nine-loads"],
)
def test_a_calendar_the_graph_cannot_answer_says_so_and_runs_matrix_alone(
    args: tuple[str, ...], end: date, reason: str, monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    calls = _matrix_calls(monkeypatch)
    seen = _graphs_are(monkeypatch, {})
    result = _run(*args, end=end)
    err = _flat(result.stderr)
    assert result.exit_code == 0, result.output
    assert err.count(_NOT_ASKED) == 1
    assert reason in err
    assert calls == {"calendar": 1, "weave": 0}
    assert seen == []
    assert "opening Chrome" not in err


def test_a_ten_load_window_is_refused_before_any_load() -> None:
    """The count behind the ten-load refusal: ten trip lengths over 31 days."""
    window = CalendarWindow(
        start=_START, end=_START + timedelta(days=30), duration_min=3, duration_max=12
    )
    legs = (Leg.of("JFK", "LHR"), Leg.of("LHR", "JFK"))
    search = CalendarSearch(legs=legs, window=window)
    assert cg.page_budget_blocker(search) == (
        "a window and trip-length range needing 10 price-graph loads (at most 8)"
    )
    one_way = search.model_copy(update={"legs": legs[:1]})
    assert cg.page_budget_blocker(one_way) is None  # a one-way is one graph


@pytest.mark.parametrize("round_trip", [False, True], ids=["one-way", "one-trip-length"])
def test_a_single_graph_refused_for_its_window_names_no_trip_length_range(
    round_trip: bool,
) -> None:
    window = CalendarWindow(
        start=_START, end=_START + timedelta(days=258), duration_min=7, duration_max=7
    )
    legs = (Leg.of("JFK", "LHR"), Leg.of("LHR", "JFK"))
    search = CalendarSearch(legs=legs if round_trip else legs[:1], window=window)
    assert cg.page_budget_blocker(search) == "a window needing 9 price-graph loads (at most 8)"


def test_json_is_matrixs_document_alone_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    calls = _matrix_calls(monkeypatch)
    seen = _graphs_are(monkeypatch, {})
    base = _run("NYC", "LON", "--one-way", "--format", "json", "--gf-transport", "http")
    result = _run("NYC", "LON", "--one-way", "--format", "json")
    assert result.exit_code == base.exit_code == 0
    assert result.stdout == base.stdout
    assert result.stderr == base.stderr
    assert calls == {"calendar": 2, "weave": 0}
    assert seen == []


def test_json_with_a_transport_asked_for_says_why_the_graph_is_not(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    """A flag asking for Chrome that is then dropped is said out loud, on stderr."""
    seen = _graphs_are(monkeypatch, {})
    base = _run("NYC", "LON", "--one-way", "--format", "json", "--gf-transport", "http")
    result = _run("NYC", "LON", "--one-way", "--format", "json", "--gf-transport", "browser")
    assert result.stdout == base.stdout
    assert _flat(result.stderr).count(f"{_NOT_ASKED} JSON output.") == 1
    assert seen == []


def test_http_keeps_the_weave_for_a_one_way_pair_and_never_asks_the_graph(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    calls = _matrix_calls(monkeypatch)
    seen = _graphs_are(monkeypatch, {})
    result = _run("JFK", "LHR", "--one-way", "--gf-transport", "http")
    assert result.exit_code == 0, result.output
    assert calls == {"calendar": 0, "weave": 1}
    assert seen == []
    assert _NOT_ASKED not in _flat(result.stderr)


def test_http_keeps_matrix_alone_for_a_set(monkeypatch: pytest.MonkeyPatch, matrix: None) -> None:
    calls = _matrix_calls(monkeypatch)
    seen = _graphs_are(monkeypatch, {})
    result = _run("NYC", "LON", "--gf-transport", "http")
    assert result.exit_code == 0, result.output
    assert calls == {"calendar": 1, "weave": 0}
    assert seen == []
    assert "Google Flights" not in _flat(result.stderr)


# ───────────────────────────── the concurrent run ────────────────────────────


def test_the_graph_is_read_while_matrix_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each half waits for the other to have started: run one after the other,
    the first to wait would time out."""
    job_started, matrix_asked = threading.Event(), threading.Event()
    met: dict[str, bool] = {}

    class _Waiting(_Matrix):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            met["matrix saw the job"] = job_started.wait(5)
            matrix_asked.set()
            return await super().execute(search, cache=cache)

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        del search, headed, pages
        job_started.set()
        met["the job saw matrix"] = matrix_asked.wait(5)
        return _OW

    monkeypatch.setattr(cli, "MatrixClient", _Waiting)
    monkeypatch.setattr(cg, "price_graph", _price_graph)
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 0, result.output
    assert met == {"matrix saw the job": True, "the job saw matrix": True}


def test_matrixs_table_and_links_come_before_googles_table(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    _graphs_are(monkeypatch, {None: _OW})
    base = _run("JFK", "LHR", "--one-way", "--gf-transport", "http")
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(base.stdout)
    google = result.stdout[len(base.stdout) :]
    assert "Matrix deep-link:" in base.stdout
    assert "(Google Flights)" not in base.stdout
    assert _flat(google).startswith("2 priced days · cheapest: 204 (USD)")
    assert "lowest fare per departure day (Google Flights)" in _flat(google)


def test_the_job_runs_guarded_scoped_and_headed_on_its_own_thread(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    """The handler is armed while the job runs and restored after; the job holds
    one scope, and its session is closed on the thread that opened it."""
    closed_on: list[int] = []
    seen: list[dict[str, Any]] = []

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        del search, pages
        gfb.session(headed=headed)  # this thread's session, unlaunched
        seen.append(
            {
                "headed": headed,
                "depth": gfb._scope_depth.n,
                "handler": signal.getsignal(signal.SIGINT),
                "thread": threading.get_ident(),
            }
        )
        return _OW

    def _close(_self: gfb.GfBrowserSession) -> None:
        closed_on.append(threading.get_ident())

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    monkeypatch.setattr(gfb.GfBrowserSession, "close", _close)
    before = signal.getsignal(signal.SIGINT)
    result = _run("JFK", "LHR", "--one-way", "--gf-headed")
    assert result.exit_code == 0, result.output
    (call,) = seen
    assert call["headed"] is True
    assert call["depth"] == 1
    assert call["handler"] is not before
    assert signal.getsignal(signal.SIGINT) is before
    assert call["thread"] != threading.get_ident()
    assert closed_on == [call["thread"]]


@pytest.mark.parametrize(
    ("loads", "pages"),
    [(1, [8, 7, 6]), (2, [8, 6, 4])],
    ids=["one-load-each", "two-loads-each"],
)
def test_a_trip_length_range_asks_each_length_with_the_loads_left(
    loads: int, pages: list[int], monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    seen = _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0 + n), loads=loads) for n in (5, 6, 7)})
    result = _run("NYC", "LON", "-d", "5-7")
    assert result.exit_code == 0, result.output
    assert [s["nights"] for s in seen] == [5, 6, 7]
    assert [s["pages"] for s in seen] == pages
    assert len({s["thread"] for s in seen}) == 1


def _google_part(result: Result, base: Result) -> str:
    """What the run printed after Matrix's own output, flattened."""
    assert result.stdout.startswith(base.stdout)
    return _flat(result.stdout[len(base.stdout) :])


def _missed(loads: int = 2) -> cg.GfGraphStalledError:
    """A length whose page drew no graph on either of its loads."""
    click = "Chrome could not click 'Price graph' on Google Flights' page: Locator.click: Timeout."
    return cg.GfGraphStalledError(GfBrowserUnavailableError(click), loads=loads)


def test_a_length_no_load_is_left_for_is_named_not_dropped(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    seen = _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0), loads=4) for n in (5, 6, 7)})
    base = _run("NYC", "LON", "-d", "5-7", "--gf-transport", "http")
    result = _run("NYC", "LON", "-d", "5-7")
    err = _flat(result.stderr)
    assert result.exit_code == 0, result.output
    assert [s["nights"] for s in seen] == [5, 6]
    assert "┃ departure ┃ min (USD) ┃ 5n ┃ 6n ┃" in _google_part(result, base)
    assert err.count(_NOT_SHOWN) == 1
    assert f"{_NOT_SHOWN} 7-night trips: no price-graph load of the 8 was left." in err


@pytest.mark.parametrize(
    ("lost", "columns", "trips"),
    [
        (7, "┃ 5n ┃ 6n ┃", "5-6-night"),
        (6, "┃ 5n ┃ 7n ┃", "5- and 7-night"),
        (5, "┃ 6n ┃ 7n ┃", "6-7-night"),
    ],
    ids=["last", "middle", "first"],
)
def test_a_length_whose_page_drew_no_graph_leaves_the_others_and_is_named(
    lost: int, columns: str, trips: str, monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    """Its column is absent rather than a column of dashes nobody priced, the
    summary names only the lengths shown, and the one line names the lost one
    with the browser's own words."""
    answers: dict[int | None, cg.PriceGraph | BaseException] = {
        n: _graph(n, (0, 300.0 + n), (1, 310.0 + n)) for n in (5, 6, 7)
    }
    answers[lost] = _missed()
    seen = _graphs_are(monkeypatch, answers)
    base = _run("NYC", "LON", "-d", "5-7", "--gf-transport", "http")
    result = _run("NYC", "LON", "-d", "5-7")
    err = _flat(result.stderr)
    google = _google_part(result, base)
    assert result.exit_code == 0, result.output
    assert [s["nights"] for s in seen] == [5, 6, 7]
    assert google.startswith(f"2 priced days · {trips} round trips · cheapest:")
    assert f"┃ departure ┃ min (USD) {columns}" in google
    assert f"{lost}n" not in google
    assert err.count(_NOT_SHOWN) == 1
    assert (
        f"{_NOT_SHOWN} {lost}-night trips: Chrome could not click 'Price graph' on "
        "Google Flights' page: Locator.click: Timeout. --gf-transport http skips Chrome."
    ) in err


def test_a_wall_on_one_length_names_it_and_the_lengths_not_asked_after_it(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    seen = _graphs_are(
        monkeypatch,
        {5: _graph(5, (0, 305.0)), 6: GfThrottledError("x"), 7: _graph(7, (0, 307.0))},
    )
    base = _run("NYC", "LON", "-d", "5-7", "--gf-transport", "http")
    result = _run("NYC", "LON", "-d", "5-7")
    err = _flat(result.stderr)
    google = _google_part(result, base)
    assert result.exit_code == 0, result.output
    assert [s["nights"] for s in seen] == [5, 6]
    assert google.startswith("1 priced days · 5-night round trip · cheapest: 305 (USD)")
    assert err.count(_NOT_SHOWN) == 1
    assert (
        f"{_NOT_SHOWN} 6-night trips: Google Flights rate-limited the browser rung. "
        "7-night trips: not asked after 6-night trips failed."
    ) in err


@pytest.mark.parametrize(
    ("answers", "said"),
    [
        (
            {n: _missed() for n in (5, 6, 7)},
            f"{_NOT_SHOWN} Chrome could not click 'Price graph' on Google Flights' page: "
            "Locator.click: Timeout. --gf-transport http skips Chrome.",
        ),
        (
            {5: cg.GfPriceGraphError("Google Flights answered the price graph with error 13")},
            f"{_NOT_SHOWN} 5-night trips: Google Flights answered the price graph with error 13.",
        ),
    ],
    ids=["every-page-missed", "error-row"],
)
def test_a_range_with_no_length_priced_keeps_its_one_line(
    answers: dict[int | None, cg.PriceGraph | BaseException],
    said: str,
    monkeypatch: pytest.MonkeyPatch,
    matrix: None,
) -> None:
    _graphs_are(monkeypatch, answers)
    base = _run("NYC", "LON", "-d", "5-7", "--gf-transport", "http")
    result = _run("NYC", "LON", "-d", "5-7")
    err = _flat(result.stderr)
    assert result.exit_code == 0, result.output
    assert result.stdout == base.stdout
    assert err.count(_NOT_SHOWN) == 1
    assert said in err


# ───────────────────────────── Google's table ────────────────────────────────


def _console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=100, no_color=True))
    return buf


def test_the_range_table_has_a_column_per_length_and_the_row_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second date is priced by the 5-night graph alone, so its 7-night cell
    is a dash; a date neither length priced has no row."""
    out = _console(monkeypatch)
    graphs = [
        _graph(5, (0, 410.0), (1, 380.0)),
        _graph(7, (0, 395.5), (3, 420.0)),
    ]
    cli._render_graph_range(
        graphs, origin=("JFK",), destination=("LHR",), sd=_START, ed=_END, across_set=False
    )
    d0, d1, d3 = (_START + timedelta(days=i) for i in (0, 1, 3))
    rows = [ln for ln in out.getvalue().splitlines() if ln.startswith("│")]
    assert [[c.strip() for c in ln.strip("│").split("│")] for ln in rows] == [
        [d1.isoformat(), "380", "380", "—"],
        [d0.isoformat(), "396", "410", "396"],
        [d3.isoformat(), "420", "—", "420"],
    ]
    text = _flat(out.getvalue())
    assert text.startswith(
        f"3 priced days · 5- and 7-night round trips · cheapest: 380 (USD) · "
        f"window {_START} → {_END}"
    )
    assert "┃ departure ┃ min (USD) ┃ 5n ┃ 7n ┃" in text
    assert "lowest fare per departure day and trip length (Google Flights)" in text
    assert "cheapest across" not in text


@pytest.mark.parametrize(
    ("lengths", "trips"),
    [
        ((5, 6, 7), "5-7-night"),
        ((5, 7), "5- and 7-night"),
        ((5, 7, 8), "5-, 7- and 8-night"),
    ],
    ids=["unbroken", "one-gap", "gap-then-run"],
)
def test_the_range_summary_names_only_the_lengths_its_table_shows(
    lengths: tuple[int, ...], trips: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _console(monkeypatch)
    graphs = [_graph(n, (0, 400.0 + n)) for n in lengths]
    cli._render_graph_range(
        graphs, origin=("JFK",), destination=("LHR",), sd=_START, ed=_END, across_set=False
    )
    assert _flat(out.getvalue()).startswith(f"1 priced days · {trips} round trips · cheapest:")


@pytest.mark.parametrize(
    ("args", "graphs"),
    [
        (("NYC", "LON", "--one-way"), {None: _OW}),
        (("JFK,EWR", "LHR", "-d", "5-6"), {5: _graph(5, (0, 400.0)), 6: _graph(6, (0, 390.0))}),
    ],
    ids=["metro-one-way", "list-range"],
)
def test_a_set_titles_the_route_as_typed_and_the_cheapest_across_it(
    args: tuple[str, ...],
    graphs: dict[int | None, cg.PriceGraph | BaseException],
    monkeypatch: pytest.MonkeyPatch,
    matrix: None,
) -> None:
    _graphs_are(monkeypatch, graphs)
    base = _run(*args, "--gf-transport", "http")
    result = _run(*args)
    assert result.exit_code == 0, result.output
    google = _flat(result.stdout[len(base.stdout) :])
    assert f"{args[0]} → {args[1]}:" in google
    assert "cheapest across every airport pair (Google Flights)" in google


def test_the_fast_title_of_a_set_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _graphs_are(monkeypatch, {None: _OW})
    result = _run("NYC", "LON", "--one-way", "--fast")
    assert result.exit_code == 0, result.output
    assert "NYC → LON: lowest fare per departure day (Google Flights)" in _flat(result.stdout)


# ───────────────────────────── degradation ───────────────────────────────────


def _no_patchright() -> NoReturn:
    raise GfBrowserUnavailableError(
        "Google Flights' browser rung needs patchright, which isn't installed.",
        remedy=gfb._INSTALL_HINT,
    )


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


@pytest.mark.parametrize(
    ("factory", "failure", "said", "not_said"),
    [
        (_no_patchright, None, "needs patchright", "Retry, or use"),
        (lambda: _NoChrome, None, "Chrome failed to launch", "Retry, or use"),
        (None, GfThrottledError("x"), "rate-limited the browser rung", "error 13"),
        (None, GfConsentError("Google served its consent page"), "consent page", "rate"),
        (
            None,
            cg.GfPriceGraphError("Google Flights answered the price graph with error 13", code=13),
            "answered the price graph with error 13.",
            "rate-limited",
        ),
        (None, RuntimeError("something [/x] broke"), "something [/x] broke.", "Traceback"),
    ],
    ids=["no-patchright", "no-chrome", "throttle", "consent", "graph-error", "other"],
)
def test_a_google_failure_is_one_line_and_leaves_matrix_as_it_was(
    factory: Any,
    failure: BaseException | None,
    said: str,
    not_said: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    matrix: None,
) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv(gfb._BROWSER_BIN_ENV, raising=False)
    if factory is not None:
        monkeypatch.setattr(gfb, "_playwright_factory", factory)
    else:
        assert failure is not None
        _graphs_are(monkeypatch, {None: failure})
    base = _run("JFK", "LHR", "--one-way", "--gf-transport", "http")
    result = _run("JFK", "LHR", "--one-way")
    err = _flat(result.stderr)
    assert result.exit_code == base.exit_code == 0
    assert result.stdout == base.stdout
    assert err.count(_NOT_SHOWN) == 1
    assert said in err
    assert not_said not in err
    assert err.endswith("--gf-transport http skips Chrome.")
    assert "Traceback" not in err


def test_the_launch_remedy_survives_and_the_launch_is_announced_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, matrix: None
) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv(gfb._BROWSER_BIN_ENV, raising=False)
    monkeypatch.setattr(gfb, "_playwright_factory", lambda: _NoChrome)
    result = _run("NYC", "LON", "--one-way")
    err = _flat(result.stderr)
    assert result.exit_code == 0, result.output
    assert err.count("opening Chrome") == 1
    assert "patchright install chrome" in err
    assert gfb._BROWSER_BIN_ENV in err
    assert "--backend matrix" not in err


def test_a_google_table_that_cannot_render_is_one_line(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    _graphs_are(monkeypatch, {None: _OW})

    def _broken(*_a: object, **_k: object) -> NoReturn:
        raise ValueError("no room")

    base = _run("JFK", "LHR", "--one-way", "--gf-transport", "http")
    monkeypatch.setattr(cli, "_render_date_grid", _broken)
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 0, result.output
    assert result.stdout == base.stdout
    assert _flat(result.stderr).count(f"{_NOT_SHOWN} no room.") == 1


def test_a_matrix_failure_keeps_its_lines_and_exit_code_under_googles_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    _graphs_are(monkeypatch, {None: _OW})
    base = _run("NYC", "LON", "--one-way", "--gf-transport", "http")
    result = _run("NYC", "LON", "--one-way")
    assert base.exit_code == result.exit_code == 1
    assert result.stderr == base.stderr
    assert result.stdout.startswith(base.stdout)
    assert "NYC → LON: lowest fare per departure day" in _flat(result.stdout)


# ───────────────────────────── the two lows ──────────────────────────────────

_DIFFER = "Matrix and Google Flights differ on the lowest fare:"
_STOP_CLAUSE = (
    "; Matrix held each trip to one stop more than the fewest on its route, "
    "Google allowed any number of stops."
)
_HOW = (
    "Matrix's grid is fares Matrix priced; Google's graph is one price per date pair "
    "with no itinerary behind it; either can leave out a fare the other lists."
)
# A day's lowest fare, and its fare per trip length.
_Day = tuple[str, dict[int, str]]


def _iso(offset: int) -> str:
    return (_START + timedelta(days=offset)).isoformat()


def _matrix_prices(
    monkeypatch: pytest.MonkeyPatch, days: dict[int, _Day], pairs: dict[str, dict[int, _Day]]
) -> list[CalendarSearch]:
    """Matrix prices `_START + offset` for each offset of `days`, filed by month as
    Matrix files it; a query between the airports a key of `pairs` names
    ("EWR-LGW") prices that key's days instead. Returns the queries it is asked."""
    asked: list[CalendarSearch] = []

    class _Priced(_Matrix):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            del cache
            asked.append(search)
            leg = search.legs[0]
            answer = pairs.get(f"{','.join(leg.origins)}-{','.join(leg.destinations)}", days)
            by_month: dict[int, dict[int, tuple[str, int, dict[int, str]]]] = {}
            for offset, (low, nights) in answer.items():
                when = _START + timedelta(days=offset)
                by_month.setdefault(when.month, {})[when.day] = (low, 3, nights)
            return _result(by_month)

    monkeypatch.setattr(cli, "MatrixClient", _Priced)
    return asked


def _note(result: Result) -> str:
    """The one note on stderr, flattened, from its opening to the end."""
    err = _flat(result.stderr)
    opening = "Matrix and Google Flights "
    assert err.count(opening) == 1, err
    return err[err.index(opening) :]


_JFK_LHR_DAY = {2: ("USD500.00", {5: "USD500.00", 7: "USD520.00"})}
_JFK_LHR_GRAPHS: dict[int | None, cg.PriceGraph | BaseException] = {
    5: _graph(5, (0, 340.0)),
    6: _graph(6, (1, 300.0)),
    7: _graph(7, (0, 300.0), (1, 310.0)),
}


def test_two_lows_that_differ_get_one_note_naming_both_on_stderr_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google's 300 is on its first row, 7 nights from the first day, ahead of the
    same 300 at 6 nights a day later."""
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)

    def _no_note(*_a: object, **_k: object) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(cli, "_two_lows_note", _no_note, raising=False)
        quiet = _run("JFK", "LHR", "-d", "5-7")
    result = _run("JFK", "LHR", "-d", "5-7")
    note = (
        f"{_DIFFER} Matrix USD500.00 ({_iso(2)} to {_iso(7)}, 5 nights, JFK→LHR), "
        f"Google Flights USD300 ({_iso(0)} to {_iso(7)}, 7 nights, JFK→LHR). "
        "Both asked economy, 1 adult, 5-7 nights, in USD, between the same airports"
        f"{_STOP_CLAUSE[:-1]}. {_HOW} A search on the date pair shows what is bookable: "
        f"flight detail JFK LHR --dep {_iso(2)} --return {_iso(7)} (Matrix), "
        f"flight search JFK LHR --dep {_iso(0)} --return {_iso(7)} --backend gflight (Google)."
    )
    assert result.exit_code == quiet.exit_code == 0, result.output
    assert result.stdout == quiet.stdout
    assert _note(result) == note
    assert _flat(_flat(result.stderr).replace(note, "")) == _flat(quiet.stderr)


@pytest.mark.parametrize(
    ("limit", "flags"),
    [(("--stops", "1"), "--stops 1"), (("--ext", "MAXSTOPS 1"), "--ext 'MAXSTOPS 1'")],
    ids=["stops", "stop-code"],
)
def test_a_stop_limit_google_was_asked_drops_the_stop_clause(
    limit: tuple[str, str], flags: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)
    result = _run("JFK", "LHR", "-d", "5-7", *limit)
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert note.startswith(_DIFFER)
    assert "between the same airports. Matrix's grid" in note
    assert "one stop more than the fewest" not in note
    assert "any number of stops" not in note
    assert f"--return {_iso(7)} {flags} (Matrix)" in note
    assert f"--return {_iso(7)} {flags} --backend gflight (Google)." in note


def test_a_metro_low_names_matrixs_pair_and_googles_cheapest_across_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_prices(
        monkeypatch,
        {2: ("USD500.00", {5: "USD500.00"})},
        {"EWR-LGW": {2: ("USD450.00", {5: "USD450.00", 6: "USD470.00"})}},
    )
    _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0 + n)) for n in (5, 6, 7)})
    result = _run("NYC", "LON", "-d", "5-7")
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert note.startswith(
        f"{_DIFFER} Matrix USD450.00 ({_iso(2)} to {_iso(7)}, 5 nights, EWR→LGW), "
        f"Google Flights USD305 ({_iso(0)} to {_iso(5)}, 5 nights, cheapest across NYC→LON). "
    )
    assert note.endswith(
        f"flight detail EWR LGW --dep {_iso(2)} --return {_iso(7)} --currency USD (Matrix), "
        f"flight search NYC LON --dep {_iso(0)} --return {_iso(5)} --backend gflight (Google)."
    )


def test_a_one_way_names_one_date_and_no_nights(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_prices(monkeypatch, {2: ("USD500.00", {})}, {})
    _graphs_are(monkeypatch, {None: _graph(None, (0, 300.0), (1, 320.0))})
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 0, result.output
    assert _note(result) == (
        f"{_DIFFER} Matrix USD500.00 ({_iso(2)}, one-way, JFK→LHR), "
        f"Google Flights USD300 ({_iso(0)}, one-way, JFK→LHR). "
        "Both asked economy, 1 adult, one-way, in USD, between the same airports"
        f"{_STOP_CLAUSE[:-1]}. {_HOW} A search on the date shows what is bookable: "
        f"flight detail JFK LHR --dep {_iso(2)} (Matrix), "
        f"flight search JFK LHR --dep {_iso(0)} --backend gflight (Google)."
    )


def test_a_lost_length_names_the_lengths_google_priced_after_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, {5: _graph(5, (0, 300.0)), 6: _graph(6, (0, 310.0)), 7: _missed()})
    result = _run("JFK", "LHR", "-d", "5-7")
    err = _flat(result.stderr)
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert err.index(f"{_NOT_SHOWN} 7-night trips:") < err.index(_DIFFER)
    assert (
        "Both asked economy, 1 adult, in USD, between the same airports, Matrix for "
        f"5-7 nights and Google for 5-6-night trips only{_STOP_CLAUSE}"
    ) in note
    assert f"Google Flights USD300 ({_iso(0)} to {_iso(5)}, 5 nights, JFK→LHR)" in note


def test_a_range_left_with_one_length_names_that_length(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, {5: _graph(5, (0, 300.0)), 6: _missed(), 7: _missed()})
    result = _run("JFK", "LHR", "-d", "5-7")
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert "5-night trips only" in note
    assert "5-5-night" not in note


@pytest.mark.parametrize("low", ["GBP400.00", "GBP300.00"], ids=["apart", "same-number"])
def test_a_matrix_grid_in_another_currency_is_named_and_not_compared(
    low: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _matrix_prices(monkeypatch, {2: (low, {5: low})}, {})
    _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)})
    result = _run("LHR", "JFK", "-d", "5-7")
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert _DIFFER not in note
    assert note.startswith(
        "Matrix and Google Flights priced in different currencies, so their lowest fares "
        f"are not compared: Matrix {low} ({_iso(2)} to {_iso(7)}, 5 nights, LHR→JFK), "
        f"Google Flights USD300 ({_iso(0)} to {_iso(5)}, 5 nights, LHR→JFK); "
        "--currency USD asks Matrix in USD. Both asked economy, 1 adult, 5-7 nights, "
        "between the same airports;"
    )
    assert "in USD, between" not in note


def test_a_low_in_a_later_month_of_the_window_is_dated_in_that_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix files a day by month and day of month; 35 days on is always in a
    later month than the window's first day."""
    _matrix_prices(
        monkeypatch,
        {3: ("USD520.00", {5: "USD520.00"}), 35: ("USD500.00", {6: "USD500.00"})},
        {},
    )
    _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)})
    result = _run("JFK", "LHR", "-d", "5-7", end=_START + timedelta(days=40))
    assert result.exit_code == 0, result.output
    assert f"Matrix USD500.00 ({_iso(35)} to {_iso(41)}, 6 nights, JFK→LHR)" in _note(result)


def test_both_commands_ask_the_calendars_own_question(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_prices(monkeypatch, {2: ("USD900.00", {3: "USD900.00", 4: "USD950.00"})}, {})
    _graphs_are(monkeypatch, {n: _graph(n, (1, 700.0)) for n in (3, 4)})
    asked = ("--cabin", "business", "--adults", "2", "--stops", "1", "--routing", "AA+")
    result = _run("JFK", "LHR", "-d", "3-4", *asked)
    note = _note(result)
    assert result.exit_code == 0, result.output
    assert "Both asked business, 2 adults, 3-4 nights, in USD, between the same airports." in note
    assert note.endswith(
        f"flight detail JFK LHR --dep {_iso(2)} --return {_iso(5)} -d 3-4 --cabin business "
        "--adults 2 --stops 1 --routing AA+ (Matrix), "
        f"flight search JFK LHR --dep {_iso(1)} --return {_iso(4)} --cabin business --adults 2 "
        "--stops 1 --routing AA+ --backend gflight (Google)."
    )


def test_the_detail_it_names_asks_matrix_in_the_currency_its_grid_was_asked_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two origins and no `--currency` ask Matrix's grid in USD; one London pair
    asked alone would answer in pounds."""
    asked = _matrix_prices(monkeypatch, {2: ("USD500.00", {5: "USD500.00"})}, {})
    _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)})
    result = _run("LON", "PAR", "-d", "5-7")
    note = _note(result)
    assert result.exit_code == 0, result.output
    detail: list[CalendarFollowup] = []

    def _sent(search: CalendarFollowup, *_a: object) -> NoReturn:
        detail.append(search)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_run", _sent)
    command = shlex.split(note[note.index("flight detail ") : note.index(" (Matrix)")])
    ran = CliRunner().invoke(cli.app, command[1:])
    assert ran.exit_code == 0, ran.output
    assert {s.options.currency for s in asked} == {"USD"}
    assert [s.options.currency for s in detail] == ["USD"]


def test_a_price_matrix_wrote_is_printed_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_prices(monkeypatch, {2: ("USD[/x]500.00", {5: "USD[/x]500.00"})}, {})
    _graphs_are(monkeypatch, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)})
    result = _run("JFK", "LHR", "-d", "5-7")
    assert result.exit_code == 0, result.output
    assert f"{_DIFFER} Matrix USD[/x]500.00 ({_iso(2)} to {_iso(7)}" in _note(result)


@pytest.mark.parametrize(
    ("days", "graphs", "not_shown"),
    [
        ({2: ("USD300.00", {5: "USD300.00"})}, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)}, 0),
        ({2: ("USD300.40", {5: "USD300.40"})}, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)}, 0),
        ({2: ("USD299.50", {5: "USD299.50"})}, {n: _graph(n, (0, 299.8)) for n in (5, 6, 7)}, 0),
        ({}, {n: _graph(n, (0, 300.0)) for n in (5, 6, 7)}, 0),
        (_JFK_LHR_DAY, {n: GfThrottledError("x") for n in (5, 6, 7)}, 1),
    ],
    ids=["equal", "cents-apart", "both-print-300", "matrix-priced-nothing", "graph-failed"],
)
def test_lows_that_agree_or_a_side_that_priced_nothing_get_no_note(
    days: dict[int, _Day],
    graphs: dict[int | None, cg.PriceGraph | BaseException],
    not_shown: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_prices(monkeypatch, days, {})
    _graphs_are(monkeypatch, graphs)
    result = _run("JFK", "LHR", "-d", "5-7")
    err = _flat(result.stderr)
    assert result.exit_code == 0, result.output
    assert "Matrix and Google Flights" not in err
    assert err.count(_NOT_SHOWN) == not_shown


def test_a_failed_matrix_gets_no_note_and_keeps_its_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "MatrixClient", _DeadMatrix)
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)
    base = _run("JFK", "LHR", "-d", "5-7", "--gf-transport", "http")
    result = _run("JFK", "LHR", "-d", "5-7")
    assert base.exit_code == result.exit_code == 1
    assert result.stderr == base.stderr
    assert "Matrix and Google Flights" not in _flat(result.stderr)


@pytest.mark.parametrize(
    "shape",
    [("--format", "json"), ("--gf-transport", "http"), ("-d", "7", "--fast")],
    ids=["json", "http", "fast"],
)
def test_json_http_and_fast_print_no_note(
    shape: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)
    result = _run("JFK", "LHR", "-d", "5-7", *shape)
    assert result.exit_code == 0, result.output
    assert "Matrix and Google Flights" not in _flat(result.stdout + result.stderr)


def test_a_note_that_cannot_be_composed_leaves_both_answers_as_they_were(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_prices(monkeypatch, _JFK_LHR_DAY, {})
    _graphs_are(monkeypatch, _JFK_LHR_GRAPHS)
    base = _run("JFK", "LHR", "-d", "5-7", "--gf-transport", "http")

    def _broken(*_a: object, **_k: object) -> NoReturn:
        raise ValueError("no note")

    monkeypatch.setattr(cli, "_two_lows_note", _broken, raising=False)
    result = _run("JFK", "LHR", "-d", "5-7")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(base.stdout)
    assert result.stderr == base.stderr
    assert "Traceback" not in result.output


# ───────────────────────────── Ctrl-C ─────────────────────────────────────────


def _stopping(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Record the handler's driver stop, which is what frees the worker."""
    stopped = threading.Event()
    real = gfb.stop_all_drivers

    def _stop() -> None:
        stopped.set()
        real()

    monkeypatch.setattr(gfb, "stop_all_drivers", _stop)
    return stopped


def _job_until_stopped(
    monkeypatch: pytest.MonkeyPatch, stopped: threading.Event, before: threading.Event | None
) -> threading.Event:
    """A graph that waits for the driver to be stopped, then fails as a killed
    navigation does. With `before`, it first waits for that and sends the
    interrupt itself, to the main thread."""
    started = threading.Event()

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        del search, headed, pages
        started.set()
        if before is not None:
            assert before.wait(5)
            # Never into pytest's own handler, which would end the whole run.
            assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
            main = threading.main_thread().ident
            assert main is not None
            signal.pthread_kill(main, signal.SIGINT)
        assert stopped.wait(5)
        raise KeyboardInterrupt

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    return started


def test_a_ctrl_c_before_matrix_answers_exits_130_with_nothing_on_stdout(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None
) -> None:
    stopped = _stopping(monkeypatch)
    started = _job_until_stopped(monkeypatch, stopped, None)

    class _Interrupted(_Matrix):
        @override
        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> NoReturn:
            del search, cache
            assert started.wait(5)
            signal.raise_signal(signal.SIGINT)
            raise AssertionError("the interrupt did not land")

    monkeypatch.setattr(cli, "MatrixClient", _Interrupted)
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 130, (result.exit_code, result.output)
    assert result.stdout == ""
    assert stopped.is_set()
    assert "Traceback" not in result.stderr


def test_a_ctrl_c_while_the_graph_runs_after_matrix_exits_130_after_matrixs_output(
    monkeypatch: pytest.MonkeyPatch, keep_sigint: None, matrix: None
) -> None:
    base = _run("JFK", "LHR", "--one-way", "--gf-transport", "http")
    stopped = _stopping(monkeypatch)
    delivered = threading.Event()
    real_deliver = cli._deliver_calendar

    def _deliver(*a: Any, **kw: Any) -> None:
        real_deliver(*a, **kw)
        delivered.set()

    monkeypatch.setattr(cli, "_deliver_calendar", _deliver)
    _job_until_stopped(monkeypatch, stopped, delivered)
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 130, (result.exit_code, result.output)
    assert result.stdout == base.stdout
    assert stopped.is_set()
    assert "Traceback" not in result.stderr


def test_any_other_way_out_of_matrix_stops_the_graph_instead_of_waiting_for_it(
    monkeypatch: pytest.MonkeyPatch, matrix: None
) -> None:
    stopped = _stopping(monkeypatch)
    started = _job_until_stopped(monkeypatch, stopped, None)

    def _exits(*_a: object, **_k: object) -> NoReturn:
        assert started.wait(5)
        raise SystemExit(3)

    monkeypatch.setattr(cli, "_run_matrix_calendar", _exits)
    result = _run("JFK", "LHR", "--one-way")
    assert result.exit_code == 3, result.output
    assert stopped.is_set()
    assert result.stdout == ""
