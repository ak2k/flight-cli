# pyright: reportPrivateUsage=false
"""Fixtures used by more than one test module.

The Google Flights ones drive the page transport through a fake curl_cffi
session rather than a stubbed `search_with_ids`. Everything worth pinning about
this backend — the pinning recursion, the row parse, the Tier-2 post-filter and
the trim to what the user asked for — sits between the GET and the caller, and a
stub above all of it would pin none of it.
"""

from __future__ import annotations

import json
import pathlib
import threading
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

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

    jar: ClassVar[list[Any]] = []

    def set(self, *_a: object, **_kw: object) -> None:
        return None


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

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

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
def gf_rows() -> Callable[[str], list[Any]]:
    """The parsed flights of one committed capture, as `search_with_ids` would
    have returned them — for the paths a test reaches below the transport."""

    def build(name: str) -> list[Any]:
        from flight_cli import _gflight_ids as gfid

        board = gfid._rows_from_ds1(json.loads(_ds1(name)))
        return [gfid._parse_flight_with_id(r) for r in board.rows]

    return build
