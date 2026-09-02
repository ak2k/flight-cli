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

The board the page serves is Google's default (~30 rows per leg) with no
back-fill, so a top-N above that returns fewer rows than asked for.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import random
import re
import threading
import time
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
    FlightLeg,
    FlightResult,
)
from fli.models.google_flights.base import TripType  # pyright: ignore[reportMissingTypeStubs]

# DIVERGE: fli moved its API-row decoders to a private module in 0.9.0. These
# three (airline/airport/datetime) are purpose-built for decoding GF response
# rows — same signatures + AttributeError-on-unknown as the old SearchFlights
# static methods, with no public equivalent (core.parsers has no datetime
# parser), so this is a drop-in repoint.
from fli.search._decoders import (  # pyright: ignore[reportMissingTypeStubs]
    _parse_airline,  # pyright: ignore[reportPrivateUsage]
    _parse_airport,  # pyright: ignore[reportPrivateUsage]
    _parse_datetime,  # pyright: ignore[reportPrivateUsage]
)
from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]
from fli.search.flights import SearchFlights  # pyright: ignore[reportMissingTypeStubs]

from ._gf_errors import (
    GfBackendError,
    GfConsentError,
    GfPageShapeError,
    GfThrottledError,
)
from .links import build_search_tfs, google_flights_search_page_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
    )

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
# Mirrors fli's own `REQUEST_TIMEOUT`, which `_fetch_page` no longer inherits
# because it bypasses `Client.get`. A search page is multi-megabyte.
_REQUEST_TIMEOUT_S = 60.0


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
_SORRY_PATH = "/sorry/"
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
_CONSENT_FORM_RE = re.compile(
    rf"<form\b[^>]*\baction=[\"']https://consent\.google\.{_TLD}/", re.IGNORECASE
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
# that order so the page's ranking survives — we can't reproduce it.
_DS_ROW_BLOCKS = (2, 3)
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

    A page may carry more than one `ds:1` blob, so ALL of them are decoded and
    the one holding a board wins. Taking the first decodable blob was a
    positional bet: a placeholder emitted before the populated one — Google
    hydrates parts of this page in stages — would be served as an authoritative
    empty, or trip the arity guard, while the real board sat further down the
    document and was never looked at. Choosing by content instead of position
    costs one pass over blobs that are already in memory.

    When no blob holds a board, the first decodable one is returned unchanged,
    so a genuinely flight-less page stays a flight-less page rather than
    becoming a missing `ds:1`."""
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
        if _holds_a_board(decoded):
            return decoded
        if first_decodable is None:
            first_decodable = decoded
    return first_decodable


def _holds_a_board(payload: list[Any]) -> bool:
    """Does this payload carry a row block at `[2]` or `[3]`?

    Structural, like the scan at those indices: a block whose rows have all
    changed shape must still count as a board here, or a row-layout change
    would send us to a placeholder blob instead of reaching the 0-of-N guard."""
    return any(
        index < len(payload) and _looks_like_a_row_block(payload[index]) for index in _DS_ROW_BLOCKS
    )


def _is_page_throttled(*, final_url: str, html: str) -> bool:
    """True when Google blocked the fetch rather than serving a board.

    Checked BEFORE parsing: a block renders as zero rows, and "Google is
    throttling us" must never reach the user as "no flights on this route".

    Both signals are needed. The interstitial usually arrives as a redirect to
    `/sorry/`, but Google also serves it with HTTP 200 at the requested URL, and
    that variant is only visible in the body. An HTTP 429 never reaches here at
    all — fli's client raises it (see `_one_call`).

    The URL half reads the PATH, not the whole string: our own request URL
    carries a base64 `tfs=` parameter, and a substring test over the query
    string would call a served page a block on the right three bytes. The body
    half stays a substring because its marker is a whole English sentence, not a
    token that turns up in ordinary markup."""
    path = urllib.parse.urlsplit(final_url).path
    return _SORRY_PATH in path or any(marker in html for marker in _SORRY_MARKERS)


def _is_consent_page(*, final_url: str, html: str) -> bool:
    """True when the consent interstitial was served instead of the page.

    Two positive signals, both structural. The response came back FROM the
    consent host (or a `/consent` path on a Google host), or the body carries a
    form that submits TO the consent host. A results page links to that domain;
    it never posts to it.

    Still only meaningful once `ds:1` has come back missing — `_one_call` checks
    in that order — but it no longer answers "consent wall" for any page that
    merely mentions the domain."""
    parsed = urllib.parse.urlsplit(final_url)
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
_GOOGLE_DOMAIN_SUFFIX = "google.com"
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

# Position of the opaque per-flight ID in Google Flights' API row array.
# Mirrors the PP browser extension's parser (chunk-5KW5VSHS.js: `a = n[17]`).
_FLIGHT_ID_IDX = 17

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


@dataclass
class GFlightWithId:
    """fli's FlightResult plus Google's opaque flight_id and per-leg amenities.

    `amenities[i]` aligns with the i-th leg in `flight.legs` — same index
    in both lists points to the same physical segment.
    """

    flight: FlightResult
    flight_id: str
    amenities: list[LegAmenities]


def _parse_flight_with_id(data: list[Any]) -> GFlightWithId:
    """Mirror of fli's `_parse_flights_data` but also reads `data[0][17]`.

    Indices match the PP extension's parser (chunks/chunk-5KW5VSHS.js): n[17]
    is the per-flight opaque ID; n[2] legs; n[9] duration; t[0][-1] price."""
    price, currency = SearchFlights._parse_price_info(data)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
    flight_id = data[0][_FLIGHT_ID_IDX] if len(data[0]) > _FLIGHT_ID_IDX else ""
    leg_tuples: list[list[Any]] = data[0][2]
    flight = FlightResult(
        price=price,
        currency=currency,
        duration=data[0][9],
        stops=len(leg_tuples) - 1,
        legs=[_flight_leg(fl) for fl in leg_tuples],
    )
    amenities = [_parse_leg_amenities(fl) for fl in leg_tuples]
    return GFlightWithId(flight=flight, flight_id=flight_id, amenities=amenities)


def _flight_leg(fl: list[Any]) -> FlightLeg:
    """Build fli's FlightLeg using the BOOKING carrier (see `_resolve_booking`)
    as airline/flight_number — not the operating carrier — so the surfaced flight
    matches what a passenger books (and what Matrix returns). The operating
    carrier is preserved separately on `LegAmenities`."""
    book_code, book_number = _resolve_booking(fl)
    if book_code is None:
        # No operating or marketing carrier in the tuple — malformed leg. Raise
        # so _one_call's except skips this flight, matching the prior behaviour
        # of indexing a missing fl[22][0].
        raise ValueError("leg tuple missing carrier identity")
    return FlightLeg(
        airline=_parse_airline(book_code),
        flight_number=book_number or "",
        departure_airport=_parse_airport(fl[3]),
        arrival_airport=_parse_airport(fl[6]),
        departure_datetime=_parse_datetime(fl[20], fl[8]),
        arrival_datetime=_parse_datetime(fl[21], fl[10]),
        duration=fl[11],
    )


def _cookie_path() -> pathlib.Path:
    """Where the warmed gflight session cookies live — the shared CLI cache dir
    (same `MATRIX_CACHE_DIR` override the response cache honors)."""
    cache_dir = pathlib.Path(
        os.environ.get("MATRIX_CACHE_DIR") or pathlib.Path.home() / ".cache" / "flight-cli"
    )
    return cache_dir / "gflight-cookies.json"


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
        for c in saved:
            name = str(c["name"])
            domain = str(c.get("domain", ".google.com"))
            # The SAME allowlist the write side applies, checked again on the
            # way in. The cache is a plain file in a shared directory: anything
            # that can edit it could otherwise add a cookie of any name for any
            # domain and have this seed it onto a live session.
            if name not in _PERSIST_COOKIE_NAMES or _GOOGLE_DOMAIN_SUFFIX not in domain:
                log.debug("ignoring non-allowlisted cookie %r for %r in the cache", name, domain)
                continue
            client._session().cookies.set(
                name,
                c["value"],
                domain=domain,
                path=c.get("path", "/"),
            )
    except Exception as e:  # noqa: BLE001 — seeding is best-effort (corrupt/odd cache, never fatal)
        log.debug("could not seed gflight cookies: %s", e)


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
            for ck in client._session().cookies.jar  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
            if str(ck.name) in _PERSIST_COOKIE_NAMES
            and _GOOGLE_DOMAIN_SUFFIX in str(ck.domain or "")
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
        # 0700 explicitly: `mkdir` takes the umask otherwise, and the directory
        # holds a live Google session cookie.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    have all changed shape must reach the 0-of-N guard in `_one_call` and be
    reported as a layout change, not silently drop to an empty board."""
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


def _fetch_page(client: Any, url: str) -> Any:
    """GET the search page through fli's session, bypassing fli's `Client.get`.

    DIVERGE, and the reason is a request budget. `Client.get` is wrapped in
    `@retry(stop_after_attempt(3))` and calls `raise_for_status()`, so a
    persistently throttled leg cost three fli attempts inside each of our
    throttle retries — up to fifteen multi-megabyte GETs for one leg, and a
    multi-cabin round trip multiplies that by the cabin count. Two ladders
    stacked on the same signal is not a policy anyone chose. `retry_throttled`
    is now the only one, so throttle handling lives where the classification
    does.

    What comes back is the raw response: this function does not raise on a
    non-2xx, because a 429 IS the signal and swallowing it into an exception is
    what hid it from us before. The caller classifies the status.

    Kept from `Client.get`: the shared 10 req/sec token bucket (a
    process-global budget the fan-out threads share) and its request timeout.
    NOT kept: fli's transport-error retry — a connection reset now fails the
    leg instead of being retried three times. That is the deliberate cost of
    owning the ladder; the enriched path still answers from Matrix."""
    client._rate_limiter.acquire()  # pyright: ignore[reportAny]
    return client._session().get(  # pyright: ignore[reportAny]
        url,
        impersonate="chrome",
        allow_redirects=True,
        timeout=_REQUEST_TIMEOUT_S,
    )


def _one_call(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """One GET of the public search page; flat list of one leg's flights.

    Refusals are typed and raised (`GfThrottledError` / `GfConsentError` /
    `GfPageShapeError`); a page that decodes with zero rows returns `[]`, which
    is Google's authoritative answer and not retried."""
    client = get_client()
    _seed_cookies_once(client)
    url = google_flights_search_page_url(build_search_tfs(filters))
    resp = _fetch_page(client, url)
    status = int(resp.status_code)  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
    final_url = str(resp.url)  # pyright: ignore[reportAny]
    html = cast("str", resp.text)  # pyright: ignore[reportAny]
    if status == HTTPStatus.TOO_MANY_REQUESTS or _is_page_throttled(final_url=final_url, html=html):
        raise GfThrottledError("Google Flights rate-limited the request")
    if status >= HTTPStatus.BAD_REQUEST:
        # Not a throttle and not a page we can read. `GfBackendError` is the
        # fallback seam, so this degrades to Matrix rather than ending the
        # command — the same outcome a shape change gets, without claiming the
        # layout changed when Google simply answered 503.
        raise GfBackendError(f"Google Flights' search page returned HTTP {status}")
    payload = _extract_ds1(html)
    if payload is None:
        if _is_consent_page(final_url=final_url, html=html):
            raise GfConsentError(
                "Google served its consent page instead of search results (no flight rows to read)"
            )
        raise GfPageShapeError(
            "Google Flights' search page carried no readable ds:1 payload; the page shape changed"
        )
    # A decoded payload means Google answered a warm session — save its cookies
    # (NID) so the next one-shot CLI process starts warm instead of cold.
    _persist_cookies(client)
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
        return []  # Google's own answer: this leg has no flights.
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
        raise GfPageShapeError(
            f"none of {len(rows)} Google Flights rows parsed; "
            f"the row shape changed (sample reasons: {sample})"
        )
    return out


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

    Transport errors propagate (fli's client already retried them)."""
    empty_attempts = 0
    throttle_attempts = 0
    while True:
        try:
            result = call()
        except GfThrottledError:
            throttle_attempts += 1
            if throttle_attempts > _THROTTLE_RETRY_ATTEMPTS:
                raise
            base = _THROTTLE_BACKOFF_S * (2 ** (throttle_attempts - 1))
            backoff = base * (1 + random.random() * 0.5)  # noqa: S311 — jitter, not crypto
            log.debug(
                "gflight throttled; backoff %.1fs (retry %d/%d)",
                backoff,
                throttle_attempts,
                _THROTTLE_RETRY_ATTEMPTS,
            )
            time.sleep(backoff)
            continue
        if result or not retry_empty:
            return result
        empty_attempts += 1
        if empty_attempts >= _EMPTY_RETRY_ATTEMPTS:
            return result  # never warmed, or genuinely empty
        log.debug("empty gflight response; retry %d/%d", empty_attempts, _EMPTY_RETRY_ATTEMPTS)
        time.sleep(_EMPTY_RETRY_BACKOFF_S * empty_attempts)


def _one_call_with_retry(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """`_one_call` under the throttle retry only — a parsed-empty page is an
    answer, so it costs exactly one GET and no sleep."""
    return retry_throttled(lambda: _one_call(filters), retry_empty=False)


# How many outbound flights a round trip pins and re-fetches. Each pin is one
# more multi-megabyte page GET, so this is a REQUEST budget, not a result limit.
#
# The multi-cabin path bumps top_n (`_MULTI_CABIN_QUERY_BUMP_FACTOR`, 5x, capped
# at 100) to widen the pool it filters, which was free when this was an RPC and
# is not free on the page transport: at top_n=100 a two-cabin round trip would
# fan out to ~2 x 31 page fetches. The default `-n 10` is unchanged by this cap;
# above it, the round trip returns combinations for the ten best outbounds
# rather than for all of them.
_PINNED_FANOUT_CAP = 10


def _pinned_fanout(top_n: int) -> int:
    """How many outbounds to pin, given the caller's top_n."""
    return min(top_n, _PINNED_FANOUT_CAP)


def search_with_ids(
    filters: FlightSearchFilters,
    *,
    top_n: int = 5,
) -> list[GFlightWithId | tuple[GFlightWithId, ...]] | None:
    """Drop-in for fli's `SearchFlights().search()` but each result carries
    its Google Flights opaque flight_id.

    Round-trip / multi-city follow the same iterative leg-selection pattern
    as fli: query first leg, pick top_n, drive each through the rest. Each
    `GFlightWithId` in a returned tuple has its own per-leg flight_id."""
    first = _one_call_with_retry(filters)
    if not first:
        return None

    if filters.trip_type == TripType.ONE_WAY:
        return list(first)

    num_segments = len(filters.flight_segments)
    selected_count = sum(1 for s in filters.flight_segments if s.selected_flight is not None)
    # Last leg already — no further iteration.
    if selected_count >= num_segments - 1:
        return list(first)

    combos: list[GFlightWithId | tuple[GFlightWithId, ...]] = []
    for picked in first[: _pinned_fanout(top_n)]:
        next_filters = deepcopy(filters)
        next_filters.flight_segments[selected_count].selected_flight = picked.flight
        nxt = search_with_ids(next_filters, top_n=top_n)
        if nxt is None:
            continue
        for nx in nxt:
            if isinstance(nx, tuple):
                combos.append((picked, *nx))
            else:
                combos.append((picked, nx))
    return combos or None
