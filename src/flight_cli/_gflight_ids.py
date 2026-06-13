"""Wrapper around fli's Google Flights search that ALSO captures the opaque
`flightId` Google emits at index [17] of each flight row.

fli's `SearchFlights._parse_flights_data` parses legs, price, duration, stops —
but drops `data[0][17]`. PointsPath's `enableGoogleFlightMatching` mode joins
its award catalog against exactly that opaque ID (see PP browser extension
chunk-5KW5VSHS.js: `flightId: a` where `a = n[17]`). Without it, PP returns
an empty result for hint-based queries; with it, `matchedGoogleFlightId`
echoes back populated.

We re-use fli's `FlightSearchFilters.encode()` + curl_cffi client so the
request shape stays in lockstep with upstream; only the response parser
diverges (extends fli's by one field).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fli.models import (  # pyright: ignore[reportMissingTypeStubs]
    FlightLeg,
    FlightResult,
)
from fli.models.google_flights.base import TripType  # pyright: ignore[reportMissingTypeStubs]
from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]
from fli.search.flights import SearchFlights  # pyright: ignore[reportMissingTypeStubs]

if TYPE_CHECKING:
    from fli.models.google_flights.flights import (  # pyright: ignore[reportMissingTypeStubs]
        FlightSearchFilters,
    )

log = logging.getLogger(__name__)

_BASE_URL = SearchFlights.BASE_URL

# Google Flights' API intermittently answers a cold curl_cffi session with an
# empty body (HTTP 200, no error to retry on). fli's client is a process-wide
# singleton that warms up by acquiring a cookie on its first successful call —
# but each one-shot `flight` invocation starts a fresh process with a cold
# session, so its single request frequently comes back empty. Empirically
# (None,None,123,123 on identical inputs; [0,123,123,0,123,123] over one warming
# client) the empty almost always clears within a couple of retries on the SAME
# session. A FRESH session does NOT help — it stays cold — so we retry in place.
_EMPTY_RETRY_ATTEMPTS = 4
_EMPTY_RETRY_BACKOFF_S = 1.0  # multiplied by attempt number: 1s, 2s, 3s between tries

# Persisted gflight session cookies. The cold-session empties above are almost
# entirely "the session is missing Google's NID cookie" — a long-lived (~6mo)
# session cookie a browser keeps across restarts. Empirically, seeding a saved
# NID onto a fresh session drops the cold-start empty rate from ~40% to ~0%, so
# we persist it after a successful call and reload it at startup. Every
# subsequent one-shot `flight` process then starts warm; the retry above stays
# as the fallback for the first-ever run and NID rotation.
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
# Once-per-process latches (a dict so we mutate rather than rebind a global).
_cookie_state: dict[str, bool] = {"seeded": False, "persisted": False}

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


def _parse_leg_amenities(fl: list[Any]) -> LegAmenities:
    """Defensive read of indices 12-17 from a leg tuple. Returns an
    all-None LegAmenities if any single field is missing or wrong type —
    Google's response shape drifts and a partial extract is better than
    dropping the whole flight."""
    amenities = fl[_LEG_AMENITIES_IDX] if len(fl) > _LEG_AMENITIES_IDX else None
    legroom_raw = fl[_LEG_LEGROOM_CLASS_IDX] if len(fl) > _LEG_LEGROOM_CLASS_IDX else None
    pitch_raw = fl[_LEG_PITCH_IDX] if len(fl) > _LEG_PITCH_IDX else None
    cabin_raw = fl[_LEG_CABIN_IDX] if len(fl) > _LEG_CABIN_IDX else None
    aircraft = fl[_LEG_AIRCRAFT_IDX] if len(fl) > _LEG_AIRCRAFT_IDX else None
    return LegAmenities(
        aircraft=aircraft if isinstance(aircraft, str) and aircraft else None,
        pitch_inches=_parse_pitch(pitch_raw),
        legroom_class=_LEGROOM_CLASS.get(legroom_raw) if isinstance(legroom_raw, int) else None,
        cabin=_CABIN.get(cabin_raw) if isinstance(cabin_raw, int) else None,
        wifi=_decode_wifi(amenities),
        power=_decode_power(amenities),
        video=_decode_video(amenities),
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
        legs=[
            FlightLeg(
                airline=SearchFlights._parse_airline(fl[22][0]),  # pyright: ignore[reportPrivateUsage]
                flight_number=fl[22][1],
                departure_airport=SearchFlights._parse_airport(fl[3]),  # pyright: ignore[reportPrivateUsage]
                arrival_airport=SearchFlights._parse_airport(fl[6]),  # pyright: ignore[reportPrivateUsage]
                departure_datetime=SearchFlights._parse_datetime(fl[20], fl[8]),  # pyright: ignore[reportPrivateUsage]
                arrival_datetime=SearchFlights._parse_datetime(fl[21], fl[10]),  # pyright: ignore[reportPrivateUsage]
                duration=fl[11],
            )
            for fl in leg_tuples
        ],
    )
    amenities = [_parse_leg_amenities(fl) for fl in leg_tuples]
    return GFlightWithId(flight=flight, flight_id=flight_id, amenities=amenities)


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
    if _cookie_state["seeded"]:
        return
    _cookie_state["seeded"] = True
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
            client._client.cookies.set(
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
            for ck in client._client.cookies.jar  # pyright: ignore[reportAny]  # fli/curl_cffi untyped
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


def _one_call(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """Single HTTP round-trip to Google's endpoint; flat list of one leg's flights."""
    client = get_client()
    _seed_cookies_once(client)
    encoded = filters.encode()
    resp = client.post(
        url=_BASE_URL,
        data=f"f.req={encoded}",
        impersonate="chrome",
        allow_redirects=True,
    )
    resp.raise_for_status()
    parsed = json.loads(resp.text.lstrip(")]}'"))[0][2]
    if not parsed:
        return []
    # A truthy `parsed` means Google answered a warm session — save its cookies
    # (NID) so the next one-shot CLI process starts warm instead of cold.
    _persist_cookies(client)
    inner = json.loads(parsed)
    flights_data: list[Any] = [
        item for i in (2, 3) if isinstance(inner[i], list) for item in inner[i][0]
    ]
    out: list[GFlightWithId] = []
    for fd in flights_data:
        try:
            out.append(_parse_flight_with_id(fd))
        except (AttributeError, KeyError, ValueError, IndexError) as e:
            log.debug("skipping flight with unparseable data: %s", e)
            continue
    return out


def _one_call_with_retry(filters: FlightSearchFilters) -> list[GFlightWithId]:
    """`_one_call`, but retried on an empty result to ride out the cold-session
    empties described at `_EMPTY_RETRY_ATTEMPTS`.

    Retries reuse fli's shared (warming) client — that's the whole point; a
    fresh session would stay cold. A genuinely flight-less leg pays a few quick
    retries of latency, which is rare and preferable to a spurious "no results".
    """
    result: list[GFlightWithId] = []
    for attempt in range(1, _EMPTY_RETRY_ATTEMPTS + 1):
        result = _one_call(filters)
        if result:
            return result
        if attempt < _EMPTY_RETRY_ATTEMPTS:
            log.debug("empty gflight response; retry %d/%d", attempt, _EMPTY_RETRY_ATTEMPTS)
            time.sleep(_EMPTY_RETRY_BACKOFF_S * attempt)
    return result


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
    for picked in first[:top_n]:
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
