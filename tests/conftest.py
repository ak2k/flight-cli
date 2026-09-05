# pyright: reportPrivateUsage=false
"""Suite-wide guards, and fixtures used by more than one test module.

The load-bearing guard: **no test launches a browser.** Rung 2 of the Google
Flights transport opens a real Chrome — headless, and under 3 s, so the cost is
not what makes this matter. A test that reached it would hit the live network,
and would quietly disprove the property the CLI advertises: that
`--gf-transport http` never consults a browser. So the launcher seam is
replaced, for every test, by a callable that fails whichever test touched it.

`pytest.fail` raises a `BaseException`, deliberately: production code wraps
launch failures in `except Exception`, and a guard the code under test could
swallow would be no guard at all.

Tests that legitimately drive the seam — with a fake playwright, never a real
one — opt out with `@pytest.mark.gf_browser`.

The Google Flights fixtures drive the page transport through a fake curl_cffi
session rather than a stubbed `search_with_ids`. Everything worth pinning about
this backend — the pinning recursion, the row parse, the Tier-2 post-filter and
the trim to what the user asked for — sits between the GET and the caller, and a
stub above all of it would pin none of it.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from flight_cli import _gf_browser

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _no_browser_launch(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail any unmarked test that reaches the real patchright launcher, and
    give every test a clean set of the module's once-per-process latches.

    The resets are unconditional: the launch notice and the thread-local
    session are process-wide state, so without them the first browser test to
    run would decide what every later one sees."""
    monkeypatch.setattr(_gf_browser, "_notice_state", {"printed": False})
    monkeypatch.setattr(_gf_browser, "_sessions", threading.local())
    # The MARKER, not `request.keywords`. `keywords` also carries the node's
    # name, its parametrize ids and its containing directory — so a test
    # parametrized with the string "gf_browser", or any test under a directory
    # of that name, silently opted itself out of the guard and could reach the
    # real launcher.
    # pyright: ignore comments — `request.node` is `Any` in pytest's stubs.
    marker = cast(  # pyright: ignore[reportUnknownArgumentType]
        "object | None",
        request.node.get_closest_marker("gf_browser"),  # pyright: ignore[reportUnknownMemberType]
    )
    if marker is not None:
        return

    def _forbidden() -> object:
        pytest.fail("this test reached rung 2's real browser launcher")

    monkeypatch.setattr(_gf_browser, "_playwright_factory", _forbidden)


FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
GFLIGHT_PAGE_DIR = FIXTURE_DIR / "gflight_page"
_ENVELOPE = FIXTURE_DIR / "gf_page_envelope.json"


def _ds1(name: str) -> str:
    """One captured `ds:1` payload, as the text a page carries it as."""
    return (GFLIGHT_PAGE_DIR / name).read_text()


def _page(ds1_json: str) -> str:
    """A page carrying `ds1_json` inside the VERBATIM callback envelope.

    The envelope is a live capture rather than a rewrite of it, because the
    extraction is a regex over JavaScript: the quoting of `key`, the order of
    the properties and the `sideChannel` terminator are all load-bearing, and a
    synthesised wrapper written to suit the regex agrees with it by
    construction. `tests/test_gflight_page.py` is where the envelope itself is
    under test; here it is the carrier."""
    envelope: dict[str, str] = json.loads(_ENVELOPE.read_text())
    return (
        "<!doctype html><html><body><script>"
        f"{envelope['prefix']}{ds1_json}{envelope['suffix']}"
        "</script></body></html>"
    )


class _NullCookies:
    """Enough of curl_cffi's cookie API for the seed/persist helpers."""

    def __init__(self) -> None:
        # Per instance. A list on the class is one list for every instance in
        # the session, so the day a persist path appends to it, cookies cross
        # from one test into the next with nothing on either to say so.
        self.jar: list[Any] = []

    def set(self, *_a: object, **_kw: object) -> None:
        return None


class _NoSleepTime:
    """`time`, with `sleep` costing nothing.

    Installed over the module's OWN binding rather than over `time.sleep`.
    `_gflight_ids` does a plain `import time`, so its `time` IS the stdlib
    module object: patching `sleep` there makes it a no-op for every module and
    every thread in the process for as long as the test runs, and any code
    sleeping to order itself spins instead."""

    def sleep(self, _seconds: float) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _FakeResponse:
    def __init__(self, *, text: str, status_code: int = 200) -> None:
        self.text = text
        self.url = "https://www.google.com/travel/flights?tfs=abc"
        self.status_code = status_code


class _FakeSession:
    def __init__(self, bodies: list[str], gets: list[str]) -> None:
        self._bodies = bodies
        self.gets = gets
        self.cookies = _NullCookies()

    def get(self, url: str, **_kw: object) -> _FakeResponse:
        self.gets.append(url)
        # The last body repeats, so "every pinned leg answers this" is one
        # argument rather than one per pin.
        return _FakeResponse(text=self._bodies[min(len(self.gets) - 1, len(self._bodies) - 1)])


class _FakeRateLimiter:
    def acquire(self, *_a: object, **_kw: object) -> bool:
        return True


class _FakeClient:
    """fli's `Client` at the surface `_one_call` touches: the shared rate
    limiter and the per-thread session. Counts GETs so a test can assert the
    request budget as well as the value."""

    def __init__(self, bodies: list[str]) -> None:
        self.gets: list[str] = []
        self._rate_limiter = _FakeRateLimiter()
        self._session_obj = _FakeSession(bodies, self.gets)

    def _session(self) -> _FakeSession:
        return self._session_obj


@pytest.fixture
def gf_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> Callable[..., _FakeClient]:
    """Install a fake Google Flights session answering with the given pages.

    Cookie seeding is pointed at a temp dir and both latches are re-armed: they
    are per-process and per-thread, so a test that left one set would silently
    disable seeding for every later test in the session."""
    from flight_cli import _gflight_ids as gfid

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"persisted": False})
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())

    monkeypatch.setattr(gfid, "time", _NoSleepTime())

    def install(*bodies: str) -> _FakeClient:
        fake = _FakeClient(list(bodies))
        monkeypatch.setattr(gfid, "get_client", lambda: fake)
        return fake

    return install


def _board(rows: int, *, distinct_at: int | None = None) -> str:
    """A page carrying `rows` parseable rows, cloned from a live capture.

    `distinct_at` swaps in a row carrying different flight numbers at that
    index, which is how a test says "the row this routing constraint keeps is
    one the table never showed"."""
    payload: list[Any] = json.loads(_ds1("ds1_metadata_blocks_kept.json"))
    board: list[Any] = cast("list[Any]", payload[2][0]) + cast("list[Any]", payload[3][0])
    filler, odd_one = board[0], board[-1]
    cloned = [json.loads(json.dumps(filler)) for _ in range(rows)]
    if distinct_at is not None:
        cloned[distinct_at] = json.loads(json.dumps(odd_one))
    payload[2] = [cloned]
    payload[3] = None
    return _page(json.dumps(payload))


def _answering(
    ds1_json: str,
    *,
    origin: str | None,
    destination: str | None,
    date: str,
    name: str = "ds:1 payload",
) -> str:
    """One captured `ds:1` payload, re-pointed at the leg a query asked for.

    A capture is a real page for a real query, so its rows carry the route and
    the day that query named. Replayed against a search built from `today` they
    answer a different leg, and the pinning recursion refuses a return board
    that does not correspond to the segment it asked to fill — correctly, since
    that is what a page which dropped the pin looks like. A test that wants a
    board SERVED rather than refused therefore has to answer the question it
    asked, and this is how it says so without pinning a literal date that rots.

    The rows keep their prices, ids, carriers and connections; only the endpoint
    codes of the outer legs and every leg's calendar day move.

    The rows are located by the transport's OWN scan rather than by indices
    written out here. Which payload indices hold a board is a fact with one
    home, and a second copy of it re-points nothing on a capture whose blocks
    sit elsewhere — silently, since a helper that rewrites no row still returns
    a page. Re-pointing nothing is therefore an error and not a quiet pass: the
    caller asked for a board answering a leg, and it got the capture back."""
    from flight_cli import _gflight_ids as gfid

    payload: list[Any] = json.loads(ds1_json)
    # The transport's own scan, not a second copy of the indices it reads.
    board = gfid._rows_from_ds1(payload)
    assert board.rows, f"{name}: no rows to re-point at {origin}->{destination} on {date}" + (
        f"; rows are at payload{list(board.misplaced)}" if board.misplaced else ""
    )
    for i, row in enumerate(board.rows):
        legs = cast("list[list[Any]]", row[0][2])
        if origin is not None:
            legs[0][3] = origin
        if destination is not None:
            legs[-1][6] = destination
        for j, leg in enumerate(legs):
            for idx in (20, 21):
                # `datetime.date(*v)` on anything else raises a bare stdlib
                # TypeError naming no capture, row or leg — and this helper is
                # the one place that knows all three.
                ymd: list[Any] | None = leg[idx] if isinstance(leg[idx], list) else None
                assert ymd is not None and len(ymd) == 3, (
                    f"{name} row {i} leg {j} field {idx} is not [y, m, d]: {leg[idx]!r}"
                )
        # ONE delta for the whole row, applied to both `[y, m, d]` ends of
        # every leg. The clock times are what make the delta necessary: a
        # row's dates are not interchangeable, so writing the asked-for day
        # into both ends of a leg that lands after midnight makes it arrive
        # before it departed, and pulls the next leg back in front of the
        # flight feeding it. Sliding the whole row keeps every elapsed time,
        # every overnight and the connection order, and still lands the
        # first departure on the day the guard reads.
        shift = datetime.date.fromisoformat(date) - datetime.date(*legs[0][20])
        for leg in legs:
            for idx in (20, 21):
                moved = datetime.date(*leg[idx]) + shift
                leg[idx] = [moved.year, moved.month, moved.day]
    return json.dumps(payload)


@pytest.fixture
def gf_answering() -> Callable[..., str]:
    """A page carrying a committed capture re-pointed at one leg; see
    `_answering`."""

    def build(
        name: str, *, date: str, origin: str | None = None, destination: str | None = None
    ) -> str:
        return _page(
            _answering(_ds1(name), origin=origin, destination=destination, date=date, name=name)
        )

    return build


def _unpriced(ds1_json: str, *, index: int, name: str = "ds:1 payload") -> str:
    """The same payload with the row at `index` carrying Google's "no
    shopping-list price" marker.

    An empty price head is what a served board holds for a row Google did not
    price — premium-cabin round trips with several passengers are the routine
    case — and fli reads it as `price=None` rather than as a malformed row, so
    the row is parsed and served like any other.

    Built from a committed capture rather than committed as a ninth one: the
    marker is a two-character edit to a real board, and a capture whose only
    difference from `ds1_metadata_blocks_kept` is that edit would have to be
    kept in step with it by hand for as long as both exist."""
    from flight_cli import _gflight_ids as gfid

    payload: list[Any] = json.loads(ds1_json)
    rows = gfid._rows_from_ds1(payload).rows
    assert index < len(rows), f"{name} holds {len(rows)} rows; asked for {index}"
    # `row[1]` is the price block and `row[1][0]` its head, the two indices
    # fli's own decoder reads. Emptying the head is the marker; clearing the
    # block would be a malformed row, which is skipped rather than served.
    rows[index][1][0] = []
    return json.dumps(payload)


@pytest.fixture
def gf_unpriced() -> Callable[..., str]:
    """A page whose row `index` carries no price, optionally re-pointed at a
    leg first; see `_unpriced` and `_answering`."""

    def build(
        capture: str,
        *,
        index: int,
        date: str | None = None,
        origin: str | None = None,
        destination: str | None = None,
    ) -> str:
        ds1 = _ds1(capture)
        if date is not None:
            ds1 = _answering(ds1, origin=origin, destination=destination, date=date, name=capture)
        return _page(_unpriced(ds1, index=index, name=capture))

    return build


@pytest.fixture
def gf_capture() -> Callable[[str], str]:
    """A page carrying one committed `ds:1` capture, by file name."""

    def build(name: str) -> str:
        return _page(_ds1(name))

    return build


@pytest.fixture
def gf_board() -> Callable[..., str]:
    """A page carrying `rows` cloned rows; see `_board`."""
    return _board


@pytest.fixture
def gf_rows() -> Callable[..., list[Any]]:
    """The parsed flights of one committed capture, as `search_with_ids` would
    have returned them — for the paths a test reaches below the transport.

    `unpriced=i` empties row `i`'s price head first, so the row parses to
    `price=None`; see `_unpriced`."""

    def build(name: str, *, unpriced: int | None = None) -> list[Any]:
        from flight_cli import _gflight_ids as gfid

        ds1 = _ds1(name)
        if unpriced is not None:
            ds1 = _unpriced(ds1, index=unpriced, name=name)
        board = gfid._rows_from_ds1(json.loads(ds1))
        return [gfid._parse_flight_with_id(r) for r in board.rows]

    return build
