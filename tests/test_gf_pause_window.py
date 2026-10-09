# pyright: reportPrivateUsage=false
"""Pages of one search read side by side each get Google's server-error schedule.

A search pauses for server errors inside one 8 s window, opened by its first
pause. Pages read side by side (the cabins of a multi-cabin search) each get the
2 s and the 6 s inside that one window; a page read after it closes is read
again at once.

Each thread has its own virtual clock: a sleep advances only the sleeping
thread's, so pages that start together run in lockstep with no real sleep.
Google's error lasts `_CLEAR` virtual seconds from each page's first read.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import _FakeRateLimiter, _FakeResponse, _NullCookies
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from test_gf_full_board import _no_matrix
from test_gf_rung_parity import _answered, _html, _search
from test_gf_server_error import _error_page, _http

_CLEAR = 7.0
_URL = "https://www.google.com/travel/flights"
_SCHEDULE = list(gfid._SERVER_ERROR_PAUSES_S)


class _ThreadTime:
    """`time` with one virtual clock per thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now: dict[int, float] = {}
        self.sleeps: dict[int, list[float]] = {}

    def monotonic(self) -> float:
        with self.lock:
            return self.now.get(threading.get_ident(), 0.0)

    def sleep(self, seconds: float) -> None:
        me = threading.get_ident()
        with self.lock:
            self.sleeps.setdefault(me, []).append(seconds)
            self.now[me] = self.now.get(me, 0.0) + seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.mark.parametrize("pages", [2, 4])
def test_pages_read_side_by_side_each_get_the_schedule(
    pages: int, gf_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pages` search pages of one search read on their own threads under the
    fan-out's ladder and the search's scope, as the cabins of a multi-cabin
    search are. Each errs until `_CLEAR`, then carries the board. Red at the
    base, whose 8 s sum went to the first page to pause: the rest were refused
    after pauses of `[0.0, 0.0]`."""
    gf_session(_error_page())
    clock = _ThreadTime()
    monkeypatch.setattr(gfid, "time", clock)
    error, board = _error_page(), _html("ds1_nyc_lon_token")

    def fetch(*_a: object, **_k: object) -> PageFetch:
        return PageFetch(board if clock.monotonic() >= _CLEAR else error, _URL, 200)

    monkeypatch.setattr(gfid, "_fetch_page", fetch)
    outcomes: dict[str, object] = {}
    sleeps: dict[str, list[float]] = {}

    def run() -> None:
        name = threading.current_thread().name
        try:
            outcomes[name] = len(_http())
        except gfid.GfBackendError as e:
            outcomes[name] = type(e).__name__
        sleeps[name] = list(clock.sleeps.get(threading.get_ident(), []))

    with gfid.shared_throttle_ladder(), gfid.search_escalation():
        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(run,), name=f"p{i}")
            for i in range(pages)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert outcomes == {f"p{i}": 300 for i in range(pages)}, (outcomes, sleeps)
    assert sleeps == {f"p{i}": _SCHEDULE for i in range(pages)}, sleeps


def test_pages_read_in_turn_pause_at_most_eight_seconds(
    gf_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard, green at the base: three pages read one after another on one
    thread, each erring throughout, pause 8 s in all. The first has the whole
    schedule; the window has closed for the others, which are read again at
    once, three reads each, then refused."""
    gf_session(_error_page())
    clock = _ThreadTime()
    monkeypatch.setattr(gfid, "time", clock)
    reads = [0]

    def fetch(*_a: object, **_k: object) -> PageFetch:
        reads[0] += 1
        return PageFetch(_error_page(), _URL, 200)

    monkeypatch.setattr(gfid, "_fetch_page", fetch)
    with gfid.search_escalation():
        for _ in range(3):
            with pytest.raises(gfid.GfSearchServerError):
                _http()
    (slept,) = clock.sleeps.values()
    assert (slept, reads[0]) == ([*_SCHEDULE, 0.0, 0.0, 0.0, 0.0], 9), slept


def test_the_window_opens_at_the_first_server_error_however_late(
    gf_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: a page that errs 100 s into a search still has the whole
    schedule, because the window opens at the search's first pause and not when
    the search began."""
    gf_session(_error_page())
    clock = _ThreadTime()
    monkeypatch.setattr(gfid, "time", clock)
    board = _html("ds1_nyc_lon_token")

    def fetch(*_a: object, **_k: object) -> PageFetch:
        return PageFetch(board if clock.monotonic() >= 100.0 + _CLEAR else _error_page(), _URL, 200)

    monkeypatch.setattr(gfid, "_fetch_page", fetch)
    with gfid.search_escalation():
        clock.sleep(100.0)
        assert len(_http()) == 300
    assert list(clock.sleeps.values()) == [[100.0, *_SCHEDULE]], clock.sleeps


class _LockstepSession:
    """Each worker thread's first GET waits for the other cabins' first GET, so
    every cabin reads on its own thread from virtual second 0. A GET answers
    Google's server error until the reading thread's clock reaches `_CLEAR`."""

    def __init__(self, clock: _ThreadTime, cabins: int, board: str) -> None:
        self.cookies = _NullCookies()
        self.clock = clock
        self.start = threading.Barrier(cabins)
        self.lock = threading.Lock()
        self.seen: set[int] = set()
        self.board = board
        self.error = _error_page()

    def get(self, _url: str, **_kw: object) -> _FakeResponse:
        me = threading.get_ident()
        with self.lock:
            first = me not in self.seen
            self.seen.add(me)
        if first:
            self.start.wait(5)
        ok = self.clock.monotonic() >= _CLEAR
        return _FakeResponse(text=self.board if ok else self.error)


class _LockstepClient:
    def __init__(self, session: _LockstepSession) -> None:
        self._rate_limiter = _FakeRateLimiter()
        self._session_obj = session

    def _session(self) -> _LockstepSession:
        return self._session_obj


@pytest.mark.parametrize(
    "cabins", [(), ("--cabin", "economy,business")], ids=["one-cabin", "two-cabins"]
)
def test_each_cabin_reads_its_board_through_an_error_its_pauses_outlast(
    cabins: tuple[str, ...], gf_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`search NYC LON --dep D --backend gflight --fast [--cabin
    economy,business]`, every cabin's page erring for `_CLEAR` virtual seconds.
    Green at the base for one cabin; red there for two, whose second cabin was
    refused with a server error after pauses of `[0.0, 0.0]`."""
    gf_session(_error_page())
    clock = _ThreadTime()
    session = _LockstepSession(clock, 2 if cabins else 1, _answered("ds1_nyc_lon_token"))
    monkeypatch.setattr(gfid, "get_client", lambda: _LockstepClient(session))
    monkeypatch.setattr(gfid, "time", clock)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search("--fast", *cabins), env={"COLUMNS": "250"})
    stderr = " ".join(result.stderr.split())
    assert result.exit_code == 0, stderr
    assert "server error" not in stderr, stderr
    assert sorted(clock.sleeps.values()) == [_SCHEDULE] * (2 if cabins else 1), clock.sleeps
