# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--gf-transport auto` moves a search to Chrome when a throttle outlasts the ladder.

Rung 1 is faked at its GET (`_one_call`) or at its ladder (`_one_call_with_retry`),
and rung 2 at `_one_call_browser`; no test reaches Google, Matrix or Chrome.
`events` records each request in order, `http` or `chrome`, so "no http request
after the escalation" is read off one list."""

from __future__ import annotations

import contextvars
import json
import signal
import threading
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import capture_err
from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfBackendError, GfBrowserUnavailableError, GfThrottledError
from flight_cli.domain import Cabin
from test_envelope import (
    _hermetic,  # noqa: F401 # pyright: ignore[reportUnusedImport] — Matrix in process
)
from test_gf_browser import (
    _recording_guard,
    keep_sigint,  # noqa: F401 # pyright: ignore[reportUnusedImport] — the fixture
)
from test_gf_chunked_search import (
    _EX6_FROM,
    _EX6_PAIRS,
    _EX6_TO,
    Page,
    _flat,
    _Google,
    _missing,
    _row,
)
from test_gf_full_board import _DEP, _LAX, _RET, _URL, _served
from test_open_jaw_search import _days, _first, _second

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_LINE = (
    "Google Flights rate-limited the request; opening Chrome (rung 2) for the rest of this search"
)
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_FAST_JSON = ["--backend", "gflight", "--fast", "--format", "json"]
_THREE = [("JFK", "LAX"), ("LGA", "LAX"), ("EWR", "LAX")]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """The ladder sleeps between its rungs; these tests count requests."""

    def _noop(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", _noop)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)


def _lax() -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli.app, [*_SEARCH, *args], env={"COLUMNS": "200"})


def _rungs(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    http: Callable[..., gfid.Board[gfid.GFlightWithId]],
    chrome: Callable[..., gfid.Board[gfid.GFlightWithId]],
) -> None:
    """Rung 1's GET and rung 2's navigation, each logged to `events` as it is asked."""

    def get(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        events.append("http")
        return http(filters, currency=currency, cheapest=cheapest)

    def navigate(
        filters: Any, *, headed: bool, currency: str = "USD", cheapest: bool = False
    ) -> Any:
        assert headed is False
        events.append("chrome")
        return chrome(filters, currency=currency, cheapest=cheapest)

    monkeypatch.setattr(gfid, "_one_call", get)
    monkeypatch.setattr(gfid, "_one_call_browser", navigate)


def _throttled_from(get: int, google: _Google) -> Callable[..., gfid.Board[gfid.GFlightWithId]]:
    """`google`, throttling its `get`-th GET and every one after it."""
    seen: list[bool] = []

    def http(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        seen.append(True)
        if len(seen) >= get:
            raise GfThrottledError("rate-limited")
        return google(filters, currency=currency, cheapest=cheapest)

    return http


def _no_http_after_chrome(events: list[str]) -> None:
    assert "chrome" in events, events
    assert "http" not in events[events.index("chrome") :], events


# ─────────────────────────────── (a) one-way ────────────────────────────────


def test_a_throttled_one_way_is_answered_through_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base: `auto` was http, so the throttle failed the search with
    no Chrome asked (exit 1). The board's ladder is the only rung-1 ask: the
    Cheapest tab after it goes straight to Chrome."""
    asked: list[tuple[str, bool]] = []

    def http(*_a: object, cheapest: bool = False, **_kw: object) -> Any:
        asked.append(("http", cheapest))
        raise GfThrottledError("rate-limited")

    def navigate(*_a: object, cheapest: bool = False, **_kw: object) -> Any:
        asked.append(("chrome", cheapest))
        return _lax()

    monkeypatch.setattr(gfid, "_one_call_with_retry", http)
    monkeypatch.setattr(gfid, "_one_call_browser", navigate)
    result = _run("JFK", "LAX", "--dep", _DEP.isoformat(), *_FAST_JSON, "--gf-transport", "auto")
    assert result.exit_code == 0, result.output
    assert asked == [("http", False), ("chrome", False), ("chrome", True)]
    assert json.loads(result.stdout)
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr


# ─────────────────────── (b) a round trip's second pin ───────────────────────


def test_a_throttle_on_a_pin_moves_the_later_pins_and_the_tab_to_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: the throttled pin stopped the pinning on http. The
    outbound and the first pin are rung 1's; the second pin meets the wall,
    spends its ladder, and it, the third pin and the Cheapest tab go to Chrome."""
    events: list[str] = []
    google = _Google(_THREE)
    _rungs(monkeypatch, events, http=_throttled_from(3, google), chrome=google)
    result = _run(
        "JFK,LGA,EWR",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        *_FAST_JSON,
        "--gf-transport",
        "auto",
    )
    assert result.exit_code == 0, result.output
    # The outbound and the first pin, then the second pin's ladder: 1 + 4 retries.
    assert events == ["http"] * 7 + ["chrome"] * 3, events
    _no_http_after_chrome(events)
    assert google.cheapest == [(("JFK", "LGA", "EWR"), ("LAX",))]
    assert len(json.loads(result.stdout)) == 3 * 2
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr
    assert "stopped pinning" not in result.stderr


# ─────────────────── (c) Example 6, four pages, throttled ───────────────────


def test_a_four_page_round_trip_throttled_on_page_one_is_answered_through_one_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: page 1's throttle stopped the search and named every
    other page. Here one ladder is spent, and every page, pin and tab after it
    is Chrome's."""
    events: list[str] = []
    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS))
    _rungs(monkeypatch, events, http=_throttled_from(1, google), chrome=google)
    buf = capture_err(monkeypatch)
    result = _run(
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        *_FAST_JSON,
        "--gf-transport",
        "auto",
    )
    assert result.exit_code == 0, result.output
    assert events[:5] == ["http"] * 5, events
    _no_http_after_chrome(events)
    assert len(google.pages()) == 4
    pins = [pin for _, pin in google.calls if pin is not None]
    assert len(pins) == gfid.pinned_fanout(10)
    assert len(google.cheapest) == 4
    assert len(events) == 5 + 4 + len(pins) + 4
    printed = _flat(buf.getvalue())
    assert "is missing" not in printed, printed
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr
    assert json.loads(result.stdout)


# ─────────────── (d) Chrome refuses once the search has escalated ───────────────


def test_chrome_rate_limited_after_the_escalation_gives_the_page_up_with_rung_twos_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: no Chrome was asked, and page 1 was given up with
    rung 1's words. Chrome's throttle on page 1's outbound ends the search:
    nothing else is asked, on either rung."""
    events: list[str] = []
    google = _Google(_EX6_PAIRS)

    def refused(*_a: object, **_kw: object) -> Any:
        raise GfThrottledError("rate-limited")

    _rungs(monkeypatch, events, http=_throttled_from(1, google), chrome=refused)
    buf = capture_err(monkeypatch)
    result = _run(
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        *_FAST_JSON,
        "--gf-transport",
        "auto",
    )
    assert result.exit_code == 1, result.output
    assert events == ["http"] * 5 + ["chrome"], events
    printed = _flat(buf.getvalue())
    assert _missing(1, "Google Flights rate-limited the browser rung") in printed, printed
    for n in (2, 3, 4):
        assert _missing(n, "not asked after page 1 stopped the search") in printed, printed
    assert "Google Flights rate-limited the browser rung" in printed, printed
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr


@pytest.mark.parametrize(
    ("failure", "why"),
    [
        (GfThrottledError("rate-limited"), "Google Flights rate-limited the browser rung"),
        (
            GfBrowserUnavailableError("Chrome died.", remedy="Retry."),
            "Google Flights' browser rung is unavailable — Chrome died. Retry",
        ),
    ],
    ids=["throttled", "unavailable"],
)
def test_chrome_refusing_a_pin_after_the_escalation_ends_the_search_and_keeps_what_was_served(
    monkeypatch: pytest.MonkeyPatch, failure: GfBackendError, why: str
) -> None:
    """Red at the base: no Chrome was asked. Page 1's first pin is served on
    rung 1, its second escalates and Chrome refuses it: the pin loop keeps the
    first pin's round trips, and no page after it is asked anything."""
    events: list[str] = []
    first: Page = (_EX6_FROM[:4], _EX6_TO[:7])
    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS))
    gets: list[Page] = []

    def http(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        out = filters.flight_segments[0]
        gets.append((tuple(a[0].name for a in out.departure_airport), ()))
        if (
            out.selected_flight is not None
            and len([p for p in google.calls if p[0] == first and p[1] is not None]) >= 1
        ):
            raise GfThrottledError("rate-limited")
        return google(filters, currency=currency, cheapest=cheapest)

    def refused(*_a: object, **_kw: object) -> Any:
        raise failure

    _rungs(monkeypatch, events, http=http, chrome=refused)
    buf = capture_err(monkeypatch)
    result = _run(
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        *_FAST_JSON,
        "--gf-transport",
        "auto",
    )
    assert result.exit_code == 0, result.output
    # Four outbounds and page 1's first pin, then the second pin's ladder.
    assert events == ["http"] * (4 + 1 + 5) + ["chrome"], events
    assert len(json.loads(result.stdout)) == 2
    printed = _flat(buf.getvalue())
    assert _missing(1, why).replace("is missing", "is short") in printed, printed
    for n in (2, 3, 4):
        returns = "its returns were not asked after page 1 stopped the search"
        assert _missing(n, returns) in printed, printed
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr


# ──────────────────────────── (e), (f) the guards ────────────────────────────


def test_an_explicit_http_search_keeps_the_throttle_and_never_asks_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base: `http` is rung 1 only."""
    asked: list[str] = []

    def http(*_a: object, **_kw: object) -> Any:
        asked.append("http")
        raise GfThrottledError("rate-limited")

    def navigate(*_a: object, **_kw: object) -> Any:
        raise AssertionError("an http search reached Chrome")

    monkeypatch.setattr(gfid, "_one_call_with_retry", http)
    monkeypatch.setattr(gfid, "_one_call_browser", navigate)
    result = _run("JFK", "LAX", "--dep", _DEP.isoformat(), *_FAST_JSON, "--gf-transport", "http")
    assert result.exit_code == 1, result.output
    assert asked == ["http"]
    assert result.stdout == ""
    assert _flat(result.stderr) == (
        "Google Flights rate-limited the request. Wait a moment and retry, use "
        "--gf-transport browser, or use --backend matrix."
    )


@pytest.mark.parametrize("output", [["--format", "json"], []], ids=["json", "table"])
def test_an_auto_search_with_no_throttle_is_an_http_search(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, output: list[str]
) -> None:
    """Green at the base: with nothing throttled, `auto` asks what `http` asks
    and prints what it prints."""

    def forbidden(*, headed: bool) -> object:
        raise AssertionError(f"an unthrottled search reached Chrome (headed={headed})")

    monkeypatch.setattr(gfb, "session", forbidden)
    runs: dict[str, tuple[int, list[str], str, str]] = {}
    for mode in ("http", "auto"):
        fake = gf_session(_served(_LAX), _served(_LAX))
        args = ["JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast"]
        result = _run(*args, *output, "--gf-transport", mode)
        runs[mode] = (result.exit_code, list(fake.gets), result.stdout, result.stderr)
    assert runs["auto"] == runs["http"]
    assert runs["http"][0] == 0


# ─────────────────────── (g) the default enriched path ───────────────────────


@pytest.mark.usefixtures("keep_sigint")
def test_an_escalation_on_the_enriched_path_opens_and_closes_chrome_on_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: the weave left the guard unarmed under `auto`, and no
    Chrome was asked. The default search runs Google on a worker; the escalated
    Chrome must open and close there, the guard must be armed, and the
    `--split` worker after it asks nothing over http."""
    events: list[str] = []
    google = _Google(_THREE)
    opened: list[int] = []
    closed: list[int] = []

    class _Chrome:
        finished = False

        def __init__(self, *, headed: bool) -> None:
            assert headed is False
            opened.append(threading.get_ident())

        def close(self) -> None:
            closed.append(threading.get_ident())

    def navigate(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        # What the real rung does first: this thread's session, opened on first ask.
        gfb.session(headed=False)
        return google(filters, currency=currency, cheapest=cheapest)

    monkeypatch.setattr(gfb, "GfBrowserSession", _Chrome)
    _rungs(monkeypatch, events, http=_throttled_from(3, google), chrome=navigate)
    seen: list[object] = []
    monkeypatch.setattr(gfb, "interrupt_guard", _recording_guard(seen))
    before = signal.getsignal(signal.SIGINT)
    result = _run(
        "JFK,LGA,EWR",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        "--split",
        "--gf-transport",
        "auto",
    )
    assert result.exit_code == 0, result.output
    assert seen, "the weave never entered the guard"
    assert seen[0] is not before and callable(seen[0])
    _no_http_after_chrome(events)
    # The round trip's two pins and tab, then the split's two one-ways.
    assert events.count("chrome") == 3 + 2, events
    main = threading.main_thread().ident
    assert opened, events
    assert main not in opened
    assert sorted(closed) == sorted(opened)
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr


# ─────────────────────── (h) no patchright, no Chrome ───────────────────────


def _no_patchright(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> object:
        raise GfBrowserUnavailableError(
            "Google Flights' browser rung needs patchright, which isn't installed.",
            remedy=gfb._INSTALL_HINT,
        )

    def http(*_a: object, **_kw: object) -> Any:
        raise GfThrottledError("rate-limited")

    monkeypatch.setattr(gfb, "_playwright_factory", missing)
    monkeypatch.setattr(gfid, "_one_call_with_retry", http)


def test_an_escalation_with_no_browser_exits_with_the_browsers_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: no Chrome was asked, so the throttle was the reason."""
    _no_patchright(monkeypatch)
    result = _run("JFK", "LAX", "--dep", _DEP.isoformat(), *_FAST_JSON, "--gf-transport", "auto")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    said = _flat(result.stderr)
    assert said.count(_LINE) == 1, said
    assert said.index(_LINE) < said.index("needs patchright, which isn't installed")


def test_an_escalation_with_no_browser_hands_the_search_to_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: no Chrome was asked, so the throttle was the reason."""
    _no_patchright(monkeypatch)
    result = _run(
        "JFK", "LAX", "--dep", _DEP.isoformat(), "--format", "json", "--gf-transport", "auto"
    )
    assert result.exit_code == 0, result.output
    said = _flat(result.stderr)
    assert said.count(_LINE) == 1, said
    assert "Using Matrix: Google Flights' browser rung is unavailable" in said, said
    assert json.loads(result.stdout)


# ─────────── (i) a sibling escalates while a thread waits on its ladder ───────────


def test_a_thread_waiting_on_its_ladder_follows_a_siblings_escalation_to_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread A is in its ladder's backoff when thread B's ladder runs out and
    moves the search to Chrome. A's next attempt goes to Chrome, not to rung 1,
    and the move is said once. Both share one search as a fan-out's workers do,
    each running in a copy of the context that opened it."""
    parked = threading.Event()
    moved = threading.Event()
    calls: list[tuple[str, str]] = []

    def fetch(*_a: object, **_kw: object) -> Any:
        calls.append((threading.current_thread().name, "http"))
        raise GfThrottledError("rate-limited")

    def backoff(_s: float) -> None:
        if threading.current_thread().name == "A":
            parked.set()
            moved.wait(5)

    def navigate(*_a: object, **_kw: object) -> Any:
        calls.append((threading.current_thread().name, "chrome"))
        return _lax()

    def line() -> None:
        calls.append((threading.current_thread().name, "line"))

    monkeypatch.setattr(gfid, "_fetch_page", fetch)
    monkeypatch.setattr(gfid.time, "sleep", backoff)
    monkeypatch.setattr(gfid, "_one_call_browser", navigate)
    monkeypatch.setattr(gfb, "announce_escalation", line)
    filters: Any = object()
    answers: dict[str, object] = {}

    def ask(name: str, context: contextvars.Context) -> None:
        if name == "B":
            parked.wait(5)
        try:
            answers[name] = context.run(gfid._one_call_auto, filters, headed=False)
        except Exception as e:
            answers[name] = e
        finally:
            if name == "B":
                moved.set()

    with gfid.search_escalation():
        threads = [
            threading.Thread(target=ask, args=(name, contextvars.copy_context()), name=name)
            for name in ("A", "B")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert all(isinstance(a, gfid.Board) for a in answers.values()), answers
    b_ladder = [("B", "http")] * (gfid._THROTTLE_RETRY_ATTEMPTS + 1)
    assert calls == [("A", "http"), *b_ladder, ("B", "line"), ("B", "chrome"), ("A", "chrome")]


# ─────────── (j) the one-way tickets an open jaw and `--split` ask ───────────


def _throttled(*_a: object, **_kw: object) -> Any:
    raise GfThrottledError("rate-limited")


def _open_jaw_arm() -> tuple[list[str], Callable[..., gfid.Board[gfid.GFlightWithId]], int]:
    """An open jaw's two slices, Chrome answering each one-way by its origin,
    and the total of the cheapest combination."""
    out, back = _days()
    boards = {"JFK": _first(), "CDG": _second()}

    def chrome(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
        assert not cheapest, "a one-way ticket reads no Cheapest tab"
        return gfid.Board(boards[filters.flight_segments[0].departure_airport[0][0].name])

    args = ["--slice", f"JFK-LHR:{out}", "--slice", f"CDG-JFK:{back}", "--format", "json"]
    return args, chrome, 861


def _split_arm() -> tuple[list[str], Callable[..., gfid.Board[gfid.GFlightWithId]], int]:
    """A Google round trip under `--split`, and the total of its pair: the
    outbound one-way at 100 and a return one-way on the return day at 150."""
    google = _Google(
        [("JFK", "LAX")],
        added=lambda page: (
            [_row(7, _RET, "LAX", "JFK", 150.0)] if page == (("LAX",), ("JFK",)) else []
        ),
    )
    args = ["JFK", "LAX", "--dep", _DEP.isoformat(), "--return", _RET.isoformat(), *_FAST_JSON]
    return args, google, 250


@pytest.mark.usefixtures("keep_sigint")
@pytest.mark.parametrize("arm", [_open_jaw_arm, _split_arm], ids=["open-jaw", "split"])
def test_the_one_way_tickets_are_asked_inside_the_searchs_one_escalation(
    monkeypatch: pytest.MonkeyPatch,
    arm: Callable[[], tuple[list[str], Callable[..., gfid.Board[gfid.GFlightWithId]], int]],
) -> None:
    """Every request after the first throttle is Chrome's, and the move is said
    once, beside whichever answer the one-way tickets are asked for. The open
    jaw is red at the merge: its one-ways were asked outside any search, so
    each spent its own ladder and said the move again."""
    events: list[str] = []
    args, chrome, total = arm()
    _rungs(monkeypatch, events, http=_throttled, chrome=chrome)
    result = _run(*args, "--split", "--gf-transport", "auto")
    assert result.exit_code == 0, result.output
    assert events[:5] == ["http"] * 5, events
    _no_http_after_chrome(events)
    assert _flat(result.stderr).count(_LINE) == 1, result.stderr
    ticket = json.loads(result.stdout)["split_ticket"]
    # An open jaw's object lists its combinations; a round trip's is its pair.
    cheapest = ticket["combinations"][0] if "combinations" in ticket else ticket
    assert cheapest["total"] == total, ticket


# ─────────── (k) a cabin's Cheapest-tab note after the escalation ───────────


def test_a_cabins_unread_cheapest_tab_keeps_its_return_check_cabin_and_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note keeps both of its callers' words: the return check the tab was
    left unread for, which a one-cabin search passes, and the cabin whose tab
    it was, which a multi-cabin search passes, the refusal worded as the rung
    the search reached."""
    buf = capture_err(monkeypatch)
    board = gfid.Board[Any](separate_failed=GfThrottledError("rate-limited"))
    with gfid.search_escalation():
        latch = gfid._search_escalation.get()
        assert latch is not None and latch.take()
        cli._note_separate_tickets(
            board, gf_mode="auto", bags=False, unchecked="-AIRLINES AA", cabin=Cabin.BUSINESS
        )
    said = _flat(buf.getvalue())
    unchecked = (
        "Itineraries on separate tickets not read: Google lists no return for them "
        "to check against -AIRLINES AA."
    )
    tab = (
        "Google Flights BUSINESS: itineraries on separate tickets not read: "
        "Google Flights rate-limited the browser rung."
    )
    assert said == f"{unchecked} {tab}", said
