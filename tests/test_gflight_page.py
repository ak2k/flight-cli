# pyright: reportPrivateUsage=false
"""The search-page transport: `ds:1` extraction and refusal classification.

Google's `GetShoppingResults` RPC has been gated since 2026-08, so the gflight
backend GETs the public search page and reads the flight rows Google inlines in
its `AF_initDataCallback` `ds:1` blob. Every failure mode of that fetch — a
captcha interstitial, a consent wall, a re-shaped page — renders as zero rows,
so the load-bearing behavior under test is that none of them can reach the user
as "no flights on this route".

FIXTURE POLICY: **scrub secrets, not structure.** Fixtures are real captures
(2026-09-02) trimmed to three rows with the top-level session id at `[0][4]`
replaced; nothing else is nulled or reshaped.

What is deliberately KEPT: `[0][3]` and the base64 booking token on each row,
both of which carry the same per-search context id Google stamps on the page.
It is not a credential — it authenticates nothing, belongs to no account, and
the searches were anonymous — and the row token is the field this whole module
exists to read, so scrubbing it would leave the fixtures unable to pin the
behaviour under test. The session id at `[0][4]` is replaced because it is the
one value that identifies the capture rather than the flights.

Most of these fixtures were additionally slimmed by dropping the metadata
blocks no code reads, which is safe for what they pin but makes them useless for
the misplaced-block scan — with indices 1-31 all `None`, `misplaced == ()` holds
no matter what the scan does.
`ds1_metadata_blocks_kept.json` is the counterweight: a whole capture, all 31
indices intact including the nine blocks that are row-shaped by structure, at
39 KB. Slim a new fixture only if you know which invariant it is for.

Two page shapes are pinned because Google serves both: an initial JFK-LAX search
(a row block at `[2]` AND `[3]`) and a pinned return leg (`[2] = None`, the
whole board at `[3]`).
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import itertools
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import anyio
import anyio.to_thread
import pytest

from conftest import (
    _answering,  # one home for the re-pointing rule; see its docstring
    distinct_clones,
)
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import (
    GfBackendError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfThrottledError,
    GfTransportError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "gflight_page"
# One read for every travel date the boards and filters below name: the pin loop
# refuses a return board that departs on a day other than its segment asks for,
# so a second read after midnight would refuse every return board.
_TODAY = datetime.date.today()
_FILTERS = cast("Any", None)  # a patched client never encodes the filter
_real_tfs = gfid.build_search_tfs
_LIFTS_AFTER_BACKOFFS = 2  # how many rungs the owner climbs before the wall lifts
# How long a worker holds its first GET waiting for its siblings to reach theirs.
# Reached only if a wave never assembles, which is a failing test either way.
_WAVE_TIMEOUT_S = 5.0
# A backoff of ~1 s becomes ~20 ms: long enough that a sibling reaches the lock
# while the prober is away, short enough that the suite does not notice.
_BACKOFF_SCALE = 0.02
# Captured before anything patches it. `gfid.time` IS the stdlib module, so a
# replacement that calls `time.sleep` calls itself.
_real_sleep = time.sleep


def _first_get_of_each_worker(cabins: tuple[Any, ...]) -> tuple[Any, threading.Barrier]:
    """A latch per worker thread and the barrier their first GETs meet on.

    A fan-out's failures land within microseconds of each other — four threads
    started in one loop, one network — while the retry that would rescue a
    sibling costs a backoff and a multi-megabyte fetch. Left to the scheduler a
    test gets the lucky ordering instead, so the wave is made to assemble."""
    return threading.local(), threading.Barrier(len(cabins))


def _await_the_wave(first_get: Any, wave: threading.Barrier) -> bool:
    """True on this worker's FIRST GET, once every sibling has reached its own."""
    if getattr(first_get, "done", False):
        return False
    first_get.done = True
    with contextlib.suppress(threading.BrokenBarrierError):
        wave.wait(timeout=_WAVE_TIMEOUT_S)
    return True


def _one_prober_per_wall(
    monkeypatch: pytest.MonkeyPatch, arm: str, workers: int
) -> list[float | None]:
    """Make every worker meet the wall together, and report what each was told.

    Synchronising the GETs is not enough on its own: a faked retry costs
    microseconds here, so one worker's success can refill the budget before the
    last worker has even reported, and the test then measures the lucky
    ordering. Production goes the other way — the retry that would rescue a
    sibling costs a backoff and a multi-megabyte fetch, while the failures land
    within microseconds of each other.

    The returned verdicts are the structural difference, and they do not depend
    on ordering: one prober hands a rung to exactly ONE worker and releases the
    rest with 0.0, while a bare shared counter hands out a rung apiece until the
    budget is gone and refuses whoever arrives after that."""
    real = getattr(gfid._SharedThrottleLadder, arm)
    reported = threading.local()
    gate = threading.Barrier(workers)
    verdicts: list[float | None] = []
    seen = threading.Lock()

    def wrapped(self: Any, **kwargs: Any) -> Any:
        first = not getattr(reported, "done", False)
        if first:
            reported.done = True
            with contextlib.suppress(threading.BrokenBarrierError):
                gate.wait(timeout=_WAVE_TIMEOUT_S)
        verdict = real(self, **kwargs)
        if first:
            with seen:
                verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(gfid._SharedThrottleLadder, arm, wrapped)
    return verdicts


def _ds1(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _page(ds1_json: str) -> str:
    """The smallest page shaped like Google's: an AF_initDataCallback blob for
    an unrelated key, then the one we read."""
    return (
        "<!doctype html><html><body><script>"
        "AF_initDataCallback({key: 'ds:0', hash: '1', data:[[]], sideChannel: {}});"
        f"AF_initDataCallback({{key: 'ds:1', hash: '2', "
        f"data:{ds1_json}, sideChannel: {{}}}});"
        "</script></body></html>"
    )


_CONSENT_PAGE = (
    "<!doctype html><html><body>"
    '<form action="https://consent.google.com/save">Before you continue</form>'
    "</body></html>"
)
_SORRY_PAGE = "<!doctype html><html><body>Our systems have detected unusual traffic</body></html>"
_SHAPE_CHANGE_PAGE = (
    "<!doctype html><html><body><script>"
    "AF_initDataCallback({key: 'ds:4', hash: '9', data:[[]], sideChannel: {}});"
    "</script></body></html>"
)


class _NullCookies:
    """Enough of curl_cffi's cookie API for the seed/persist helpers."""

    jar: ClassVar[list[Any]] = []

    def set(self, *_a: object, **_kw: object) -> None:
        return None


class _FakeResponse:
    """What curl_cffi's session hands back: a status we classify ourselves.

    `_one_call` goes to the session rather than fli's `Client.get`, so nothing
    calls `raise_for_status()` and a 429 arrives as a RESPONSE."""

    def __init__(self, *, text: str, url: str = "", status_code: int = 200) -> None:
        self.text = text
        self.url = url or "https://www.google.com/travel/flights?tfs=abc"
        self.status_code = status_code


class _FakeRateLimiter:
    """fli's shared token bucket. Counted so a test can prove we still take a
    token per request after leaving `Client.get` behind."""

    def __init__(self) -> None:
        self.acquisitions = 0

    def acquire(self, *_a: object, **_kw: object) -> bool:
        self.acquisitions += 1
        return True


class _FakeSession:
    """The curl_cffi session, which is where the GET goes.

    Faking `Client.get` instead would skip the code under test: that method's
    own retry ladder is not in the path, so a fake sitting there could not
    observe the request budget."""

    def __init__(self, responses: list[Any], gets: list[str]) -> None:
        self._responses = responses
        self.gets = gets
        self.cookies = _NullCookies()
        self.last_kwargs: dict[str, Any] = {}

    def get(self, url: str, **_kw: object) -> _FakeResponse:
        self.gets.append(url)
        self.last_kwargs = dict(_kw)
        # The last entry repeats, so "429 forever" is one element.
        response: Any = self._responses[min(len(self.gets) - 1, len(self._responses) - 1)]
        if callable(response):
            # A responder rather than a fixed answer, for a condition that
            # clears on something other than a request count — a wall that
            # lifts after so many backoffs, say.
            response = response()
        if isinstance(response, Exception):
            raise response
        return cast("_FakeResponse", response)


class _FakeClient:
    """fli's `Client` at the surface `_one_call` actually touches: the shared
    rate limiter and the per-thread session. Counts GETs so a test can assert
    the request budget, not just the value."""

    def __init__(self, responses: list[Any]) -> None:
        self.gets: list[str] = []
        self._rate_limiter = _FakeRateLimiter()
        self._sessions = _FakeSession(responses, self.gets)

    def _session(self) -> _FakeSession:
        return self._sessions


def _reset_cookie_latches(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Point the cookie cache at a temp dir and re-arm BOTH latches.

    Seeding latches per thread, so a test that leaves it set silently disables
    seeding for every later test in the process."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"persisted": False})
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    """Install a fake GF client and keep cookie seeding off the real cache."""
    _reset_cookie_latches(monkeypatch, tmp_path)

    def _stub_tfs(filters: Any, *, currency: str = "USD") -> bytes:
        """The REAL encoder wherever there are filters to encode.

        A constant here would make every pinned leg of a round trip request the
        same URL, so a fan-out that re-fetched one leg N times would satisfy
        every count this file asserts. The stub survives only for the tests that
        pass no filters at all, which never reach the encoder in production."""
        if filters is None:
            return b"\x08\x1c"
        return _real_tfs(filters, currency=currency)

    monkeypatch.setattr(gfid, "build_search_tfs", _stub_tfs)

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    def install(*responses: Any) -> _FakeClient:
        """One response, or a sequence; the last one repeats for every GET
        after it, so a persistent condition is a single argument."""
        fake = _FakeClient(list(responses))
        monkeypatch.setattr(gfid, "get_client", lambda: fake)
        return fake

    return install


# ─────────────────────────── ds:1 extraction ───────────────────────────


@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("ds1_*.json")), ids=lambda p: p.name)
def test_every_page_fixture_has_its_session_id_scrubbed(path: pathlib.Path) -> None:
    """The FIXTURE POLICY above: `[0][4]` is the one value naming the capture."""
    assert json.loads(path.read_text())[0][4] == "SCRUBBED-SESSION-ID"


def test_extract_ds1_reads_the_flights_blob_past_other_keys() -> None:
    payload = gfid._extract_ds1(_page(_ds1("ds1_jfk_lax_3rows.json")))
    assert payload is not None
    rows, blocks_seen, misplaced = gfid._rows_from_ds1(payload)
    assert blocks_seen == 2
    assert misplaced == ()
    assert rows == payload[2][0] + payload[3][0]
    assert len(rows) == 3


def test_the_captured_callback_envelope_is_the_one_the_extraction_reads() -> None:
    """The one thing `_page()` above cannot pin: the wrapper itself.

    Every other fixture here is a captured `ds:1` payload dropped into a
    callback this file writes to suit the regex that reads it, so the two agree
    by construction and a production envelope that drifted — different quoting,
    reordered properties, a renamed terminator — would refuse every search
    while the suite stayed green. `gf_page_envelope.json` is the verbatim
    wrapper cut from a live page: the prefix through `data:`, and the suffix
    from the end of the array through the call's own `);`. The payload it
    carried is not committed with it; any captured board splices in."""
    envelope: dict[str, str] = json.loads(
        (FIXTURE_DIR.parent / "gf_page_envelope.json").read_text()
    )
    page = (
        "<!doctype html><html><body><script>"
        f"{envelope['prefix']}{_ds1('ds1_jfk_lax_3rows.json')}{envelope['suffix']}"
        "</script></body></html>"
    )
    payload = gfid._extract_ds1(page)
    assert payload is not None
    assert len(gfid._rows_from_ds1(payload).rows) == 3

    # And that the envelope is READ rather than merely carried: the blob is
    # JavaScript, so the single-quoted key is a literal in the regex. The same
    # page with the same rows under a double-quoted key is a page we cannot
    # read, which is the shape of the drift this fixture exists to catch.
    requoted = page.replace("key: 'ds:1'", 'key: "ds:1"')
    assert requoted != page, envelope["prefix"]
    assert gfid._extract_ds1(requoted) is None


def test_extract_ds1_returns_none_when_the_key_is_absent() -> None:
    assert gfid._extract_ds1(_SHAPE_CHANGE_PAGE) is None


def test_extract_ds1_returns_none_on_undecodable_data() -> None:
    assert gfid._extract_ds1(_page("[[,]]")) is None


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
def test_a_page_carrying_one_blob_still_yields_exactly_that_blob(name: str) -> None:
    """Choosing among blobs must not change what a page with only one gives
    back. Every committed capture — the six live ones and the two synthetic
    husks — decodes to itself."""
    raw = _ds1(name)
    assert gfid._extract_ds1(_page(raw)) == json.loads(raw)


def test_the_blob_with_the_most_rows_wins_whichever_way_round_it_sits(client: Any) -> None:
    """Google hydrates this page in stages, so a partly-filled board can be
    emitted above OR below the settled one. Position decides nothing; the row
    count does."""
    for html in (_board_of(1) + _board_of(3), _board_of(3) + _board_of(1)):
        client(_FakeResponse(text=html))
        assert len(gfid._one_call(_FILTERS)) == 3


def test_an_empty_husk_loses_to_the_board_that_carries_rows(client: Any) -> None:
    """The husk `[[]]` is a row block that holds nothing. Asking only whether a
    blob HAS a block hands the search to it and reports an authoritative empty
    for a route Google served thirty flights on."""
    husk = json.dumps([0, 0, [[]], None] + [None] * 28)
    # The real board's rows are folded into ONE block. Left across two, a
    # count of BLOCKS rather than rows still beats the husk's single block and
    # the test passes without measuring anything.
    real = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    real[2] = [real[2][0] + real[3][0]]
    real[3] = None
    client(_FakeResponse(text=_page(husk) + _page(json.dumps(real))))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_the_chosen_blob_is_counted_structurally_not_parsed(client: Any) -> None:
    """The count must NOT ask whether rows parse. A board whose rows have all
    moved carries the most rows of anything on the page and has to win, so the
    0-of-N guard reports the layout change — losing it to a smaller board that
    still parses answers a shape change with a short, plausible table."""
    moved = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    moved[2] = [[["moved-schema-row"], ["another"]]]
    moved[3] = None
    client(_FakeResponse(text=_page(json.dumps(moved)) + _board_of(1)))
    with pytest.raises(GfPageShapeError, match="none of 2 Google Flights rows parsed"):
        gfid._one_call(_FILTERS)


def test_a_decoy_whose_rows_partly_parse_is_served_short_and_silently(client: Any) -> None:
    """The other half of the same trade, and the quieter one.

    Counting structurally means a decoy wins on row count, and the 0-of-N guard
    is what turns that into a loud refusal. But the guard needs ZERO of N to
    parse: a decoy carrying one genuine row among its junk beats a real
    three-row board and serves that single flight, with no warning at any level.
    The user gets a one-row table for a route with three.

    Pinned because it is the documented cost of not parsing rows to choose, and
    a cost nobody has written down is one somebody later reads as a defect and
    'fixes' by parsing — which loses the board whose layout just changed."""
    real = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    genuine_row = copy.deepcopy(real[2][0][0])
    decoy = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    decoy[2] = [[["junk"], ["more-junk"], ["still-junk"], genuine_row]]
    decoy[3] = None
    # Four row-shaped entries against the real board's three, so the decoy wins.
    client(_FakeResponse(text=_page(json.dumps(decoy)) + _board_of(3)))
    served = gfid._one_call(_FILTERS)
    assert len(served) == 1, f"the decoy no longer outranks the real board: {len(served)}"


def _truncated_with_rows() -> str:
    """A staged blob carrying rows at `[2]` and stopping short of `[3]`."""
    return json.dumps([0, 1, [[["a"], ["b"], ["c"], ["d"], ["e"]]]])


def _rows_beside_junk_at_three() -> str:
    """A staged blob carrying rows at `[2]` and a placeholder at `[3]` that is
    neither a row block nor absent."""
    return json.dumps([0, 1, [[["a"], ["b"], ["c"], ["d"], ["e"]]], "loading"] + [None] * 28)


@pytest.mark.parametrize(
    "unservable",
    [
        pytest.param(_truncated_with_rows, id="truncated-above-[3]"),
        pytest.param(_rows_beside_junk_at_three, id="junk-at-[3]"),
    ],
)
@pytest.mark.parametrize("real_first", [True, False], ids=["real-board-first", "real-board-second"])
def test_a_blob_that_cannot_be_served_never_wins_on_row_count(
    client: Any, unservable: Any, real_first: bool
) -> None:
    """Counting rows on a blob `_rows_from_ds1` would refuse trades a board we
    can read for a typed refusal. Both of these carry MORE rows than the real
    capture, so a bare row count hands them the page in either order and the
    search reports a shape change for a page Google served correctly."""
    real = _page(_ds1("ds1_jfk_lax_3rows.json"))
    staged = _page(unservable())
    client(_FakeResponse(text=real + staged if real_first else staged + real))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_a_readable_board_is_exactly_what_the_row_scan_will_accept() -> None:
    """The floor mirrors `_rows_from_ds1`'s two refusals rather than guessing
    at them; if they ever drift, the selector starts choosing blobs the scan
    then refuses."""
    servable = [0, 1, [[["a"]]], None] + [None] * 28
    assert gfid._is_a_readable_board(servable)
    assert gfid._rows_from_ds1(servable).rows  # the scan agrees
    for payload in (json.loads(_truncated_with_rows()), json.loads(_rows_beside_junk_at_three())):
        assert not gfid._is_a_readable_board(payload)
        with pytest.raises(GfPageShapeError):
            gfid._rows_from_ds1(payload)  # the scan agrees


def test_a_tie_on_row_count_keeps_the_earlier_blob() -> None:
    """Nothing distinguishes two equally-full boards, so the document order is
    the tie-break rather than "whichever the loop saw last"."""
    earlier = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    later = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    earlier[0], later[0] = "earlier", "later"
    chosen = gfid._extract_ds1(_page(json.dumps(earlier)) + _page(json.dumps(later)))
    assert chosen is not None
    assert chosen[0] == "earlier"


def test_a_truncated_placeholder_above_a_flight_less_board_stays_an_empty(client: Any) -> None:
    """With no blob carrying rows the fallback must skip a payload too short to
    reach `[3]`. Taking the first decodable one refuses a page Google served
    correctly: the flight-less capture carries `None` at both indices."""
    client(_FakeResponse(text=_page("[]") + _page(_ds1("ds1_flightless_board.json"))))
    assert gfid._one_call(_FILTERS) == []


def test_with_nothing_long_enough_the_first_decodable_blob_is_still_returned() -> None:
    """Last rung of the fallback. A page of nothing but truncated blobs has no
    good answer, and reporting the first one keeps `_rows_from_ds1` the single
    place that decides a payload is too short to be a board."""
    assert gfid._extract_ds1(_page("[1]") + _page("[2]")) == [1]


def test_a_placeholder_blob_before_the_real_one_does_not_win(client: Any) -> None:
    """Google hydrates this page in stages, so a `ds:1` can be emitted empty and
    filled later in the document. Taking the FIRST decodable blob was a bet on
    position: the placeholder decodes fine, so the real board below it was never
    looked at and the leg read as an authoritative empty."""
    placeholder = json.dumps([0, 0, None, None] + [None] * 28)
    html = _page(placeholder) + _page(_ds1("ds1_jfk_lax_3rows.json"))
    payload = gfid._extract_ds1(html)
    assert payload is not None
    assert len(gfid._rows_from_ds1(payload).rows) == 3
    fake = client(_FakeResponse(text=html))
    assert len(gfid._one_call(_FILTERS)) == 3
    assert len(fake.gets) == 1


def test_a_truncated_placeholder_before_the_real_one_does_not_win(client: Any) -> None:
    """Same shape, but the placeholder is short enough to trip the arity guard —
    which would have refused the whole page as a truncated payload while the
    real board sat further down."""
    html = _page("[]") + _page(_ds1("ds1_jfk_lax_3rows.json"))
    client(_FakeResponse(text=html))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_two_empty_blobs_still_read_as_empty(client: Any) -> None:
    """Choosing by content must not invent a board. With no blob holding one,
    the first decodable payload is returned unchanged, so a genuinely
    flight-less page stays flight-less rather than becoming a missing ds:1."""
    html = _page(_ds1("ds1_flightless_board.json")) + _page(_ds1("ds1_flightless_board.json"))
    payload = gfid._extract_ds1(html)
    assert payload is not None
    assert payload[2] is None and payload[3] is None
    client(_FakeResponse(text=html))
    assert gfid._one_call(_FILTERS) == []


def test_rows_keep_the_top_flights_block_first() -> None:
    """`ds:1[2]` is Google's own top flights and `[3]` the rest, read in that
    order: every Google answer is put in price order before it is shown, and
    the page's order survives that only as the tie-break between equal
    fares."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    rows = gfid._rows_from_ds1(payload).rows
    assert rows[0] is payload[2][0][0]
    assert rows[1] is payload[3][0][0]


# ─────────────────────────── happy path ────────────────────────────────


def test_one_call_parses_ids_and_legroom_from_the_page(client: Any) -> None:
    fake = client(_FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert all(a.legroom_class for g in out for a in g.amenities)
    assert len(fake.gets) == 1


def _board_from_jfk_and_ewr() -> str:
    """The captured JFK->LAX board with its last row moved to EWR, as a board
    for JFK,EWR -> LAX comes back: one ranking over both origins."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    row = payload[3][0][1][0]
    row[3] = "EWR"
    row[2][0][3] = "EWR"
    row[2][0][4] = "Newark Liberty International Airport"
    return json.dumps(payload)


def test_a_board_over_an_airport_set_keeps_every_origins_rows(client: Any) -> None:
    from fli.models import Airport  # pyright: ignore[reportMissingTypeStubs]  # fli ships no stubs

    client(_FakeResponse(text=_page(_board_from_jfk_and_ewr())))
    out = gfid._one_call(_FILTERS)
    assert [g.flight.legs[0].departure_airport.name for g in out] == ["JFK", "JFK", "EWR"]
    # A board is checked against the whole set it was asked for, so both pass.
    # A namespace, because fli's segment validator refuses the fixture's past date.
    wanted = SimpleNamespace(
        departure_airport=[[Airport["JFK"], 0], [Airport["EWR"], 0]],
        arrival_airport=[[Airport["LAX"], 0]],
        travel_date=out[0].flight.legs[0].departure_datetime.date().isoformat(),
    )
    assert gfid._unpinned_board(list(out), cast("Any", wanted)) is None


# ─────────────────────── refusals are never "no results" ───────────────


def test_zero_row_page_is_an_authoritative_empty(client: Any) -> None:
    """A page that decodes with no rows is Google's answer, not a refusal —
    one GET, no retry, no raise."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_zero_rows.json"))))
    assert gfid._one_call_with_retry(_FILTERS) == []
    assert len(fake.gets) == 1


def test_sorry_redirect_raises_throttled(client: Any) -> None:
    client(
        _FakeResponse(
            text=_SORRY_PAGE,
            url="https://www.google.com/sorry/index?continue=https://www.google.com/travel",
        )
    )
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_http_429_raises_throttled(client: Any) -> None:
    """A 429 now arrives as a RESPONSE, not an exception — nothing calls
    `raise_for_status()` on our behalf any more, which is what lets the ladder
    see the throttle rather than a wrapped transport error."""
    client(_FakeResponse(text="", status_code=429))
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_other_http_errors_are_not_mistaken_for_throttling(client: Any) -> None:
    """A 503 is not a throttle and not a shape change. It refuses as the base
    `GfBackendError`, which is the seam that degrades to Matrix — backing off
    and retrying would spend the ladder on something backing off cannot fix."""
    client(_FakeResponse(text="", status_code=503))
    with pytest.raises(GfBackendError) as excinfo:
        gfid._one_call(_FILTERS)
    assert not isinstance(excinfo.value, GfThrottledError)
    assert "503" in str(excinfo.value)


def test_a_persistent_throttle_costs_exactly_one_ladder(client: Any) -> None:
    """THE request budget: one initial GET plus `_THROTTLE_RETRY_ATTEMPTS`
    retries, and no second ladder underneath it. What a nested one would cost is
    worked out once, in the budget section of
    docs/memories/gf_routing_and_carriers.md."""
    fake = client(_FakeResponse(text="", status_code=429))
    with pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)
    assert len(fake.gets) == gfid._THROTTLE_RETRY_ATTEMPTS + 1
    # Every GET still takes a token from fli's process-wide bucket; bypassing
    # `Client.get` must not bypass the rate limit the fan-out threads share.
    assert fake._rate_limiter.acquisitions == len(fake.gets)


def test_a_leg_that_both_throttles_and_blips_spends_both_budgets(client: Any) -> None:
    """The two counters are independent on purpose — a wall that lifts and a
    network that drops are different failures — so one leg can spend both. The
    ceiling is the number the request budget has to be read from."""
    fake = client(
        _FakeResponse(text="", status_code=429),
        _FakeResponse(text="", status_code=429),
        _FakeResponse(text="", status_code=429),
        _FakeResponse(text="", status_code=429),
        _transport_error(),
        _transport_error(),
        _FakeResponse(text="", status_code=429),
    )
    with pytest.raises(GfThrottledError):
        gfid._one_call_with_retry(_FILTERS)
    assert len(fake.gets) == 1 + gfid._THROTTLE_RETRY_ATTEMPTS + gfid._TRANSPORT_RETRY_ATTEMPTS == 7


def _one_ladders_worth_of_sleep() -> float:
    """The most one throttle ladder can sleep, jitter at its ceiling."""
    return sum(
        gfid._THROTTLE_BACKOFF_S * (2 ** (attempt - 1)) * 1.5
        for attempt in range(1, gfid._THROTTLE_RETRY_ATTEMPTS + 1)
    )


def _multi_cabin_legs() -> Any:
    from flight_cli.domain import Leg

    return (Leg.of("JFK", "LAX", _TODAY + datetime.timedelta(days=45)),)


def _fan_out(
    monkeypatch: pytest.MonkeyPatch,
    *cabins: Any,
    slept: list[float] | None = None,
    real_time: bool = False,
) -> tuple[dict[Any, Any], list[float]]:
    """Run the real cabin fan-out with its stderr swallowed, reporting what it
    returned and every backoff it slept.

    `slept` may be supplied so a responder can decide by backoff count.

    `real_time` sleeps a scaled fraction of each backoff instead of returning at
    once. A test about what the OTHER workers do while the prober is away needs
    the prober to be away for longer than a lock acquisition: with instant
    sleeps a whole retry — backoff, fetch, reset — finishes before a sibling is
    scheduled, and the fan-out runs as though it were sequential."""
    import io

    from rich.console import Console

    from flight_cli import cli
    from flight_cli.domain import SearchOptions

    slept = [] if slept is None else slept
    recorded = slept

    def sleep_a_little(seconds: float) -> None:
        recorded.append(seconds)
        _real_sleep(seconds * _BACKOFF_SCALE)

    monkeypatch.setattr(gfid.time, "sleep", sleep_a_little if real_time else slept.append)
    monkeypatch.setattr(cli, "err", Console(file=io.StringIO(), width=400))
    out = cli._run_gflight_multi(
        legs=_multi_cabin_legs(),
        opts=SearchOptions(cabin=cabins[0]),
        cabins=tuple(cabins),
        top_n=5,
    )
    return out, slept


def test_a_persistent_throttle_costs_one_ladder_for_the_WHOLE_cabin_fan_out(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four cabins each running their own ladder spend 4 x 5 = 20 multi-megabyte
    GETs against an IP that is already refusing us, and four ladders' worth of
    backoff, to learn the one thing the first ladder learned. The wall is
    per-IP, so the budget is too: one ladder, plus the requests each cabin had
    already put in flight before anyone saw a 429."""
    from flight_cli.domain import Cabin

    fake = client(_FakeResponse(text="", status_code=429))
    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    out, slept = _fan_out(monkeypatch, *cabins)

    assert out == {}, "a throttled fan-out has no cabin to render"
    one_ladder = gfid._THROTTLE_RETRY_ATTEMPTS + 1
    assert len(fake.gets) <= one_ladder + (len(cabins) - 1) == 8
    assert sum(slept) <= _one_ladders_worth_of_sleep()


def test_a_wall_that_lifts_inside_the_ladder_serves_every_cabin(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor under the ceiling. Bounding the fan-out's requests is only
    half the job: one worker probes the wall, so when its probe gets through
    every cabin that was waiting on it has to be released to retry. A design
    that merely shares a counter serves whichever cabin happened to be probing
    and refuses the other three for a wall that is no longer there."""
    from flight_cli.domain import Cabin

    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    slept: list[float] = []
    first_get, wave = _first_get_of_each_worker(cabins)

    def wall_that_lifts() -> _FakeResponse:
        # Every worker's FIRST GET meets the wall, decided before the owner can
        # climb: reading the rung count here instead would let a worker that is
        # slow out of the barrier find the wall already lifted and never park,
        # which is the test passing for the wrong reason.
        if _await_the_wave(first_get, wave):
            return _FakeResponse(text="", status_code=429)
        if len(slept) >= _LIFTS_AFTER_BACKOFFS:
            return _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
        return _FakeResponse(text="", status_code=429)

    fake = client(wall_that_lifts)
    out, slept_out = _fan_out(monkeypatch, *cabins, slept=slept)

    assert set(out) == set(cabins), f"a cabin was refused a wall that lifted: {sorted(out)}"
    assert all(len(rows) == 3 for rows in out.values())  # the capture's row count
    # One worker owned the backoff and climbed two rungs; the other three parked
    # on its outcome and were handed 0.0 — a release, which `retry_throttled`
    # sleeps like any other backoff. Counting only the owner's two would forbid
    # the release this test exists to prove happened.
    assert len(slept_out) == _LIFTS_AFTER_BACKOFFS + (len(cabins) - 1), slept_out
    assert sum(slept_out) <= _one_ladders_worth_of_sleep()
    assert len(fake.gets) <= len(cabins) * 2 + 1


def test_a_success_gives_the_ladder_its_rungs_back(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rungs measure ONE wall. A search that gets through has shown the
    wall is gone, so a wall that returns later is a different wall and gets a
    full budget — otherwise the first brief throttle of a long fan-out spends
    the budget and every later cabin refuses on its first 429 without a retry."""
    served = _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
    fake = client(
        _FakeResponse(text="", status_code=429),  # the first wall
        served,  # cabin recovers on its first retry, refilling the ladder
        _FakeResponse(text="", status_code=429),  # a later wall
        served,
    )
    slept: list[float] = []
    monkeypatch.setattr(gfid.time, "sleep", slept.append)
    with gfid.shared_throttle_ladder():
        assert len(gfid._one_call_with_retry(_FILTERS)) == 3
        assert len(gfid._one_call_with_retry(_FILTERS)) == 3
    assert len(fake.gets) == 4
    # Two separate rung-1 backoffs. Without the refill the second call would
    # have started from rung 2 and, on a longer wall, refused with no retries.
    assert len(slept) == 2
    # Which rung, not just how many: jitter is unstubbed here, so rung 1 lands
    # in [1.0, 1.5) and rung 2 in [2.0, 3.0). The counts alone hold either way,
    # which is what let an unrefilled ladder pass.
    assert all(s < 2.0 for s in slept), slept


def test_a_transport_outage_costs_one_ladder_for_the_WHOLE_cabin_fan_out(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network is one network, so its budget is shared for the same reason
    the wall's is. Per-worker, a single outage costs a transport ladder per
    cabin — and on a round trip, per cabin per pin.

    The ceiling; the floor beside it is what makes the sharing safe."""
    from flight_cli.domain import Cabin

    fake = client(_transport_error("connection reset by peer"))
    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    out, slept = _fan_out(monkeypatch, *cabins)

    assert out == {}
    one_ladder = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    assert len(fake.gets) <= one_ladder + (len(cabins) - 1) == 6
    assert len(slept) <= gfid._TRANSPORT_RETRY_ATTEMPTS


def test_a_transport_blip_that_clears_serves_every_cabin(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor under the transport ceiling, and the reason the network arm
    needs a prober rather than a bare counter.

    A shared budget without one is spent by whoever arrives first: four cabins
    whose sockets reset together consume it before any retry lands, and the last
    two are refused after a single try. The blip then clears and their columns
    are missing anyway — and a missing column reads as "no fare in this cabin",
    not as "we never asked", so the holes are the answer the user keeps."""
    from flight_cli.domain import Cabin

    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    served = _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
    first_get, wave = _first_get_of_each_worker(cabins)

    def blip_then_serve() -> Any:
        if _await_the_wave(first_get, wave):
            return _transport_error("connection reset by peer")
        return served

    fake = client(blip_then_serve)
    verdicts = _one_prober_per_wall(monkeypatch, "transport_failed", len(cabins))
    out, slept = _fan_out(monkeypatch, *cabins, real_time=True)

    assert set(out) == set(cabins), f"a cabin was lost to a blip that cleared: {sorted(out)}"
    assert all(len(rows) == 3 for rows in out.values())  # the capture's row count
    # Exactly one worker paid a backoff and the rest were released by its probe.
    # A bare counter gives every worker its own rung until the budget is gone,
    # which is the same request total and a different fan-out — the cabins that
    # report last are refused after a single try.
    paid = [v for v in verdicts if v]
    assert paid == [slept[0]], (paid, slept)  # the owner's rung, and nobody else's
    assert verdicts.count(0.0) == len(cabins) - 1, verdicts
    # One GET each to meet the blip, one each to be served: the owner's retry is
    # the probe and the waiters are released by it, so nobody re-measures.
    assert len(fake.gets) == len(cabins) * 2, fake.gets
    assert sum(slept) <= _one_ladders_worth_of_sleep()


def test_one_cabins_socket_fault_is_not_refilled_by_its_healthy_siblings(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read timeout on one cabin's board is a fault of THAT request, and the
    budget for it has to end.

    fli's session is a `threading.local`, so a sibling's success rode a
    different socket and is no evidence about this one. Crediting it here means
    every healthy sibling hands the failing worker another rung: with three
    cabins answering, one bad socket is retried for as long as they keep
    answering, and the ladder that is supposed to bound it never exhausts.

    The wall is the opposite case and stays shared — it is per-IP, so a
    sibling getting through really does mean it lifted."""
    from flight_cli.domain import Cabin

    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    served = _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
    role = threading.local()
    claimed: list[bool] = []
    claim = threading.Lock()

    def one_bad_socket() -> Any:
        if not hasattr(role, "failing"):
            with claim:
                role.failing = not claimed  # the first worker in is the unlucky one
                claimed.append(True)
        return _transport_error("read timeout") if role.failing else served

    fake = client(one_bad_socket)
    out, _slept = _fan_out(monkeypatch, *cabins)

    assert len(out) == len(cabins) - 1, f"a healthy cabin was lost: {sorted(out)}"
    # One ladder for the failing worker, one GET for each cabin that answered.
    one_ladder = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    assert len(fake.gets) == one_ladder + (len(cabins) - 1) == 6, fake.gets


def test_a_flapping_network_costs_each_cabin_one_ladder_and_no_more(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling has to be an exit, not a period.

    A sibling's success refills the wall, correctly — so on a network that
    keeps dropping and recovering, a budget checked only against the shared
    ladder is handed back between rungs and the loop has no end. Each call
    carries its own count as well, so a fan-out on a flapping link costs a
    bounded number of requests whatever the siblings are doing."""
    from flight_cli.domain import Cabin

    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    served = _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
    seen = threading.local()

    def flapping() -> Any:
        seen.n = getattr(seen, "n", 0) + 1
        return served if seen.n % 2 == 0 else _transport_error("connection reset by peer")

    fake = client(flapping)
    out, _slept = _fan_out(monkeypatch, *cabins)

    assert set(out) == set(cabins), f"a cabin was lost to a link that recovered: {sorted(out)}"
    one_ladder = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    assert len(fake.gets) <= len(cabins) * one_ladder, fake.gets
    assert len(fake.gets) == len(cabins) * 2 == 8, fake.gets


def test_a_single_cabin_fan_out_still_gets_a_whole_ladder(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sharing a ladder must not shorten one. With nobody to share with, the
    lone cabin spends exactly what a plain search spends."""
    from flight_cli.domain import Cabin

    fake = client(_FakeResponse(text="", status_code=429))
    out, slept = _fan_out(monkeypatch, Cabin.COACH)

    assert out == {}
    assert len(fake.gets) == gfid._THROTTLE_RETRY_ATTEMPTS + 1
    assert len(slept) == gfid._THROTTLE_RETRY_ATTEMPTS


def test_the_page_get_does_not_go_through_flis_retrying_wrapper(client: Any) -> None:
    """`Client.get` is wrapped in `@retry(stop_after_attempt(3))`. Calling it
    would put a second ladder under ours, which is the amplification this path
    exists to avoid — so the fake client has no `get` at all and a regression
    here is an AttributeError, not a quietly larger request count."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))))
    assert not hasattr(fake, "get")
    assert len(gfid._one_call(_FILTERS)) == 3
    assert len(fake.gets) == 1


def test_sorry_body_at_the_original_url_raises_throttled(client: Any) -> None:
    """Google also serves the interstitial in place, with no redirect to key
    off — the body is the only tell, and without it this reads as a shape
    change."""
    client(_FakeResponse(text=_SORRY_PAGE))
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_a_pinned_return_leg_page_serves_one_block(client: Any) -> None:
    """A served page may omit `[2]` and carry its whole board at `[3]`.

    Captured live 2026-09-02 from an HNL-MIA round-trip expansion. One row
    block is an ordinary served page, not a shape change."""
    payload = json.loads(_ds1("ds1_return_leg_pinned.json"))
    assert payload[2] is None, "fixture must keep the served shape"
    fake = client(_FakeResponse(text=_page(_ds1("ds1_return_leg_pinned.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert len(fake.gets) == 1


def test_a_flightless_board_is_an_authoritative_empty(client: Any) -> None:
    """MEASURED: an HNL-MIA nonstop-only search, where no nonstop exists, is
    served as an ordinary results page with no flight cards — `[2]` and `[3]`
    both `None`. Refusing that reports "the page shape changed" for a route
    that simply has no matching flights."""
    payload = json.loads(_ds1("ds1_flightless_board.json"))
    assert payload[2] is None and payload[3] is None
    client(_FakeResponse(text=_page(_ds1("ds1_flightless_board.json"))))
    assert gfid._one_call(_FILTERS) == []


@pytest.mark.parametrize("fixture", ["ds1_zero_rows.json", "ds1_single_block_empty.json"])
def test_synthetic_empty_block_shapes_are_authoritative_empties(client: Any, fixture: str) -> None:
    """SYNTHETIC shapes, hand-edited from the JFK-LAX capture — an empty row
    block at both indices, and at one. Neither has been seen in the wild (the
    measured flight-less board carries no block at all), but an empty block
    must never read as a refusal if Google starts sending one."""
    client(_FakeResponse(text=_page(_ds1(fixture))))
    assert gfid._one_call(_FILTERS) == []


def test_metadata_blocks_are_not_mistaken_for_relocated_rows() -> None:
    """`ds:1` carries other list-of-list-of-list structures on every page —
    indices 1, 6, 7, 11, 14, 17, 25, 26 and 30, the union across the three
    captures, of which any one page carries 4 to 9. A nesting-depth test would
    call those relocated rows and refuse every ordinary page, so the probe
    parses instead."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    board = gfid._rows_from_ds1(payload)
    assert board.misplaced == ()
    assert board.blocks_seen == 2
    assert not gfid._holds_flight_rows(["not-a-row-block"])
    assert not gfid._holds_flight_rows([["metadata", "strings"]])


# The nine indices this capture serves that ARE row-shaped by structure — the
# whole reason the probe has to parse. Asserted first so a future re-trim that
# strips them fails loudly here instead of quietly making the test below pass
# for no reason.
_METADATA_DECOYS = (1, 6, 7, 11, 14, 17, 25, 26, 30)


def test_a_capture_with_every_metadata_block_intact_reports_no_relocation(client: Any) -> None:
    """The fixture the other ds1_*.json files can't be: a whole 31-index page,
    nothing nulled. On the slimmed fixtures `misplaced == ()` is true however
    the scan behaves, because there is nothing left to mistake for rows."""
    payload = json.loads(_ds1("ds1_metadata_blocks_kept.json"))
    decoys = tuple(
        i
        for i, block in enumerate(payload)
        if i not in gfid._DS_ROW_BLOCKS and gfid._looks_like_a_row_block(block)
    )
    assert decoys == _METADATA_DECOYS, "fixture was re-trimmed; it no longer pins anything"
    board = gfid._rows_from_ds1(payload)
    assert board.misplaced == ()
    assert board.blocks_seen == 2
    assert len(board.rows) == 3
    fake = client(_FakeResponse(text=_page(_ds1("ds1_metadata_blocks_kept.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert len(fake.gets) == 1


def test_a_block_that_is_not_a_row_list_is_a_shape_change(client: Any) -> None:
    """Junk at the indices we read is a value Google has never served. Reading
    it as an empty board would report "no flights on this route" for a page we
    simply can no longer parse."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = ["not-a-row-block"]
    payload[3] = ["nor-this"]
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"ds:1\[2\] holds list"):
        gfid._one_call(_FILTERS)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("[]", id="nothing-at-all"),
        pytest.param("[null]", id="one-entry"),
        pytest.param("[null,null,null]", id="one-short-of-3"),
        pytest.param('[null,null,null,["x"]]', id="list-of-strings-at-3"),
        pytest.param('[0,0,"junk",[[]]]', id="bare-string-at-2"),
        pytest.param('[0,0,{"a":1},[[]]]', id="object-at-2"),
        pytest.param("[0,0,7,[[]]]", id="int-at-2"),
    ],
)
def test_a_truncated_or_junk_payload_is_a_shape_change(client: Any, payload: str) -> None:
    """Iterating a list never visits an index that isn't there, so without an
    arity check the first three of these decode, skip the scan entirely and
    reach the user as an authoritative "no flights" at exit 0."""
    client(_FakeResponse(text=_page(payload)))
    with pytest.raises(GfPageShapeError):
        gfid._one_call(_FILTERS)


def test_the_shortest_payload_that_can_hold_a_board_is_read_not_refused(client: Any) -> None:
    """The boundary the arity check sits on: at arity 4 index [3] exists, so an
    absent board there is Google's answer rather than a truncation."""
    client(_FakeResponse(text=_page("[0,0,null,null]")))
    assert gfid._one_call(_FILTERS) == []


def test_an_absent_board_records_the_types_it_saw(client: Any, caplog: Any) -> None:
    """An absent board and a board we failed to recognise look identical from
    the outside, so the types at the two indices are the only thing a later
    reader has to tell them apart with."""
    client(_FakeResponse(text=_page(_ds1("ds1_flightless_board.json"))))
    with caplog.at_level(logging.DEBUG, logger="flight_cli._gflight_ids"):
        assert gfid._one_call(_FILTERS) == []
    assert "carried no row block at [2, 3] (types ['NoneType', 'NoneType'])" in caplog.text


def test_relocated_row_blocks_raise_page_shape(client: Any) -> None:
    """A payload that decodes but whose row blocks moved off [2]/[3] yields no
    rows — indistinguishable from an empty board without the block count."""
    client(_FakeResponse(text=_page(_ds1("ds1_blocks_relocated.json"))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


@pytest.mark.parametrize("placeholder_first", [True, False], ids=["above", "below"])
def test_a_placeholder_does_not_hide_a_board_whose_blocks_moved(
    client: Any, placeholder_first: bool
) -> None:
    """Whichever order Google emits them in, the relocation is the news.

    Both blobs are readable and both count zero rows at the indices we read, so
    neither can win on rows and the choice would otherwise fall to whichever
    came first. Pick the placeholder and the user is told this route has no
    flights; pick the board whose blocks moved and the refusal degrades to
    Matrix. Position deciding that is a coin toss on a fact the page states."""
    placeholder = _page(_ds1("ds1_flightless_board.json"))
    relocated = _page(_ds1("ds1_blocks_relocated.json"))
    html = placeholder + relocated if placeholder_first else relocated + placeholder
    client(_FakeResponse(text=html))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


def test_re_pointing_a_capture_whose_blocks_moved_is_an_error_not_a_no_op(
    gf_answering: Callable[..., str],
) -> None:
    """A fixture helper that rewrites nothing still returns a page.

    `_answering` exists so a test can say "this board answers the leg I asked
    for"; a capture whose row blocks sit somewhere the helper does not look
    comes back with its own route and its own dates, and the pin loop then
    refuses it — a refusal the test reads as the behaviour under test rather
    than as its own fixture. So the helper locates rows through the transport's
    own scan and says so when that finds none, naming where the rows actually
    are.

    `ds1_blocks_relocated` is the capture that shows it: both indices the board
    normally occupies are `None` and the rows are two blocks further on."""
    with pytest.raises(AssertionError, match=r"no rows to re-point.*payload\[4, 5\]"):
        gf_answering(
            "ds1_blocks_relocated.json", origin="LAX", destination="JFK", date=_return_date()
        )


def test_a_placeholder_above_a_drifted_board_does_not_become_an_empty(client: Any) -> None:
    """The other half of the same rule, for the board the readability floor
    turns away.

    A settled board carrying a value at `[3]` that is neither absent nor
    row-shaped is exactly the drift the floor exists to survive, so it scores no
    rows and cannot win. But it still CARRIES rows, at `[2]`, and a page with
    rows on it somewhere is not a page with no flights — so it has to beat a
    placeholder that carries none, and reach the refusal that names the drift."""
    drifted = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    drifted[3] = {"moved": 1}
    assert not gfid._is_a_readable_board(drifted), "the floor must still turn this away"
    client(
        _FakeResponse(text=_page(_ds1("ds1_flightless_board.json")) + _page(json.dumps(drifted)))
    )
    with pytest.raises(GfPageShapeError, match=r"ds:1\[3\] holds dict"):
        gfid._one_call(_FILTERS)


@pytest.mark.parametrize(
    "name",
    ["ds1_jfk_lax_3rows.json", "ds1_brace_in_string.json", "ds1_return_leg_pinned.json"],
)
def test_a_placeholder_above_a_served_board_still_serves_it(client: Any, name: str) -> None:
    """The runner-up must not disturb the page that works. A board that carries
    rows where we read them wins on rows, as it always did, and the placeholder
    above it changes nothing about what the user is shown."""
    client(_FakeResponse(text=_page(_ds1("ds1_flightless_board.json")) + _page(_ds1(name))))
    with_placeholder = [f.flight_id for f in gfid._one_call(_FILTERS)]
    client(_FakeResponse(text=_page(_ds1(name))))
    assert with_placeholder == [f.flight_id for f in gfid._one_call(_FILTERS)]
    assert with_placeholder, "this capture is one that carries rows"


@pytest.mark.parametrize("bad_rows", [1, 3, 7])
def test_bad_leading_rows_do_not_hide_a_relocation(client: Any, bad_rows: int) -> None:
    """Unparseable rows at the head of a moved block are exactly what a shape
    change looks like, so any probe that stops after a fixed number of rows
    answers "not rows" on the very payloads it exists to catch. Reading every
    row is what makes the number of them irrelevant."""
    payload = json.loads(_ds1("ds1_blocks_relocated.json"))
    for index in (4, 5):
        for _ in range(bad_rows):
            payload[index][0].insert(0, ["not-a-row"])
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


def test_an_empty_list_at_one_index_is_absent_not_junk(client: Any) -> None:
    """A bare `[]` carries no rows and claims nothing, so it reads like `None`:
    the board is whatever the other index holds. Refusing it would fail a
    round-trip page over a value that says the same thing as the shape Google
    already serves."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = []
    board = gfid._rows_from_ds1(payload)
    assert board.blocks_seen == 1
    assert board.misplaced == ()
    client(_FakeResponse(text=_page(json.dumps(payload))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 2  # exactly what [3] carries
    assert all(g.flight_id for g in out)


def test_an_empty_husk_plus_rows_elsewhere_is_a_relocation(client: Any) -> None:
    """An empty block `[[]]` at a board index has never been observed live —
    the measured flight-less shape is `None` at both. A husk plus flight rows
    sitting somewhere else is far likelier a relocation than a coincidence, and
    refusing degrades to Matrix while reading it as an empty tells the user
    this route has no flights."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[5] = copy.deepcopy(payload[2])  # a block elsewhere that really parses
    payload[2] = [[]]  # a block, holding no rows
    payload[3] = [[]]
    board = gfid._rows_from_ds1(payload)
    assert board.blocks_seen == 2
    assert board.misplaced == (5,)
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[5\]"):
        gfid._one_call(_FILTERS)


def test_an_empty_husk_with_nothing_misplaced_is_still_an_empty(client: Any) -> None:
    """The other half of that rule. Nothing row-shaped anywhere else means
    there is no relocation to suspect, so an empty board is Google's answer —
    this is the shape the two synthetic empty-block fixtures pin."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = [[]]
    payload[3] = [[]]
    board = gfid._rows_from_ds1(payload)
    assert board.blocks_seen == 2
    assert board.misplaced == ()
    client(_FakeResponse(text=_page(json.dumps(payload))))
    assert gfid._one_call(_FILTERS) == []


def test_a_relocation_with_no_block_left_behind_still_raises(client: Any) -> None:
    """The other arm: rows elsewhere and no block at all where we read. Nothing
    about that page says "flight-less" — it says the board moved."""
    payload = json.loads(_ds1("ds1_blocks_relocated.json"))
    assert payload[2] is None and payload[3] is None
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


def test_a_served_board_with_row_shaped_blocks_elsewhere_is_served_with_a_warning(
    client: Any, caplog: Any
) -> None:
    """Live pages carry 4-9 blocks that are row-shaped by structure (4, 9 and 7
    across the three captures), so refusing whenever one of them happens to
    parse would throw away a board we answered completely. The warning is what
    keeps it findable."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[5] = copy.deepcopy(payload[2])
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert "carried row-shaped blocks outside [2, 3] at [5]; served 3 rows" in caplog.text


@pytest.mark.parametrize(
    ("field", "value", "raised"),
    [
        pytest.param([0, 2, 0, 20], [10**100, 1, 1], OverflowError, id="year-past-a-c-long"),
        pytest.param([0, 2], None, TypeError, id="null-legs-field"),
    ],
)
def test_a_row_the_decoder_cannot_survive_is_typed_not_a_traceback(
    client: Any, field: list[int], value: Any, raised: type[Exception]
) -> None:
    """The row decoder reaches into untrusted remote data, and neither of these
    is an exception the original guard listed. A traceback here is the same
    outcome as a crash — the query dies and the user gets no typed refusal."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    for row in payload[2][0] + payload[3][0]:
        target = row
        for key in field[:-1]:
            target = target[key]
        target[field[-1]] = value
    with pytest.raises(raised):
        gfid._parse_flight_with_id(payload[2][0][0])  # the edit really does raise it
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match="none of 3 Google Flights rows parsed"):
        gfid._one_call(_FILTERS)


def test_the_probe_survives_the_same_rows_away_from_the_board(client: Any) -> None:
    """Same rows, parked at an index the probe scans rather than at [2]/[3].
    The probe feeds arbitrary metadata to the row decoder on every page, so it
    must classify a row it cannot decode, never propagate the failure."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[5] = copy.deepcopy(payload[2])
    payload[5][0][0][0][2][0][20] = [10**100, 1, 1]
    assert gfid._holds_flight_rows(payload[5]) is False
    client(_FakeResponse(text=_page(json.dumps(payload))))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_brace_in_a_row_string_does_not_truncate_the_blob(client: Any) -> None:
    """`});` inside benign Google copy must not cut the capture short — the
    blob terminates on the `sideChannel` key for exactly this reason."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_brace_in_string.json"))))
    assert len(gfid._one_call(_FILTERS)) == 3
    assert len(fake.gets) == 1


def test_second_ds1_is_consulted_when_the_first_is_undecodable() -> None:
    html = _page("[[,]]") + _page(_ds1("ds1_jfk_lax_3rows.json"))
    payload = gfid._extract_ds1(html)
    assert payload is not None
    assert len(gfid._rows_from_ds1(payload).rows) == 3


def test_consent_page_raises_consent(client: Any) -> None:
    client(_FakeResponse(text=_CONSENT_PAGE))
    with pytest.raises(GfConsentError):
        gfid._one_call(_FILTERS)


def test_missing_ds1_raises_page_shape(client: Any) -> None:
    client(_FakeResponse(text=_SHAPE_CHANGE_PAGE))
    with pytest.raises(GfPageShapeError, match="page shape changed"):
        gfid._one_call(_FILTERS)


def _unparseable_board() -> str:
    """A page whose two rows are row-shaped and neither of them parses."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = [[["not-a-row"], ["nor-this"]]]
    payload[3] = [[]]
    return _page(json.dumps(payload))


def test_zero_of_n_rows_parsing_raises_page_shape_with_reasons(client: Any) -> None:
    """Rows present and none parsed is a moved row layout, a different fact
    from an empty board — and the sampled reasons are what makes it fixable, so
    the reason string is asserted rather than just the headline."""
    client(_FakeResponse(text=_unparseable_board()))
    with pytest.raises(GfPageShapeError, match="none of 2 Google Flights rows parsed") as excinfo:
        gfid._one_call(_FILTERS)
    reported = str(excinfo.value)
    assert "sample reasons: ValueError: ValueError(" in reported, reported


def test_a_sampled_reason_cannot_write_page_bytes_at_the_terminal(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reasons are quoted (`!r`), not interpolated. They carry text the PAGE
    chose — a decoder reports a bad value by formatting it into its message —
    and a raw ESC or C1 byte on its way to a terminal is not a diagnostic."""

    def _raise_with_page_bytes(_data: Any) -> Any:
        raise ValueError("bad value \x1b[2J\x9b31m")

    monkeypatch.setattr(gfid, "_parse_flight_with_id", _raise_with_page_bytes)
    client(_FakeResponse(text=_unparseable_board()))
    with pytest.raises(GfPageShapeError) as excinfo:
        gfid._one_call(_FILTERS)
    reported = str(excinfo.value)
    assert "\\x1b" in reported, reported
    assert "\x1b" not in reported
    assert "\x9b" not in reported


def test_a_real_results_page_is_not_read_as_consent(client: Any) -> None:
    """Google's own footer links to the consent domain, so the consent markers
    only mean anything once ds:1 has already come back missing."""
    page = _page(_ds1("ds1_jfk_lax_3rows.json")).replace(
        "</body>", '<a href="https://consent.google.com/">Privacy</a></body>'
    )
    client(_FakeResponse(text=page))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_a_shape_change_on_a_page_that_links_to_consent_is_a_shape_change(
    client: Any,
) -> None:
    """The combination the substring test got wrong. A page whose `ds:1` really
    is gone, which also carries a link to the consent domain, is a LAYOUT
    change. Calling it a consent wall hands the user a remedy for a problem
    they don't have and hides the one they do."""
    page = _SHAPE_CHANGE_PAGE.replace(
        "</body>", '<a href="https://consent.google.com/">Privacy</a></body>'
    )
    client(_FakeResponse(text=page))
    with pytest.raises(GfPageShapeError, match="page shape changed"):
        gfid._one_call(_FILTERS)


def test_a_consent_redirect_is_a_consent_wall(client: Any) -> None:
    """Where the response came FROM is the strongest signal: Google redirects to
    its consent host, and the body that arrives has no `ds:1` at all."""
    client(
        _FakeResponse(
            text="<!doctype html><html><body>Before you continue</body></html>",
            url="https://consent.google.com/m?continue=https://www.google.com/travel/flights",
        )
    )
    with pytest.raises(GfConsentError):
        gfid._one_call(_FILTERS)


def test_a_consent_form_is_a_consent_wall_without_a_redirect(client: Any) -> None:
    """Served in place, with the original URL. A results page LINKS to the
    consent host; only the interstitial POSTS to it, so the form is what tells
    them apart."""
    client(_FakeResponse(text=_CONSENT_PAGE))
    with pytest.raises(GfConsentError):
        gfid._one_call(_FILTERS)


@pytest.mark.parametrize(
    ("final_url", "expected"),
    [
        pytest.param("https://consent.google.com/m?continue=x", True, id="consent-host"),
        pytest.param("https://consent.google.co.uk/m", True, id="consent-host-cctld"),
        pytest.param("https://www.google.com/consent?continue=x", True, id="google-consent-path"),
        pytest.param("https://www.google.de/consent?continue=x", True, id="cctld-consent-path"),
        pytest.param("https://www.google.com/travel/flights?tfs=abc", False, id="the-real-page"),
        pytest.param(
            "https://consent.google.com.evil.example/", False, id="consent-host-as-a-prefix"
        ),
        pytest.param(
            "https://www.google.com/travel/flights?tfs=Y29uc2VudC5nb29nbGUuY29t",
            False,
            id="consent-host-inside-our-own-tfs-parameter",
        ),
        pytest.param(
            "https://evil.example.com/?x=consent.google.com", False, id="another-host-saying-it"
        ),
    ],
)
def test_consent_is_decided_by_the_url_not_by_a_substring(final_url: str, expected: bool) -> None:
    """The host is parsed, never matched as text. Our own request URL carries a
    base64 `tfs=` blob, so any string can turn up inside it."""
    assert gfid._is_consent_page(final_url=final_url, html="") is expected


def test_the_consent_form_scan_does_not_blow_up_on_unterminated_tags(client: Any) -> None:
    """A real search page is megabytes of untrusted markup and a `<form` in it
    need not be closed. Letting the attribute run cross a tag boundary makes
    every one of them rescan the rest of the document, which is quadratic: the
    72 KB built below took over 100 seconds before the bound and takes under a
    millisecond after it. The classifier runs on every page that comes back
    without a `ds:1`."""
    hostile = "<form " * 12_000
    start = time.perf_counter()
    assert not gfid._is_consent_page(final_url="https://www.google.com/travel", html=hostile)
    assert time.perf_counter() - start < 1.5


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_CONSENT_PAGE, id="the-committed-consent-shape"),
        pytest.param(
            '<form method="POST" class="consent" action="https://consent.google.com/save">x</form>',
            id="several-attributes-before-the-action",
        ),
        pytest.param(
            "<form action='https://consent.google.co.uk/save'>x</form>", id="single-quoted-cctld"
        ),
    ],
)
def test_every_consent_form_shape_is_still_recognised(body: str) -> None:
    """The bound must not narrow what it matches: an open tag's attributes never
    contain a tag boundary, so nothing real changes."""
    assert gfid._is_consent_page(final_url="https://www.google.com/travel/flights", html=body)


@pytest.mark.parametrize(
    ("path_or_url", "blocked"),
    [
        pytest.param("/sorry/index?continue=x", True, id="the-redirect-google-sends"),
        pytest.param("/sorry/", True, id="a-trailing-slash"),
        # A trailing slash is not a promise, and the equality arm is the only
        # thing that catches the bare form: a `"/sorry/" in path` test misses it.
        pytest.param("/sorry", True, id="the-bare-path"),
        # Neither of these is the interstitial, and a substring test calls both
        # of them one.
        pytest.param("/sorryabout", False, id="a-longer-first-segment"),
        pytest.param("/notsorry", False, id="a-longer-segment-ending-in-sorry"),
        pytest.param("/notsorry/index", False, id="and-with-a-path-under-it"),
    ],
)
def test_only_the_sorry_path_itself_reads_as_a_block(path_or_url: str, blocked: bool) -> None:
    """The path is a sequence of segments, not a string that happens to contain
    one. Both spellings Google uses count and neither near miss does."""
    assert (
        gfid._is_page_throttled(final_url=f"https://www.google.com{path_or_url}", html="")
        is blocked
    )


def test_a_throttle_is_decided_by_the_url_path_not_the_query() -> None:
    """Same shape of bug one function over: `/sorry/` inside the `tfs=`
    parameter is our own request, not Google's captcha."""
    assert gfid._is_page_throttled(
        final_url="https://www.google.com/sorry/index?continue=x", html=""
    )
    assert not gfid._is_page_throttled(
        final_url="https://www.google.com/travel/flights?tfs=L3NvcnJ5Lw&q=/sorry/", html=""
    )


# ─────────────────── request budget: the round-trip fan-out ────────────────


def _return_date() -> str:
    """The date the RETURN segment of `_round_trip_filters` names.

    Derived rather than pinned, for the reason that function derives its own:
    fli refuses a travel date in the past, so a literal rots the suite."""
    return (_TODAY + datetime.timedelta(days=52)).isoformat()


def _cloned_ds1(n: int) -> str:
    """A ds:1 payload carrying `n` parseable rows, cloned from the real
    capture."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = [distinct_clones(payload[2][0][0], n)]
    payload[3] = None
    return json.dumps(payload)


def _board_of(n: int) -> str:
    """A ds:1 page carrying `n` parseable rows, cloned from the real capture."""
    return _page(_cloned_ds1(n))


def _return_board_of(n: int) -> str:
    """The same board, answering the RETURN segment of `_round_trip_filters`.

    The capture is an outbound JFK-LAX page, and the pin loop refuses a return
    board whose first leg does not correspond to the segment it was asked to
    fill — an outbound board served back is precisely the shape it refuses. A
    test whose subject is the pin BUDGET therefore answers the leg it asked
    for, or it measures a refusal instead of a fan-out."""
    return _page(_answering(_cloned_ds1(n), origin="LAX", destination="JFK", date=_return_date()))


def test_a_re_pointed_board_still_reports_the_row_that_was_captured() -> None:
    """Moving a row's dates must not reshape the row.

    Two captures carry a leg that lands after midnight. A helper that writes the
    asked-for day into BOTH ends of every leg makes that leg arrive before it
    departed, drops the `+Nd` marker the cell formatter exists to print, and
    pulls the next leg back in front of the flight feeding it — on rows other
    tests then assert prices and identities against.

    So the row moves as a row: ONE delta, every leg, both ends. Pinned on a
    SERVED board rather than on the helper's arithmetic, because it is the
    served rows that every other test in the round-trip section reads."""
    from flight_cli import cli
    from flight_cli.pp.gflight_adapter import fli_results_to_search_result

    asked = (_TODAY + datetime.timedelta(days=52)).isoformat()
    payload = json.loads(
        _answering(_ds1("ds1_return_leg_pinned.json"), origin="MIA", destination="HNL", date=asked)
    )
    flights = [gfid._parse_flight_with_id(r) for r in gfid._rows_from_ds1(payload).rows]
    assert flights, "no rows parsed from the re-pointed board"

    overnight_legs = 0
    for flight in flights:
        legs = flight.flight.legs
        assert legs[0].departure_datetime.date().isoformat() == asked, legs[0].departure_datetime
        for leg in legs:
            assert leg.arrival_datetime > leg.departure_datetime, (
                f"{leg.departure_airport.name}-{leg.arrival_airport.name} arrives "
                f"{leg.arrival_datetime} having departed {leg.departure_datetime}"
            )
            overnight_legs += leg.arrival_datetime.date() > leg.departure_datetime.date()
        for before, after in itertools.pairwise(legs):
            assert after.departure_datetime >= before.arrival_datetime, (
                f"the connection departs {after.departure_datetime} and the flight "
                f"feeding it lands {before.arrival_datetime}"
            )
    # Without one the assertions above hold on any board at all — this capture
    # is in the suite precisely because it carries a leg over midnight.
    assert overnight_legs == 1, overnight_legs

    cells = [
        cli._fmt_slice_times(s.departure or "", s.arrival or "")
        for it in fli_results_to_search_result(flights).solutions
        if it.itinerary
        for s in it.itinerary.slices
    ]
    assert any("+1d" in c for c in cells), cells


def _round_trip_filters() -> Any:
    """Two unselected segments, which is what drives the pinning recursion."""
    from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
        # fli ships no stubs; these are fixture builders, not typed API use.
        Airport,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
    )
    from fli.models.google_flights.base import (  # pyright: ignore[reportMissingTypeStubs]
        TripType as _TripType,  # fli ships no stubs
    )
    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,  # fli ships no stubs
    )

    dep = (_TODAY + datetime.timedelta(days=45)).isoformat()
    ret = (_TODAY + datetime.timedelta(days=52)).isoformat()
    return FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport["JFK"], 0]],
                arrival_airport=[[Airport["LAX"], 0]],
                travel_date=dep,
            ),
            FlightSegment(
                departure_airport=[[Airport["LAX"], 0]],
                arrival_airport=[[Airport["JFK"], 0]],
                travel_date=ret,
            ),
        ],
        stops=MaxStops.ANY,
        seat_type=SeatType.ECONOMY,
        trip_type=_TripType.ROUND_TRIP,
    )


def test_the_pinned_fanout_is_capped_regardless_of_top_n(client: Any) -> None:
    """Each pinned outbound is another multi-megabyte page GET, and the
    multi-cabin path bumps top_n by 5x (capped at 100) to widen the pool it
    filters — free on an RPC, not free here. At top_n=50 the round trip costs
    1 outbound + 10 pins, not one pin per row of the board."""
    fake = client(
        _FakeResponse(text=_board_of(30)),  # the outbound board
        _FakeResponse(text=_return_board_of(1)),  # every pinned leg answers
    )
    out = gfid.search_with_ids(_round_trip_filters(), top_n=50)
    assert out is not None
    assert len(fake.gets) == 1 + gfid._PINNED_FANOUT_CAP == 11


def test_every_pin_asks_for_a_different_return_board(client: Any) -> None:
    """The pins exist to price a DIFFERENT outbound each. Nothing else in this
    file would notice a fan-out that pinned the same leg ten times: the GET
    count, the combination count and the warning counts would all still add
    up, and the user would get ten copies of one itinerary."""
    fake = client(
        _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))),
        _FakeResponse(text=_return_board_of(3)),
    )
    out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(fake.gets) == 4  # the outbound board, then one GET per pin
    assert len(set(fake.gets)) == 4, f"a pin re-requested another pin's URL: {fake.gets}"


def test_the_default_top_n_is_unchanged_by_the_cap(client: Any) -> None:
    """The cap must not narrow an ordinary search: `-n 10` is the default and
    sits exactly on it."""
    fake = client(
        _FakeResponse(text=_board_of(30)),
        _FakeResponse(text=_return_board_of(1)),
    )
    gfid.search_with_ids(_round_trip_filters(), top_n=10)
    assert len(fake.gets) == 11
    assert gfid.pinned_fanout(3) == 3  # below the cap, top_n still decides


def test_a_throttle_with_nothing_served_is_still_the_outcome(client: Any) -> None:
    """A throttle on the FIRST pin leaves nothing to return, so the refusal is
    the whole answer and must reach the caller: one outbound, one exhausted
    ladder, and nothing after it. Swallowing it here would report a round trip
    with no return legs as a route with no return flights."""
    fake = client(
        _FakeResponse(text=_board_of(30)),  # the outbound board
        _FakeResponse(text="", status_code=429),  # every pinned leg, forever
    )
    with pytest.raises(GfThrottledError):
        gfid.search_with_ids(_round_trip_filters(), top_n=50)
    assert len(fake.gets) == 1 + (gfid._THROTTLE_RETRY_ATTEMPTS + 1) == 6


def _moved_row_page() -> str:
    """A page whose rows are row-shaped and none of them parse — the shape a
    return board refuses in."""
    moved = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    moved[2] = [[["moved-schema-row"]]]
    moved[3] = None
    return _page(json.dumps(moved))


def test_one_refused_return_board_does_not_discard_the_pins_already_fetched(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Each pin is an independent query, and a round trip is worth answering
    partly. Unwinding on the second of three turns a trip with two priced
    return boards into no answer at all — and hands the user a page-shape
    refusal for a page that mostly worked."""
    fake = client(
        _FakeResponse(text=_board_of(3)),  # the outbound board
        _FakeResponse(text=_return_board_of(1)),  # pin 1 returns
        _FakeResponse(text=_moved_row_page()),  # pin 2 refuses
        _FakeResponse(text=_return_board_of(1)),  # pin 3 returns
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(out) == 2
    assert len(fake.gets) == 4
    assert "1 of 3 return boards unavailable" in caplog.text


def test_a_refused_return_board_names_its_pin_with_the_refusal(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Red at the base, whose count line quoted the last refusal and named no
    pin. The line follows the count and carries the refusal's own words."""
    client(
        _FakeResponse(text=_board_of(3)),
        _FakeResponse(text=_return_board_of(1)),
        _FakeResponse(text=_moved_row_page()),
        _FakeResponse(text=_return_board_of(1)),
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(out) == 2
    lines = [r.getMessage() for r in caplog.records]
    [count] = [
        i for i, ln in enumerate(lines) if ln.startswith("1 of 3 return boards unavailable: ")
    ]
    refusal = lines[count].split(": ", 1)[1]
    named = re.compile(
        r"pinned outbound [A-Z0-9]{2}\d+(?:/[A-Z0-9]{2}\d+)* \(USD\d+\.\d{2}\) lost: "
        + re.escape(refusal)
    )
    assert [i for i, ln in enumerate(lines) if named.fullmatch(ln)] == [count + 1]


def test_a_return_board_that_ignored_the_pin_never_becomes_a_combination(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A page that dropped `selected_flight` arrives shaped exactly like one
    that honoured it — nothing in the response says which pin it belongs to.

    Paired unchecked, its rows become combinations whose members are legs
    nobody asked for: this filter asks JFK-LAX out and LAX-JFK back a week
    later, and the outbound board served back would emit six trips that fly
    JFK-LAX twice and never come home, priced and printed beside real ones.
    That is a refusal wearing the shape of a result, which is the failure this
    module refuses everywhere else and the one no later stage can catch."""
    client(
        _FakeResponse(text=_board_of(2)),  # the outbound board
        _FakeResponse(text=_board_of(3)),  # every pinned leg: the OUTBOUND again
    )
    with (
        caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"),
        pytest.raises(GfPinIgnoredError) as excinfo,
    ):
        gfid.search_with_ids(_round_trip_filters(), top_n=2)

    # Nothing served, so the refusal IS the outcome rather than a footnote —
    # swallowing it would report a round trip whose return boards stopped
    # meaning what we asked as a route with no return flights.
    assert "the pinned leg was ignored" in str(excinfo.value), excinfo.value
    # Counted per BOARD, so the count stays a count of the pins asked for.
    assert "2 of 2 return boards unavailable" in caplog.text, caplog.text


def test_a_return_board_for_the_wrong_day_is_refused_too(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the same correspondence. A board for the right route on
    a day nobody asked for is still not an answer to the segment that was sent,
    and it is the half a same-route trip would otherwise leave unchecked."""
    client(
        _FakeResponse(text=_board_of(2)),
        _FakeResponse(
            text=_page(
                _answering(
                    _cloned_ds1(3),
                    origin="LAX",
                    destination="JFK",
                    date=(_TODAY + datetime.timedelta(days=53)).isoformat(),
                )
            )
        ),
    )
    with (
        caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"),
        pytest.raises(GfPinIgnoredError) as excinfo,
    ):
        gfid.search_with_ids(_round_trip_filters(), top_n=2)

    assert "the pinned leg was ignored" in str(excinfo.value), excinfo.value
    assert _return_date() in str(excinfo.value), excinfo.value
    assert "2 of 2 return boards unavailable" in caplog.text, caplog.text


def _same_day_filters() -> Any:
    """A round trip out and back on ONE day, which is what blinds the date arm.

    Nothing forbids one: `Leg` wants a date per leg and never compares them,
    the CLI never compares them, and fli refuses only a date in the PAST. So
    the query reaches this recursion with both segments naming the same day,
    and a served board can then be wrong about the route while being right
    about the only date there is."""
    from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
        # fli ships no stubs; these are fixture builders, not typed API use.
        Airport,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
    )
    from fli.models.google_flights.base import (  # pyright: ignore[reportMissingTypeStubs]
        TripType as _TripType,  # fli ships no stubs
    )
    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,  # fli ships no stubs
    )

    day = _same_day()
    return FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport["JFK"], 0]],
                arrival_airport=[[Airport["LAX"], 0]],
                travel_date=day,
            ),
            FlightSegment(
                departure_airport=[[Airport["LAX"], 0]],
                arrival_airport=[[Airport["JFK"], 0]],
                travel_date=day,
            ),
        ],
        stops=MaxStops.ANY,
        seat_type=SeatType.ECONOMY,
        trip_type=_TripType.ROUND_TRIP,
    )


def _same_day() -> str:
    """The one date a same-day round trip names. Derived: fli refuses a past
    travel date, so a literal rots the suite."""
    return (_TODAY + datetime.timedelta(days=45)).isoformat()


def test_a_return_board_that_lands_somewhere_else_is_refused(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The third field of the correspondence, and the one nothing on screen
    would show.

    A board re-pointed to arrive LAX for a LAX-JFK segment is right about its
    origin and right about its day, so both other arms pass it. Its rows then
    become combinations for a trip that never comes home — and the table's
    `legs` column carries flight numbers, not endpoints, so the user sees four
    ordinary-looking itineraries priced beside real ones."""
    client(
        _FakeResponse(text=_board_of(2)),  # the outbound board
        _FakeResponse(
            text=_page(
                _answering(
                    _cloned_ds1(3),
                    origin="LAX",  # the segment's own origin: this arm passes
                    destination="MIA",  # ...but the board lands nowhere near JFK
                    date=_return_date(),  # ...on the day that was asked for
                )
            )
        ),
    )
    with (
        caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"),
        pytest.raises(GfPinIgnoredError) as excinfo,
    ):
        gfid.search_with_ids(_round_trip_filters(), top_n=2)

    assert "arrives MIA, not JFK" in str(excinfo.value), excinfo.value
    assert "the pinned leg was ignored" in str(excinfo.value), excinfo.value
    assert "2 of 2 return boards unavailable" in caplog.text, caplog.text


def test_a_same_day_return_board_from_the_wrong_airport_is_refused(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The origin arm carrying the check on its own.

    Every other board this file refuses is wrong in at least two fields, so the
    origin arm can be deleted with the suite still green. A SAME-DAY round trip
    is where it stands alone: the date arm cannot tell the two legs apart, and a
    board that lands where it should but departs from the next airport over —
    Google answering with a metro neighbour — is right about everything the
    other two arms read. Unrefused, its rows are combinations that start from an
    airport the traveller is not at."""
    client(
        _FakeResponse(text=_board_of(2)),  # the outbound board
        _FakeResponse(
            text=_page(
                _answering(
                    _cloned_ds1(3),
                    origin="BUR",  # ...but not the LAX the segment named
                    destination="JFK",  # the segment's own destination
                    date=_same_day(),  # the only date either segment has
                )
            )
        ),
    )
    with (
        caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"),
        pytest.raises(GfPinIgnoredError) as excinfo,
    ):
        gfid.search_with_ids(_same_day_filters(), top_n=2)

    assert "departs BUR, not LAX" in str(excinfo.value), excinfo.value
    assert "2 of 2 return boards unavailable" in caplog.text, caplog.text


def test_a_same_day_return_board_that_answers_its_segment_is_still_paired(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the arm above: a same-day trip is not refused for being
    same-day. Without it, an origin arm that refused everything would satisfy
    the reproduction on its own."""
    client(
        _FakeResponse(text=_board_of(2)),
        _FakeResponse(
            text=_page(
                _answering(_cloned_ds1(3), origin="LAX", destination="JFK", date=_same_day())
            )
        ),
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_same_day_filters(), top_n=2)

    assert out is not None
    assert len(out) == 2 * 3, out
    assert "return boards unavailable" not in caplog.text, caplog.text


def test_a_return_board_that_answers_the_segment_is_paired(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The control the check above needs: a board that DOES answer the leg it
    was asked for is paired, in full, with no warning.

    Without it the correspondence check reads as a test of the fixtures rather
    than of the rule — a guard that refused everything would satisfy the
    reproduction on its own."""
    client(
        _FakeResponse(text=_board_of(2)),
        _FakeResponse(text=_return_board_of(3)),
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=2)

    assert out is not None
    assert len(out) == 2 * 3, out
    assert all(isinstance(c, tuple) and len(c) == 2 for c in out), out
    assert "return boards unavailable" not in caplog.text, caplog.text


def test_one_pin_answered_with_the_wrong_leg_keeps_the_pins_that_were_honoured(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Refused per board, and the rest of the fan-out still runs.

    A page that stops meaning what we asked is the same class as a re-shaped
    one, so it takes the same arm: this pin is dropped, the pins that answered
    are kept, and the count tells the user their table is short."""
    client(
        _FakeResponse(text=_board_of(2)),  # the outbound board
        _FakeResponse(text=_board_of(3)),  # pin 1 answers the wrong leg
        _FakeResponse(text=_return_board_of(3)),  # pin 2 answers the one asked
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=2)

    assert out is not None
    assert len(out) == 3, out
    assert "1 of 2 return boards unavailable" in caplog.text, caplog.text


def test_a_refusal_on_every_pin_is_still_the_outcome(client: Any) -> None:
    """Continuing past one bad board must not turn a wholly failed round trip
    into a silent `None` the caller renders as "no results"."""
    client(
        _FakeResponse(text=_board_of(3)),
        _FakeResponse(text=_SHAPE_CHANGE_PAGE),  # every pin, forever
    )
    with pytest.raises(GfPageShapeError):
        gfid.search_with_ids(_round_trip_filters(), top_n=3)


def test_one_empty_board_among_refusals_does_not_suppress_the_refusal(
    client: Any,
) -> None:
    """ "Nothing was served" is the rule, not "every pin refused".

    The two differ by exactly one pin. Under the narrower rule a round trip
    whose return boards have stopped parsing, with a single genuinely empty
    board among them, is `None` — which the caller renders as "no results (or
    none matched the routing)" and exits 0. That is a wrong answer, not a
    partial one: the user is told this route has no return flights when what
    happened is that we can no longer read the page.

    The trade the other way is deliberate. This also raises when one pin refused
    and the rest were honestly empty, preferring a false refusal to a false "no
    flights" — a refusal degrades to Matrix or exits with a reason, while "no
    results" is unrecoverable."""
    empty = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    empty[2] = None
    empty[3] = None
    client(
        _FakeResponse(text=_board_of(3)),  # the outbound board
        _FakeResponse(text=_page(json.dumps(empty))),  # pin 1: a real empty board
        _FakeResponse(text=_SHAPE_CHANGE_PAGE),  # pins 2-3 and on: refusals
    )
    with pytest.raises(GfPageShapeError):
        gfid.search_with_ids(_round_trip_filters(), top_n=3)


def test_a_throttle_on_a_later_pin_keeps_what_was_already_served(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The wall is per-IP, so pin 3 would spend another ladder measuring what
    pin 2 just measured — stop. But pin 1's board was already paid for and its
    combination is a real answer, so it is returned rather than unwound."""
    fake = client(
        _FakeResponse(text=_board_of(3)),
        _FakeResponse(text=_return_board_of(1)),  # pin 1 returns
        _FakeResponse(text="", status_code=429),  # pin 2 onwards
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(out) == 1
    # Outbound, pin 1, then one ladder on pin 2. Pin 3 is never fetched.
    assert len(fake.gets) == 2 + (gfid._THROTTLE_RETRY_ATTEMPTS + 1) == 7
    assert "rate-limited this IP" in caplog.text
    assert "2 of 3 return boards skipped" in caplog.text


def test_a_transport_outage_on_a_later_pin_keeps_what_was_already_served(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Same rule, the other cause. A network that is down is down for every
    remaining pin, and continuing past it multiplied one outage by the pin
    count — a transport ladder each, for a board none of them could reach."""
    fake = client(
        _FakeResponse(text=_board_of(5)),
        _FakeResponse(text=_return_board_of(1)),  # pin 1 returns
        _FakeResponse(text=_return_board_of(1)),  # pin 2 returns
        _transport_error("connection reset by peer"),  # pin 3 onwards
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=5)
    assert out is not None
    assert len(out) == 2
    # Outbound, two served pins, then one transport ladder on pin 3.
    assert len(fake.gets) == 1 + 2 + (gfid._TRANSPORT_RETRY_ATTEMPTS + 1) == 6
    assert "was unreachable" in caplog.text
    assert "3 of 5 return boards skipped" in caplog.text


def test_a_stop_with_nothing_served_still_reports_the_boards_that_would_not_parse(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The throttle is what ended the pinning, but it is not the whole news.

    Two of three return boards no longer parse, and the user is about to be told
    to wait and retry — advice that will not work and hides the page-shape
    change behind it. The raise is the outcome; the refusals seen on the way
    there still have to be said, and every other exit from this accounting says
    them."""
    client(
        _FakeResponse(text=_board_of(3)),  # the outbound board
        _FakeResponse(text=_moved_row_page()),  # pin 1 refuses
        _FakeResponse(text=_moved_row_page()),  # pin 2 refuses
        _FakeResponse(text="", status_code=429),  # pin 3 throttles, nothing served
    )
    with (
        caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"),
        pytest.raises(GfThrottledError),
    ):
        gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert "2 of 3 return boards unavailable" in caplog.text, caplog.text


def test_a_transport_outage_with_nothing_served_is_the_outcome(client: Any) -> None:
    """Stopping early must not turn a wholly unreachable round trip into a
    silent `None` the caller renders as "no results". Exactly one transport
    ladder is spent, on the first pin."""
    fake = client(
        _FakeResponse(text=_board_of(3)),
        _transport_error("dns lookup failed"),  # every pin, forever
    )
    with pytest.raises(GfTransportError):
        gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert len(fake.gets) == 1 + (gfid._TRANSPORT_RETRY_ATTEMPTS + 1) == 4


# ───────────────────── recursion, both places it can bite ──────────────────


def _nested(depth: int) -> list[Any]:
    """A list nested `depth` deep, built iteratively — building it recursively
    would hit the limit here rather than in the code under test."""
    root: list[Any] = []
    cur = root
    for _ in range(depth):
        nxt: list[Any] = []
        cur.append(nxt)
        cur = nxt
    return root


def test_a_blob_nested_past_the_json_limit_is_skipped_not_fatal(client: Any) -> None:
    """`json.loads` recurses once per nesting level and gives out near 10k, so
    a deep blob raises RecursionError where every other bad blob raises
    ValueError. Skipping it is what lets the next blob still be read."""
    deep = "[" * 12000 + "]" * 12000
    html = _page(deep) + _page(_ds1("ds1_jfk_lax_3rows.json"))
    client(_FakeResponse(text=html))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_a_row_that_recurses_the_decoder_is_a_typed_outcome() -> None:
    """fli reports a bad price by formatting the value into its message, and
    repr of a deeply nested list recurses — so the error REPORTING overflows
    before the error exists.

    Unreachable from a real page: `json.loads` refuses at 9998 levels and repr
    survives to about 15k, so `_extract_ds1` rejects any blob deep enough to
    trigger this. Pinned anyway because the row decoder is called on values
    that did not come through `json.loads` — the misplaced-block probe feeds it
    arbitrary metadata — and because the tuple entry is free."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    for row in payload[2][0] + payload[3][0]:
        row[1] = [[_nested(20000)], "USD"]  # the price head fli reprs on failure
    with pytest.raises(RecursionError):
        gfid._parse_flight_with_id(payload[2][0][0])
    # The row loop's own guard is what has to classify it.
    assert isinstance(RecursionError(), gfid._ROW_PARSE_ERRORS)
    assert not gfid._holds_flight_rows([payload[2][0]])  # the probe survives it too


# ─────────────────── transport failures: our ladder, briefly ────────────────


def _transport_error(message: str = "connection reset by peer") -> Exception:
    """A real curl_cffi exception, not a stand-in — the classifier keys off
    curl's own base class, so a look-alike would pass a test the code fails."""
    from curl_cffi.requests import exceptions as curl_exc

    return curl_exc.ConnectionError(message)


def test_a_transient_transport_failure_is_retried_then_served(client: Any) -> None:
    """Bypassing fli's `Client.get` to own the throttle ladder also dropped its
    transport retry, so one reset connection failed the leg. Ours covers it on
    a small budget: two blips, then the page."""
    fake = client(
        _transport_error(),
        _transport_error(),
        _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))),
    )
    assert len(gfid._one_call_with_retry(_FILTERS)) == 3
    assert len(fake.gets) == 3


def test_a_persistent_transport_failure_is_typed_not_a_traceback(client: Any) -> None:
    """A network that stays down is not worth a long backoff — the user is
    going to get the Matrix fallback either way, and a curl traceback is not a
    refusal. Exactly `_TRANSPORT_RETRY_ATTEMPTS` retries, then a typed line."""
    fake = client(_transport_error("dns lookup failed"))
    with pytest.raises(GfTransportError) as excinfo:
        gfid._one_call_with_retry(_FILTERS)
    assert isinstance(excinfo.value, GfBackendError)  # still the Matrix-fallback seam
    assert "dns lookup failed" in str(excinfo.value)
    assert len(fake.gets) == gfid._TRANSPORT_RETRY_ATTEMPTS + 1 == 3


def test_a_transport_failure_degrades_to_matrix_rather_than_crashing() -> None:
    """The seam that matters: `GfBackendError` is what the enriched path
    catches to keep Matrix authoritative and what `_gf_refusal` renders as a
    typed line. An escaping curl error would be neither."""
    assert issubclass(gfid._RetryableTransportError, GfBackendError)
    assert gfid._is_transport_failure(_transport_error())
    assert not gfid._is_transport_failure(ValueError("not a transport problem"))


def _curl_error(name: str) -> Exception:
    """One of curl_cffi's non-transport failures, by name."""
    from curl_cffi.requests import exceptions as curl_exc

    return cast("Exception", getattr(curl_exc, name)(f"{name} from a request we built"))


@pytest.mark.parametrize(
    "name",
    ["InvalidURL", "InvalidSchema", "SessionClosed", "CookieConflict", "TooManyRedirects"],
)
def test_a_curl_error_that_is_not_a_failure_to_connect_is_not_a_blip(name: str) -> None:
    """These all descend from curl's `CurlError` base and none of them clears on
    a second attempt: each names a request WE built wrongly. Keying the retry on
    that base is what turns a `build_search_tfs` regression into three GETs and
    the words "Google could not be reached"."""
    assert not gfid._is_transport_failure(_curl_error(name))


# Transient conditions curl reports through a class that also carries permanent
# ones, so only the code tells them apart: a body cut short arrives as
# `IncompleteRead`, and the HTTP/2 and HTTP/3 stream errors all arrive as
# `HTTPError`, which is otherwise a status we must never retry.
_RETRY_BY_CODE = ("PARTIAL_FILE", "HTTP2", "HTTP2_STREAM", "HTTP3")
# Permanent faults in the request we built, or in how this machine is set up.
_NEVER_RETRIED = (
    "URL_MALFORMAT",
    "UNSUPPORTED_PROTOCOL",
    "TOO_MANY_REDIRECTS",
    "PROXY",
    "INTERFACE_FAILED",
)


def _by_code(name: str) -> Exception:
    """The exception curl_cffi raises for the named result code."""
    from curl_cffi.const import CurlECode
    from curl_cffi.requests import exceptions as curl_exc

    code = getattr(CurlECode, name)
    return cast("Exception", curl_exc.CODE2ERROR[code](f"curl said {name}", code=code))


@pytest.mark.parametrize("name", _RETRY_BY_CODE)
def test_a_transient_curl_code_is_retried_even_where_its_class_is_not(name: str) -> None:
    """A half-received multi-megabyte page and a broken HTTP/2 stream are the
    ordinary way this transport fails on a flaky link. Both arrive in a class
    that also carries permanent faults, so a rule written in classes alone
    lets them out raw: no retry, no typed refusal, and a message that is a
    bare byte count."""
    assert gfid._is_transport_failure(_by_code(name))


@pytest.mark.parametrize("name", _NEVER_RETRIED)
def test_a_permanent_curl_code_is_not_retried(name: str) -> None:
    assert not gfid._is_transport_failure(_by_code(name))


# Every TLS code curl_cffi maps, with the decision written out one by one
# instead of derived from the rule under test. `SSLError` subclasses
# `ConnectionError`, so the class arm answers "retry" for all twelve, and a test
# that recomputes the rule agrees with it every time — including where it is
# wrong. An independent list is the only kind that can disagree.
_SSL_CODE_IS_RETRIED = {
    # The handshake, or the peer's certificate. Either can differ on the next
    # attempt, and a flaky link produces them.
    "SSL_CONNECT_ERROR": True,
    "SSL_CERTPROBLEM": True,
    "SSL_CIPHER": True,
    "SSL_ISSUER_ERROR": True,
    "SSL_INVALIDCERTSTATUS": True,
    # This machine's own TLS setup: an engine it does not have, a CA bundle or
    # CRL it cannot read, a pin that does not match, a client certificate the
    # server refused. Identical on the third attempt, and reported as "Google
    # Flights could not be reached" — which sends the reader to the network for
    # a fault that is here.
    "SSL_ENGINE_NOTFOUND": False,
    "SSL_ENGINE_SETFAILED": False,
    "SSL_ENGINE_INITFAILED": False,
    "SSL_CACERT_BADFILE": False,
    "SSL_CRL_BADFILE": False,
    "SSL_PINNEDPUBKEYNOTMATCH": False,
    "SSL_CLIENTCERT": False,
}


@pytest.mark.parametrize(("name", "retried"), sorted(_SSL_CODE_IS_RETRIED.items()))
def test_each_tls_code_is_decided_on_its_own_merits(name: str, retried: bool) -> None:
    """One class covers all of these and two answers are needed, so the
    deny-list is read before the class."""
    assert gfid._is_transport_failure(_by_code(name)) is retried


def test_every_curl_result_code_gets_a_decision_and_only_these_are_retried() -> None:
    """Enumerated, not sampled. curl_cffi maps 41 result codes onto a dozen
    classes, so a rule written in classes quietly decides for codes nobody
    looked at — which is how a stalled body came to be classified as a bug in
    our own request.

    The TLS codes carry their decision from the table above rather than from the
    class, which is the half this shape of test could not see before."""
    from curl_cffi.const import CurlECode
    from curl_cffi.requests import exceptions as curl_exc

    by_code = {getattr(CurlECode, n) for n in _RETRY_BY_CODE}
    named = {getattr(CurlECode, n): r for n, r in _SSL_CODE_IS_RETRIED.items()}
    seen = 0
    for code, cls in curl_exc.CODE2ERROR.items():
        error = cls(f"curl said {code}", code=cast("Any", code))
        if code in named:
            expected = named[code]
            seen += 1
        else:
            reachability = isinstance(error, curl_exc.ConnectionError | curl_exc.Timeout)
            expected = reachability or code in by_code
        assert gfid._is_transport_failure(error) is expected, (
            f"{getattr(code, 'name', code)} ({type(error).__name__}) is classified wrongly"
        )
    assert seen == len(_SSL_CODE_IS_RETRIED), "a named TLS code is no longer mapped"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("a bug, not a blip"), id="our-own-bug"),
        pytest.param(_curl_error("InvalidURL"), id="a-url-we-built-wrong"),
    ],
)
def test_a_non_transport_exception_is_not_retried(client: Any, error: Exception) -> None:
    """The budget covers curl failing to REACH Google, nothing else. A bug in
    the request we sent must not be retried three times and relabelled."""
    fake = client(error)
    with pytest.raises(type(error)):
        gfid._one_call_with_retry(_FILTERS)
    assert len(fake.gets) == 1


def test_the_page_get_carries_flis_own_request_timeout(client: Any) -> None:
    """A GET with no timeout at all hangs on a half-open socket until the OS
    gives up, and the enriched path waits on it. `_get_search_page` bypasses
    `Client.get`, so it has to pass the value `Client.get` would have."""
    from fli.search.client import REQUEST_TIMEOUT  # pyright: ignore[reportMissingTypeStubs]

    fake = client(_FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))))
    gfid._one_call(_FILTERS)
    assert fake._session().last_kwargs["timeout"] == REQUEST_TIMEOUT


def test_the_configured_request_timeout_reaches_the_session_get() -> None:
    """`FLI_TIMEOUT` is read and validated by fli at import, so the value is
    frozen the moment `fli.search.client` loads — a fresh process is the only
    way to see the environment reach the socket. Copying fli's default into a
    constant of our own looked equivalent and silently dropped this."""
    probe = textwrap.dedent(
        """
        import json, sys
        from flight_cli import _gflight_ids as gfid

        seen = {}

        class _Session:
            def get(self, url, **kw):
                seen["timeout"] = kw["timeout"]
                return "a page nobody reads"

        class _Bucket:
            def acquire(self, *a, **kw):
                return True

        class _Client:
            _rate_limiter = _Bucket()

            def _session(self):
                return _Session()

        gfid._get_search_page(_Client(), "https://www.google.com/travel/flights")
        json.dump(seen, sys.stdout)
        """
    )
    done = subprocess.run(  # noqa: S603 — this interpreter, a literal script
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "FLI_TIMEOUT": "5"},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"timeout": 5.0}


def test_the_first_selling_carrier_is_the_one_a_passenger_books_under() -> None:
    """A leg can be sold under several codes at once, and exactly one of them
    is the identity the booking shows.

    Google lists them in order and the first is the headline — the same one
    Matrix surfaces, which is what lets the cross-backend join fire on a
    codeshare. Taking any other entry pairs a real flight with a code the
    passenger will never see on a ticket, and the join then silently misses.
    Nothing pinned the choice, so reading the last entry passed every test."""
    leg: list[Any] = [None] * 23
    leg[15] = [["LH", "9498", None, "Lufthansa"], ["UA", "8871", None, "United"]]
    leg[18] = None  # not self-marketed, so the selling carrier is the headline
    leg[22] = ["EN", "8858", None, "Air Dolomiti"]

    assert gfid._resolve_booking(leg) == ("LH", "9498")
    # The whole set is still carried, so a marketing-carrier filter can match
    # any of them; only the headline is singular.
    assert gfid._marketing_codes(leg) == ("LH", "UA")


def test_a_flightless_capture_with_its_metadata_intact_is_still_an_empty(
    client: Any,
) -> None:
    """The runner-up rule reads every OTHER index for row-shaped content, so it
    meets the blocks a real page carries — and a real page carries several
    lists-of-lists-of-lists that a structural test would call relocated rows.

    The only capture that still has those blocks has flights in it, and every
    flight-less capture was slimmed, so no test until now put the strict half
    of that rule in front of a real decoy. Emptied here rather than captured:
    the shape under test is 'no board, real metadata', which no committed page
    has."""
    payload = json.loads(_ds1("ds1_metadata_blocks_kept.json"))
    assert sum(1 for b in payload if _is_row_shaped(b)) >= 2, "this capture lost its decoys"
    payload[2] = None
    payload[3] = None
    client(_FakeResponse(text=_page(json.dumps(payload))))
    assert gfid._one_call(_FILTERS) == [], "a real page's metadata read as relocated rows"


def _is_row_shaped(block: Any) -> bool:
    """The structural test, asked of a block the scan would meet away from the
    indices it reads."""
    return gfid._looks_like_a_row_block(block)


def test_the_documented_round_trip_costs_compose_from_their_factors(client: Any) -> None:
    """The memo quotes two aggregate numbers and each factor is measured
    somewhere, but nothing multiplies them — so a change to the pin cap or the
    transport budget would leave the memo quoting an arithmetic that no longer
    holds.

    A round trip where every pin blips once and recovers, and one where every
    return board refuses, both composed from the constants rather than from a
    literal."""
    pins = gfid.pinned_fanout(10)
    board = _board_of(pins)
    served = _FakeResponse(text=_return_board_of(1))

    # Every pin spends its whole transport ladder and then recovers: the
    # outbound once, then budget + 1 GETs per pin. No ladder exhausts, so the
    # stop rule never fires and the cost is the flaky link, not the outage.
    per_pin = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    count = {"n": 0}

    def blip_then_serve() -> Any:
        count["n"] += 1
        if count["n"] == 1:
            return _FakeResponse(text=board)  # the outbound board
        return (
            served
            if (count["n"] - 2) % per_pin == per_pin - 1
            else _transport_error("connection reset by peer")
        )

    fake = client(blip_then_serve)
    out = gfid.search_with_ids(_round_trip_filters(), top_n=10)
    assert out is not None and len(out) == pins
    assert len(fake.gets) == 1 + per_pin * pins == 31, fake.gets

    # Every return board's `ds:1` decodes to a layout we cannot read: one GET
    # each, no retry, and the refusal is the outcome — the same cost as a
    # search that worked.
    relaid = _SHAPE_CHANGE_PAGE.replace("'ds:4'", "'ds:1'")
    fake = client(_FakeResponse(text=board), _FakeResponse(text=relaid))
    with pytest.raises(GfPageShapeError, match="too few to hold a board"):
        gfid.search_with_ids(_round_trip_filters(), top_n=10)
    assert len(fake.gets) == 1 + pins == 11, fake.gets

    # Every return board carries no `ds:1` at all: read twice with the token
    # and once without it before it is refused.
    fake = client(_FakeResponse(text=board), _FakeResponse(text=_SHAPE_CHANGE_PAGE))
    with pytest.raises(GfPageShapeError, match="no readable ds:1 payload"):
        gfid.search_with_ids(_round_trip_filters(), top_n=10)
    assert len(fake.gets) == 1 + 3 * pins == 31, fake.gets


# One GET, standing in for fli's request timeout: long enough that a waiter
# released early would start its own while the owner's is still in flight, which
# is the thing being counted below.
_SLOW_GET_S = 0.20


def test_a_waiter_does_not_probe_while_the_prober_is_still_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four cabins meet one outage: the wave they had already committed to, then
    the owner's ladder run alone.

    Any rule that lets a waiter go before the owner reports — a clock, a poll, a
    wait that reads running out as an answer — shows up here as a GET starting
    beside one already in flight, which is what the count below reads."""
    # Long enough that the owner's next rung cannot overlap the tail of the
    # opening wave on a loaded machine, short enough that the suite pays little
    # for it: what is being counted is overlap, so the two must not be close.
    monkeypatch.setattr(gfid, "_THROTTLE_BACKOFF_S", 0.05)

    cabins = 4
    starts: list[int] = []
    inflight = {"n": 0}
    guard = threading.Lock()
    first_get, wave = _first_get_of_each_worker(tuple(range(cabins)))

    def slow_and_dead() -> object:
        # The wave is made to assemble rather than left to the scheduler: a
        # worker whose thread starts late would still be in its first GET when
        # the owner begins its second, and the count below would read that as a
        # waiter probing behind the prober.
        _await_the_wave(first_get, wave)
        with guard:
            starts.append(inflight["n"])  # how many GETs were already out
            inflight["n"] += 1
        _real_sleep(_SLOW_GET_S)  # the request timeout burning down
        with guard:
            inflight["n"] -= 1
        raise gfid._RetryableTransportError("read timeout")

    def worker() -> str:
        try:
            gfid.retry_throttled(slow_and_dead)
        except GfTransportError:
            return "unreachable"
        return "served"

    async def fan_out() -> list[str]:
        out: list[str] = []

        async def one() -> None:
            out.append(await anyio.to_thread.run_sync(worker))

        async with anyio.create_task_group() as tg:
            for _ in range(cabins):
                tg.start_soon(one)
        return out

    with gfid.shared_throttle_ladder():
        outcomes = anyio.run(fan_out)

    assert outcomes == ["unreachable"] * cabins
    # The wave each worker had already committed to, then the owner probing
    # ALONE. A GET starting beside another after the wave is a waiter that took
    # a timeout for an answer.
    assert starts[:cabins] == list(range(cabins)), starts
    assert all(n == 0 for n in starts[cabins:]), starts
    one_ladder = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    assert len(starts) == one_ladder + (cabins - 1) == 6, starts


def test_a_payload_too_short_to_hold_a_board_is_a_typed_refusal_not_a_traceback(
    client: Any,
) -> None:
    """Both callers establish the arity before asking, so this guard is defence
    in depth — and a defence nothing measures is one a later reader deletes as
    dead code.

    What it defends is the difference between a refusal that degrades to Matrix
    and an `IndexError` reaching the user, which is the one outcome this module
    exists to prevent."""
    with pytest.raises(GfPageShapeError, match="too few to count a board"):
        gfid._board_row_count([[], []])

    # And through the real path: a decodable blob too short to reach [3] is an
    # answer the caller can act on, not a crash.
    client(_FakeResponse(text=_page(json.dumps([0, 1]))))
    with pytest.raises(GfBackendError):
        gfid._one_call(_FILTERS)
