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
import contextvars
import threading
import time
from typing import Any, ClassVar, cast, override

import pytest

from flight_cli import _gflight_ids
from flight_cli._gf_errors import GfPageShapeError, GfThrottledError, GfTransportError
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
# A park ends only when the round is released, so a probe that is still alive is
# a probe that parked. Short, because it is paid on every pass of the loop that
# uses it, and the signal it reads is the verdict list rather than the clock.
_PARKED_S = 0.02
# Enough short-lived threads that this platform hands the same id out again —
# 60 sequential threads produced 3 distinct ids when this was measured.
_RECYCLE_ROUNDS = 40
# How long a crossed pair is given to spend its budgets down. Reached only when
# the budgets move without ever running out, which is one of the two failures
# this shape has; the other — a pair that parks on each other and never moves at
# all — is a worker that does not come back, and the join timeout reports that.
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


def _meet_until_refused(meet: Any, deadline: float, rungs: list[float]) -> Any:
    """Keep meeting one wall until its budget says stop, or time runs out.

    Returning None is the wall answering. Anything else means the worker was
    still being handed rungs when the clock ran out, which is what a budget
    that keeps being refunded looks like from outside. Every positive backoff
    handed out on the way is recorded: that count is the budget actually spent,
    which the final `spent` cannot show once a round has been re-elected."""
    while time.monotonic() < deadline:
        verdict = meet()
        if verdict is None:
            return None
        if verdict:
            rungs.append(verdict)
    return "still asking"


def test_two_workers_that_cross_walls_do_not_hold_what_the_other_waits_for() -> None:
    """One worker can meet BOTH walls — a cabin throttled on one attempt and
    reset on the next — and two of them can cross.

    A owns the throttle round and then meets the network; B owns the network
    round and then meets the wall. If a worker parks while still owning the
    round its sibling needs, neither budget ever moves: each waits on a round
    whose owner has stopped probing it, no rung is spent, and nothing exhausts
    to end it. A park ends when the round is released and at no other time, so
    that pair waits for as long as the process lives.

    Every other ladder test drives one arm, which is why this shape had no
    cover. What makes it terminate is giving up a round before waiting on
    another: a worker that has stopped meeting its own wall is not probing it,
    and a round nobody probes must not be one somebody waits for."""
    ladder = _ladder()
    crossed = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    rungs: list[float] = []
    handed = threading.Lock()
    deadline = time.monotonic() + _CROSSED_DEADLINE_S

    def worker(tag: str, own: Any, then_meet: Any) -> None:
        mine: list[float] = []
        try:
            first = own()  # take one round
            if first:
                mine.append(first)
            with contextlib.suppress(threading.BrokenBarrierError):
                crossed.wait(timeout=_JOIN_TIMEOUT_S)  # both own one before either crosses
            outcomes[tag] = _meet_until_refused(then_meet, deadline, mine)
        finally:
            with handed:
                rungs.extend(mine)
            # What `retry_throttled` does in its own `finally`, and the reason
            # it does: a worker that leaves still owning a round strands
            # whoever is waiting on it. Without this the test would be
            # measuring the harness rather than the ladder.
            ladder.stand_down()

    # Daemons, because the failure this test exists for is a pair that never
    # comes back: the assertion below reports it, and a non-daemon pair parked
    # on each other would hold the interpreter open after the report.
    threads = [
        threading.Thread(
            target=worker,
            args=("owns-the-wall", ladder.throttled, ladder.transport_failed),
            daemon=True,
        ),
        threading.Thread(
            target=worker,
            args=("owns-the-network", ladder.transport_failed, ladder.throttled),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)

    assert not any(t.is_alive() for t in threads), "a worker never came back"
    assert outcomes == {"owns-the-wall": None, "owns-the-network": None}, outcomes
    # One ladder per arm and no more. Giving a round UP is not giving its budget
    # back: `refill()` here instead of `release()` is the plausible misreading —
    # "I have stopped probing, so take the rungs back" — and it silently turns a
    # budget spent on evidence into one a crossing refunds. The final `spent`
    # cannot see it, because the round is re-elected from zero either way; the
    # rungs handed out can.
    one_of_each = _gflight_ids._THROTTLE_RETRY_ATTEMPTS + _gflight_ids._TRANSPORT_RETRY_ATTEMPTS
    assert len(rungs) == one_of_each, rungs


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


@pytest.mark.parametrize(
    ("arm", "budget_name"),
    [
        pytest.param("net", "_TRANSPORT_RETRY_ATTEMPTS", id="network"),
        pytest.param("wall", "_THROTTLE_RETRY_ATTEMPTS", id="throttle"),
    ],
)
def test_a_call_ends_on_either_arm_while_a_sibling_keeps_refilling(
    arm: str, budget_name: str
) -> None:
    """Both arms, because both are refillable from underneath.

    The wall's refill is unconditional and deliberate — it is per-IP, so any
    sibling getting through is evidence it lifted — which means on a flapping
    wall the ladder hands this call a fresh budget between every rung and the
    per-call count is the ONLY exit. The network's refill is narrower, so that
    arm has two guards and the wall has one — the wall is the arm where the
    per-call count is the only thing standing."""
    attempts = {"n": 0}

    def always_fail() -> object:
        attempts["n"] += 1
        bound = _gflight_ids._fanout_ladder.get()
        assert bound is not None, "the scope should have bound a ladder"
        bound.succeeded(network=arm == "net")  # a sibling got through
        if arm == "net":
            raise _gflight_ids._RetryableTransportError("connection reset by peer")
        raise GfThrottledError("rate-limited")

    with (
        _gflight_ids.shared_throttle_ladder(),
        pytest.raises((GfTransportError, GfThrottledError)),
    ):
        _gflight_ids.retry_throttled(always_fail)

    assert attempts["n"] == getattr(_gflight_ids, budget_name) + 1, attempts


def test_an_owner_that_raises_releases_the_round_it_was_probing() -> None:
    """A worker can leave a round by a door that is not the ladder's — a shape
    error, a consent wall, an exception from the call itself.

    It is still the owner when it goes, and a waiter parked on its outcome would
    otherwise hold until the ceiling: two minutes per attempt at the shipped
    values, for a probe that will never report. `retry_throttled` gives the
    round back in a `finally` for that reason, and this drives the real function
    rather than standing in for it — the crossing test hand-rolls the same
    stand-down in its own harness, so it cannot see this.

    What is pinned is that the round comes BACK, not which way the sibling then
    takes it: released while parked and found free are the same guarantee."""
    started = threading.Barrier(2)
    outcome: list[Any] = []

    def owner() -> None:
        def boom() -> object:
            # Neither a throttle nor a transport failure, so the loop does not
            # catch it: the owner leaves by a door that spends no rung and
            # reaches no release of its own. Exhausting the budget instead would
            # release the round anyway and prove nothing about the `finally`.
            raise GfPageShapeError("the page changed")

        with contextlib.suppress(GfPageShapeError):
            _gflight_ids.retry_throttled(boom)

    def sibling(ladder: Any) -> None:
        with contextlib.suppress(threading.BrokenBarrierError):
            started.wait(timeout=_JOIN_TIMEOUT_S)
        outcome.append(ladder.throttled())

    with _gflight_ids.shared_throttle_ladder():
        ladder = _gflight_ids._fanout_ladder.get()
        assert ladder is not None
        assert ladder.throttled() is not None  # this thread owns the wall
        # Daemon: without the stand-down this waiter never returns, and a
        # failing run must not wedge the interpreter on the way out.
        waiter = threading.Thread(target=sibling, args=(ladder,), daemon=True)
        waiter.start()
        with contextlib.suppress(threading.BrokenBarrierError):
            started.wait(timeout=_JOIN_TIMEOUT_S)
        owner()  # runs the real loop, which stands down in its finally
        waiter.join(timeout=_JOIN_TIMEOUT_S)

    assert not waiter.is_alive(), "the waiter was left holding a probe that never reported"
    # Either shape proves the round came back: released while parked (0.0), or
    # found free and taken (a rung). Being stranded is the failure, and it shows
    # as no verdict at all.
    assert len(outcome) == 1 and outcome[0] is not None, outcome


def test_the_internal_retry_marker_is_a_transport_error() -> None:
    """Its base decides two things at once: how it renders if it ever escaped an
    un-laddered path, and which arm of the pin loop it lands in.

    Under the plain base it renders as "declined the request", which is the one
    sentence the transport arm exists to stop, and it lands in the
    refuse-and-continue arm rather than the stop rule — so one unreachable
    network would be met once per pin."""
    assert issubclass(_gflight_ids._RetryableTransportError, GfTransportError)


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

    def is_alive(self) -> bool:
        """Alive: this stands in for a worker that is still out probing, which
        is the only state in which a rung is handed to anybody at all."""
        return True


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


def test_a_call_with_no_attempts_left_does_not_park_for_the_prober() -> None:
    """A call whose own attempts are spent cannot use a backoff, so waiting for
    one is pure latency: the owner's remaining ladder is several full request
    timeouts, and the answer arrives after this call has already given up. The
    park is also unbounded from here — nothing but the owner's report ends it."""
    ladder = _ladder()
    holding = threading.Event()
    took_it = threading.Event()

    def prober() -> None:
        ladder.transport_failed()  # owns the round
        took_it.set()
        holding.wait(timeout=_JOIN_TIMEOUT_S)  # still out, the way a GET is

    owner = threading.Thread(target=prober, daemon=True)
    owner.start()
    assert took_it.wait(timeout=_JOIN_TIMEOUT_S), "the prober never took the round"

    verdict: list[Any] = []
    asked = threading.Event()

    def spent_caller() -> None:
        asked.set()
        verdict.append(ladder.transport_failed(final=True))

    caller = threading.Thread(target=spent_caller, daemon=True)
    caller.start()
    assert asked.wait(timeout=_JOIN_TIMEOUT_S)
    caller.join(timeout=_JOIN_TIMEOUT_S)

    assert not caller.is_alive(), "a call with nothing left to spend parked anyway"
    assert verdict == [None], verdict
    holding.set()
    owner.join(timeout=_JOIN_TIMEOUT_S)


def test_the_caller_tells_the_ladder_when_an_attempt_is_its_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry_throttled` is where "last attempt" is known, so it is where the
    ladder has to be told.

    Told nothing, a call whose own budget is spent asks for a backoff like any
    other and parks on whichever prober is out — several full request timeouts
    of waiting for a number it will throw away. Driven through the real caller,
    because the ladder alone cannot know."""
    with _gflight_ids.shared_throttle_ladder():
        ladder = _gflight_ids._fanout_ladder.get()
        assert ladder is not None
        holding = threading.Event()
        took_it = threading.Event()

        def prober() -> None:
            ladder.transport_failed()  # owns the round
            took_it.set()
            holding.wait(timeout=_JOIN_TIMEOUT_S)  # still out, the way a GET is

        owner = threading.Thread(target=prober, daemon=True)
        owner.start()
        assert took_it.wait(timeout=_JOIN_TIMEOUT_S), "the prober never took the round"

        # Patched AFTER the ladder was built, so the shared round keeps the
        # rungs the prober is climbing and only THIS call is out of attempts.
        monkeypatch.setattr(_gflight_ids, "_TRANSPORT_RETRY_ATTEMPTS", 0)
        outcome: list[str] = []

        def spent_caller() -> None:
            def always_resets() -> object:
                raise _gflight_ids._RetryableTransportError("connection reset by peer")

            try:
                _gflight_ids.retry_throttled(always_resets)
            except GfTransportError:
                outcome.append("unreachable")

        # The fan-out's ladder rides a ContextVar, and a bare thread starts
        # with an empty context — it would build a ladder of its own, own it,
        # and never park at all. `anyio.to_thread.run_sync` copies the caller's
        # context in production; this is that copy, made explicit.
        in_the_fanout = contextvars.copy_context()
        caller = threading.Thread(target=lambda: in_the_fanout.run(spent_caller), daemon=True)
        caller.start()
        caller.join(timeout=_JOIN_TIMEOUT_S)

        parked = caller.is_alive()
        holding.set()
        owner.join(timeout=_JOIN_TIMEOUT_S)
        caller.join(timeout=_JOIN_TIMEOUT_S)
        assert not parked, "a call with no attempts left parked for the prober"
        assert outcome == ["unreachable"], outcome


def test_a_last_attempt_still_books_the_rung_that_ends_the_round() -> None:
    """The other half: a caller with nothing left is still the owner of a round,
    and the rung it is about to walk away from is the one that says the wall has
    been measured to the end.

    Left unbooked, the round is merely released — so every waiter wakes to a
    budget that looks unspent and pays a GET each to learn what this call
    already knows. What that costs is in the budget section of
    docs/memories/gf_routing_and_carriers.md."""
    ladder = _ladder()
    for _ in range(_gflight_ids._TRANSPORT_RETRY_ATTEMPTS):
        assert ladder.transport_failed() is not None  # this thread owns the round
    assert ladder.transport_failed(final=True) is None

    sibling: list[Any] = []
    asked = threading.Event()

    def meet_the_same_wall() -> None:
        asked.set()
        sibling.append(ladder.transport_failed())

    t = threading.Thread(target=meet_the_same_wall, daemon=True)
    t.start()
    assert asked.wait(timeout=_JOIN_TIMEOUT_S)
    t.join(timeout=_JOIN_TIMEOUT_S)
    assert not t.is_alive(), "the round was released rather than ended, so a sibling parked on it"
    assert sibling == [None], sibling


def test_a_round_left_by_a_dead_thread_is_taken_over_and_not_inherited() -> None:
    """Ownership is a thread, not its id, and this is the difference.

    Thread ids are unique only among LIVE threads: this platform hands the same
    one to a later worker within a few dozen short-lived threads. A worker given
    a dead owner's ID would be MISTAKEN for it — it would answer `owner == me`,
    keep booking rungs as the owner and never be able to park as a waiter, on a
    wall it has not met. Holding the thread object cannot be confused that way,
    since the dead thread is kept alive by the round that names it.

    What a later worker gets instead is the round in its own name: the dead
    owner is one that will never report, so the round is released and taken
    over rather than left standing. Both halves are asserted, because a pass
    that only showed a verdict coming back would be equally true of the
    confusion this exists to rule out."""
    collided = False
    for _ in range(_RECYCLE_ROUNDS):
        ladder = _ladder()
        # A thread that takes the round and dies still owning it — the one shape
        # the `finally` cannot cover.
        dead = threading.Thread(target=ladder.throttled)
        dead.start()
        dead.join(timeout=_JOIN_TIMEOUT_S)

        verdict: list[Any] = []
        idents: list[int] = [cast("int", dead.ident)]
        asked = threading.Event()

        def probe(
            verdict: list[Any] = verdict, idents: list[int] = idents, asked: threading.Event = asked
        ) -> None:
            asked.set()
            idents.append(threading.get_ident())
            verdict.append(ladder.throttled())  # noqa: B023 — one ladder per pass, by construction

        later = threading.Thread(target=probe, daemon=True)
        later.start()
        assert asked.wait(timeout=_JOIN_TIMEOUT_S)
        later.join(timeout=_JOIN_TIMEOUT_S)
        assert not later.is_alive(), "a round nobody can report on left a worker parked"
        collided = collided or idents[0] == idents[1]
        assert verdict != [], "the later worker never got an answer"
        assert ladder._wall.owner is later, (
            f"the round is not in the later worker's name: {ladder._wall.owner!r}"
        )

    if not collided:
        # The id half of this is only under test where an id is actually reused.
        # Said out loud, because a probabilistic precondition that goes unmet in
        # silence is a test reporting a pass it did not measure.
        pytest.skip("this platform does not recycle thread ids")


def test_a_waiter_is_let_go_by_the_next_worker_to_meet_the_wall() -> None:
    """The park carries no clock, so a round nobody can report on is a park
    nothing ends.

    `retry_throttled`'s `finally` stands an owner down through every ordinary
    door, so reaching this needs a thread that left by one there is no `finally`
    for. The cost of being wrong is a worker parked for the life of the process,
    against one comparison to rule it out — and the worker already parked is the
    one that cannot rescue itself, since it is inside the wait rather than
    asking for a rung."""
    ladder = _ladder()
    holding = threading.Event()
    took_it = threading.Event()

    def prober() -> None:
        ladder.throttled()  # owns the round
        took_it.set()
        holding.wait(timeout=_JOIN_TIMEOUT_S)  # still out, the way a GET is

    owner = threading.Thread(target=prober, daemon=True)
    owner.start()
    assert took_it.wait(timeout=_JOIN_TIMEOUT_S), "the prober never took the round"

    verdict: list[Any] = []
    asked = threading.Event()

    def waits() -> None:
        asked.set()
        verdict.append(ladder.throttled())

    waiter = threading.Thread(target=waits, daemon=True)
    waiter.start()
    assert asked.wait(timeout=_JOIN_TIMEOUT_S)
    waiter.join(timeout=_PARKED_S)
    assert waiter.is_alive(), "the waiter never parked on the owner's outcome"

    # The owner leaves without standing the round down.
    holding.set()
    owner.join(timeout=_JOIN_TIMEOUT_S)
    assert not owner.is_alive(), "the prober never left"

    # A later worker meets the same wall, finds an owner that will never report,
    # and ends the round rather than joining the queue behind it.
    later = threading.Thread(target=ladder.throttled, daemon=True)
    later.start()
    later.join(timeout=_JOIN_TIMEOUT_S)
    waiter.join(timeout=_JOIN_TIMEOUT_S)
    assert not waiter.is_alive(), "the waiter is still parked on a round nobody will report on"
    assert verdict == [0.0], f"a released waiter retries at once: {verdict}"


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
