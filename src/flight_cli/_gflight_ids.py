"""Google Flights search that ALSO captures the opaque `flightId` Google emits
at index [17] of each flight row.

fli's row decoder parses legs, price, duration, stops — but drops
`data[0][17]`. PointsPath's `enableGoogleFlightMatching` mode joins its award
catalog against exactly that opaque ID (see PP browser extension
chunk-5KW5VSHS.js: `flightId: a` where `a = n[17]`). Without it, PP returns
an empty result for hint-based queries; with it, `matchedGoogleFlightId`
echoes back populated.

Transport is the PUBLIC SEARCH PAGE, not the `GetShoppingResults` RPC: since
2026-08 that RPC requires an `x-goog-batchexecute-bgr` header signed by the
page's own JavaScript over the exact request bytes, so a plain HTTP client gets
HTTP 200 with a payload-less `wrb.fr` row and error 13 — a refusal shaped
exactly like "no flights on this route". `https://www.google.com/travel/
flights?tfs=…` inlines the identical rows in its `AF_initDataCallback` `ds:1`
blob (`[2]` = Google's top-flights board, `[3]` = the rest), so one row parser
serves both. Verified live 2026-09-02: flight_id at data[0][17], 33-element leg
tuples, leg[13] legroom class present, round-trip pins return correctly-directed
returns with distinct flight_ids.

The page URL asks for the full board (`tfu=`, see
`links.google_flights_search_page_url`), so every row Google has for a leg is
parsed and a top-N is the caller's trim. A multi-airport board stops at
Google's 300 cheapest rows, and says so (`_ROW_CAP`, `Board.capped_at`).

That page has two transports (`GfTransport`, `_one_call_laddered`): rung 1 is
the curl_cffi GET below, rung 2 is a real Chrome navigating the same URL
(`_gf_browser`), which earns a far larger rate budget. Both send the
`HeadlessChrome` UA token, by which Google picks the board it serves a
multi-airport search (`_SEARCH_PAGE_UA`), and both go through
`_rows_from_page_html` — one parser, one set of verdicts about what a block
means. A rung supplies bytes; it never gets to interpret them.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import functools
import itertools
import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, assert_never, cast

from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
)
from fli.models.google_flights.base import TripType  # pyright: ignore[reportMissingTypeStubs]

# DIVERGE: fli's API-row decoders live in a private module, with no public
# equivalent (core.parsers has no datetime parser). Datetimes decode through
# them. Airlines and airports decode through `fli_bridge`'s tables, which keep
# a code fli aliases; an airport reaches `_parse_airport` only for a code fli
# has no entry for, so the row fails with fli's warning and AttributeError.
from fli.search._decoders import (  # pyright: ignore[reportMissingTypeStubs]
    _parse_airport,  # pyright: ignore[reportPrivateUsage]
    _parse_datetime,  # pyright: ignore[reportPrivateUsage]
)
from fli.search.client import (  # pyright: ignore[reportMissingTypeStubs]
    REQUEST_TIMEOUT,
    get_client,
)
from fli.search.flights import SearchFlights  # pyright: ignore[reportMissingTypeStubs]

# Rung 2, imported like anything else. The whole cost of doing so: 0.2 ms, one
# `rich.Console` built at module scope, and one process-wide `atexit` hook that
# does nothing unless a browser was launched. No optional dependency —
# patchright is imported inside `_playwright_factory`, at launch — and the
# transport vocabulary lives in `_gf_common`, so the import below runs one way
# only: `_gf_browser` never reaches back for this module, which is what keeps
# it a leaf rather than half of a cycle. Imported as a module, not
# `from ._gf_browser import session`, so
# `_one_call_browser` looks the attribute up per call and a test can substitute
# the session without a browser anywhere in the process.
from . import _gf_browser
from ._envelope import narrow
from ._gf_common import TRANSPORT_HTTP, GfTransportMode, PageFetch, cache_dir
from ._gf_errors import (
    BROWSER_DEFAULT_REMEDY,
    GfBackendError,
    GfBrowserUnavailableError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfThrottledError,
    GfTransportError,
    GfUpstreamStatusError,
)
from .fli_bridge import fli_airline, fli_airports
from .links import build_search_tfs, google_flights_search_page_url

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Generator, Iterable, Sequence

    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
        FlightSegment,
    )

    from ._gf_postfilter import StopDrops

# DIVERGE: stdlib logging, where the rest of the package uses structlog (see
# AGENTS.md's stack table and `_http.py`). The refusal classification here is
# exercised almost entirely through fixtures, and `caplog` — pytest's own
# capture, which every one of those tests asserts on — sees stdlib records only.
# `log.configure()` attaches a stderr handler to the `flight_cli` logger so
# these lines still reach a `-v`/`-vv` run; without it they go nowhere.
log = logging.getLogger(__name__)

# Google Flights' RPC endpoints intermittently answer a cold curl_cffi session
# with an empty body (HTTP 200, no error to retry on). fli's client is a
# process-wide singleton that warms up by acquiring a cookie on its first
# successful call — but each one-shot `flight` invocation starts a fresh process
# with a cold session, so its single request frequently comes back empty.
# Empirically (None,None,123,123 on identical inputs; [0,123,123,0,123,123] over
# one warming client) the empty almost always clears within a couple of retries
# on the SAME session. A FRESH session does NOT help — it stays cold — so we
# retry in place. The date grid still POSTs an RPC and still needs this; the
# search page does not (see `retry_throttled`'s `retry_empty`).
_EMPTY_RETRY_ATTEMPTS = 4
_EMPTY_RETRY_BACKOFF_S = 1.0  # multiplied by attempt number: 1s, 2s, 3s between tries

# A genuine throttle is distinct from the cold-session empty above: Google
# rate-limits this IP, answering the RPC with an error envelope (code-13 /
# `ErrorResponse`) or the search page with a `/sorry/` captcha. Measured
# 2026-06-14, the limit is DYNAMIC (the ceiling drifts run-to-run) with FAST
# recovery, so a fixed rate cap is the wrong tool: we back off exponentially and
# retry, surfacing GfThrottledError only when that's exhausted (the caller can
# then degrade to Matrix). Backoff is jittered so concurrent one-shot `flight`
# processes — which share the per-IP signal but can't share a budget — don't all
# retry in lockstep and re-trip it.
_THROTTLE_RETRY_ATTEMPTS = 4
_THROTTLE_BACKOFF_S = 1.0  # exponential base: ~1, 2, 4, 8s (plus 0-50% jitter)

# THE transport budget, and the one place it is explained. A reset connection or
# a read timeout is worth a couple of quick retries and nothing more. Nothing
# below it retries one: `_get_search_page` goes to fli's session rather than
# `Client.get` and its three attempts, so a single blip would otherwise fail the
# leg. Deliberately smaller than the throttle budget, because the two failures
# are not alike: a throttle is a wall that lifts on its own, while a transport
# failure surviving three attempts is usually the network being down, and a long
# backoff there only delays the Matrix fallback the user is going to get anyway.
_TRANSPORT_RETRY_ATTEMPTS = 2


def _backoff_for(attempt: int) -> float:
    """The jittered exponential backoff for the given rung.

    Jitter so concurrent one-shot `flight` processes — which share the per-IP
    signal but cannot share a budget — do not retry in lockstep and re-trip it."""
    return _THROTTLE_BACKOFF_S * (2 ** (attempt - 1)) * (1 + random.random() * 0.5)  # noqa: S311 — jitter, not crypto


class _Round:
    """One arm's shared budget: the prober that owns it, the rungs it has spent,
    and the waiters parked on its outcome.

    An arm is a wall of one kind. Two of them ride one ladder because a fan-out
    can meet both at once, and they are kept apart for two reasons that hold:
    one `exhausted` flag across both would answer a transport waiter with the
    throttle's verdict, so the caller names the wrong wall to the user; and one
    worker can own both at once, so each needs its own owner to give up.

    A success releases the WALL's waiters always, since the wall is per-IP and
    any call getting through has measured it. It releases the NETWORK's only for
    the worker that met a transport failure on that call — a sibling's socket is
    not this one's. See `succeeded`, which is where that asymmetry lives.

    The lock lives on the ladder, which holds every round, so a worker that owns
    both takes it once."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.spent = 0
        self.exhausted = False
        # The thread itself, not `get_ident()`: an id is only unique among LIVE
        # threads and this platform hands the same one out again within a few
        # dozen short-lived threads. A round is meant to be released by the
        # thread that took it — `retry_throttled`'s `finally` is what makes that
        # happen on every door out — and holding the object means that even a
        # thread that somehow died still owning one cannot have its identity
        # handed to a later worker, which would then be MISTAKEN for the owner:
        # it would answer `owner == me`, keep booking rungs and never be able to
        # park as a waiter, on a wall it has not met. The round's SPEND is
        # inherited either way, and correctly — the budget is the group's.
        self.owner: threading.Thread | None = None
        # Set when the owner's round resolves. Replaced per round so a waiter
        # cannot be woken by the previous round's result.
        self.settled = threading.Event()

    def release(self) -> None:
        """End the current round. The ladder's lock is held."""
        self.owner = None
        self.settled.set()
        self.settled = threading.Event()

    def refill(self) -> None:
        """Hand the rungs back. The ladder's lock is held."""
        self.spent = 0
        self.exhausted = False
        self.release()


# DIVERGE: a hand-written ladder where the rest of the package would reach for a
# retry decorator (stamina, already a dependency for `_http`'s Matrix calls). A
# decorator retries ONE call against a counter of its own. This budget belongs
# to the per-IP wall, not to a call: several worker threads share it, one of
# them owns the backoff while the others wait on its outcome, and any success
# refills it. There is no decorator that can express state shared sideways
# across threads, so the loop in `retry_throttled` is written out.
class _SharedThrottleLadder:
    """One ladder against the wall, for every worker of a fan-out at once.

    Google's throttle is per-IP, so cabins querying together meet ONE wall.
    Laddering against it separately spends the cabin count times the requests to
    learn a single fact, and gives up at N different moments.

    So exactly one worker OWNS the backoff. It sleeps a rung and retries, and
    that retry is the probe: a worker that gets throttled while an owner exists
    waits for the probe's outcome rather than running a schedule of its own.
    When the probe gets through, every waiter retries at once — a wall that
    lifts inside the ladder serves the whole fan-out, not just whoever happened
    to be probing. When the rungs run out, the waiters raise without spending a
    request on a wall that has just been measured.

    Any successful call RESETS THE WALL, so the ceiling is a statement about a
    wall nothing is getting through — and each CALL carries its own attempt
    count too, because a refillable shared budget cannot bound one. The
    arithmetic and the measured costs live once, in the budget section of
    docs/memories/gf_routing_and_carriers.md.

    The transport budget rides the same object because the network is one
    network, and it probes the same way: `_is_transport_failure` admits only the
    families that DO clear, so a waiter has an outcome worth waiting for. It
    does NOT reset the same way — see `succeeded`. The two arms stay separate
    rounds; only the lock is shared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wall = _Round(_THROTTLE_RETRY_ATTEMPTS)
        self._net = _Round(_TRANSPORT_RETRY_ATTEMPTS)

    def throttled(self, *, final: bool = False) -> float | None:
        """This worker's call came back throttled. How long to wait before
        trying again, or None when the shared budget is gone — or whenever it is
        `final`; see `_step`.

        A non-owner blocks here for the owner's outcome and then retries
        immediately, so its wait is the owner's backoff rather than one of its
        own."""
        return self._step(self._wall, final=final)

    def transport_failed(self, *, final: bool = False) -> float | None:
        """A curl-level failure. How long to wait before trying again, or None
        when the shared transport budget is gone — or whenever it is `final`;
        see `_step`.

        One prober here too: four cabins whose sockets reset together would
        otherwise eat the whole budget before any retry lands, and the ones that
        arrive last are refused after a single try — their columns then go
        missing from a table that cannot say why."""
        return self._step(self._net, final=final)

    def _step(self, round_: _Round, *, final: bool = False) -> float | None:
        """One worker's turn on one arm: own the round and get a rung, or park
        on the owner's outcome and retry the moment it reports.

        `final` is a caller whose OWN attempts are spent. It cannot use a
        backoff, so it neither parks — the answer would arrive after it has
        already given up, having cost it the owner's whole remaining ladder in
        latency — nor takes a round it will not probe. What it still does is
        spend the rung of a round it already owns: that spend is how a group
        learns the wall has been measured to the end, and an owner that walked
        away without booking it would leave every waiter to find an unexhausted
        round and spend a GET each proving what this call already knows.

        A `final` owner whose spend does not exhaust the round leaves it owned
        and unreleased on the way out: it raises on the line after this returns,
        and `retry_throttled`'s `finally` is what stands it down and frees
        whoever is parked on it. Nothing in this class does that for it."""
        me = threading.current_thread()
        with self._lock:
            if round_.exhausted:
                return None
            if round_.owner is not None and not round_.owner.is_alive():
                # An owner that will never report. `retry_throttled`'s `finally`
                # covers every door out of an ordinary call, so reaching here
                # means the thread left by a door there is no `finally` for —
                # and the wait below carries no clock, so a round nobody can
                # end is a park nothing ends. The trade is one comparison and,
                # for whoever takes the round next, one GET against a wall this
                # round had already measured; the alternative is a worker
                # parked for the life of the process. What this does NOT reach
                # is a worker already inside the wait: it is freed by the next
                # worker to ask for a rung, and a two-worker fan-out has none.
                round_.release()
            if round_.owner is None and not final:
                round_.owner = me
            if round_.owner == me:
                round_.spent += 1
                if round_.spent > round_.budget:
                    round_.exhausted = True
                    round_.release()
                    return None
                return None if final else _backoff_for(round_.spent)
            if final:
                return None
            # About to wait on someone else's round. A worker that has stopped
            # meeting the wall it owns is not probing it any more, and a round
            # nobody is probing must not be one somebody else is waiting for:
            # two workers that each hold what the other waits for never move,
            # and no budget is spent to end it.
            #
            # This is the PARK path only. A worker that crosses and finds the
            # other round free takes it and never reaches here, so it holds both
            # until `retry_throttled`'s `finally` stands it down — that is the
            # other half of the same guarantee, not a spare.
            for other in (self._wall, self._net):
                if other is not round_ and other.owner == me:
                    other.release()
            settled = round_.settled
        # No clock: `release()` sets the very event held here, so waking is the
        # owner reporting and nothing else can end this wait. What bounds it is
        # the caller's own attempt count, and what guarantees the report arrives
        # is `retry_throttled`'s `finally`, which stands an owner down whatever
        # door it leaves by. The reasoning and the elapsed bounds live in the
        # budget section of docs/memories/gf_routing_and_carriers.md.
        settled.wait()
        with self._lock:
            return None if round_.exhausted else 0.0

    def succeeded(self, *, network: bool = False) -> None:
        """A call got through: return the rungs it earned back and release the
        waiters that were parked on them.

        The WALL always. It is per-IP, so any call getting through is evidence
        it lifted whoever made it, and releasing the waiters is what turns a
        wall that lifts inside the ladder into a fan-out that is served rather
        than one cabin that happened to be probing.

        The NETWORK only for a worker that met a transport failure on this
        call. fli's session is a `threading.local`, so the socket that carried
        this call is this worker's own and is no evidence about a sibling's.
        Refilling it for everyone lets each healthy sibling hand a failing
        worker another rung, so a per-request fault — a read timeout on one
        cabin's multi-megabyte board is the ordinary shape — retries for as long
        as the siblings keep succeeding, which is no bound at all.

        The cost on the other side: one ladder is one budget for the whole
        fan-out, so a cabin that keeps
        failing spends rungs its siblings would have had, and a group under a
        transport outage gets at most `_TRANSPORT_RETRY_ATTEMPTS + 1 +
        (cabins - 1)` GETs between them rather than that many each. Sharing is
        what makes the budget a statement about the network, which is one
        network. The arithmetic and the measured costs live once, in the budget
        section of docs/memories/gf_routing_and_carriers.md — the same place
        the class docstring points at for the wall."""
        with self._lock:
            self._wall.refill()
            if network:
                self._net.refill()

    def stand_down(self) -> None:
        """Give up ownership without a verdict — for a worker leaving the ladder
        by some other door (a shape error, a consent wall).

        Waiters are released to retry rather than left holding a probe that will
        never report: whatever ended the owner's call says nothing about the
        wall, and one request each is cheaper than a fan-out that hangs.

        Every round this worker owns, since a throttled owner whose probe meets
        a reset socket holds one while it climbs the other."""
        me = threading.current_thread()
        with self._lock:
            for round_ in (self._wall, self._net):
                if round_.owner == me:
                    round_.release()


# The ladder the current fan-out shares, or None when nothing is fanning out.
#
# Not thread-local: the workers ARE threads and the wall they share is per-IP,
# so they have to see ONE ladder. Not a plain global either — two fan-outs can
# overlap, and a save/restore global is LIFO-correct only on one thread: each
# scope would leave the other holding a foreign, possibly spent ladder, and the
# last one out would leave a stale ladder bound for every later search in the
# process. A ContextVar is both at once, because `anyio.to_thread.run_sync`
# copies the caller's context into the worker: shared downward into a fan-out's
# threads, isolated sideways between fan-outs.
_fanout_ladder: contextvars.ContextVar[_SharedThrottleLadder | None] = contextvars.ContextVar(
    "flight_cli_fanout_ladder", default=None
)


@contextlib.contextmanager
def shared_throttle_ladder() -> Generator[None]:
    """Make every GF call inside this block draw on ONE ladder — for a caller
    that issues several searches at once.

    Outside it each call ladders on its own, which is what a lone search wants:
    there is nobody else to share the wall with. The previous ladder is restored
    on the way out, so nesting one scope inside another cannot strand a fan-out
    on an inner budget."""
    token = _fanout_ladder.set(_SharedThrottleLadder())
    try:
        yield
    finally:
        _fanout_ladder.reset(token)


def _is_throttle_block(body: str) -> bool:  # pyright: ignore[reportUnusedFunction]  # read by _gf_dategrid, not here
    """True when a non-data GF **RPC** response is a genuine throttle (error
    envelope), not a cold-session / no-results empty. The throttle body carries
    a `type.googleapis.com/...ErrorResponse` marker; an empty body does not.

    Lives here because the date grid — still an RPC POST — imports it from this
    module. The search page's own refusals are classified by
    `_is_page_throttled` / `_is_consent_page` below."""
    return "ErrorResponse" in body or "type.googleapis.com" in body


# The search page's block is Google's captcha interstitial — reached by redirect
# to `/sorry/`, or served in place with HTTP 200. Neither carries a `ds:1` blob,
# so both would otherwise read as an empty board.
_SORRY_PATH = "/sorry"
# The same interstitial served at the requested URL with HTTP 200 — no redirect
# to key off, so the body is the only tell. English-only, and we always request
# `hl=en`; a localised block would still be caught by the `/sorry/` URL check
# whenever Google redirects, which is the common shape.
_SORRY_MARKERS = ("Our systems have detected unusual traffic",)
# Consent interstitial. EU/EEA egress lands here; the page has no `ds:1`, so
# without a check for it a consent wall is indistinguishable from a shape change.
#
# Classified from WHERE the response came from, or from a structural feature of
# the consent page — never from a bare substring in the body. `consent.google.com`
# is a domain Google's own pages link to, and the body is two megabytes of
# untrusted markup, so a substring test says "consent wall" for any shape-changed
# page that happens to mention it. That sends the user to fix a consent problem
# they do not have, instead of reporting the layout change we actually saw.
# One or two trailing labels, each a plausible TLD — `.com`, `.de`, `.co.uk`.
# NOT an open `[a-z.]+`: that is greedy to the end of the string, so
# `consent.google.com.evil.example` would read as Google's consent host.
_TLD = r"[a-z]{2,4}(?:\.[a-z]{2,4})?"
# Anchored at both ends, so it names a host rather than finding one inside a
# longer string. The ccTLD arm is not hypothetical: EU egress is redirected to
# the consent host for the local Google domain, `consent.google.co.uk` among
# them, and only the `.com` spelling would otherwise be recognised.
_CONSENT_HOST_RE = re.compile(rf"^consent\.google\.{_TLD}$", re.IGNORECASE)
_CONSENT_PATH = "/consent"
_GOOGLE_HOST_RE = re.compile(rf"^(?:[a-z0-9-]+\.)*google\.{_TLD}$", re.IGNORECASE)
# The interstitial SUBMITS the user's choice to the consent host. A results page
# can link to that host; only the consent page itself posts a form to it.
#
# `[^<>]`, not `[^>]`: the attribute run belongs to ONE open tag, so it stops at
# the next tag boundary either way. Letting it cross `<` lets every `<form` in a
# two-megabyte body rescan the whole rest of the document looking for an
# `action=` that is not there — quadratic, and measured at seconds on a body
# holding a few thousand of them.
_CONSENT_FORM_RE = re.compile(
    rf"<form\b[^<>]*\baction=[\"']https://consent\.google\.{_TLD}/", re.IGNORECASE
)

# `AF_initDataCallback({key: 'ds:1', hash: '..', data:[...], sideChannel: {}});`
# — the search page inlines the flight rows here. Literal regexes because the
# blob is JavaScript, not JSON: only `data:` holds a JSON value. Anything about
# the shape drifting is a `GfPageShapeError`, never an empty result.
#
# The blob terminates on the `sideChannel` KEY, never on `});`: row payloads
# carry arbitrary Google copy, and one airline name or airport string containing
# `});` would otherwise cut the capture short and fail the whole search. (An
# alternation that accepts either terminator does NOT work — the lazy quantifier
# stops at whichever comes first, so an embedded `});` still wins.)
#
# The body is tempered so it cannot run past the next `AF_initDataCallback(`.
# A blob carrying no `sideChannel` therefore matches nothing instead of
# swallowing its successor, and the scan resumes at that successor.
_DS_BLOB_RE = re.compile(
    r"AF_initDataCallback\(((?:(?!AF_initDataCallback\()[\s\S])*?),\s*sideChannel\s*:"
)
_DS_KEY_RE = re.compile(r"key:\s*'([^']+)'")
# Greedy to the end of the captured head — `data:` is the last key before
# `sideChannel`, so everything after the first one is the payload.
_DS_DATA_RE = re.compile(r"data:\s*(.*)$", re.S)
_DS_FLIGHTS_KEY = "ds:1"
# `ds:1[2]` is Google's own top-flights board, `[3]` the rest. Concatenated in
# that order because page order is what breaks a tie between equal fares once a
# board is ordered by price (`fare_key`).
_DS_ROW_BLOCKS = (2, 3)
# The most rows one page serves across both blocks: a multi-airport board read
# under the `HeadlessChrome` token stops there, at its cheapest 300 (measured
# 2026-10-05 on seven NYC-LON pages, 5 + 295 each).
_ROW_CAP = 300
_SHAPE_ERROR_SAMPLE_REASONS = 3
# What "this data is not a decodable flight row" means, in ONE place. The probe
# and the row loop must agree: a decode failure the probe swallowed but the row
# loop still crashed on would read as "no rows anywhere" on one page and a raw
# traceback on the next. OverflowError (an int too large to convert, e.g. a
# price block holding 10**100) and TypeError (a null where a sequence is
# indexed) are here because live payloads produce both. RecursionError joins
# them because fli's own decoder formats the offending value into its message
# (`raise ValueError(f"...{raw_price!r}")`), and repr of a deeply nested list
# recurses — the error REPORTING blows the stack before the error is raised.
# Measured on CPython 3.12: repr gives out around 15k nesting levels, and
# `json.loads` refuses at 9998, so a PAGE can't reach it — `_extract_ds1`
# rejects the blob first. It is here for callers that build rows another way,
# and because a typed miss costs a traceback while this costs one tuple entry.
_ROW_PARSE_ERRORS = (
    AttributeError,
    KeyError,
    ValueError,
    IndexError,
    TypeError,
    OverflowError,
    RecursionError,
)


def _extract_ds1(html: str) -> list[Any] | None:
    """The decoded `ds:1` payload from a rendered search page, or None when the
    page doesn't carry one in the shape we read.

    Shaped like the RPC's own payload, so callers index `payload[2]` /
    `payload[3]` for flight rows.

    A page may carry more than one `ds:1` blob — Google hydrates parts of this
    page in stages — so ALL of them are decoded and the one carrying the most
    rows wins, earliest blob on a tie. Choosing by position is a bet: a
    placeholder emitted before the populated blob is served as an authoritative
    empty, or trips the arity guard, while the real board sits further down the
    document unread. Choosing on "carries a row block" is the same bet one level
    down, because a staged blob can carry an empty husk `[[]]`, or one row where
    the settled board carries thirty. Counting costs one pass over blobs that
    are already in memory.

    Only a blob `_rows_from_ds1` could actually SERVE competes on rows
    (`_is_a_readable_board`). A staged blob can be truncated above `[3]`, or
    carry rows at `[2]` beside a placeholder at `[3]`; counting its rows would
    let it beat the finished board and turn a served page into a refusal.

    The count is STRUCTURAL, exactly like the scan that follows it: rows are
    counted, never parsed, so a board whose rows have all changed shape still
    wins and reaches the 0-of-N guard as a layout change instead of losing to a
    husk.

    When no readable board carries rows, a blob that carries rows SOMEWHERE
    still beats one that carries none. Rows we cannot read are a layout we no
    longer understand and the refusal downstream says so; rows nowhere on the
    page is a route with no flights. Without the distinction the answer turns
    on which blob Google emitted first, and a placeholder ahead of a board we
    cannot read is served as an authoritative "no flights on this route" — the
    one outcome none of these failures may reach the user as.

    Failing all of that the first blob long enough to reach `[3]` is returned,
    and failing that the first decodable one at all: a genuinely flight-less
    page stays a flight-less page rather than becoming a missing `ds:1`, and a
    truncated placeholder above it does not become a shape error."""
    best: list[Any] | None = None
    best_rows = 0
    carries_rows: list[Any] | None = None
    long_enough: list[Any] | None = None
    first_decodable: list[Any] | None = None
    for match in _DS_BLOB_RE.finditer(html):
        blob = match.group(1)
        key = _DS_KEY_RE.search(blob)
        if not key or key.group(1) != _DS_FLIGHTS_KEY:
            continue
        data = _DS_DATA_RE.search(blob)
        if not data:
            continue
        try:
            payload: Any = json.loads(data.group(1))
        except (ValueError, RecursionError):
            # RecursionError as well as ValueError: `json.loads` recurses per
            # nesting level, so a blob nested past roughly ten thousand deep
            # exhausts the stack instead of reporting bad JSON. Either way this
            # blob is unreadable and the next one may not be.
            log.debug("ds:1 blob is not readable JSON")
            continue
        if not isinstance(payload, list):
            continue
        decoded = cast("list[Any]", payload)
        # Counting STRUCTURALLY costs this: a decoy carrying one row that
        # happens to parse outranks a complete three-row board and is served
        # short with no warning at any level, because the 0-of-N guard never
        # fires when one of N parsed. Taken because the alternative — a
        # selector that parses rows to choose — drops a real board whose row
        # layout has just changed, which is the failure this backend actually
        # meets. The ruling and the measurements are under
        # "A page may carry more than one `ds:1` blob" in
        # docs/memories/gf_routing_and_carriers.md. Pinned by
        # `test_a_decoy_whose_rows_partly_parse_is_served_short_and_silently`
        # and `test_the_chosen_blob_is_counted_structurally_not_parsed`.
        rows = _board_row_count(decoded) if _is_a_readable_board(decoded) else 0
        if rows > best_rows:  # strictly greater, so a tie keeps the earlier blob
            best, best_rows = decoded, rows
        if len(decoded) > max(_DS_ROW_BLOCKS):
            if long_enough is None:
                long_enough = decoded
            if carries_rows is None and _carries_rows_anywhere(decoded):
                carries_rows = decoded
        if first_decodable is None:
            first_decodable = decoded
    if best is not None:
        return best
    if carries_rows is not None:
        return carries_rows
    return long_enough if long_enough is not None else first_decodable


def _is_a_readable_board(payload: list[Any]) -> bool:
    """Could `_rows_from_ds1` serve this payload at all?

    Its two refusals, asked in advance: an arity that cannot reach `[3]`, and a
    value at `[2]`/`[3]` that is neither absent nor row-shaped. A blob that
    fails either cannot be served however many rows it appears to hold, so
    letting it win on row count trades a board we can read for a typed refusal
    — a staged blob with rows at `[2]` and a placeholder at `[3]` is exactly
    that shape."""
    if len(payload) <= max(_DS_ROW_BLOCKS):
        return False
    return all(
        _is_an_absent_board(payload[index]) or _looks_like_a_row_block(payload[index])
        for index in _DS_ROW_BLOCKS
    )


def _carries_rows_anywhere(payload: list[Any]) -> bool:
    """Does this payload hold flight rows at all — where we read them, or where
    we do not?

    The runner-up test, and the one that separates the two failures this module
    must never confuse. Rows at an index we cannot read, or at one we do not
    read, mean a board whose layout moved; no rows anywhere means a page with no
    flights on it. Only the second is an answer.

    Both halves are the tests the scan itself uses, so a blob answers the same
    way here and there: structural at `_DS_ROW_BLOCKS`, where anything
    row-shaped counts, and strict elsewhere, where `ds:1`'s several other
    lists-of-lists-of-lists would otherwise read as relocated rows.

    Caller guarantees the arity: only a payload long enough to reach `[3]` is
    asked."""
    if _board_row_count(payload) > 0:
        return True
    return any(
        index not in _DS_ROW_BLOCKS and _holds_flight_rows(block)
        for index, block in enumerate(payload)
    )


def _board_row_count(payload: list[Any]) -> int:
    """How many rows this payload carries at `[2]` and `[3]` together.

    Structural, like the scan at those indices: rows are counted, not parsed, so
    a block whose rows have all changed shape still counts as the board it is
    and reaches the 0-of-N guard. An empty husk `[[]]` is a block that exists
    and holds nothing, so it counts as the zero rows it has — the whole point of
    counting rather than asking whether a block is there.

    Callers guarantee the arity — every one of them has already established
    that the payload reaches `[3]`. Checked rather than assumed, because the
    cost of being wrong is the difference between a refusal that degrades to
    Matrix and an `IndexError` traceback, and this module's whole contract is
    that no page shape reaches the user as a crash."""
    if len(payload) <= max(_DS_ROW_BLOCKS):
        raise GfPageShapeError(
            f"ds:1 decoded to {len(payload)} top-level entries, too few to count a "
            f"board at {list(_DS_ROW_BLOCKS)}; the payload layout changed"
        )
    total = 0
    for index in _DS_ROW_BLOCKS:
        if _looks_like_a_row_block(payload[index]):
            total += len(cast("list[Any]", cast("list[Any]", payload[index])[0]))
    return total


def _split_url(url: str) -> urllib.parse.SplitResult:
    """`urlsplit`, with an unparseable URL reading as no URL at all.

    `urlsplit` raises on a malformed authority — a bracketed host that is not an
    IPv6 literal is the reachable one, since the URL comes back from a redirect
    we did not build. Both classifiers below already answer "no" for a URL that
    carries no marker and fall through to their body signals, and that is the
    right answer for a URL nobody can read; ending the whole search on it is
    not."""
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return urllib.parse.SplitResult("", "", "", "", "")


def _is_page_throttled(*, final_url: str, html: str) -> bool:
    """True when Google blocked the fetch rather than serving a board.

    Checked BEFORE parsing: a block renders as zero rows, and "Google is
    throttling us" must never reach the user as "no flights on this route".

    Both signals are needed. The interstitial usually arrives as a redirect to
    `/sorry/`, but Google also serves it with HTTP 200 at the requested URL, and
    that variant is only visible in the body. An HTTP 429 is a third shape and
    is not this predicate's job: it arrives as a status on the response, which
    `_rows_from_page_html` reads directly.

    The URL half reads the PATH, not the whole string: our own request URL
    carries a base64 `tfs=` parameter, and a substring test over the query
    string would call a served page a block on the right three bytes. Both
    spellings of the path count — Google redirects to `/sorry/index`, but a
    bare `/sorry` is the same interstitial and a trailing slash is not a
    promise. The body half stays a substring because its marker is a whole
    English sentence, not a token that turns up in ordinary markup."""
    path = _split_url(final_url).path
    blocked = path == _SORRY_PATH or f"{_SORRY_PATH}/" in path
    return blocked or any(marker in html for marker in _SORRY_MARKERS)


def _is_consent_page(*, final_url: str, html: str) -> bool:
    """True when the consent interstitial was served instead of the page.

    Two positive signals, both structural. The response came back FROM the
    consent host (or a `/consent` path on a Google host), or the body carries a
    form that submits TO the consent host. A results page links to that domain;
    it never posts to it.

    Only meaningful once `ds:1` has come back missing, and `_rows_from_page_html`
    checks in that order. Neither signal is a bare substring: a results page
    that merely mentions the domain is not a consent wall, and answering that it
    is sends the user to fix a problem they do not have."""
    parsed = _split_url(final_url)
    host = (parsed.hostname or "").lower()
    if _CONSENT_HOST_RE.match(host):
        return True
    if _GOOGLE_HOST_RE.match(host) and parsed.path.startswith(_CONSENT_PATH):
        return True
    return _CONSENT_FORM_RE.search(html) is not None


# Persisted gflight session cookies. The cold-session empties above are almost
# entirely "the session is missing Google's NID cookie" — a long-lived (~6mo)
# session cookie a browser keeps across restarts. Empirically, seeding a saved
# NID onto a fresh session drops the cold-start empty rate from ~40% to ~0%, so
# we persist it after a successful call and reload it at startup. Every
# subsequent one-shot `flight` process then starts warm; the retry above stays
# as the fallback for the first-ever run and NID rotation.
#
# The jar lives on fli's `Client._session()`, which is a `threading.local` —
# each worker thread gets its OWN curl_cffi session, so seeding warms only the
# thread that calls it. The multi-cabin fan-out and the enrich path both query
# from threads, which is why the seed latch below is thread-local too: a
# process-wide one left every thread but the first with a cold, NID-less
# session, exactly where warmth matters most.
#
# We persist ONLY the named cookies below (and only on the google.com domain),
# not the whole jar: NID is the one we've validated, and replaying an unknown
# stale anti-bot/consent cookie could do more harm than good. Add a name here if
# a future capture shows another session cookie is load-bearing.
_GOOGLE_DOMAIN = "google.com"
_PERSIST_COOKIE_NAMES = frozenset({"NID"})
# Re-warm a fresh NID periodically rather than ride one identity indefinitely —
# a hedge in case Google ever keys rate-limiting on the cookie. The retry above
# absorbs the single cold start when this lapses.
_COOKIE_TTL_S = 14 * 24 * 3600  # 14 days
# Persistence guards a shared FILE, so once per process is right (a dict so we
# mutate rather than rebind a global).
_cookie_state: dict[str, bool] = {"seeded": False, "persisted": False}
# Seeding guards a per-thread session, so its latch is per-thread. Tests reset it
# by rebinding this to a fresh `threading.local()`.
_seed_latch = threading.local()


def _is_google_domain(domain: str) -> bool:
    """Is this cookie domain `google.com` itself, or one of its subdomains?

    A domain-wide cookie carries a leading dot, so that goes first. A substring
    test accepts `google.com.evil.example` and `notgoogle.com`: whatever can
    write the cookie cache could then have a cookie of an allowlisted name
    seeded onto a live session for a host we never talk to. Widen HERE, in one
    place both the read and the write side call, if a ccTLD NID ever matters."""
    d = domain.lstrip(".").lower()
    return d == _GOOGLE_DOMAIN or d.endswith(f".{_GOOGLE_DOMAIN}")


# Position of the opaque per-flight ID in Google Flights' API row array.
# Mirrors the PP browser extension's parser (chunk-5KW5VSHS.js: `a = n[17]`).
_FLIGHT_ID_IDX = 17
# Per connection, `[minutes, arrival airport, departure airport, ...]`.
_LAYOVERS_IDX = 13

# `row[4][6]` is [checked, carry-on]: the bags the row's price covers, counted
# for the whole party. It matched the page's own text ("1 carry-on bag
# included. 0 checked bags included" for [0, 1]).
_ROW_FARE_IDX = 4
_FARE_BAGS_IDX = 6

# `row[7]` is how Google sells the itinerary, matched to the Cheapest tab's own
# labels row by row: `[1]` "Self transfer", `[2]` "Separate tickets booked
# together", `[]` one ticket. fli's `row[0][12]` reads False on all three.
_ROW_TICKETING_IDX = 7
_TICKETING_SELF_TRANSFER = 1
_TICKETING_SEPARATE = 2

# `row[22]` is Google's CO2 estimate for the row's own flights: grams at [7], the
# route's typical grams at [8], the row's signed percent from that typical at [3],
# and at [2] Google's label for that same comparison. [10]/[11] compare with the
# board's median instead, so [11]'s label differs from [2]'s on over a third of a
# board's rows and is not the one Google's help describes.
_ROW_CO2_IDX = 22
_CO2_LABEL_IDX = 2
_CO2_DELTA_IDX = 3
_CO2_GRAMS_IDX = 7
_CO2_TYPICAL_IDX = 8
_CO2_LABEL: dict[int, str] = {1: "lower", 2: "typical", 3: "higher"}
_LEG_CO2_IDX = 31  # the leg's own grams; the row's [7] is their sum, rounded

# Per-leg field indices in `data[0][2][i]`. Mirrors the Legrooms+ extension's
# parser (load_flight_data.js function `u`). See docs/memories/legroom_recipe.md.
_LEG_AMENITIES_IDX = 12  # array — bit positions decoded into wifi/power/video
_LEG_LEGROOM_CLASS_IDX = 13  # int enum (see _LEGROOM_CLASS)
_LEG_PITCH_IDX = 14  # int (inches)
_LEG_CABIN_IDX = 16  # int enum (see _CABIN)
_LEG_AIRCRAFT_IDX = 17  # string

# Carrier identity (distinct from amenities). A leg tuple separates the OPERATING
# carrier (fl[22], the metal) from the MARKETING/booking carrier (fl[15], what a
# passenger books under). fl[18] is truthy when the operating carrier self-markets
# under its own code; falsy on operated-for (regional feeder) legs. Matrix surfaces
# the marketing identity too, so reading the booking carrier here keeps flight
# numbers consistent across backends.
_LEG_MARKETING_IDX = 15  # list[[code, number, _, name]] of selling carriers (None if none)
_LEG_SELF_MARKETED_IDX = 18  # truthy -> operating carrier markets under its own code
_LEG_OPERATING_IDX = 22  # [code, number, _, name] of the operating carrier
# Field layout within a [code, number, _, name] carrier tuple.
_CARRIER_CODE_IDX = 0
_CARRIER_NUMBER_IDX = 1
_CARRIER_NAME_IDX = 3

_LEGROOM_CLASS: dict[int, str] = {
    1: "AVERAGE",
    2: "BELOW",
    3: "ABOVE",
    4: "Extra Reclining",
    5: "Lie Flat",
    6: "Suite",
    8: "Reclining",
    9: "Angled Flat",
}
_CABIN: dict[int, str] = {1: "ECONOMY", 2: "PREMIUM", 3: "BUSINESS", 4: "FIRST"}


@dataclass
class LegAmenities:
    """Per-leg legroom + amenity extract, decoded from data[0][2][i] indices 12-17."""

    aircraft: str | None = None
    pitch_inches: int | None = None
    legroom_class: str | None = None
    cabin: str | None = None
    wifi: str | None = None  # "free" | "paid" | None (no ground-internet wifi)
    power: str | None = None
    video: str | None = None
    # Carrier identity beyond fli's FlightLeg (which now carries the booking
    # carrier). Operating carrier drives the "operated by" label + the `O:`
    # routing filter; the marketing-carrier set drives marketing-carrier matches.
    operating_carrier: str | None = None  # IATA code of the metal, e.g. "EN"
    operating_carrier_name: str | None = None  # e.g. "Air Dolomiti"
    marketing_carriers: tuple[str, ...] = ()  # IATA codes from fl[15] (selling carriers)
    marketing_flights: tuple[str, ...] = ()  # full marketing flight #s, e.g. "LH9407"


def _decode_power(amenities: Any) -> str | None:
    """t[12] amenity array — [1] or [3] truthy → in-seat plug; [5] → USB.

    Position [1] is the dominant power signal on current routes. [3] and [5]
    are kept for legacy/edge-case routes the original Legrooms+ extension
    mapped before Google's sparse-encoding shift (see legroom_recipe.md)."""
    if not amenities:
        return None
    try:
        if amenities[1] or amenities[3]:
            return "plug"
        if amenities[5]:
            return "usb"
    except (IndexError, TypeError):
        return None
    return None


def _decode_video(amenities: Any) -> str | None:
    """Three-state video enum, validated 2026-05 against Google Flights' UI:

      - `[8]` True → "Live TV" (seatback, B6 DirecTV-style)         → "stream"
      - `[9]` True → "On-demand video" (seatback IFE, DL / EK / UA) → "ondemand"
      - `[10]` True → "Stream media to your device" (BYOD, AA / WN) → "byod"

    Priority order matches Google's labelling preference: when more than one
    delivery channel is available, the most-premium seatback option takes
    the label slot. DIVERGES from Legrooms+ v11.5.0 (used [10] for stream).
    """
    if not amenities:
        return None
    try:
        if amenities[8]:
            return "stream"
        if amenities[9]:
            return "ondemand"
        if amenities[10]:
            return "byod"
    except (IndexError, TypeError):
        return None
    return None


_WIFI: dict[int, str] = {2: "free", 3: "paid"}


def _decode_wifi(amenities: Any) -> str | None:
    """`amenities[11]` is the ground-internet wifi enum: 1=none, 2=free, 3=paid.

    Empirically calibrated 2026-05 by scraping Google Flights' detail-panel
    labels for a sample of flights and correlating against the bit array:

      - `[11]=1` → no "Wi-Fi" label (e.g. F9 Frontier)
      - `[11]=2` → "Free Wi-Fi"  (e.g. AA / B6 / DL / KL / WN — modern US
                  mainline + some international)
      - `[11]=3` → "Wi-Fi for a fee" (e.g. EK / AS / UA / AC / OS — paid
                  models, even where some elite tiers get it free)

    DIVERGES from the Legrooms+ extension v11.5.0 (which reads `[0]`).
    Position `[0]` is consistently None on 2026 responses — the extension's
    wifi icon doesn't fire at all on current data. The real signal moved
    to `[11]` and became a three-state enum (was a Boolean).

    Returns None for "no wifi", "free", or "paid" — None is render-as-empty;
    callers distinguish the two truthy states for UI presentation.
    """
    if not amenities:
        return None
    try:
        raw = amenities[11]
    except (IndexError, TypeError):
        return None
    return _WIFI.get(raw) if isinstance(raw, int) else None


def _parse_pitch(raw: Any) -> int | None:
    """Pitch arrives as '31 in' (Google's units-suffixed string) on most
    routes, occasionally bare int. Normalize to int inches; None on
    unrecognized shape."""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        for token in raw.split():
            if token.isdigit():
                return int(token)
    return None


def _carrier_entry(raw: Any) -> tuple[str | None, str | None, str | None]:
    """(code, number, name) from a `[code, number, _, name]` carrier tuple.
    All-None on any malformed shape — Google's response drifts."""
    if not isinstance(raw, list):
        return None, None, None
    items = cast("list[Any]", raw)
    if len(items) <= _CARRIER_NUMBER_IDX:  # a valid entry has at least code + number
        return None, None, None
    code = items[_CARRIER_CODE_IDX]
    number = items[_CARRIER_NUMBER_IDX]
    name = items[_CARRIER_NAME_IDX] if len(items) > _CARRIER_NAME_IDX else None
    return (
        code if isinstance(code, str) else None,
        number if isinstance(number, str) else None,
        name if isinstance(name, str) else None,
    )


def _marketing_codes(fl: list[Any]) -> tuple[str, ...]:
    """IATA codes of the marketing (selling) carriers from fl[15], in order."""
    raw = fl[_LEG_MARKETING_IDX] if len(fl) > _LEG_MARKETING_IDX else None
    if not isinstance(raw, list):
        return ()
    entries = cast("list[Any]", raw)
    return tuple(code for entry in entries if (code := _carrier_entry(entry)[0]))


def _marketing_flights(fl: list[Any]) -> tuple[str, ...]:
    """Full marketing flight numbers from fl[15] (e.g. 'LH9407') — the codeshare
    identities a flight is also sold under. Used for codeshare-aware display."""
    raw = fl[_LEG_MARKETING_IDX] if len(fl) > _LEG_MARKETING_IDX else None
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in cast("list[Any]", raw):
        code, number, _ = _carrier_entry(entry)
        if code and number:
            out.append(f"{code}{number}")
    return tuple(out)


def _resolve_booking(fl: list[Any]) -> tuple[str | None, str | None]:
    """The (carrier code, flight number) a passenger books under.

    On an operated-for leg (a regional flies metal sold under a mainline's code:
    fl[15] present AND fl[18] falsy) that's the marketing carrier (fl[15][0]).
    Otherwise the operating carrier self-markets (fl[18] truthy) or there's no
    codeshare (fl[15] empty), so it's the operating carrier (fl[22]). Matrix
    surfaces this same marketing identity, so aligning here lets the cross-backend
    flight#+date join fire on codeshares. Ground-truthed 2026-06-13 vs GF's own
    headline: OS36 (fl18=[true] -> Austrian), Air Dolomiti EN8858 (fl18=null ->
    Lufthansa LH9498), SWISS LX39 (no fl15 -> SWISS)."""
    marketing = fl[_LEG_MARKETING_IDX] if len(fl) > _LEG_MARKETING_IDX else None
    self_marketed = bool(fl[_LEG_SELF_MARKETED_IDX]) if len(fl) > _LEG_SELF_MARKETED_IDX else False
    if isinstance(marketing, list) and marketing and not self_marketed:
        code, number, _ = _carrier_entry(marketing[0])
        if code and number:
            return code, number
    operating = fl[_LEG_OPERATING_IDX] if len(fl) > _LEG_OPERATING_IDX else None
    code, number, _ = _carrier_entry(operating)
    return code, number


def _parse_leg_amenities(fl: list[Any]) -> LegAmenities:
    """Defensive read of a leg tuple: amenities (indices 12-17) plus carrier
    identity (operating fl[22], marketing fl[15]). Returns an all-None/empty
    LegAmenities for any field that's missing or the wrong type — Google's
    response shape drifts and a partial extract beats dropping the flight."""
    amenities = fl[_LEG_AMENITIES_IDX] if len(fl) > _LEG_AMENITIES_IDX else None
    legroom_raw = fl[_LEG_LEGROOM_CLASS_IDX] if len(fl) > _LEG_LEGROOM_CLASS_IDX else None
    pitch_raw = fl[_LEG_PITCH_IDX] if len(fl) > _LEG_PITCH_IDX else None
    cabin_raw = fl[_LEG_CABIN_IDX] if len(fl) > _LEG_CABIN_IDX else None
    aircraft = fl[_LEG_AIRCRAFT_IDX] if len(fl) > _LEG_AIRCRAFT_IDX else None
    operating_raw = fl[_LEG_OPERATING_IDX] if len(fl) > _LEG_OPERATING_IDX else None
    op_code, _, op_name = _carrier_entry(operating_raw)
    return LegAmenities(
        aircraft=aircraft if isinstance(aircraft, str) and aircraft else None,
        pitch_inches=_parse_pitch(pitch_raw),
        legroom_class=_LEGROOM_CLASS.get(legroom_raw) if isinstance(legroom_raw, int) else None,
        cabin=_CABIN.get(cabin_raw) if isinstance(cabin_raw, int) else None,
        wifi=_decode_wifi(amenities),
        power=_decode_power(amenities),
        video=_decode_video(amenities),
        operating_carrier=op_code,
        operating_carrier_name=op_name,
        marketing_carriers=_marketing_codes(fl),
        marketing_flights=_marketing_flights(fl),
    )


# A self transfer is separate tickets on which the traveler also collects and
# rechecks bags between flights.
type Ticketing = Literal["self_transfer", "separate_tickets"]


@dataclass
class GFlightWithId:
    """fli's FlightResult plus Google's opaque flight_id and per-leg amenities.

    `amenities[i]` aligns with the i-th leg in `flight.legs` — same index
    in both lists points to the same physical segment.
    """

    flight: FlightResult
    flight_id: str
    amenities: list[LegAmenities]
    # Per leg, the operating carrier's (airline, flight number) from fl[22], or
    # None where the tuple has none. Kept off `amenities` because `--format json`
    # dumps every amenities field.
    operating: tuple[tuple[Airline, str] | None, ...] = ()
    # (checked, carry-on) bags Google says the price covers; None where it does
    # not say.
    bags_included: tuple[int | None, int | None] = (None, None)
    # Per connection, the layover in minutes as the page states it, or None
    # where it states none for that connection (`_layover_minutes`).
    layovers: tuple[int | None, ...] = ()
    # Set when Google sells the itinerary as more than one booking; None for one
    # ticket and for a row that does not say (`_ticketing`).
    ticketing: Ticketing | None = None
    # The dearer listings of this itinerary that `_deduped` folded into this
    # one, one per other cabin mix, cheapest first: a cabin requirement this
    # listing fails can still be met by one of them (`_listing`).
    others: tuple[GFlightWithId, ...] = ()


def _ticketing(data: list[Any]) -> tuple[Ticketing | None, bool | None]:
    """The row's ticketing and fli's `self_transfer`. A slot that is absent or
    not a list states nothing, so `self_transfer` stays None."""
    slot = data[_ROW_TICKETING_IDX] if len(data) > _ROW_TICKETING_IDX else None
    if not isinstance(slot, list):
        return None, None
    codes = cast("list[Any]", slot)
    if _TICKETING_SELF_TRANSFER in codes:
        return "self_transfer", True
    if _TICKETING_SEPARATE in codes:
        return "separate_tickets", False
    return None, False


def _operating_identity(fl: list[Any]) -> tuple[Airline, str] | None:
    """The (airline, flight number) of the metal a leg flies on, or None."""
    raw = fl[_LEG_OPERATING_IDX] if len(fl) > _LEG_OPERATING_IDX else None
    code, number, _ = _carrier_entry(raw)
    if not code or not number:
        return None
    try:
        return fli_airline(code), number
    except AttributeError:  # a code fli has no member for
        return None


def _bag_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bags_included(data: list[Any]) -> tuple[int | None, int | None]:
    """The row's bag statement. A missing, short or malformed slot says
    nothing, which is never a reason to drop the row."""
    fare = data[_ROW_FARE_IDX] if len(data) > _ROW_FARE_IDX else None
    if not isinstance(fare, list) or len(cast("list[Any]", fare)) <= _FARE_BAGS_IDX:
        return None, None
    slot = cast("list[Any]", fare)[_FARE_BAGS_IDX]
    if not isinstance(slot, list):
        return None, None
    counts = [*cast("list[Any]", slot)[:2], None, None]
    return _bag_count(counts[0]), _bag_count(counts[1])


def _int_slot(block: Any, idx: int, *, signed: bool = False) -> int | None:
    """`block[idx]` when it is an int, and not below 0 unless `signed`. Anything
    else, or a block too short or not a list, says nothing."""
    if not isinstance(block, list) or len(cast("list[Any]", block)) <= idx:
        return None
    value = cast("list[Any]", block)[idx]
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if signed or value >= 0 else None


def _row_co2(data: list[Any]) -> dict[str, Any]:
    """The row's CO2 figures as Google states them, keyed as fli's FlightResult
    names them; a slot Google leaves empty stays None."""
    block = data[0][_ROW_CO2_IDX] if len(data[0]) > _ROW_CO2_IDX else None
    label = _int_slot(block, _CO2_LABEL_IDX)
    return {
        "co2_emissions_g": _int_slot(block, _CO2_GRAMS_IDX),
        "co2_emissions_typical_g": _int_slot(block, _CO2_TYPICAL_IDX),
        "co2_emissions_delta_pct": _int_slot(block, _CO2_DELTA_IDX, signed=True),
        "emissions_tag": None if label is None else _CO2_LABEL.get(label),
    }


def _layover_minutes(data: list[Any], leg_tuples: list[list[Any]]) -> tuple[int | None, ...]:
    """The page's own minutes for each connection. Those are elapsed time; the
    leg datetimes are clock readings, an hour out across a daylight-saving
    change at the connecting airport. An entry naming other airports than the
    legs either side of it is not that connection's, so it counts as none."""
    raw = data[0][_LAYOVERS_IDX] if len(data[0]) > _LAYOVERS_IDX else None
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    out: list[int | None] = []
    for i, (inbound, outbound) in enumerate(itertools.pairwise(leg_tuples)):
        entry = entries[i] if i < len(entries) else None
        fields = cast("list[Any]", entry) if isinstance(entry, list) else []
        minutes = fields[0] if fields else None
        at = fields[1:3] == [inbound[6], outbound[3]]
        out.append(minutes if isinstance(minutes, int) and minutes >= 0 and at else None)
    return tuple(out)


def _parse_flight_with_id(data: list[Any]) -> GFlightWithId:
    """Mirror of fli's `_parse_flights_data` but also reads `data[0][17]`.

    Indices match the PP extension's parser (chunks/chunk-5KW5VSHS.js): n[17]
    is the per-flight opaque ID; n[2] legs; n[9] duration; t[0][-1] price."""
    price, currency = SearchFlights._parse_price_info(data)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
    flight_id = data[0][_FLIGHT_ID_IDX] if len(data[0]) > _FLIGHT_ID_IDX else ""
    leg_tuples: list[list[Any]] = data[0][2]
    ticketing, self_transfer = _ticketing(data)
    flight = FlightResult(
        price=price,
        currency=currency,
        duration=data[0][9],
        stops=len(leg_tuples) - 1,
        legs=[_flight_leg(fl) for fl in leg_tuples],
        self_transfer=self_transfer,
        **_row_co2(data),
    )
    amenities = [_parse_leg_amenities(fl) for fl in leg_tuples]
    return GFlightWithId(
        flight=flight,
        flight_id=flight_id,
        amenities=amenities,
        operating=tuple(_operating_identity(fl) for fl in leg_tuples),
        bags_included=_bags_included(data),
        layovers=_layover_minutes(data, leg_tuples),
        ticketing=ticketing,
    )


def _flight_leg(fl: list[Any]) -> FlightLeg:
    """Build fli's FlightLeg using the BOOKING carrier (see `_resolve_booking`)
    as airline/flight_number — not the operating carrier — so the surfaced flight
    matches what a passenger books (and what Matrix returns). The operating
    carrier is preserved separately on `LegAmenities`."""
    book_code, book_number = _resolve_booking(fl)
    if book_code is None:
        # No operating or marketing carrier in the tuple — malformed leg. Raise
        # so the parse loop in `_rows_from_page_html` skips this flight, matching
        # the prior behaviour of indexing a missing fl[22][0].
        raise ValueError("leg tuple missing carrier identity")
    return FlightLeg(
        airline=fli_airline(book_code),
        flight_number=book_number or "",
        departure_airport=_leg_airport(fl[3]),
        arrival_airport=_leg_airport(fl[6]),
        departure_datetime=_parse_datetime(fl[20], fl[8]),
        arrival_datetime=_parse_datetime(fl[21], fl[10]),
        duration=fl[11],
        co2_emissions_g=_int_slot(fl, _LEG_CO2_IDX),
    )


def _leg_airport(code: Any) -> Airport:
    """The member for a leg's airport code, one fli aliases included. A code fli
    has no entry for fails the row through fli's own decoder, which logs it."""
    member = fli_airports().get(code)
    return member if member is not None else _parse_airport(code)


def _cookie_path() -> pathlib.Path:
    """Where the warmed gflight session cookies live: a directory of this
    component's own under the shared CLI cache root (same `MATRIX_CACHE_DIR`
    override the response cache honors).

    Its own directory rather than the root, because the file is a live Google
    session cookie and wants a private one — and the root is not ours to make
    private. The Matrix response cache shares that root and creates it with no
    mode, so on any machine that has run a search it already exists at the umask
    default; tightening it here would change a directory this component was
    handed rather than created, and take the response cache's permissions with
    it. A directory we create is ours to set a mode on, so we create one."""
    return cache_dir() / "gflight" / "gflight-cookies.json"


def _seed_cookies_once(client: Any) -> None:
    """Load saved Google cookies onto the shared session, once per process,
    before the first request — so a fresh CLI invocation starts warm instead of
    cold. Best-effort: missing/corrupt cache or a cookie-set failure leaves the
    session as-is (the retry then warms it)."""
    if getattr(_seed_latch, "done", False):
        return
    _seed_latch.done = True
    try:
        payload: dict[str, Any] = json.loads(_cookie_path().read_text())
        saved_at = float(payload["saved_at"])
        saved = payload["cookies"]  # untrusted Any; iterated defensively below
    except OSError:
        return  # no saved cookies yet (first-ever run)
    except (ValueError, TypeError, KeyError):
        log.debug("ignoring unparseable gflight cookie cache")
        return
    if time.time() - saved_at > _COOKIE_TTL_S:
        log.debug("gflight cookie cache past TTL; re-warming")
        return
    try:
        jar = _cookie_jar(client)
        for c in saved:
            name = str(c["name"])
            domain = str(c.get("domain", ".google.com"))
            # The SAME allowlist the write side applies, checked again on the
            # way in. The cache is a plain file in a shared directory: anything
            # that can edit it could otherwise add a cookie of any name for any
            # domain and have this seed it onto a live session.
            if name not in _PERSIST_COOKIE_NAMES or not _is_google_domain(domain):
                log.debug("ignoring non-allowlisted cookie %r for %r in the cache", name, domain)
                continue
            jar.set(
                name,
                c["value"],
                domain=domain,
                path=c.get("path", "/"),
            )
    except Exception as e:  # noqa: BLE001 — seeding is best-effort (corrupt/odd cache, never fatal)
        log.debug("could not seed gflight cookies: %s", e)


def _cookie_jar(client: Any) -> Any:
    """The session cookie jar, across fli client shapes.

    fli <=0.8 exposed `Client._client`; 0.9 replaced it with a per-thread
    `Client._session()`. Both hand back an object with the same `.set()` /
    `.jar` interface. Reaching for the old attribute silently raised
    AttributeError into this module's best-effort `except`, which turned NID
    seeding AND persistence into no-ops — so every process started cold, and
    the comments above put the cold-start empty rate at ~40% versus ~0% warm.

    Raises AttributeError when neither shape is present, so a future upstream
    rename fails loudly at the callers' `except` + debug log rather than
    degrading silently forever.
    """
    session = getattr(client, "_client", None)
    if session is None:
        # fli 0.9: per-thread session accessor replaced the old `_client` attr.
        session = client._session()  # pyright: ignore[reportAny]
    return session.cookies  # pyright: ignore[reportAny]


def _persist_cookies(client: Any) -> None:
    """Write the session's allowlisted Google cookies (NID) to disk after a warm
    call, once per process, so the next invocation starts warm. Best-effort."""
    if _cookie_state["persisted"]:
        return
    try:
        cookies = [
            {
                "name": str(ck.name),
                "value": str(ck.value or ""),
                "domain": str(ck.domain or ".google.com"),
                "path": str(ck.path or "/"),
            }
            for ck in _cookie_jar(client).jar  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
            if str(ck.name) in _PERSIST_COOKIE_NAMES and _is_google_domain(str(ck.domain or ""))
        ]
    except Exception as e:  # noqa: BLE001 — cookie read is best-effort, never fatal
        log.debug("could not read session cookies to persist: %s", e)
        return
    if not cookies:
        return
    path = _cookie_path()
    # The multi-cabin fan-out runs several threads through here, and the
    # once-per-process latch above is an unsynchronised check-then-set, so two
    # of them can reach this write. Rename-into-place is what keeps that
    # harmless: a reader either sees the previous whole file or the new whole
    # file, never the bytes in between. The temp name carries pid and thread id
    # so two writers cannot share a scratch file either.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    created = False
    try:
        # 0700 explicitly: `mkdir` takes the umask otherwise, and this directory
        # holds a live Google session cookie. `parents=True` creates the shared
        # cache root with the default mode and only the leaf with this one,
        # which is the split we want — the root belongs to whoever made it and
        # holds the Matrix response cache too, while the leaf is ours alone. The
        # chmod covers the leaf already existing at some other mode, since the
        # mode on `mkdir` applies only when that call creates it. Suppressed
        # because the cache may belong to another user on a shared box, where a
        # private directory is not ours to fix and a search is still worth
        # serving.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)
        # `os.open` with the mode, not a write-then-chmod: the rename below
        # carries this inode and its mode onto the cache, so a mode fixed after
        # the fact would leave a live Google session cookie world-readable for
        # the length of the write. O_EXCL because a temp we did not create is
        # not ours to overwrite.
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        with os.fdopen(fd, "w") as fh:
            json.dump({"saved_at": time.time(), "cookies": cookies}, fh, indent=2)
        tmp.replace(path)  # os.replace under the hood: one atomic rename
        _cookie_state["persisted"] = True
    except OSError as e:
        log.debug("could not persist gflight cookies: %s", e)
    finally:
        # Only a temp THIS call created. `O_EXCL` failing means the file was
        # already there and belongs to someone else — unlinking it would delete
        # another writer's scratch file mid-write. Otherwise: a successful
        # rename already consumed ours, and this removes it on every other exit,
        # including a KeyboardInterrupt mid-write. Nothing else sweeps the cache
        # directory, and the temp holds the NID.
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


class _Ds1Board(NamedTuple):
    """What a decoded `ds:1` payload holds, and where."""

    rows: list[Any]
    blocks_seen: int  # row blocks at the indices we read
    misplaced: tuple[int, ...]  # row blocks anywhere else


def _looks_like_a_row_block(block: Any) -> bool:
    """Structural test: is `block` shaped `[[…], …]`?

    Used only at the indices we already read, where position vouches for the
    block. Deliberately does NOT require the rows to parse — a block whose rows
    have all changed shape must reach the 0-of-N guard in `_rows_from_page_html`
    and be reported as a layout change, not silently drop to an empty board."""
    if not isinstance(block, list) or not block:
        return False
    return isinstance(cast("list[Any]", block)[0], list)


def _is_an_absent_board(block: Any) -> bool:
    """Does `block` mean "Google put no board at this index"?

    `None` is the shape measured live — a flight-less search carries it at both
    indices, a pinned return leg at `[2]` alone. A bare `[]` claims exactly the
    same thing with the same amount of data, so it reads the same way: the board
    is whatever the other index holds. `[[]]` is different and NOT absent — it is
    a block that exists and carries no rows."""
    return block is None or (isinstance(block, list) and not cast("list[Any]", block))


def _holds_flight_rows(block: Any) -> bool:
    """Strict test: does ANY row in `block` parse as a flight row?

    Used only away from those indices, to spot rows that moved. `ds:1` carries
    several other list-of-list-of-list structures — indices 1, 6, 7, 11, 14, 17,
    25, 26 and 30, the union across the three captures — so the structural test
    above would call those relocated rows and refuse every ordinary page.

    Reads every row, not a leading window: a relocated block whose first rows
    are unparseable is exactly the shape a layout change arrives in, and any
    fixed depth is a number an unlucky payload sits just past. The decoy blocks
    hold 2-7 rows and the scan runs in well under a millisecond, so full depth
    costs nothing worth a cutoff."""
    if not _looks_like_a_row_block(block):
        return False
    inner = cast("list[Any]", cast("list[Any]", block)[0])
    for row in inner:
        try:
            _parse_flight_with_id(row)
        except _ROW_PARSE_ERRORS:
            continue
        return True
    return False


def _rows_from_ds1(payload: list[Any]) -> _Ds1Board:
    """The flight rows inlined in a `ds:1` payload, top-flights board first,
    plus where else in the payload flight rows turned up.

    How many row blocks a SERVED page carries varies by request — see the
    shapes table in docs/memories/gf_routing_and_carriers.md — so counting them
    is not a validity test, and a board with no flights at all is served with
    nothing at either index. The only layout change the payload can actually
    prove is rows appearing somewhere we don't read, so the scan is positive:
    collect from the indices we read, and probe every other index for rows that
    moved. The two use different tests, on purpose — see each.

    Raises GfPageShapeError when the payload cannot hold a board at all: too
    short to reach `[3]`, or carrying something at `[2]`/`[3]` that is neither
    absent nor row-shaped. Enumeration alone never visits a missing index, so
    without the arity check a truncated payload reads as a served empty board.

    The arity floor is exactly `max(_DS_ROW_BLOCKS) + 1` and asks nothing of the
    rest of the payload. A served page can be short (27 entries measured) and a
    real board can be entirely absent — the flight-less capture carries `None`
    at both indices — so there is no index whose presence proves the page is
    intact, and a sentinel requirement would refuse pages Google still serves."""
    if len(payload) <= max(_DS_ROW_BLOCKS):
        raise GfPageShapeError(
            f"ds:1 decoded to {len(payload)} top-level entries, too few to hold a "
            f"board at {list(_DS_ROW_BLOCKS)}; the payload layout changed"
        )
    rows: list[Any] = []
    blocks_seen = 0
    misplaced: list[int] = []
    for index, block in enumerate(payload):
        if index in _DS_ROW_BLOCKS:
            # Anything not absent and not row-shaped is a value we've never been
            # served and can't read.
            if _is_an_absent_board(block):
                continue
            if not _looks_like_a_row_block(block):
                raise GfPageShapeError(
                    f"ds:1[{index}] holds {type(block).__name__}, not a row block "
                    "and not absent; the payload layout changed"
                )
            blocks_seen += 1
            rows.extend(cast("list[Any]", cast("list[Any]", block)[0]))
        elif _holds_flight_rows(block):
            misplaced.append(index)
    return _Ds1Board(rows, blocks_seen, tuple(misplaced))


def search_page_url(
    filters: FlightSearchFilters, *, currency: str = "USD", cheapest: bool = False
) -> str:
    """The public search-page URL for `filters` — the one address both rungs
    fetch, so neither can drift into asking Google a different question.
    `cheapest` is the same question asked of the Cheapest tab."""
    return google_flights_search_page_url(
        build_search_tfs(filters, currency=currency), currency=currency, cheapest=cheapest
    )


class _RetryableTransportError(GfTransportError):
    """A curl-level failure `retry_throttled` may try again.

    Internal to this module: `retry_throttled` converts it to `GfTransportError`
    once the budget is spent, and a caller seeing that distinct type IS the stop
    rule — the network is one network, so a loop over related queries learns
    from one of these that the rest will fail the same way.

    It subclasses `GfTransportError` rather than the base, so that if it ever
    did escape an un-laddered path it degrades to Matrix AND renders as the
    wall it actually is. Under the base it would reach the user as "Google
    Flights declined the request: connection reset by peer" — the one sentence
    the transport arm exists to stop."""


@functools.cache
def _retryable_curl_codes() -> frozenset[Any]:
    """Curl result codes worth another attempt that no exception CLASS selects.

    curl_cffi maps several codes onto classes that also carry permanent faults,
    so the class alone cannot decide: `PARTIAL_FILE` arrives as `IncompleteRead`
    — a multi-megabyte body cut short, which is the page we asked for and a
    retry usually completes — and the three HTTP/2 and HTTP/3 stream errors all
    arrive as `HTTPError`, which is otherwise a status we must not retry.

    Cached because the import is deferred: `curl_cffi` costs ~100ms cold and
    this is reached only once an exception has come back from the session."""
    from curl_cffi.const import CurlECode  # noqa: PLC0415

    return frozenset(
        {
            CurlECode.PARTIAL_FILE,
            CurlECode.HTTP2,
            CurlECode.HTTP2_STREAM,
            CurlECode.HTTP3,
        }
    )


@functools.cache
def _permanent_curl_codes() -> frozenset[Any]:
    """Curl result codes that name THIS machine's TLS configuration.

    `SSLError` subclasses curl_cffi's `ConnectionError`, so the class arm below
    admits the entire TLS family — and most of it belongs there, because a
    handshake that failed once usually completes. These seven do not: a CA
    bundle that cannot be read, a crypto engine that is not installed, a client
    certificate the server would not take. Each is the same on the third
    attempt as the first, and reporting it as "Google Flights could not be
    reached" sends the reader to look at the network for a fault that is here.

    Left retryable, and one of them arguably wrongly: `SSL_CONNECT_ERROR` and
    the certificate-status codes describe the peer or the moment, but
    `SSL_CERTPROBLEM` is curl's name for a fault in the LOCAL client
    certificate. It is excluded because it is unreachable — nothing here sends
    one — not because it would clear. The reachable local-config code is
    `SSL_CIPHER`: `_get_search_page` passes `impersonate="chrome"`, which is what
    sets a cipher list, so that is the one this codebase can provoke. It is
    knowingly left retried: curl_cffi vendors its own BoringSSL, so the list it
    is asked for is one it ships, and a fault there is a packaging problem three
    attempts will not worsen. Move it up here if that stops being true.

    Checked BEFORE the class arm, because the class is what sweeps them in."""
    from curl_cffi.const import CurlECode  # noqa: PLC0415

    return frozenset(
        {
            CurlECode.SSL_ENGINE_NOTFOUND,
            CurlECode.SSL_ENGINE_SETFAILED,
            CurlECode.SSL_ENGINE_INITFAILED,
            CurlECode.SSL_CACERT_BADFILE,
            CurlECode.SSL_CRL_BADFILE,
            CurlECode.SSL_PINNEDPUBKEYNOTMATCH,
            CurlECode.SSL_CLIENTCERT,
        }
    )


def _is_transport_failure(e: BaseException) -> bool:
    """Is this curl failing to REACH Google, rather than a page we read?

    Two families by class, because only these clear on a second attempt:
    `ConnectionError` (DNS, TLS, a reset socket) and `Timeout` (connect and
    read). Then the handful of codes above, which those classes do not cover.
    Minus the codes those classes cover but should not — a deny-list read first,
    since a class cannot see the difference between a network that is down and a
    CA bundle that is missing.

    Everything else propagates on its first try, including the rest of curl's
    `CurlError` tree — `InvalidURL`, `InvalidSchema`, `SessionClosed`,
    `CookieConflict`, `ImpersonateError`, `TooManyRedirects`. Each names a
    request WE built wrongly, which is the shape a `build_search_tfs`
    regression takes, and retrying it three times under the words "Google could
    not be reached" is how such a defect becomes unfindable.

    The import is deferred and on the error path only, for the reason above."""
    from curl_cffi.requests import exceptions as curl_exc  # noqa: PLC0415

    code = getattr(e, "code", None)
    if code is not None and code in _permanent_curl_codes():
        return False
    if isinstance(e, curl_exc.ConnectionError | curl_exc.Timeout):
        return True
    return isinstance(e, curl_exc.RequestException) and code in _retryable_curl_codes()


# The board Google serves a multi-airport search follows this token: under
# `HeadlessChrome` it is the 300 cheapest rows across every airport pair, under
# `Chrome` a curated ~75 that can leave the cheapest pair's fare out. Rung 2's
# headless Chrome sends the token, so rung 1 sends it too and both read one
# board. This is curl_cffi's own `chrome` UA with the token added; a test pins
# its version to curl_cffi's default Chrome profile.
_SEARCH_PAGE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36"
)


def _get_search_page(client: Any, url: str) -> Any:
    """GET the search page through fli's session, bypassing fli's `Client.get`.

    DIVERGE, and the reason is a request budget. `Client.get` is wrapped in
    `@retry(stop_after_attempt(3))` and calls `raise_for_status()`, so a
    persistently throttled leg would cost three fli attempts inside each of our
    throttle retries. The arithmetic and the resulting number live once, in the
    budget section of docs/memories/gf_routing_and_carriers.md. `retry_throttled`
    is the only ladder, so throttle handling lives where the classification
    does.

    What comes back is the raw response: this function does not raise on a
    non-2xx, because a 429 IS the signal and an exception hides it from the
    classifier. The caller reads the status.

    Kept from `Client.get`: the shared 10 req/sec token bucket, a process-global
    budget the fan-out threads share, and fli's own `REQUEST_TIMEOUT`. Its
    transport retry is not kept but is re-homed: a curl-level failure comes back
    as `_RetryableTransportError`, which `retry_throttled` retries on the budget
    at `_TRANSPORT_RETRY_ATTEMPTS` — three GETs per leg — and then raises
    `GfTransportError`.

    Those are per-leg numbers, and the aggregates are what actually reach
    Google. A fan-out shares one budget rather than one each
    (`shared_throttle_ladder`), so four cabins under an outage cost three GETs
    plus the three already in flight, not four ladders; and a round trip stops
    pinning at the pin that met the outage rather than spending a ladder on
    each of the ten."""
    client._rate_limiter.acquire()  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
    try:
        return client._session().get(  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
            url,
            impersonate="chrome",
            # Per request, never on the session: every other request fli makes
            # on this thread's session keeps curl_cffi's own UA.
            headers={"User-Agent": _SEARCH_PAGE_UA},
            allow_redirects=True,
            # fli's own value, imported rather than copied: it is the one that
            # reads and validates `FLI_TIMEOUT`, and a duplicate here silently
            # ignores whatever the user set. A search page is multi-megabyte.
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        if _is_transport_failure(e):
            raise _RetryableTransportError(str(e)) from e
        raise


def _fetch_page(
    filters: FlightSearchFilters, *, currency: str = "USD", cheapest: bool = False
) -> PageFetch:
    """One GET of the public search page.

    Everything visible in the bytes themselves is left to
    `_rows_from_page_html`, so rung 2 — which fetches the same page through
    Chrome and never goes near fli — reaches the same verdicts from the same
    evidence.

    The status is one of those things. `_get_search_page` goes around fli's
    `Client.get` and the `raise_for_status()` inside it, so a 429 or a 503 comes
    back as a RESPONSE and is reported here as the status it was given.
    Classifying it here instead would hide from the ladder the one signal it
    backs off on."""
    client = get_client()
    _seed_cookies_once(client)
    resp = _get_search_page(client, search_page_url(filters, currency=currency, cheapest=cheapest))
    return PageFetch(
        html=resp.text,  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
        final_url=str(resp.url),  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
        status_code=int(resp.status_code),  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
    )


@dataclass(frozen=True)
class PriceInsight:
    """The price insight for a search: the cheapest fare it answers with and
    the range Google says fares for this trip usually fall in, in the page's
    currency.

    From `ds:1[5]`, measured as `[code, [None, cheapest], [None, _], [None, _],
    [None, typical_low], [None, typical_high], ...]`. `[0]` looks like a level
    code (4 on two captures priced inside the range, 5 on one priced above it),
    but three samples do not pin its values, so it is not used and the level is
    derived from the numbers instead."""

    cheapest: float
    typical_low: float
    typical_high: float
    currency: str

    @property
    def level(self) -> Literal["low", "typical", "high"]:
        if self.cheapest < self.typical_low:
            return "low"
        if self.cheapest > self.typical_high:
            return "high"
        return "typical"


_INSIGHT_IDX = 5
_INSIGHT_CHEAPEST_IDX = 1
_INSIGHT_TYPICAL_LOW_IDX = 4
_INSIGHT_TYPICAL_HIGH_IDX = 5


def _insight_amount(block: list[Any], index: int) -> float | None:
    """The number in a `[None, amount]` pair at `block[index]`, or None."""
    pair = block[index] if len(block) > index else None
    if not isinstance(pair, list) or len(cast("list[Any]", pair)) < 2:  # noqa: PLR2004 — a pair
        return None
    amount = cast("list[Any]", pair)[1]
    if isinstance(amount, bool) or not isinstance(amount, int | float):
        return None
    return float(amount)


def _price_insight(payload: list[Any], rows: list[GFlightWithId]) -> PriceInsight | None:
    """The page's price insight, or None when it carries none.

    The currency is read off a priced row of the same page: Google states the
    insight in the page's currency and writes no currency beside it, and an
    unpriced row carries none."""
    block = payload[_INSIGHT_IDX] if len(payload) > _INSIGHT_IDX else None
    if not isinstance(block, list):
        return None
    items = cast("list[Any]", block)
    cheapest = _insight_amount(items, _INSIGHT_CHEAPEST_IDX)
    low = _insight_amount(items, _INSIGHT_TYPICAL_LOW_IDX)
    high = _insight_amount(items, _INSIGHT_TYPICAL_HIGH_IDX)
    currency = next(
        (r.flight.currency for r in rows if r.flight.price is not None and r.flight.currency),
        None,
    )
    if cheapest is None or low is None or high is None or low > high or currency is None:
        return None
    return PriceInsight(cheapest=cheapest, typical_low=low, typical_high=high, currency=currency)


@dataclass(frozen=True)
class PriceHistory:
    """Google's daily price history for a search: `(date, price)` per day, oldest
    first, in the page's currency (None when no priced row names it).

    From `ds:1[5][10][0]`, measured as `[[epoch_ms, price], ...]`, one point a day.
    Each stamp is 04:00 UTC on the captures, which is the client's local midnight,
    so the day is the UTC date twelve hours after the stamp."""

    points: tuple[tuple[datetime.date, float], ...]
    currency: str | None


_HISTORY_IDX = 10
_HISTORY_DAY_OFFSET = datetime.timedelta(hours=12)


def _history_point(point: Any) -> tuple[datetime.date, float] | None:
    """One `[epoch_ms, price]` pair as `(date, price)`, or None for any other shape."""
    if not isinstance(point, list) or len(cast("list[Any]", point)) != 2:  # noqa: PLR2004 — a pair
        return None
    stamp, price = cast("list[Any]", point)
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in (stamp, price)):
        return None
    try:
        when = datetime.datetime.fromtimestamp(stamp / 1000, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return (when + _HISTORY_DAY_OFFSET).date(), float(price)


def _price_history(payload: list[Any], rows: list[GFlightWithId]) -> PriceHistory | None:
    """The page's daily price history, or None when it carries none.

    The currency is read off a priced row, as `_price_insight` reads the
    insight's: the page writes none beside either. A series with any point of
    another shape is refused whole, since a skipped day would read as a gap
    in the history rather than as a layout change."""
    block = payload[_INSIGHT_IDX] if len(payload) > _INSIGHT_IDX else None
    if not isinstance(block, list) or len(cast("list[Any]", block)) <= _HISTORY_IDX:
        return None
    holder = cast("list[Any]", block)[_HISTORY_IDX]
    if not isinstance(holder, list) or not holder:
        return None
    series = cast("list[Any]", holder)[0]
    if not isinstance(series, list) or not series:
        return None
    points = [_history_point(p) for p in cast("list[Any]", series)]
    kept = [p for p in points if p is not None]
    if len(kept) != len(points):
        return None
    currency = next(
        (r.flight.currency for r in rows if r.flight.price is not None and r.flight.currency),
        None,
    )
    return PriceHistory(points=tuple(kept), currency=currency)


def _kept_insight(
    insight: PriceInsight | None,
    rows: Iterable[GFlightWithId | tuple[GFlightWithId, ...]],
    dropped: int,
) -> PriceInsight | None:
    """`insight` for the rows a routing filter kept.

    Google's cheapest is the unfiltered board's, so once the filter has removed
    a row the level is restated from the cheapest fare kept, against Google's
    own range. A combination's fare is its last member's. With no priced row
    kept there is no level to state."""
    if insight is None or not dropped:
        return insight
    fares = [
        fare
        for row in rows
        if (fare := (row[-1] if isinstance(row, tuple) else row).flight.price) is not None
    ]
    return replace(insight, cheapest=min(fares)) if fares else None


# Whether a search also reads the Cheapest tab for the itineraries Google sells
# as separate tickets: "show" adds them to the answer, "hide" only counts them.
type SeparateTickets = Literal["off", "show", "hide"]


class Board[T](list[T]):
    """Rows as a search served them, plus what the page said beside them.

    A list, so every test of "was anything served" (`retry_throttled`'s empty
    retry, `search_with_ids`' `if not first`) still reads the rows alone.
    `dropped` counts the rows a routing filter removed on the way here, which
    is how an empty answer tells "none matched the routing" from "Google has no
    flights". `pinned` counts the outbounds a round trip searched returns for,
    because an empty answer from those says nothing about the outbounds it did
    not pin. `partial` says the rows stop short of what one search of the same
    legs would list: a page of a search asked as several did not answer, or a
    round trip asked as several pages priced each return only between its own
    page's airports. `unread` counts the rows the pages served that the parser
    could not read: a flight on one of them is on Google's board though no row
    here names it. `history` is the route's, so a filter that restates the
    insight leaves it as the page gave it. `stop_drops` is set by the caller
    that built the row filter: the rows it dropped for the stop ceiling, for
    whichever path shows the board to say so. `page_insights` and
    `page_histories` are set on a board merged from several pages, one per
    page that carried one, in page order: each describes its page's airports
    alone, so the merged board's `insight` and `history` are None.
    `separate_hidden` counts the separate-ticket itineraries a search asked to
    hide, and `separate_failed` is why the Cheapest tab, where those are listed,
    went unread. `capped_at` is the highest fare on a page that stopped at
    Google's row cap (`_ROW_CAP`): every fare at or below it is listed, and a
    dearer one may be missing. A round trip carries its outbound page's."""

    def __init__(
        self,
        rows: Iterable[T] = (),
        *,
        insight: PriceInsight | None = None,
        history: PriceHistory | None = None,
        dropped: int = 0,
        pinned: int = 0,
        partial: bool = False,
        unread: int = 0,
        separate_hidden: int = 0,
        separate_failed: GfBackendError | None = None,
        capped_at: float | None = None,
    ) -> None:
        super().__init__(rows)
        self.insight = insight
        self.history = history
        self.dropped = dropped
        self.pinned = pinned
        self.partial = partial
        self.unread = unread
        self.capped_at = capped_at
        self.stop_drops: StopDrops | None = None
        self.separate_hidden = separate_hidden
        self.separate_failed = separate_failed
        self.page_insights: tuple[PriceInsight, ...] = ()
        self.page_histories: tuple[PriceHistory, ...] = ()


class _PageUnreadError(GfPageShapeError):
    """Rows were served and not one of them parsed. `unread` counts them, so a
    round trip that goes on without this return page still has them in its
    board's `unread`."""

    def __init__(self, message: str, *, unread: int) -> None:
        super().__init__(message)
        self.unread = unread


# One itinerary, as `_deduped` and the round-trip pins tell them apart.
type ItineraryKey = tuple[tuple[Airline, str, datetime.datetime], ...]


def _itinerary_key(row: GFlightWithId) -> ItineraryKey:
    return tuple(
        (leg.airline, leg.flight_number, leg.departure_datetime) for leg in row.flight.legs
    )


def row_key(row: GFlightWithId | tuple[GFlightWithId, ...]) -> tuple[ItineraryKey, ...]:
    """A served row as the trip it is: a one-way row's itinerary, or each
    member's of a round-trip combination, in slice order."""
    members = row if isinstance(row, tuple) else (row,)
    return tuple(_itinerary_key(m) for m in members)


def fare_key(row: GFlightWithId) -> tuple[int, float]:
    """Sort key for a Google row by its fare, a row Google did not price after
    every row it did.

    Google surfaces no shopping-list price for some rows — premium-cabin round
    trips with several passengers are the routine case — and a row it did not
    price is still a row the board served. There is no number to rank it on, so
    it goes last rather than being dropped or read as a zero fare; the leading
    term is what carries that, and it leaves the priced rows compared on the
    fare alone. A sort on it is stable, so rows sharing a fare keep the order
    they came in.

    Reads `.flight.price` and no other attribute, so the key holds for anything
    shaped like a result row rather than only for fli's own model."""
    price = row.flight.price
    return (1, 0.0) if price is None else (0, price)


def _deduped(rows: list[GFlightWithId]) -> list[GFlightWithId]:
    """One row per itinerary, keeping the better fare.

    Google can list one itinerary twice at two prices, and only the cheaper is
    on offer. The key is every leg's carrier, flight number and departure time,
    dates included: the same flight numbers a day apart are a different trip.
    The first listing keeps its place, because page order breaks ties between
    equal fares in the trim and in the round-trip pins. A listing booked in
    another cabin mix is kept on the row as one of its `others`."""
    listed: dict[ItineraryKey, list[GFlightWithId]] = {}
    for row in rows:
        listed.setdefault(_itinerary_key(row), []).append(row)
    out: list[GFlightWithId] = []
    for listings in listed.values():
        best = min(listings, key=fare_key)
        mixes = {_cabins(best)}
        others: list[GFlightWithId] = []
        for row in sorted(listings, key=fare_key):
            if _cabins(row) not in mixes:
                mixes.add(_cabins(row))
                others.append(row)
        out.append(replace(best, others=tuple(others)) if others else best)
    return out


def _cabins(row: GFlightWithId) -> tuple[str | None, ...]:
    return tuple(a.cabin for a in row.amenities)


def _rows_from_page_html(page: PageFetch) -> Board[GFlightWithId]:
    """The flight rows a rendered search page carries — the single parser both
    rungs go through; a rung supplies bytes, never interpretation.

    The order is load-bearing. The captcha interstitial is a throttle before it
    is anything else; only then is a non-2xx Google declining to serve; only then
    is a missing `ds:1` read as consent; only then is an absent row block one.
    Every one of those would otherwise decode as zero rows and reach the user
    as "no flights on this route".

    Refusals are typed and raised (`GfThrottledError` / `GfUpstreamStatusError`
    / `GfConsentError` / `GfPageShapeError`); a page that decodes with zero rows
    returns an empty board, which is Google's authoritative answer and not retried.
    A row that does not parse is skipped and counted in the board's `unread`."""
    html, final_url, status_code = page
    # Both rungs arrive here carrying the status they were served: rung 1 reads
    # it off the response, rung 2 off the navigation, and neither rules on it.
    # The 429 test must come FIRST: the next branch would otherwise claim it as
    # a generic upstream status and lose the one fact a caller can act on, that
    # backing off is the fix.
    if status_code == HTTPStatus.TOO_MANY_REQUESTS or _is_page_throttled(
        final_url=final_url, html=html
    ):
        raise GfThrottledError("Google Flights rate-limited the request")
    if not HTTPStatus.OK <= status_code < HTTPStatus.MULTIPLE_CHOICES:
        # Non-2xx, not `>= 400`: a 304 out of the persistent profile's cache or
        # a redirect Chrome did not follow carries no page either, and reading
        # that as a shape error sends the next reader hunting an extract bug
        # during an outage. Not a shape error at all — nothing was served to
        # re-derive an extract from.
        raise GfUpstreamStatusError(status_code)
    payload = _extract_ds1(html)
    if payload is None:
        if _is_consent_page(final_url=final_url, html=html):
            raise GfConsentError(
                "Google served its consent page instead of search results (no flight rows to read)"
            )
        raise GfPageShapeError(
            "Google Flights' search page carried no readable ds:1 payload; the page shape changed"
        )
    board = _rows_from_ds1(payload)
    if board.misplaced and not board.rows:
        # Rows found elsewhere and none served here. The measured flight-less
        # shape is `None` at BOTH indices, not an empty husk `[[]]` — that husk
        # has never been observed on a live page, and a husk plus flight rows
        # sitting somewhere else is far likelier a relocation than a
        # coincidence. Refusing degrades to Matrix; reading it as an empty
        # tells the user this route has no flights. A zero-row board with
        # nothing misplaced is still an authoritative empty.
        raise GfPageShapeError(
            f"ds:1 holds flight rows at {list(board.misplaced)}, not at "
            f"{list(_DS_ROW_BLOCKS)} (found {board.blocks_seen} there); "
            "the payload layout changed"
        )
    if board.misplaced:
        # A served board plus something row-shaped elsewhere. Live pages carry
        # 4-9 candidate blocks each (4, 9 and 7 across the three captures), so
        # refusing here would fail a query we can already answer; the log is
        # what makes a real partial relocation findable without costing the
        # user their results.
        log.warning(
            "ds:1 carried row-shaped blocks outside %s at %s; served %d rows",
            list(_DS_ROW_BLOCKS),
            list(board.misplaced),
            len(board.rows),
        )
    rows = board.rows
    if not rows:
        if not board.blocks_seen:
            # No block at either index. Indistinguishable from a genuinely
            # flight-less board, so it is served as an empty — the types are the
            # only thing left to re-derive the layout from if it was not one.
            log.debug(
                "ds:1 carried no row block at %s (types %s); reading the board as empty",
                list(_DS_ROW_BLOCKS),
                [type(payload[i]).__name__ for i in _DS_ROW_BLOCKS],
            )
        return Board()  # Google's own answer: this leg has no flights.
    out: list[GFlightWithId] = []
    reasons: list[str] = []
    for fd in rows:
        try:
            out.append(_parse_flight_with_id(fd))
        except _ROW_PARSE_ERRORS as e:
            # %r / !r, not %s: this text comes from the page, and a raw ESC
            # or C1 byte written to a terminal is not a diagnostic.
            log.debug("skipping flight with unparseable data: %r", e)
            reasons.append(f"{type(e).__name__}: {e!r}")
            continue
    if not out:
        # Rows were there and none of them parsed — the row layout moved, which
        # is a different fact from "this route has no flights". Sampled reasons
        # give the next reader something to re-derive the indices from.
        sample = "; ".join(reasons[:_SHAPE_ERROR_SAMPLE_REASONS])
        raise _PageUnreadError(
            f"none of {len(rows)} Google Flights rows parsed; "
            f"the row shape changed (sample reasons: {sample})",
            unread=len(reasons),
        )
    served = _deduped(out)
    fares = [r.flight.price for r in served if r.flight.price is not None]
    return Board(
        served,
        insight=_price_insight(payload, out),
        history=_price_history(payload, out),
        unread=len(reasons),
        # The raw count, unread rows included: the cap is on what Google
        # served, and no field of the page states it.
        capped_at=max(fares) if len(rows) >= _ROW_CAP and fares else None,
    )


def _one_call(
    filters: FlightSearchFilters, *, currency: str = "USD", cheapest: bool = False
) -> Board[GFlightWithId]:
    """Rung 1: fetch the search page over curl_cffi and read its rows."""
    rows = _rows_from_page_html(_fetch_page(filters, currency=currency, cheapest=cheapest))
    # A page we could READ means Google answered a warm session — save its
    # cookies (NID) so the next one-shot CLI process starts warm instead of
    # cold. Rung-1 only: rung 2 keeps its own Chrome profile, and its cookies
    # are not this session's to persist.
    #
    # Narrower than persisting right after the ds:1 decode: a page that decodes
    # and then fails a shape guard no longer re-warms the NID. Accepted, and
    # deliberately not worked around. A shape error means the extract is broken
    # and the next run wants a human, not a warmer cookie; keeping the persist
    # here is what lets the rule stay "we understood the page".
    _persist_cookies(get_client())
    return rows


def retry_throttled[T](call: Callable[[], T], *, retry_empty: bool = True) -> T:
    """Run a GF call under two distinct retry policies (see the constants above);
    shared by the search and date-grid paths.

    - cold-session **falsy result** -> a few quick, linearly-spaced retries on the
      same (warming) client; a fresh session would stay cold. Returns the falsy
      result if it never warms.
    - genuine **throttle** (GfThrottledError) -> exponential, jittered backoff;
      re-raised when exhausted so the caller can degrade to Matrix.

    `retry_empty=False` turns the first policy off, for callers whose empty is
    authoritative rather than cold: the search page either decodes or raises, so
    re-fetching a multi-megabyte page can only return the same zero rows.

    - **transport failure** (a reset connection, a read timeout) -> the same
      backoff on the smaller budget at `_TRANSPORT_RETRY_ATTEMPTS`, which is
      where that budget is explained, and `GfTransportError` when it is spent.
      The type matters to the caller: the network is not a property of the
      query, so a caller looping over related queries stops rather than meeting
      one outage once per query.

    Both budgets come from ONE ladder object, bound here for the whole call:
    the fan-out's when there is one, otherwise this call's own. Inside a
    fan-out that ladder is shared, so the group backs off once against a wall
    that is per-IP (see `_SharedThrottleLadder`)."""
    ladder = _fanout_ladder.get() or _SharedThrottleLadder()
    empty_attempts = 0
    # Per-CALL ceilings beside the ladder's per-fan-out ones. The ladder bounds
    # what the group spends against one wall; these bound what THIS call spends
    # whatever the group is doing. Two things need them. A sibling's success
    # refills the wall — correctly, it is per-IP — so a flapping wall would
    # otherwise give this call a fresh budget between every rung and the loop
    # would never exhaust. And a worker released by a probe retries at once, so
    # an interleaving nobody predicted costs one attempt more rather than a
    # loop with no exit.
    wall_attempts = 0
    net_attempts = 0
    met_network = False
    try:
        while True:
            try:
                result = call()
            except _RetryableTransportError as e:
                met_network = True
                net_attempts += 1
                # The ladder is told either way, and told WHICH: a call with no
                # attempts left cannot use a backoff, so it must not park for
                # one — that park costs the owner's whole remaining ladder in
                # latency and the answer arrives after this call has given up.
                # It is still the ladder's business, because an owner on its
                # last attempt has a rung to book before it leaves.
                final = net_attempts > _TRANSPORT_RETRY_ATTEMPTS
                backoff = ladder.transport_failed(final=final)
                if final or backoff is None:
                    raise GfTransportError(f"Google Flights could not be reached: {e}") from e
                log.debug(
                    "gflight transport failure (%s); backoff %.1fs (budget %d)",
                    e,
                    backoff,
                    _TRANSPORT_RETRY_ATTEMPTS,
                )
                time.sleep(backoff)
                continue
            except GfThrottledError:
                wall_attempts += 1
                # Same shape, and for the same reason, as the arm above.
                final = wall_attempts > _THROTTLE_RETRY_ATTEMPTS
                backoff = ladder.throttled(final=final)
                if final or backoff is None:
                    raise
                log.debug(
                    "gflight throttled; backoff %.1fs (budget %d)",
                    backoff,
                    _THROTTLE_RETRY_ATTEMPTS,
                )
                time.sleep(backoff)
                continue
            ladder.succeeded(network=met_network)
            if result or not retry_empty:
                return result
            empty_attempts += 1
            if empty_attempts >= _EMPTY_RETRY_ATTEMPTS:
                return result  # never warmed, or genuinely empty
            log.debug("empty gflight response; retry %d/%d", empty_attempts, _EMPTY_RETRY_ATTEMPTS)
            time.sleep(_EMPTY_RETRY_BACKOFF_S * empty_attempts)
    finally:
        # Whatever door this call left by, it is no longer probing the wall.
        # A waiter holding a probe that will never report is a hung fan-out.
        ladder.stand_down()


def _one_call_with_retry(
    filters: FlightSearchFilters, *, currency: str = "USD", cheapest: bool = False
) -> Board[GFlightWithId]:
    """`_one_call` under the throttle retry only — a parsed-empty page is an
    answer, so it costs exactly one GET and no sleep."""
    return retry_throttled(
        lambda: _one_call(filters, currency=currency, cheapest=cheapest), retry_empty=False
    )


@dataclass(frozen=True)
class GfTransport:
    """Which rung of the search-page transport a query may use.

    - `http` — rung 1 only: one curl_cffi GET under the throttle backoff.
    - `browser` — rung 2 only: one real-Chrome navigation, no rung-1 fallback.
    - `auto` — today identical to `http`. The escalate-on-persistent-throttle
      rung lands separately; the mode exists now so the CLI surface and every
      call site are already the shape it needs.

    Frozen, and defaulting to `http`, so an unpassed `transport` is rung 1.

    `mode` is a `Literal` (`_gf_common.GfTransportMode`) so that adding a rung
    without teaching `_one_call_laddered` about it is a type error rather than a
    silent downgrade to rung 1. `_one_call_laddered`'s `assert_never` is what
    makes that promise true.
    """

    mode: GfTransportMode = TRANSPORT_HTTP
    headed: bool = False


HTTP_TRANSPORT = GfTransport()


def _one_call_browser(
    filters: FlightSearchFilters, *, headed: bool, currency: str = "USD", cheapest: bool = False
) -> Board[GFlightWithId]:
    """Rung 2: one real-Chrome navigation of the same URL, read by the same parser.

    No retry ladder around it. Rung 2 costs a browser launch and up to a 30 s
    navigation, and a refusal it hits is terminal — re-driving Chrome through
    rung 1's backoff would spend ~22 s more to be told the same thing.

    Reached through the module attribute, never a name bound at import time, so
    a test can substitute the session without a browser anywhere in the
    process."""
    return _rows_from_page_html(
        _gf_browser.session(headed=headed).get_html(
            search_page_url(filters, currency=currency, cheapest=cheapest)
        )
    )


def _one_call_laddered(
    filters: FlightSearchFilters,
    transport: GfTransport,
    *,
    currency: str = "USD",
    cheapest: bool = False,
) -> Board[GFlightWithId]:
    """One leg on the rung `transport` asks for.

    The single place that knows which rungs exist, so `search_with_ids` — and
    the recursion that drives a round trip's return legs — never has to.

    Exhaustive, and checked: `auto` is spelled out beside `http` rather than
    swept up by a trailing `else`, and `assert_never` makes a fourth mode that
    never reached here a basedpyright error at the point it is added. Without
    that, a forgotten rung ships green and quietly runs the transport the user
    did not ask for — a failure that reads as the browser rung simply not
    working.

    Literal patterns rather than the `TRANSPORT_*` constants: a bare name in a
    `case` is a capture pattern, so `case TRANSPORT_BROWSER` would bind every
    mode and match all of them."""
    match transport.mode:
        case "browser":
            return _one_call_browser(
                filters, headed=transport.headed, currency=currency, cheapest=cheapest
            )
        case "http" | "auto":
            return _one_call_with_retry(filters, currency=currency, cheapest=cheapest)
        case _:
            assert_never(transport.mode)


# THE pin budget, and the one place it is explained. How many outbound flights a
# round trip pins and re-fetches: each pin is one more multi-megabyte page GET,
# so this is a REQUEST budget rather than a result limit.
#
# The multi-cabin path bumps top_n to widen the pool it filters, which is free
# on an RPC and is not free here. With the cap, a two-cabin round trip costs
# 2 x 11 = 22 page fetches; without one, at the bumped top_n=100, it would cost
# ~2 x 31. The default `-n 10` sits exactly on the cap and is unchanged by it;
# above it, the round trip returns combinations for the ten cheapest outbounds
# rather than for all of them. A multi-cabin round trip spends every cabin's
# budget on the sort cabin's outbounds first (`prefer`), because a cabin that
# pins its own ten cheapest may price none of the itineraries the table shows;
# `cli._multi_cabin_join_note` says so where a user can see the consequence.
#
# Multi-city never reaches this: `cli._pick_backend` routes a multi-city query
# to Matrix, so the recursion below only ever runs the two legs of a round trip.
_PINNED_FANOUT_CAP = 10


def pinned_fanout(top_n: int) -> int:
    """How many outbounds to pin, given the caller's top_n."""
    return min(top_n, _PINNED_FANOUT_CAP)


def _listing(
    leg: int, row: GFlightWithId, fits: Callable[[int, GFlightWithId], bool] | None
) -> GFlightWithId:
    """The listing of `row`'s itinerary that `keep` is handed for segment `leg`:
    `row`, unless `fits` refuses it and accepts one of its `others`, cheapest
    first. Chosen before `keep` and not by it, so each itinerary meets `keep`
    once and a row over the stop ceiling is counted once."""
    if fits is None or fits(leg, row):
        return row
    return next((o for o in row.others if fits(leg, o)), row)


def _marked_listing(
    row: GFlightWithId, fits: Callable[[int, GFlightWithId], bool] | None
) -> GFlightWithId | None:
    """The listing of a Cheapest-tab row's itinerary sold as separate tickets
    that `keep` is handed: `_listing`'s choice among the marked listings alone,
    or None where that is a one-ticket listing. A one-ticket listing is the base
    board's to show, so it never stands in for a marked one."""
    if fits is not None and not fits(0, row):
        row = next((o for o in row.others if o.ticketing is not None and fits(0, o)), row)
    return row if row.ticketing is not None else None


def _kept_outbounds(
    first: Board[GFlightWithId],
    keep: Callable[[int, GFlightWithId], bool] | None,
    fits: Callable[[int, GFlightWithId], bool] | None = None,
) -> list[GFlightWithId]:
    if keep is None:
        return first
    return [r for r in (_listing(0, r, fits) for r in first) if keep(0, r)]


def _pins(
    board: list[GFlightWithId], top_n: int, prefer: Sequence[ItineraryKey]
) -> list[GFlightWithId]:
    """Every `prefer` key `board` lists, in `prefer` order, then `board`'s other
    rows cheapest first (`fare_key`): `pinned_fanout(top_n)` in all, so `prefer`
    reorders the budget and never grows it.

    Cheapest, because an outbound row's price is already the cheapest round
    trip through it: the outbounds that price lowest are where the cheapest
    combinations are, wherever the page listed them."""
    budget = pinned_fanout(top_n)
    at: dict[ItineraryKey, GFlightWithId] = {}
    for row in board:
        at.setdefault(_itinerary_key(row), row)
    chosen = [at[k] for k in dict.fromkeys(prefer) if k in at][:budget]
    taken = {id(r) for r in chosen}
    rest = sorted((r for r in board if id(r) not in taken), key=fare_key)
    return chosen + rest[: budget - len(chosen)]


def pin_keys(
    first: Board[GFlightWithId],
    *,
    top_n: int,
    keep: Callable[[int, GFlightWithId], bool] | None = None,
    prefer: Sequence[ItineraryKey] = (),
) -> list[ItineraryKey]:
    """The outbounds `search_with_ids` pins when handed `first` with the same
    arguments, as the keys another search can be asked to `prefer`."""
    return [_itinerary_key(r) for r in _pins(_kept_outbounds(first, keep), top_n, prefer)]


def outbound_page(
    filters: FlightSearchFilters, *, transport: GfTransport, currency: str
) -> Board[GFlightWithId]:
    """The page `search_with_ids` starts from, for a caller that needs it before
    the pins are chosen and hands it back as `first`."""
    return _with_board_currency(_one_call_laddered(filters, transport, currency=currency), currency)


def _unpinned_board(
    board: list[GFlightWithId | tuple[GFlightWithId, ...]],
    wanted: FlightSegment,
) -> str | None:
    """Why `board` is not an answer to `wanted`, or None when it is.

    A pin goes out as `selected_flight` and comes back only as whatever page
    Google chose to serve: the response says nothing about which pin it belongs
    to, so a page that dropped the pin arrives shaped exactly like one that
    honoured it. Paired unchecked, its rows become combinations whose members
    are legs nobody asked for — a trip that flies the outbound twice and never
    comes home, priced and printed beside real ones. That is this module's worst
    failure class, a refusal wearing the shape of a result, and it is the one
    thing no later stage can catch: `cli._price_ordered` and the renderers read
    a combination as a combination.

    Origin, destination and date, because between them they identify the leg
    the request asked for, and they are what a served board can be checked
    against without a second query. Three and not two: a board with the right
    origin on the right day can still land somewhere else, and that one is
    invisible on screen — the table's `legs` column carries flight numbers, so
    a trip that never comes home reads like any other. The endpoints are read
    off opposite ends of the row, the origin from the first leg and the arrival
    from the last, because a connection's own endpoints are the route rather
    than the answer.

    The whole board, because a page answering the wrong segment answers it for
    every row, and one honest row beside wrong ones is still not the board that
    was asked for."""
    # `departure_airport` is fli's `[[Airport, weight], …]` shape, and a segment
    # may carry several. Narrowed to the airports because that is what a served
    # leg can be compared against. fli itself does not require the entries to be
    # `Airport` members — its validator skips a first entry that is not one — so
    # what keeps these sets non-empty is this package: every segment reaching
    # here is built by a constructor that resolves each code to a member first.
    # An empty set would refuse every board with the wanted side of the sentence
    # blank, which is why the guarantee is worth naming rather than assuming.
    origins = {a[0] for a in wanted.departure_airport if isinstance(a[0], Airport)}
    dests = {a[0] for a in wanted.arrival_airport if isinstance(a[0], Airport)}
    for row in board:
        member = row[0] if isinstance(row, tuple) else row
        legs = member.flight.legs
        if not legs:
            return "a Google Flights return board carried a flight with no legs"
        leg = legs[0]
        if leg.departure_airport not in origins:
            return (
                f"a Google Flights return board departs {leg.departure_airport.name}, "
                f"not {'/'.join(sorted(a.name for a in origins))}; the pinned leg was ignored"
            )
        if legs[-1].arrival_airport not in dests:
            return (
                f"a Google Flights return board arrives {legs[-1].arrival_airport.name}, "
                f"not {'/'.join(sorted(a.name for a in dests))}; the pinned leg was ignored"
            )
        flown = leg.departure_datetime.date().isoformat()
        if flown != wanted.travel_date:
            return (
                f"a Google Flights return board departs {flown}, not {wanted.travel_date}; "
                "the pinned leg was ignored"
            )
    return None


def _with_board_currency(board: Board[GFlightWithId], requested: str) -> Board[GFlightWithId]:
    """`board` with a currency on every row.

    fli reads a row's currency from a token beside its price and returns None
    when that decode fails, and every renderer downstream would then label the
    price USD. One page is priced in one currency, so the row takes the
    currency the rows beside it decoded to, and the requested one only when
    none did. The page's price insight rides along."""
    decoded = Counter(r.flight.currency for r in board if r.flight.currency)
    fill = decoded.most_common(1)[0][0] if decoded else requested

    def filled(r: GFlightWithId) -> GFlightWithId:
        if not r.flight.currency:
            r = replace(r, flight=r.flight.model_copy(update={"currency": fill}))
        return replace(r, others=tuple(map(filled, r.others))) if r.others else r

    return Board(
        map(filled, board),
        insight=board.insight,
        history=board.history,
        dropped=board.dropped,
        pinned=board.pinned,
        unread=board.unread,
        capped_at=board.capped_at,
    )


def _pinned_flight(picked: GFlightWithId) -> FlightResult:
    """`picked.flight` with every leg named by its operating flight, which is
    what a pin has to carry: AA142 pinned as AY3787, the codeshare number it is
    booked under, comes back with no return board at all, and pinned as AA142
    with twenty rows. A leg with no operating identity keeps its booking one."""
    legs = list(picked.flight.legs)
    for i, op in enumerate(picked.operating[: len(legs)]):
        if op is not None:
            legs[i] = legs[i].model_copy(update={"airline": op[0], "flight_number": op[1]})
    return picked.flight.model_copy(update={"legs": legs})


def _lost_pin(picked: GFlightWithId, why: str, currency: str) -> str:
    """The line naming a pin that has no return: the flights it is booked as,
    which the table shows, and its fare."""
    # fli writes a digit-leading code with a leading `_` (`_0B`).
    flights = "/".join(
        f"{leg.airline.name.removeprefix('_')}{leg.flight_number}" for leg in picked.flight.legs
    )
    price = picked.flight.price
    fare = "" if price is None else f" ({picked.flight.currency or currency}{price:.2f})"
    return f"pinned outbound {flights}{fare} lost: {why}"


def search_with_ids(  # noqa: PLR0915 — one arm per way a pin ends, each accounting for it
    filters: FlightSearchFilters,
    *,
    top_n: int = 5,
    transport: GfTransport = HTTP_TRANSPORT,
    currency: str = "USD",
    keep: Callable[[int, GFlightWithId], bool] | None = None,
    checks: str = "the routing",
    first: Board[GFlightWithId] | None = None,
    prefer: Sequence[ItineraryKey] = (),
    separate_tickets: SeparateTickets = "off",
    fits: Callable[[int, GFlightWithId], bool] | None = None,
) -> Board[GFlightWithId | tuple[GFlightWithId, ...]] | None:
    """Drop-in for fli's `SearchFlights().search()` but each result carries
    its Google Flights opaque flight_id.

    Round-trip / multi-city follow the same iterative leg-selection pattern
    as fli: query first leg, pick top_n, drive each through the rest. Each
    `GFlightWithId` in a returned tuple has its own per-leg flight_id.

    `top_n` bounds the PINS, not the rows returned: the page serves its whole
    board and every row of it is returned, because the callers filter what they
    were served (the Tier-2 post-filter) and join across it (the multi-cabin
    path, which asks for a deliberately wider pool). Trimming to what the user
    asked for is `cli._run_gflight_path`'s, on the way out.

    A pin whose return board refuses for its own reasons is dropped with a
    warning and the rest are still fetched. Every pin dropped that way, served
    no return or left none by `keep` is named on a line of its own, after the
    counts. A throttle, an exhausted transport ladder or a dead browser session
    stops the pinning instead, because none of the three says anything about
    the pin: the wall is per-IP and the network is one network, and every
    remaining pin would navigate on that same dead session. Either way the
    combinations already fetched are returned, and the error is raised only
    when nothing at all was served.

    `transport` rides the recursion so every leg of one trip runs on the same
    rung — a round trip that opened Chrome for its outbound must not silently
    drop back to curl_cffi for the returns. `currency` rides it for the same
    reason: every board of one trip is asked for in one currency.

    `keep(i, row)` is the routing filter for segment `i`. It runs on the
    outbound board BEFORE the pins are taken, because the pins are the cheapest
    rows of the board they are taken from and a filter applied after them
    answers from pins it then discards. It runs on each return board after the
    pin check, so a page that ignored its pin is refused as one rather than read
    as "no return matches". The result carries the outbound page's price insight, restated
    for the rows the filter kept. `checks` names what `keep` holds a row to,
    for the warning that counts the pins it left with no return.

    `first` is this search's page when the caller already fetched it with
    `outbound_page`, so it is not fetched twice. `prefer` puts those outbounds
    first among the pins when the page lists them and `keep` passes them, which
    is how one cabin prices another cabin's outbounds; the pin count is
    unchanged by it.

    `fits(i, row)` is whether a listing's own cabins meet segment `i`'s cabin
    requirement. Google can list one itinerary at several cabin mixes and the
    board shows the cheapest, so `fits` picks which of them `keep` is handed
    (`_listing`).

    `separate_tickets` other than "off" reads the Cheapest tab once more, after
    every other fetch, for the itineraries Google sells as separate tickets
    (`_with_separate_tickets`). Only the search itself does; a pinned leg never
    does. A round trip with `top_n` 0 pins nothing and answers with that tab's
    rows alone, for a page of a search asked as several whose outbounds are
    pinned on other pages."""
    if first is None:
        first = outbound_page(filters, transport=transport, currency=currency)

    num_segments = len(filters.flight_segments)
    selected_count = sum(1 for s in filters.flight_segments if s.selected_flight is not None)
    separate = (
        functools.partial(
            _with_separate_tickets,
            filters,
            mode=separate_tickets,
            transport=transport,
            currency=currency,
            keep=keep,
            fits=fits,
        )
        if separate_tickets != "off" and not selected_count
        else None
    )
    if not first:
        return None if separate is None else separate(Board())
    # A pinned board is filtered by the caller, after its pin check.
    board = first if selected_count else _kept_outbounds(first, keep, fits)
    dropped = len(first) - len(board)
    # One-way, or the last leg already — no further iteration.
    if filters.trip_type == TripType.ONE_WAY or selected_count >= num_segments - 1 or not board:
        answer: Board[GFlightWithId | tuple[GFlightWithId, ...]] = Board(
            board,
            insight=_kept_insight(first.insight, board, dropped),
            history=first.history,
            dropped=dropped,
            unread=first.unread,
            capped_at=first.capped_at,
        )
        return answer if separate is None else separate(answer)

    combos: list[GFlightWithId | tuple[GFlightWithId, ...]] = []
    pins = _pins(board, top_n, prefer)
    refused: list[GfBackendError] = []
    stopped: GfBackendError | None = None
    skipped = 0
    unmatched = 0  # pins whose whole return board the routing filter removed
    empty = 0  # pins Google served no return board for
    lost: list[str] = []
    dropped_returns = 0
    unread = first.unread
    # The segment the recursion below is asked to FILL, which is the one after
    # the pin it is given — checking the pinned segment instead would compare a
    # return board against the outbound and accept a page that ignored the pin,
    # the shape this check exists for. In range because the base case above
    # returned for `selected_count >= num_segments - 1`.
    wanted = filters.flight_segments[selected_count + 1]
    for index, picked in enumerate(pins):
        next_filters = deepcopy(filters)
        next_filters.flight_segments[selected_count].selected_flight = _pinned_flight(picked)
        try:
            nxt = search_with_ids(next_filters, top_n=top_n, transport=transport, currency=currency)
        except (GfThrottledError, GfTransportError, GfBrowserUnavailableError) as e:
            # None of the three is a fact about THIS pin. The wall is per-IP and
            # the network is one network, so every remaining pin walks into the
            # same one, having just spent a whole ladder measuring it. A dead
            # browser is a fact about the SESSION for the same reason: every
            # remaining pin navigates on it, so re-driving it pays the 30 s
            # navigation ceiling per pin and reports one process failure as N
            # independent board refusals.
            stopped = e
            skipped = len(pins) - index
            break
        except GfBackendError as e:
            # A refusal of this URL: a re-shaped return board, a consent wall,
            # a 503. The pins are independent queries, so the next one may well
            # be served, and unwinding would throw away every combination
            # already fetched.
            #
            # A 503 arrives here having spent no ladder. `retry_throttled`
            # retries throttles and transport failures and nothing else, and
            # a 5xx comes back as `GfUpstreamStatusError` — so ten pins
            # meeting ten 503s cost ten GETs, not ten ladders.
            refused.append(e)
            lost.append(_lost_pin(picked, str(e), currency))
            unread += e.unread if isinstance(e, _PageUnreadError) else 0
            continue
        if nxt is None:
            empty += 1
            lost.append(_lost_pin(picked, "Google served no return for it", currency))
            continue
        ignored = _unpinned_board(nxt, wanted)
        if ignored is not None:
            # Refused per BOARD and not per row, so that the count the warning
            # prints stays a count of boards out of the pins that were asked
            # for. A page that answers the wrong segment is a page whose shape
            # stopped meaning what we sent it, so it takes the arm a re-shaped
            # board takes: this pin is dropped, the others are still fetched,
            # and nothing served at all still raises.
            refused.append(GfPinIgnoredError(ignored))
            lost.append(_lost_pin(picked, str(refused[-1]), currency))
            continue
        leg = selected_count + 1
        listed = [nx if isinstance(nx, tuple) else _listing(leg, nx, fits) for nx in nxt]
        kept = [
            nx for nx in listed if keep is None or keep(leg, nx[0] if isinstance(nx, tuple) else nx)
        ]
        dropped_returns += len(nxt) - len(kept)
        unread += nxt.unread
        if not kept:
            unmatched += 1
            returns = f"{len(nxt):d} return{'' if len(nxt) == 1 else 's'}"
            why = f"Google served {returns} for it, none matching {checks}"
            lost.append(_lost_pin(picked, why, currency))
            continue
        for nx in kept:
            if isinstance(nx, tuple):
                combos.append((picked, *nx))
            else:
                combos.append((picked, nx))
    # A Board even with no pair in it: the pins were taken from rows Google
    # served, so the rows the filter removed on either leg are why it is empty,
    # and None would read as Google serving nothing.
    dropped += dropped_returns
    paired = Board(
        combos,
        # With no pin asked for, the outbounds the filter kept are what is left
        # to restate the insight from, as on a board with nothing to pin.
        insight=_kept_insight(first.insight, combos if pins else board, dropped),
        history=first.history,
        dropped=dropped,
        pinned=len(pins),
        unread=unread,
        # The outbound page's alone: each return page lists one pin's returns,
        # so its cap says nothing about which trips are on the board.
        capped_at=first.capped_at,
    )
    # Before the pin outcome is judged: a separate-ticket itinerary needs no
    # return board, so one that is shown is served even when every return
    # board refused.
    answer = paired if separate is None else separate(paired, stopped=stopped)
    _report_pin_outcome(
        served=bool(answer),
        pins=len(pins),
        refused=refused,
        stopped=stopped,
        skipped=skipped,
        unmatched=unmatched,
        bags=filters.bags is not None,
        checks=checks,
        empty=empty,
        lost=lost,
    )
    return answer


def _with_separate_tickets(
    filters: FlightSearchFilters,
    answer: Board[GFlightWithId | tuple[GFlightWithId, ...]],
    *,
    mode: SeparateTickets,
    transport: GfTransport,
    currency: str,
    keep: Callable[[int, GFlightWithId], bool] | None,
    fits: Callable[[int, GFlightWithId], bool] | None = None,
    stopped: GfBackendError | None = None,
) -> Board[GFlightWithId | tuple[GFlightWithId, ...]]:
    """`answer` with the Cheapest tab's separate-ticket itineraries added
    ("show") or counted ("hide").

    Added beside the base rows, never in their place: the Cheapest board also
    reprices some one-ticket rows, so swapping boards would change rows the
    base prints. A marked row is added even when its flights are on the base
    board, because it is a different booking at a different price.

    On a round trip each is a one-member row, its outbound alone at Google's
    round-trip total: Google serves no return board for it, pinned or not.

    `fits` picks each marked itinerary's listing, among its marked listings, as
    it picks a base row's (`_marked_listing`).

    The page is not fetched when pinning stopped on a wall, the network or the
    browser, because it would meet the same one. A refusal of this page leaves
    `answer` as it is and says why on `separate_failed`.

    The page's unread rows are added to `answer`'s, as its filtered marked rows
    are to its dropped ones: a flight on one of them is on Google's board."""

    def with_notes(
        rows: Iterable[GFlightWithId | tuple[GFlightWithId, ...]],
        *,
        hidden: int = 0,
        failed: GfBackendError | None = None,
        insight: PriceInsight | None = answer.insight,
        filtered: int = 0,
        unread: int = 0,
    ) -> Board[GFlightWithId | tuple[GFlightWithId, ...]]:
        return Board(
            rows,
            insight=insight,
            history=answer.history,
            dropped=answer.dropped + filtered,
            pinned=answer.pinned,
            unread=answer.unread + unread,
            separate_hidden=hidden,
            separate_failed=failed,
            capped_at=answer.capped_at,
        )

    if stopped is not None:
        return with_notes(answer, failed=stopped)
    try:
        page = _with_board_currency(
            _one_call_laddered(filters, transport, currency=currency, cheapest=True), currency
        )
    except GfBackendError as e:
        unparsed = e.unread if isinstance(e, _PageUnreadError) else 0
        return with_notes(answer, failed=e, unread=unparsed)
    listed = [m for r in page if (m := _marked_listing(r, fits)) is not None]
    marked = [r for r in listed if keep is None or keep(0, r)]
    # Counted with the base's, so an answer they would have filled reads as
    # none matching the routing, not as Google having no flights.
    filtered = len(listed) - len(marked)
    if mode == "hide":
        return with_notes(answer, hidden=len(marked), filtered=filtered, unread=page.unread)
    # The insight's level is read off the cheapest fare the answer holds, and a
    # separate-ticket fare can undercut every one-ticket fare on the base board.
    insight = answer.insight
    fares = [r.flight.price for r in marked if r.flight.price is not None]
    if insight is not None and fares:
        insight = replace(insight, cheapest=min(insight.cheapest, *fares))
    unread = page.unread
    if filters.trip_type == TripType.ONE_WAY:
        return with_notes([*answer, *marked], insight=insight, filtered=filtered, unread=unread)
    return with_notes(
        [*answer, *((r,) for r in marked)], insight=insight, filtered=filtered, unread=unread
    )


def _report_pin_outcome(
    *,
    served: bool,
    pins: int,
    refused: list[GfBackendError],
    stopped: GfBackendError | None,
    skipped: int,
    unmatched: int = 0,
    bags: bool = False,
    checks: str = "the routing",
    empty: int = 0,
    lost: Sequence[str] = (),
) -> None:
    """Account for what the pin loop met: a counted warning, or a raise.

    `unmatched` pins had return boards the row filter emptied, holding rows to
    `checks`. They are counted, not raised: that board was served, and "no
    return matches `checks`" is its answer. So is an `empty` one, a pin Google
    served no return for. `lost` names each pin dropped, after every count and
    before any raise: a count says the table is short, and only the names say
    which outbounds a user can look up elsewhere.

    Raising is for the case where nothing at all was served — then the refusal
    IS the outcome, and swallowing it reports a round trip with no return legs
    as a route with no return flights. Anything served makes every refusal a
    footnote to a real answer, and the counts are what tell the user their
    table is short."""
    # A short table is narrower than what was asked, and so is one without the
    # round trips the outbound board priced through an `empty` pin; a pin the
    # row filter emptied is an answer, so `unmatched` does not count.
    if served and (refused or empty or stopped is not None):
        narrow()
    # First, because two of the exits below leave by `raise` and nothing after
    # them runs. A stop rule that fires with nothing served would otherwise take
    # the per-URL refusals with it, and "rate-limited, wait and retry" is the
    # wrong advice for a round trip whose return boards no longer parse — the
    # page-shape change is the news, and this is the only place it is said.
    if unmatched:
        log.warning(
            "%d of %d pinned outbounds have no return flight matching %s",
            unmatched,
            pins,
            checks,
        )
    if refused:
        log.warning("%d of %d return boards unavailable: %s", len(refused), pins, refused[-1])
    if empty:
        log.warning("%d of %d pinned outbounds have no return flight on Google", empty, pins)
    if stopped is not None and served:
        # A partial round trip is a success BY CONTRACT: exit 0, `--format
        # json` in the ordinary shape, and this counted warning as the whole
        # account of what is missing. What that costs a machine consumer, and
        # why the alternatives were refused, is argued once under
        # "A partial round trip is a success, deliberately." in
        # docs/memories/gf_routing_and_carriers.md. The comment below covers
        # the all-refused arm and the pin-ORDER trade, which are different
        # questions. Driven by
        # `test_a_throttle_on_a_later_pin_keeps_what_was_already_served`, its
        # transport-outage sibling, and — for the arm whose sentence is read
        # off the exception —
        # `test_a_browser_that_dies_after_a_served_pin_still_says_what_to_do`.
        log.warning(
            "stopped pinning: %d of %d return boards skipped; %s",
            skipped,
            pins,
            _why_pinning_stopped(stopped, bags=bags),
        )
    for line in lost:
        log.warning("%s", line)
    if stopped is not None:
        if not served:
            raise stopped
    elif refused and not served:
        # "Nothing was served", rather than "every pin refused": one pin
        # returning a genuinely empty board is not a served pin, and suppressing
        # the raise on it reports a round trip whose return boards have stopped
        # parsing as a route with no return flights.
        #
        # The trade, stated: this also raises when one pin refused and the rest
        # came back honestly empty, so it prefers a false refusal to a false
        # "no flights". That is the right way round — a refusal degrades to
        # Matrix on the auto path and exits with a typed reason on the explicit
        # one, while "no results" is unrecoverable and indistinguishable from an
        # answer. The loop cannot tell the two empties apart anyway: a pin whose
        # own sub-pins all refused also comes back as nothing.
        #
        # The LAST refusal: the pins are independent queries, so no one of them
        # is more authoritative than another about the trip, and the last is the
        # one the counted warning above already names — the line the user reads
        # and the exception the caller degrades on then describe one event.
        #
        # A MIXED set is therefore decided by pin ORDER and not by kind, which
        # matters because `cli._gf_refusal` words each kind differently: a 503
        # arriving last speaks for boards that stopped parsing. Ranking the
        # kinds is the alternative, and it needs a rule this loop does not have.
        raise refused[-1]


# Matrix, the default remedy's last resort, prices no bags.
_BROWSER_BAGS_REMEDY = (
    "Retry, or use `--gf-transport http` (or drop `--bags` to search Matrix, which prices no bags)."
)


def browser_remedy(e: GfBrowserUnavailableError, *, bags: bool) -> str:
    """`e`'s remedy, ending on dropping `--bags` rather than on Matrix when
    the search asked for bags."""
    if bags and e.remedy.endswith(BROWSER_DEFAULT_REMEDY):
        return e.remedy.removesuffix(BROWSER_DEFAULT_REMEDY) + _BROWSER_BAGS_REMEDY
    return e.remedy


def _why_pinning_stopped(stopped: GfBackendError, *, bags: bool = False) -> str:
    """The clause naming what ended the fan-out, in the failure's own words.

    Not one fixed phrase, because the three stops send the reader to three
    different places: "was unreachable" describes a network and asks them to
    check one, while a Chrome that will not run is fixed locally and the
    exception is where that fix is written down. The caller prints this LAST
    in its line because a browser refusal ends in its own remedy, and a
    sentence that ends on the move the user makes reads as one."""
    if isinstance(stopped, GfThrottledError):
        return "Google Flights rate-limited this IP"
    if isinstance(stopped, GfBrowserUnavailableError):
        return f"the browser rung stopped — {stopped.reason} {browser_remedy(stopped, bags=bags)}"
    return "Google Flights was unreachable"
