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

import copy
import datetime
import json
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any, ClassVar, cast

import pytest

from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import (
    GfBackendError,
    GfConsentError,
    GfPageShapeError,
    GfThrottledError,
    GfTransportError,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "gflight_page"
_FILTERS = cast("Any", None)  # a patched client never encodes the filter
_real_tfs = gfid.build_search_tfs
_LIFTS_AFTER_BACKOFFS = 2  # how many rungs the owner climbs before the wall lifts


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

    `_one_call` no longer goes through fli's `Client.get`, so nothing has
    called `raise_for_status()` and a 429 arrives as a RESPONSE."""

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
    """The curl_cffi session, which is where the GET now goes.

    Faking `Client.get` instead would skip the code under test: the whole point
    of the change is that `Client.get`'s own retry ladder is no longer in the
    path, so a fake sitting there could not observe the request budget."""

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

    def _stub_tfs(filters: Any) -> bytes:
        """The REAL encoder wherever there are filters to encode.

        A constant here would make every pinned leg of a round trip request the
        same URL, so a fan-out that re-fetched one leg N times would satisfy
        every count this file asserts. The stub survives only for the tests that
        pass no filters at all, which never reach the encoder in production."""
        if filters is None:
            return b"\x08\x1c"
        return _real_tfs(filters)

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


def test_extract_ds1_reads_the_flights_blob_past_other_keys() -> None:
    payload = gfid._extract_ds1(_page(_ds1("ds1_jfk_lax_3rows.json")))
    assert payload is not None
    rows, blocks_seen, misplaced = gfid._rows_from_ds1(payload)
    assert blocks_seen == 2
    assert misplaced == ()
    assert rows == payload[2][0] + payload[3][0]
    assert len(rows) == 3


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
    """`ds:1[2]` is Google's own ranking and `[3]` the rest; concatenating in
    that order is the only way the page's ordering survives — we can't
    reproduce the blended rank locally."""
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
    """THE request budget. fli's `Client.get` retried three times inside each of
    our throttle retries, so one throttled leg cost up to fifteen multi-megabyte
    GETs and a multi-cabin round trip multiplied that again. Ours is now the
    only ladder: one initial GET plus `_THROTTLE_RETRY_ATTEMPTS` retries."""
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

    return (Leg.of("JFK", "LAX", datetime.date.today() + datetime.timedelta(days=45)),)


def _fan_out(
    monkeypatch: pytest.MonkeyPatch, *cabins: Any, slept: list[float] | None = None
) -> tuple[dict[Any, Any], list[float]]:
    """Run the real cabin fan-out with its stderr swallowed, reporting what it
    returned and every backoff it slept.

    `slept` may be supplied so a responder can decide by backoff count."""
    import io

    from rich.console import Console

    from flight_cli import cli
    from flight_cli.domain import SearchOptions

    slept = [] if slept is None else slept
    monkeypatch.setattr(gfid.time, "sleep", slept.append)
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

    slept: list[float] = []

    def wall_that_lifts() -> _FakeResponse:
        if len(slept) >= _LIFTS_AFTER_BACKOFFS:
            return _FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json")))
        return _FakeResponse(text="", status_code=429)

    fake = client(wall_that_lifts)
    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    out, slept_out = _fan_out(monkeypatch, *cabins, slept=slept)

    assert set(out) == set(cabins), f"a cabin was refused a wall that lifted: {sorted(out)}"
    assert all(len(rows) == 3 for rows in out.values())  # the capture's row count
    # One worker owned the backoff; the other three waited on its outcome
    # rather than each sleeping a schedule of their own.
    assert len(slept_out) == _LIFTS_AFTER_BACKOFFS, slept_out  # only the owner backed off
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


def test_a_transport_outage_costs_one_ladder_for_the_WHOLE_cabin_fan_out(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network is one network, so its budget is shared for the same reason
    the wall's is. Per-worker, a single outage cost a transport ladder per
    cabin — and on a round trip, per cabin per pin."""
    from flight_cli.domain import Cabin

    fake = client(_transport_error("connection reset by peer"))
    cabins = (Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS, Cabin.FIRST)
    out, slept = _fan_out(monkeypatch, *cabins)

    assert out == {}
    one_ladder = gfid._TRANSPORT_RETRY_ATTEMPTS + 1
    assert len(fake.gets) <= one_ladder + (len(cabins) - 1) == 6
    assert len(slept) <= gfid._TRANSPORT_RETRY_ATTEMPTS


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
    would put a second ladder under ours, which is the amplification this
    replaced — so the fake client has no `get` at all and a regression here is
    an AttributeError, not a quietly larger request count."""
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


def _board_of(n: int) -> str:
    """A ds:1 page carrying `n` parseable rows, cloned from the real capture."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    row = payload[2][0][0]
    payload[2] = [[copy.deepcopy(row) for _ in range(n)]]
    payload[3] = None
    return _page(json.dumps(payload))


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

    dep = (datetime.date.today() + datetime.timedelta(days=45)).isoformat()
    ret = (datetime.date.today() + datetime.timedelta(days=52)).isoformat()
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
    filters — free on an RPC, not free here. At top_n=50 over a 30-row board
    the round trip costs 1 outbound + 10 pins, not 1 + 30."""
    fake = client(_FakeResponse(text=_board_of(30)))
    out = gfid.search_with_ids(_round_trip_filters(), top_n=50)
    assert out is not None
    assert len(fake.gets) == 1 + gfid._PINNED_FANOUT_CAP == 11


def test_every_pin_asks_for_a_different_return_board(client: Any) -> None:
    """The pins exist to price a DIFFERENT outbound each. Nothing else in this
    file would notice a fan-out that pinned the same leg ten times: the GET
    count, the combination count and the warning counts would all still add
    up, and the user would get ten copies of one itinerary."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))))
    out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(fake.gets) == 4  # the outbound board, then one GET per pin
    assert len(set(fake.gets)) == 4, f"a pin re-requested another pin's URL: {fake.gets}"


def test_the_default_top_n_is_unchanged_by_the_cap(client: Any) -> None:
    """The cap must not narrow an ordinary search: `-n 10` is the default and
    sits exactly on it."""
    fake = client(_FakeResponse(text=_board_of(30)))
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
        _FakeResponse(text=_board_of(1)),  # pin 1 returns
        _FakeResponse(text=_moved_row_page()),  # pin 2 refuses
        _FakeResponse(text=_board_of(1)),  # pin 3 returns
    )
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid.search_with_ids(_round_trip_filters(), top_n=3)
    assert out is not None
    assert len(out) == 2
    assert len(fake.gets) == 4
    assert "1 of 3 return boards unavailable" in caplog.text


def test_a_refusal_on_every_pin_is_still_the_outcome(client: Any) -> None:
    """Continuing past one bad board must not turn a wholly failed round trip
    into a silent `None` the caller renders as "no results"."""
    client(
        _FakeResponse(text=_board_of(3)),
        _FakeResponse(text=_SHAPE_CHANGE_PAGE),  # every pin, forever
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
        _FakeResponse(text=_board_of(1)),  # pin 1 returns
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
        _FakeResponse(text=_board_of(1)),  # pin 1 returns
        _FakeResponse(text=_board_of(1)),  # pin 2 returns
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


def test_every_curl_result_code_gets_a_decision_and_only_these_are_retried() -> None:
    """Enumerated, not sampled. curl_cffi maps 41 result codes onto a dozen
    classes, so a rule written in classes quietly decides for codes nobody
    looked at — which is how a stalled body came to be classified as a bug in
    our own request."""
    from curl_cffi.const import CurlECode
    from curl_cffi.requests import exceptions as curl_exc

    by_code = {getattr(CurlECode, n) for n in _RETRY_BY_CODE}
    for code, cls in curl_exc.CODE2ERROR.items():
        error = cls(f"curl said {code}", code=cast("Any", code))
        reachability = isinstance(error, curl_exc.ConnectionError | curl_exc.Timeout)
        assert gfid._is_transport_failure(error) is (reachability or code in by_code), (
            f"{getattr(code, 'name', code)} ({type(error).__name__}) is classified wrongly"
        )


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
    gives up, and the enriched path waits on it. `_fetch_page` bypasses
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

        gfid._fetch_page(_Client(), "https://www.google.com/travel/flights")
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
