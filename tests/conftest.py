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

The same holds one level down: **no test resolves a name or connects a socket**
to anything but this machine (`_no_network`). A test that needs the real thing
opts out with `@pytest.mark.network`.

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
import functools
import io
import ipaddress
import json
import os
import pathlib
import socket
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog
from typer import rich_utils

# Rich settles whether a console styles its output when the console is built, and
# `flight_cli` builds its consoles at import, so this precedes that import.
# `TTY_COMPATIBLE=0` outranks `FORCE_COLOR`, which would otherwise style the output
# and break the suite's text assertions.
os.environ["TTY_COMPATIBLE"] = "0"

from flight_cli import _config, _gf_browser
from flight_cli.pp import auth as pp_auth
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

# Today, for the modules whose searches carry literal travel dates. fli refuses a
# travel date before today, so those modules pin the clock before every date they
# search on, the earliest being 2026-10-14. time-machine reads a naive datetime as
# UTC, and noon UTC keeps the local date before that in any timezone. Naive,
# because an aware one makes time-machine set TZ=UTC for the test.
LITERAL_DATES_NOW = datetime.datetime(2026, 9, 30, 12)


@pytest.fixture(autouse=True)
def _no_forced_terminal(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """CI sets `GITHUB_ACTIONS`, which makes Typer style the help it renders and
    breaks assertions on its text. Typer passes `force_terminal` explicitly, which
    outranks `TTY_COMPATIBLE`, so the setting above does not reach it."""
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False)


@pytest.fixture(autouse=True)
def _no_browser_launch(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail any unmarked test that reaches the real patchright launcher, and
    give every test a clean set of the module's once-per-process latches.

    The resets are unconditional: the launch notice, the thread-local session,
    the registry of sessions holding a driver and the interrupt latch a launch
    reads before it starts one are process-wide state, so without them the first
    browser test to run would decide what every later one sees — and a leaked
    registry entry would let one test's session be stopped by another's
    interrupt. A leaked scope depth is the same hazard one level up: the close
    at the end of a scope is skipped while the depth reads non-zero, so the
    next test on that thread inherits a Chrome holding the profile."""
    monkeypatch.setattr(_gf_browser, "_notice_state", {"printed": False})
    monkeypatch.setattr(_gf_browser, "_sessions", threading.local())
    # Parameterised so the empty set is not partially unknown to the checker.
    monkeypatch.setattr(_gf_browser, "_live", set[_gf_browser.GfBrowserSession]())
    monkeypatch.setattr(_gf_browser, "_interrupt_state", {"seen": False})
    monkeypatch.setattr(_gf_browser, "_scope_depth", threading.local())
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


def pytest_configure(config: pytest.Config) -> None:
    """Declare the marker here: `--strict-markers` rejects an undeclared one."""
    config.addinivalue_line(
        "markers",
        "network: may resolve names and connect sockets; opts out of the conftest network guard",
    )


def _is_local(host: object) -> bool:
    """Whether `host` names this machine without a lookup."""
    # The resolver and `connect` take an encoded host too, and anyio always sends one.
    if isinstance(host, bytes | bytearray):
        host = host.decode(errors="replace")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail any unmarked test that resolves a name or connects a socket to an
    address other than loopback or a Unix socket.

    Socket creation stays open: asyncio's event loop builds a `socketpair`, and
    a socket that never connects reaches nothing. `pytest.fail` raises a
    `BaseException` for the reason `_no_browser_launch` gives."""
    if request.node.get_closest_marker("network") is not None:  # pyright: ignore[reportUnknownMemberType]
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _refuse(what: str, host: object) -> None:
        pytest.fail(f"this test reached the network: {what} {host!r}")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_local(host):
            _refuse("resolved", host)
        return real_getaddrinfo(host, *args, **kwargs)

    def _checked(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address[0]):
            _refuse("connected to", address[0])

    def connect(self: socket.socket, address: Any) -> None:
        _checked(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _checked(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _no_local_provider_credentials(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test reads or writes this machine's award-provider credentials or
    config; a test that needs one sets it itself."""
    names = ("PP_ACCESS_TOKEN", "PP_REFRESH_TOKEN", "SEATS_AERO_API_KEY", "FLIGHT_CLI_CONFIG_DIR")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    # Not the test's `tmp_path`, so a test's own files never meet these stores.
    home = tmp_path_factory.mktemp("no_credentials")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    # The stores are fixed when their modules are imported, so HOME does not move them.
    config = home / ".config" / "flight-cli"
    monkeypatch.setattr(pp_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(pp_auth, "TOKENS_PATH", config / "pp.json")
    profile = home / ".cache" / "flight-cli" / "browser-profile"
    monkeypatch.setattr(pp_auth, "BROWSER_PROFILE_DIR", profile)
    monkeypatch.setattr(seats_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(seats_auth, "KEY_PATH", config / "seats.json")
    monkeypatch.setattr(_config, "DEFAULT_CONFIG_PATH", config / "config.toml")


@pytest.fixture(autouse=True)
def _structlog_defaults() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """Every test starts on structlog's defaults.

    The CLI's callback configures structlog for the whole process, at the
    warning level and with each logger cached on first use. Left in place, one
    CLI test decides the level every later test's module loggers emit at, and
    `structlog.testing.capture_logs` swaps the processors, never that level: a
    debug event it is waiting for is dropped before it arrives. Resetting the
    configuration is half of it; a module logger first used under it keeps the
    logger it cached, as an instance attribute over its class's `bind`."""
    yield
    if not structlog.is_configured():
        return
    structlog.reset_defaults()
    proxy: type[object] = type(cast("object", structlog.get_logger()))
    for name, module in list(sys.modules.items()):
        if name == "flight_cli" or name.startswith("flight_cli."):
            for value in list(vars(module).values()):
                if isinstance(value, proxy):
                    vars(value).pop("bind", None)


def capture_err(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Replace `cli.err` with a wide, colourless console over a buffer.

    Public, unlike the fixture helpers around it: every caller is another test
    module, and a leading underscore would say the opposite.

    Wide on purpose: an assertion on a substring that rich wrapped mid-token
    fails for a reason that has nothing to do with what is under test."""
    from rich.console import Console

    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "err", Console(file=buf, width=1000, force_terminal=False, no_color=True)
    )
    return buf


def hand_out_providers(monkeypatch: pytest.MonkeyPatch, *providers: object) -> None:
    """Every award search the registry runs is handed `providers`, already
    built, in this order, in place of the real ones."""
    from flight_cli.providers import registry

    async def handed(provider: object) -> object:
        return provider

    def builders(**_kw: object) -> list[Callable[[], Awaitable[object]]]:
        return [functools.partial(handed, p) for p in providers]

    monkeypatch.setattr(registry, "_enabled_builders", builders)


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
    """fli's `Client` at the surface `_get_search_page` touches: the shared rate
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


def distinct_clones(row: list[Any], n: int) -> list[Any]:
    """`n` copies of one captured row, each departing a minute apart.

    A board of identical copies is one itinerary listed `n` times, which the
    parser collapses to one row. Moving the first departure's minute makes
    each copy its own itinerary and leaves every other field as captured."""
    clones: list[Any] = [json.loads(json.dumps(row)) for _ in range(n)]
    for i, clone in enumerate(clones):
        first_leg = cast("list[Any]", clone[0][2][0])
        first_leg[8] = [cast("list[Any]", first_leg[8])[0], i % 60]
    return clones


def _board(rows: int, *, distinct_at: int | None = None, one_flight: bool = False) -> str:
    """A page carrying `rows` parseable rows, cloned from a live capture.

    `distinct_at` swaps in a row carrying different flight numbers at that
    index, which is how a test says "the row this routing constraint keeps is
    one the table never showed". `one_flight` flies every leg of that row under
    its first flight number (AS627+AS305 becomes AS627 twice), the one flight a
    bare flight-number routing keeps."""
    payload: list[Any] = json.loads(_ds1("ds1_metadata_blocks_kept.json"))
    board: list[Any] = cast("list[Any]", payload[2][0]) + cast("list[Any]", payload[3][0])
    filler, odd_one = board[0], board[-1]
    cloned = distinct_clones(filler, rows)
    if distinct_at is not None:
        odd: Any = json.loads(json.dumps(odd_one))
        if one_flight:
            legs = cast("list[Any]", odd[0][2])
            for leg in legs:
                leg[22][1] = legs[0][22][1]
        cloned[distinct_at] = odd
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


def _unreadable(ds1_json: str, *, index: int) -> str:
    """The same payload with the row at `index` read and not parsed: its first
    leg states no departure day, which every served row carries."""
    from flight_cli import _gflight_ids as gfid

    payload: list[Any] = json.loads(ds1_json)
    rows = gfid._rows_from_ds1(payload).rows
    rows[index][0][2][0][20] = None
    return json.dumps(payload)


def dl_beside_unreadable_as() -> str:
    """The JFK-LAX capture cut to two rows, DL1788 and AS21+AS487, the AS row
    unreadable: parsed, the board names no AS flight."""
    from flight_cli import _gflight_ids as gfid

    payload: list[Any] = json.loads(_unreadable(_ds1("ds1_jfk_lax_tfu.json"), index=16))
    rows = gfid._rows_from_ds1(payload).rows
    payload[2] = [[rows[0], rows[16]]]
    payload[3] = None
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
