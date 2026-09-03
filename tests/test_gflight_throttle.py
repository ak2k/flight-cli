# pyright: reportPrivateUsage=false
"""GF throttle detection on both transports, and the retry that wraps them.

Two transports, two block signals: the date grid still POSTs an RPC and reads a
code-13 error envelope out of the body (`_is_throttle_block`); the search path
GETs a page and reads the final URL and the body (`_is_page_throttled`); an
outright 429 is a third shape, read straight off the response by `_one_call`.
`retry_throttled` backs off the same way for all of them, against a ladder that
one fan-out shares.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, ClassVar, cast, override

import pytest

from flight_cli import _gflight_ids
from flight_cli._gf_errors import GfThrottledError, GfTransportError
from flight_cli._gflight_ids import _is_consent_page, _is_page_throttled, _is_throttle_block

# A genuine RPC throttle body: HTTP 200 wrapper with a code-13 ErrorResponse.
_BLOCK_BODY = (
    ')]}\'\n\n[["wrb.fr",null,null,null,null,[13,null,'
    '[["type.googleapis.com/travel.frontend.flights.ErrorResponse",[[null]]]]]]]'
)
_FILTERS = cast("Any", None)  # patched _one_call ignores its arg
# Long enough that a loaded machine cannot fail a test that is really passing,
# short enough that a genuine hang is reported rather than waited out.
_JOIN_TIMEOUT_S = 5.0
# Deliberately short: with mutual exclusion this timeout IS the pass, so the
# suite pays it once.
_BARRIER_TIMEOUT_S = 0.3
# Short enough that a worker parked on a round it should never have parked on
# is a test that fails in seconds rather than one that waits out the real
# ceiling. Passing must not depend on a park timing out.
_CROSSED_CEILING_S = 0.05
# How long a crossed pair is given to spend its budgets down. Reached only when
# neither budget moves, which is the failure.
_CROSSED_DEADLINE_S = 2.0


@pytest.fixture(autouse=True)
def _no_sleep_no_jitter(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(*_a: object) -> None:
        return None

    def _zero() -> float:
        return 0.0

    monkeypatch.setattr(_gflight_ids.time, "sleep", _noop)
    monkeypatch.setattr(_gflight_ids.random, "random", _zero)


# ──────────────────── detection: RPC body (date grid) ──────────────────


def test_is_throttle_block_true_on_error_envelope() -> None:
    assert _is_throttle_block(_BLOCK_BODY)


def test_is_throttle_block_false_on_empty_or_data() -> None:
    assert not _is_throttle_block("")
    assert not _is_throttle_block(')]}\'\n[["wrb.fr",null,"realpayloadhere"]]')


# ──────────────────── detection: search page (search) ──────────────────


def test_is_page_throttled_on_sorry_redirect() -> None:
    assert _is_page_throttled(
        final_url="https://www.google.com/sorry/index?continue=x", html="<html>captcha</html>"
    )


def test_is_page_throttled_on_the_interstitial_served_in_place() -> None:
    """Google also serves the block at the requested URL with HTTP 200, leaving
    the body as the only tell. An HTTP 429 never reaches this predicate: it is a
    status, and `_one_call` reads it off the response itself."""
    assert _is_page_throttled(
        final_url="https://www.google.com/travel/flights?tfs=abc",
        html="<html>Our systems have detected unusual traffic</html>",
    )


def test_is_page_throttled_false_on_a_served_page() -> None:
    assert not _is_page_throttled(
        final_url="https://www.google.com/travel/flights?tfs=abc", html="<html>results</html>"
    )


def test_is_consent_page_on_the_interstitial() -> None:
    assert _is_consent_page(final_url="https://consent.google.com/m?continue=x", html="")
    assert _is_consent_page(final_url="", html='<form action="https://consent.google.com/save">')


def test_is_consent_page_false_on_an_ordinary_page() -> None:
    assert not _is_consent_page(final_url="https://www.google.com/travel/flights", html="<html>")


_MALFORMED_URL = "https://[::1/travel/flights"  # an unclosed IPv6 literal


def test_a_malformed_final_url_classifies_as_neither_wall() -> None:
    """`urlsplit` raises on an authority it cannot parse, and the final URL
    comes back from a redirect we did not build. Both classifiers already
    answer "no" for a URL carrying no marker; a URL nobody can read is the same
    answer, not a traceback out of the middle of a search."""
    assert not _is_page_throttled(final_url=_MALFORMED_URL, html="<html>results</html>")
    assert not _is_consent_page(final_url=_MALFORMED_URL, html="<html>results</html>")


def test_a_malformed_url_still_lets_the_body_decide() -> None:
    """Falling through must reach the body signals, not short-circuit past
    them: the interstitial served in place is only visible there."""
    assert _is_page_throttled(
        final_url=_MALFORMED_URL,
        html="<html>Our systems have detected unusual traffic</html>",
    )
    assert _is_consent_page(
        final_url=_MALFORMED_URL,
        html='<form action="https://consent.google.com/save">',
    )


# ─────────────────────────── throttle retry ────────────────────────────


def test_retry_recovers_from_transient_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    data: list[Any] = [object()]

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise GfThrottledError("throttled")
        return data

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) is data
    assert calls["n"] == 3  # two blocks (backoff+retry) then success


def test_retry_raises_when_throttle_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        raise GfThrottledError("throttled")

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    with pytest.raises(GfThrottledError):
        _gflight_ids._one_call_with_retry(_FILTERS)
    assert calls["n"] == _gflight_ids._THROTTLE_RETRY_ATTEMPTS + 1


def test_search_path_does_not_retry_a_parsed_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page either decodes or raises, so an empty board is Google's answer.
    Retrying it would re-fetch megabytes to be told the same thing."""
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) == []
    assert calls["n"] == 1


# ────────────────── the shared ladder, as an object ────────────────────


def _ladder() -> Any:
    return _gflight_ids._SharedThrottleLadder()


def test_the_first_worker_to_be_throttled_owns_the_backoff() -> None:
    """One worker climbs the rungs and its retry is the probe. A second rung
    handed to a second worker would be two clients backing off against one
    per-IP wall on schedules that do not know about each other."""
    ladder = _ladder()
    rungs = [ladder.throttled() for _ in range(_gflight_ids._THROTTLE_RETRY_ATTEMPTS)]
    assert all(r is not None and r > 0 for r in rungs)
    assert ladder.throttled() is None  # the budget is spent, and says so


def test_a_sibling_waits_for_the_owner_rather_than_backing_off_itself() -> None:
    """The whole point of one prober. A sibling that slept its own schedule
    would probe the same wall a second time, which is the cost the shared
    ladder exists to remove."""
    ladder = _ladder()
    assert ladder.throttled() is not None  # this thread owns the ladder
    outcome: list[Any] = []
    sibling = threading.Thread(target=lambda: outcome.append(ladder.throttled()))
    sibling.start()
    sibling.join(timeout=0.2)
    assert sibling.is_alive(), "the sibling ran its own backoff instead of waiting"
    ladder.succeeded()  # the owner's probe got through
    sibling.join(timeout=_JOIN_TIMEOUT_S)
    assert not sibling.is_alive(), "the sibling was never released"
    assert outcome == [0.0], "a released sibling retries at once, it does not sleep again"


def test_a_sibling_released_by_an_exhausted_ladder_does_not_spend_a_request() -> None:
    """When the prober has just measured the wall to the end of the budget,
    the answer for everyone else is already known and another GET only pays to
    be told it again."""
    ladder = _ladder()
    outcome: list[Any] = []
    for _ in range(_gflight_ids._THROTTLE_RETRY_ATTEMPTS):
        ladder.throttled()
    sibling = threading.Thread(target=lambda: outcome.append(ladder.throttled()))
    sibling.start()
    sibling.join(timeout=0.2)
    assert sibling.is_alive()
    assert ladder.throttled() is None  # the owner exhausts and releases
    sibling.join(timeout=_JOIN_TIMEOUT_S)
    assert outcome == [None], "the sibling must raise rather than retry"


def test_an_owner_leaving_by_another_door_does_not_strand_its_waiters() -> None:
    """A prober whose call ends in a shape error or a consent wall never
    reports on the throttle. Holding waiters for a verdict that will not come
    is a hung fan-out, which is worse than the request it would save."""
    ladder = _ladder()
    assert ladder.throttled() is not None
    outcome: list[Any] = []
    sibling = threading.Thread(target=lambda: outcome.append(ladder.throttled()))
    sibling.start()
    sibling.join(timeout=0.2)
    assert sibling.is_alive()
    ladder.stand_down()
    sibling.join(timeout=_JOIN_TIMEOUT_S)
    assert outcome == [0.0]


def _spend_both(ladder: Any) -> None:
    for _ in range(_gflight_ids._THROTTLE_RETRY_ATTEMPTS):
        ladder.throttled()
    for _ in range(_gflight_ids._TRANSPORT_RETRY_ATTEMPTS):
        ladder.transport_failed()
    assert ladder.throttled() is None
    assert ladder.transport_failed() is None


def test_any_success_refills_the_wall_because_the_wall_is_shared() -> None:
    """The throttle is per-IP, so a call getting through is evidence it lifted
    whoever made it. Releasing those waiters is what serves a whole fan-out on
    a wall that lifts inside the ladder, rather than the one cabin probing."""
    ladder = _ladder()
    _spend_both(ladder)
    ladder.succeeded()
    assert ladder.throttled() is not None, "the wall was measured open and stayed shut"


def test_a_success_refills_the_network_only_for_the_worker_that_met_it() -> None:
    """The socket is not shared the way the wall is: fli's session is a
    `threading.local`, so the connection that carried a sibling's call says
    nothing about this one's.

    Refilling it for everyone means every healthy sibling hands a failing
    worker another rung — a read timeout on one cabin's board is then retried
    for as long as the other cabins keep succeeding, which is no bound at
    all."""
    ladder = _ladder()
    _spend_both(ladder)
    ladder.succeeded()
    assert ladder.transport_failed() is None, "a sibling's success refilled someone else's socket"
    ladder.succeeded(network=True)
    assert ladder.transport_failed() is not None, "the worker that met it got no rungs back"


class _RacingInt(int):
    """An int whose addition parks until a second thread reaches the same line.

    The barrier is what makes the race deterministic instead of a matter of
    scheduling luck: without mutual exclusion both threads meet inside the
    increment and it releases; with it, only one thread can be there and the
    wait times out."""

    barrier: ClassVar[threading.Barrier] = threading.Barrier(2)

    @override
    def __add__(self, other: int) -> int:
        with contextlib.suppress(threading.BrokenBarrierError):
            self.barrier.wait(timeout=_BARRIER_TIMEOUT_S)
        return int(self) + other


def _meet_until_refused(meet: Any, deadline: float) -> Any:
    """Keep meeting one wall until its budget says stop, or time runs out.

    Returning None is the wall answering. Anything else means the worker was
    still asking when the clock ran out, which is what a pair holding each
    other's rounds looks like from outside."""
    while time.monotonic() < deadline:
        if meet() is None:
            return None
    return "still asking"


def test_two_workers_that_cross_walls_do_not_hold_what_the_other_waits_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One worker can meet BOTH walls — a cabin throttled on one attempt and
    reset on the next — and two of them can cross.

    A owns the throttle round and then meets the network; B owns the network
    round and then meets the wall. If a worker parks while still owning the
    round its sibling needs, neither budget ever moves: the park times out, the
    round it is waiting on is no more exhausted than before, so it is told to
    retry, meets the other wall again and parks again. No rungs are spent, so
    nothing ever exhausts to end it — and it costs a multi-megabyte GET per
    worker per timeout, forever.

    Every other ladder test drives one arm, which is why this shape had no
    cover. What makes it terminate is giving up a round before waiting on
    another: a worker that has stopped meeting its own wall is not probing it,
    and a round nobody probes must not be one somebody waits for."""
    # A park must never be how this passes, and a failing run must not leave
    # two threads asleep for two minutes after the assertion.
    monkeypatch.setattr(_gflight_ids, "_LADDER_WAIT_CEILING_S", _CROSSED_CEILING_S)
    ladder = _ladder()
    crossed = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    deadline = time.monotonic() + _CROSSED_DEADLINE_S

    def worker(tag: str, own: Any, then_meet: Any) -> None:
        try:
            own()  # take one round
            with contextlib.suppress(threading.BrokenBarrierError):
                crossed.wait(timeout=_JOIN_TIMEOUT_S)  # both own one before either crosses
            outcomes[tag] = _meet_until_refused(then_meet, deadline)
        finally:
            # What `retry_throttled` does in its own `finally`, and the reason
            # it does: a worker that leaves still owning a round strands
            # whoever is waiting on it. Without this the test would be
            # measuring the harness rather than the ladder.
            ladder.stand_down()

    threads = [
        threading.Thread(
            target=worker, args=("owns-the-wall", ladder.throttled, ladder.transport_failed)
        ),
        threading.Thread(
            target=worker, args=("owns-the-network", ladder.transport_failed, ladder.throttled)
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)

    assert not any(t.is_alive() for t in threads), "a worker never came back"
    assert outcomes == {"owns-the-wall": None, "owns-the-network": None}, outcomes


def test_a_call_ends_even_while_a_sibling_keeps_refilling_the_budget() -> None:
    """The shared ladder is not a ceiling on its own, because it is refillable.

    A sibling that met the network and then got through returns that round's
    rungs — right for the sibling, and it hands this call a fresh budget every
    time. Checked only against the ladder, the loop has no exit: the round is
    never exhausted when it is asked. The per-call count is what ends it, and
    it is the same shape the empty-result retry already uses.

    Driven with the refill inside the failing call, which is the interleaving a
    fan-out produces and the one nothing else in this file reaches."""
    attempts = {"n": 0}

    def always_reset() -> object:
        attempts["n"] += 1
        bound = _gflight_ids._fanout_ladder.get()
        assert bound is not None, "the scope should have bound a ladder"
        bound.succeeded(network=True)  # a sibling got through on its own socket
        raise _gflight_ids._RetryableTransportError("connection reset by peer")

    with (
        _gflight_ids.shared_throttle_ladder(),
        pytest.raises(GfTransportError),
    ):
        _gflight_ids.retry_throttled(always_reset)

    assert attempts["n"] == _gflight_ids._TRANSPORT_RETRY_ATTEMPTS + 1, attempts


class _EveryThread:
    """An owner that compares equal to whichever worker asks.

    A rung is handed out under `owner == me`, so this opens that gate for both
    workers at once and leaves the increment as the only thing between them —
    which is what the lock is there to protect. Electing a real owner instead
    would park the loser as a waiter and the race would never happen."""

    @override
    def __eq__(self, other: object) -> bool:
        return True

    @override
    def __hash__(self) -> int:
        return 0


@pytest.mark.parametrize(
    ("arm", "meet_the_wall"),
    [
        pytest.param("_wall", "throttled", id="throttle"),
        pytest.param("_net", "transport_failed", id="transport"),
    ],
)
def test_one_rung_is_not_handed_to_two_workers_at_once(arm: str, meet_the_wall: str) -> None:
    """Two cabins failing at the same instant must consume two rungs, not one.

    An unguarded read-modify-write hands both workers the same rung, so the
    fan-out issues one more request than the budget authorises and runs two
    backoff schedules against one wall — per pair, per round, which is the
    multiplication a shared budget exists to stop.

    Both arms, because each keeps its own rungs and the lock is the only thing
    they share: a race pinned on one says nothing about the other."""
    ladder = _ladder()
    round_ = getattr(ladder, arm)
    round_.owner = cast("Any", _EveryThread())
    round_.spent = _RacingInt(0)
    _RacingInt.barrier.reset()
    threads = [threading.Thread(target=getattr(ladder, meet_the_wall)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)
    assert round_.spent == 2, "two workers were authorised on one rung"


def test_a_nested_scope_restores_the_ladder_it_replaced() -> None:
    """Scopes nest wherever a fan-out ever runs inside another. An inner scope
    that leaked would leave the outer fan-out spending an inner budget, and one
    that never cleared would leave a later lone search sharing a spent ladder
    with nobody."""
    assert _gflight_ids._fanout_ladder.get() is None
    with _gflight_ids.shared_throttle_ladder():
        outer = _gflight_ids._fanout_ladder.get()
        assert outer is not None
        with _gflight_ids.shared_throttle_ladder():
            assert _gflight_ids._fanout_ladder.get() is not outer
        assert _gflight_ids._fanout_ladder.get() is outer
    assert _gflight_ids._fanout_ladder.get() is None


def test_a_scope_left_by_an_exception_still_restores() -> None:
    with contextlib.suppress(RuntimeError), _gflight_ids.shared_throttle_ladder():
        raise RuntimeError("the fan-out failed")
    assert _gflight_ids._fanout_ladder.get() is None


def test_two_overlapping_scopes_on_different_threads_do_not_clobber_each_other() -> None:
    """Scopes on one thread nest; scopes on two threads OVERLAP, and a global
    that saves and restores is only correct for the first shape.

    Interleaved, each scope would restore what it saw on entry — the other
    thread's ladder, or None — so both fan-outs end up laddering against a
    budget that is not theirs, and whichever exits last leaves a spent ladder
    bound for every later search in the process. Forced to interleave here,
    because scheduling would usually hide it."""
    entered = threading.Barrier(2)
    seen: dict[str, Any] = {}
    left: list[Any] = []

    def scope(tag: str) -> None:
        with _gflight_ids.shared_throttle_ladder():
            seen[tag] = _gflight_ids._fanout_ladder.get()
            entered.wait(timeout=_JOIN_TIMEOUT_S)  # both scopes open at once
            # Still its own, after the sibling opened one.
            seen[tag + "-after"] = _gflight_ids._fanout_ladder.get()
        left.append(_gflight_ids._fanout_ladder.get())

    threads = [threading.Thread(target=scope, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)

    assert seen["a"] is not seen["b"], "two fan-outs shared one ladder"
    assert seen["a-after"] is seen["a"], "a sibling's scope replaced this one's ladder"
    assert seen["b-after"] is seen["b"], "a sibling's scope replaced this one's ladder"
    assert left == [None, None], f"a scope leaked a ladder on the way out: {left}"


def test_two_sequential_scopes_do_not_share_a_spent_budget() -> None:
    """One command can fan out more than once. A second fan-out inheriting the
    first one's spent rungs would refuse its first throttle with no retry."""
    ladders: list[Any] = []
    for _ in range(2):
        with _gflight_ids.shared_throttle_ladder():
            ladder = _gflight_ids._fanout_ladder.get()
            assert ladder is not None
            ladders.append(ladder)
            for _ in range(_gflight_ids._THROTTLE_RETRY_ATTEMPTS):
                assert ladder.throttled() is not None
            assert ladder.throttled() is None
    assert ladders[0] is not ladders[1]


def test_throttle_backoff_survives_an_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # A throttle then an empty: the throttle is retried, the empty is returned.
    seq: list[Any] = ["throttle", []]
    calls = {"n": 0}

    def fake(_f: Any) -> list[Any]:
        item = seq[calls["n"]]
        calls["n"] += 1
        if item == "throttle":
            raise GfThrottledError("throttled")
        return cast("list[Any]", item)

    monkeypatch.setattr(_gflight_ids, "_one_call", fake)
    assert _gflight_ids._one_call_with_retry(_FILTERS) == []
    assert calls["n"] == 2
