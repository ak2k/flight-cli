# pyright: reportPrivateUsage=false
"""GF throttle detection on both transports, and the retry that wraps them.

Two transports, two block signals: the date grid still POSTs an RPC and reads a
code-13 error envelope out of the body (`_is_throttle_block`); the search path
GETs a page and reads the final URL and the body (`_is_page_throttled`), because
an outright 429 is raised by fli's client and never reaches that predicate.
`retry_throttled` backs off the same way for both.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, ClassVar, cast, override

import pytest

from flight_cli import _gflight_ids
from flight_cli._gf_errors import GfThrottledError
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
    the body as the only tell. An HTTP 429 never reaches this predicate — fli's
    client raises it (see `_one_call`)."""
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


def test_a_success_refills_both_budgets() -> None:
    ladder = _ladder()
    for _ in range(_gflight_ids._THROTTLE_RETRY_ATTEMPTS):
        ladder.throttled()
    for _ in range(_gflight_ids._TRANSPORT_RETRY_ATTEMPTS):
        ladder.transport_failed()
    assert ladder.throttled() is None
    assert ladder.transport_failed() is None
    ladder.succeeded()
    assert ladder.throttled() is not None
    assert ladder.transport_failed() is not None


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


def test_the_shared_counter_is_not_incremented_by_two_workers_at_once() -> None:
    """Two cabins failing at the same instant must consume two rungs, not one.

    An unguarded read-modify-write hands both workers the same rung, so the
    fan-out issues one more request than the budget authorises — per pair, per
    round, which is exactly the multiplication a shared budget exists to stop.
    Driven through the transport counter because it is the plain one; the same
    lock guards the throttle arm's owner election."""
    ladder = _ladder()
    ladder._transport_spent = _RacingInt(0)
    _RacingInt.barrier.reset()
    threads = [threading.Thread(target=ladder.transport_failed) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)
    assert ladder._transport_spent == 2, "two workers were authorised on one rung"


def test_a_nested_scope_restores_the_ladder_it_replaced() -> None:
    """Scopes nest wherever a fan-out ever runs inside another. An inner scope
    that leaked would leave the outer fan-out spending an inner budget, and one
    that never cleared would leave a later lone search sharing a spent ladder
    with nobody."""
    assert _gflight_ids._fanout_ladder["current"] is None
    with _gflight_ids.shared_throttle_ladder():
        outer = _gflight_ids._fanout_ladder["current"]
        assert outer is not None
        with _gflight_ids.shared_throttle_ladder():
            assert _gflight_ids._fanout_ladder["current"] is not outer
        assert _gflight_ids._fanout_ladder["current"] is outer
    assert _gflight_ids._fanout_ladder["current"] is None


def test_a_scope_left_by_an_exception_still_restores() -> None:
    with contextlib.suppress(RuntimeError), _gflight_ids.shared_throttle_ladder():
        raise RuntimeError("the fan-out failed")
    assert _gflight_ids._fanout_ladder["current"] is None


def test_two_sequential_scopes_do_not_share_a_spent_budget() -> None:
    """One command can fan out more than once. A second fan-out inheriting the
    first one's spent rungs would refuse its first throttle with no retry."""
    ladders: list[Any] = []
    for _ in range(2):
        with _gflight_ids.shared_throttle_ladder():
            ladder = _gflight_ids._fanout_ladder["current"]
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
