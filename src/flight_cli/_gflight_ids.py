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

That page has two transports (`GfTransport`, `_one_call_laddered`): rung 1 is
the curl_cffi GET below, rung 2 is a real Chrome navigating the same URL
(`_gf_browser`), which earns a far larger rate budget. Both go through
`_rows_from_page_html` — one parser, one set of verdicts about what a block
means. A rung supplies bytes; it never gets to interpret them.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple, assert_never, cast

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
from fli.search.exceptions import (  # pyright: ignore[reportMissingTypeStubs]
    SearchHTTPError,
)
from fli.search.flights import SearchFlights  # pyright: ignore[reportMissingTypeStubs]

# Rung 2, imported like anything else. It costs 0.2 ms and pulls no optional
# dependency — patchright is loaded inside `_playwright_factory`, at launch —
# and `_gf_common` broke the cycle that used to force this import into a
# function body. Imported as a module, not `from ._gf_browser import session`,
# so `_one_call_browser` looks the attribute up per call and a test can
# substitute the session without a browser anywhere in the process.
from . import _gf_browser
from ._gf_common import TRANSPORT_HTTP, GfTransportMode, PageFetch, cache_dir
from ._gf_errors import (
    GfConsentError,
    GfPageShapeError,
    GfThrottledError,
    GfUpstreamStatusError,
)
from .links import build_search_tfs, google_flights_search_page_url

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
    )

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
# Consent interstitial markers. EU/EEA egress lands here; the page has no
# `ds:1`, so without this it would be indistinguishable from a shape change.
_CONSENT_MARKERS = ("consent.google.com", "/consent?continue=", "CONSENT_PAGE")

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


def _extract_ds1(html: str) -> list[Any] | None:
    """The decoded `ds:1` payload from a rendered search page, or None when the
    page doesn't carry one in the shape we read.

    Shaped like the RPC's own payload, so callers index `payload[2]` /
    `payload[3]` for flight rows."""
    # `continue`, never an early `return`: the page may carry more than one
    # `ds:1` blob, and giving up on the first undecodable one would report a
    # readable board as a shape change.
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
        except ValueError:
            log.debug("ds:1 blob is not valid JSON")
            continue
        if isinstance(payload, list):
            return cast("list[Any]", payload)
    return None


def _is_page_throttled(*, final_url: str, html: str) -> bool:
    """True when Google blocked the fetch rather than serving a board.

    Checked BEFORE parsing: a block renders as zero rows, and "Google is
    throttling us" must never reach the user as "no flights on this route".

    Both signals are needed. The interstitial usually arrives as a redirect to
    `/sorry/`, but Google also serves it with HTTP 200 at the requested URL, and
    that variant is only visible in the body. An HTTP 429 never reaches here at
    all — fli's client raises it (see `_fetch_page`)."""
    return _SORRY_PATH in final_url or any(marker in html for marker in _SORRY_MARKERS)


def _is_consent_page(*, final_url: str, html: str) -> bool:
    """True when the consent interstitial was served instead of the page.

    Only meaningful once `ds:1` has already come back missing: a real results
    page links to Google's consent domain in its footer, so these markers on
    their own don't mean the board is absent."""
    return any(marker in final_url or marker in html for marker in _CONSENT_MARKERS)


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
        # so the parse loop in `_rows_from_page_html` skips this flight, matching
        # the prior behaviour of indexing a missing fl[22][0].
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
    """Where the warmed gflight session cookies live."""
    return cache_dir() / "gflight-cookies.json"


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
            client._session().cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ".google.com"),
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time.time(), "cookies": cookies}, indent=2))
        _cookie_state["persisted"] = True
    except OSError as e:
        log.debug("could not persist gflight cookies: %s", e)


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


def _holds_flight_rows(block: Any) -> bool:
    """Strict test: does `block`'s first element PARSE as a flight row?

    Used only away from those indices, to spot rows that moved. `ds:1` carries
    several other list-of-list-of-list structures — on every page measured,
    indices 1, 7, 14 and 17 among them — so the structural test above would
    call those relocated rows and refuse every ordinary page."""
    if not _looks_like_a_row_block(block):
        return False
    inner = cast("list[Any]", cast("list[Any]", block)[0])
    if not inner:
        return False
    try:
        _parse_flight_with_id(inner[0])
    except (AttributeError, KeyError, ValueError, IndexError, TypeError):
        return False
    return True


def _rows_from_ds1(payload: list[Any]) -> _Ds1Board:
    """The flight rows inlined in a `ds:1` payload, top-flights board first,
    plus where else in the payload flight rows turned up.

    How many row blocks a SERVED page carries varies by request — see the
    shapes table in docs/memories/gf_routing_and_carriers.md — so counting them
    is not a validity test, and a board with no flights at all is served with
    nothing at either index. The only layout change the payload can actually
    prove is rows appearing somewhere we don't read, so the scan is positive:
    collect from the indices we read, and probe every other index for rows that
    moved. The two use different tests, on purpose — see each."""
    rows: list[Any] = []
    blocks_seen = 0
    misplaced: list[int] = []
    for index, block in enumerate(payload):
        if index in _DS_ROW_BLOCKS:
            if _looks_like_a_row_block(block):
                blocks_seen += 1
                rows.extend(cast("list[Any]", cast("list[Any]", block)[0]))
        elif _holds_flight_rows(block):
            misplaced.append(index)
    return _Ds1Board(rows, blocks_seen, tuple(misplaced))


def search_page_url(filters: FlightSearchFilters) -> str:
    """The public search-page URL for `filters` — the one address both rungs
    fetch, so neither can drift into asking Google a different question."""
    return google_flights_search_page_url(build_search_tfs(filters))


def _fetch_page(filters: FlightSearchFilters) -> PageFetch:
    """One GET of the public search page.

    Everything visible in the bytes themselves is left to
    `_rows_from_page_html`, so rung 2 — which fetches the same page through
    Chrome and never goes near fli — reaches the same verdicts from the same
    evidence.

    The one thing it must classify here is the throttle fli hides. `Client.get`
    calls `raise_for_status()` itself, inside a three-attempt retry, and wraps
    what it catches in `SearchHTTPError` — so a 429 never comes back as a
    response for anyone downstream to inspect, and this is the only place that
    can name it. Measured against fli 0.9.0 with a stubbed session: a
    persistent 429 costs three GETs and ~3 s inside fli before it raises.

    The status is reported as `OK` rather than read off the response, and that
    is the honest value: fli raised on anything else before returning, so a
    response reaching this line is 2xx by construction. The browser rung is
    where a real non-2xx shows up, because a navigation reports one instead of
    raising on it."""
    client = get_client()
    _seed_cookies_once(client)
    try:
        resp = client.get(search_page_url(filters), impersonate="chrome", allow_redirects=True)
    except SearchHTTPError as e:
        if e.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise GfThrottledError("Google Flights rate-limited the request") from e
        raise
    return PageFetch(html=resp.text, final_url=str(resp.url), status_code=HTTPStatus.OK)


def _rows_from_page_html(page: PageFetch) -> list[GFlightWithId]:
    """The flight rows a rendered search page carries — the single parser both
    rungs go through; a rung supplies bytes, never interpretation.

    The order is load-bearing. The captcha interstitial is a throttle before it
    is anything else; only then is a non-2xx Google declining to serve; only then
    is a missing `ds:1` read as consent; only then is an absent row block one.
    Every one of those would otherwise decode as zero rows and reach the user
    as "no flights on this route".

    Refusals are typed and raised (`GfThrottledError` / `GfConsentError` /
    `GfPageShapeError`); a page that decodes with zero rows returns `[]`, which
    is Google's authoritative answer and not retried."""
    html, final_url, status_code = page
    # Both status tests serve the browser rung alone — Chrome reports a status
    # where fli raises on it, so a curl_cffi page arrives here already 2xx with
    # its 429 turned into this same refusal. The 429 test must come FIRST: the
    # next branch would otherwise claim it as a generic upstream status and lose
    # the one fact a caller can act on, that backing off is the fix.
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
    if board.misplaced:
        # Rows exist, just not where we read them. This is the one layout
        # change the payload can actually prove; an absent board cannot be told
        # apart from a flight-less one.
        raise GfPageShapeError(
            f"ds:1 holds flight rows at {list(board.misplaced)}, not at "
            f"{list(_DS_ROW_BLOCKS)} (found {board.blocks_seen} there); "
            "the payload layout changed"
        )
    rows = board.rows
    if not rows:
        return []  # Google's own answer: this leg has no flights.
    out: list[GFlightWithId] = []
    reasons: list[str] = []
    for fd in rows:
        try:
            out.append(_parse_flight_with_id(fd))
        except (AttributeError, KeyError, ValueError, IndexError) as e:
            log.debug("skipping flight with unparseable data: %s", e)
            reasons.append(f"{type(e).__name__}: {e}")
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


def _one_call(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """Rung 1: fetch the search page over curl_cffi and read its rows."""
    rows = _rows_from_page_html(_fetch_page(filters))
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


def _one_call_browser(filters: FlightSearchFilters, *, headed: bool) -> list[GFlightWithId]:
    """Rung 2: one real-Chrome navigation of the same URL, read by the same parser.

    No retry ladder around it. Rung 2 costs a browser launch and up to a 30 s
    navigation, and a refusal it hits is terminal — re-driving Chrome through
    rung 1's backoff would spend ~22 s more to be told the same thing.

    Reached through the module attribute, never a name bound at import time, so
    a test can substitute the session without a browser anywhere in the
    process."""
    return _rows_from_page_html(
        _gf_browser.session(headed=headed).get_html(search_page_url(filters))
    )


def _one_call_laddered(filters: FlightSearchFilters, transport: GfTransport) -> list[GFlightWithId]:
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
            return _one_call_browser(filters, headed=transport.headed)
        case "http" | "auto":
            return _one_call_with_retry(filters)
        case _:
            assert_never(transport.mode)


def search_with_ids(
    filters: FlightSearchFilters,
    *,
    top_n: int = 5,
    transport: GfTransport = HTTP_TRANSPORT,
) -> list[GFlightWithId | tuple[GFlightWithId, ...]] | None:
    """Drop-in for fli's `SearchFlights().search()` but each result carries
    its Google Flights opaque flight_id.

    Round-trip / multi-city follow the same iterative leg-selection pattern
    as fli: query first leg, pick top_n, drive each through the rest. Each
    `GFlightWithId` in a returned tuple has its own per-leg flight_id.

    `transport` rides the recursion so every leg of one trip runs on the same
    rung — a round trip that opened Chrome for its outbound must not silently
    drop back to curl_cffi for the returns."""
    first = _one_call_laddered(filters, transport)
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
    for picked in first[:top_n]:
        next_filters = deepcopy(filters)
        next_filters.flight_segments[selected_count].selected_flight = picked.flight
        nxt = search_with_ids(next_filters, top_n=top_n, transport=transport)
        if nxt is None:
            continue
        for nx in nxt:
            if isinstance(nx, tuple):
                combos.append((picked, *nx))
            else:
                combos.append((picked, nx))
    return combos or None
