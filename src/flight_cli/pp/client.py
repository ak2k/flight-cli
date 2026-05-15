"""Async PointsPath HTTP client.

Three endpoints matter:
  POST /api/airline-search    one POST per airline (request body holds route + cabin)
  GET  /api/pricing-info      airline catalog + transfer-partner mapping (cached 24h)
  GET  /api/extension-config  per-tier feature flags (cached 7d) — drives airline filter

401 → refresh tokens once → retry. Anything else propagates.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from .auth import Tokens, get_valid_tokens, refresh as refresh_tokens
from .models import AirlineSearchResponse, PricingInfoResponse

log = logging.getLogger(__name__)

API_BASE = "https://api.pointspath.com"

PRICING_CACHE = Path.home() / ".cache" / "flight-cli" / "pp_pricing.json"
PRICING_TTL_SECS = 24 * 3600

EXT_CONFIG_CACHE = Path.home() / ".cache" / "flight-cli" / "pp_extension_config.json"
EXT_CONFIG_TTL_SECS = 7 * 24 * 3600
EXT_CONFIG_VERSION = "1.10.4"

# Airlines observed firing in a single GFlights international search. Used as
# the default fan-out set when --pp-airlines isn't provided. Real catalog
# probably differs by Pro tier — the extension's /api/extension-config feature
# flags drive this — but starting with what we observed avoids guessing.
DEFAULT_AIRLINES: tuple[str, ...] = (
    "AirCanada", "AirFrance", "Alaska", "American", "Avianca",
    "Delta", "Etihad", "JetBlue", "Qantas", "Qatar",
    "United", "VirginAtlantic", "VirginAustralia",
)

DEFAULT_CABINS: tuple[str, ...] = ("Economy", "Business")

# PointsPath fans out concurrent queries; mirror that but bound it. The server
# already 500s under load (we saw 2/40 in our session); 5 is a safe default.
DEFAULT_CONCURRENCY = 5


@dataclass
class CashFlightHint:
    """Subset of GFlights itinerary data the airline-search endpoint expects
    when enableGoogleFlightMatching=True. Unused when matching is False."""
    origin: str
    dest: str
    start_dt: str   # "YYYY-MM-DD HH:MM"
    end_dt: str
    flight_id: str
    airline: str
    google_airlines: list[str]
    num_connections: int
    first_flight_number: str
    cash_price_usd: int
    raw_cash_price: str

    def to_payload(self) -> dict:
        return {
            "origin": self.origin,
            "dest": self.dest,
            "startDateTime": self.start_dt,
            "endDateTime": self.end_dt,
            "flightId": self.flight_id,
            "airline": self.airline,
            "googleAirlines": self.google_airlines,
            "numConnections": self.num_connections,
            "hasCarryOnBaggage": False,
            "firstFlightNumber": self.first_flight_number,
            "cashPrice": self.cash_price_usd,
            "rawCashPriceString": self.raw_cash_price,
        }


@dataclass
class SearchSpec:
    origin: str
    destination: str
    date: str           # YYYY-MM-DD
    return_date: str = ""
    is_round_trip_return: bool = False
    num_passengers: int = 1
    cabin_class: str = "Economy"
    currency: str = "USD"
    enable_matching: bool = False
    cash_hints: tuple[CashFlightHint, ...] = ()
    disable_cache: bool = False


def _payload(spec: SearchSpec, airline: str) -> dict:
    return {
        "selectedFlightNumber": "",
        "selectedFlightPricing": [],
        "originAirport": spec.origin,
        "destinationAirport": spec.destination,
        "date": spec.date,
        "returnDate": spec.return_date,
        "numPassengers": spec.num_passengers,
        "googleFlightCurrencyCode": spec.currency,
        "cabinClass": spec.cabin_class,
        "isRoundTripReturn": spec.is_round_trip_return,
        "airline": airline,
        "enableGoogleFlightMatching": spec.enable_matching,
        "googleFlightDetails": [h.to_payload() for h in spec.cash_hints],
        "disableCache": spec.disable_cache,
    }


class PPClient:
    """Thin async wrapper. Construct with `await PPClient.create()` so token
    refresh runs once up front."""

    def __init__(self, tokens: Tokens, *, timeout: float = 25.0,
                 concurrency: int = DEFAULT_CONCURRENCY):
        self._tokens = tokens
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={"user-agent": _UA},
        )

    @classmethod
    async def create(cls, **kw) -> "PPClient":
        # get_valid_tokens is sync (httpx.post for refresh) — fine; called once.
        return cls(get_valid_tokens(), **kw)

    async def aclose(self) -> None:
        await self._client.aclose()

    # context-manager sugar
    async def __aenter__(self) -> "PPClient": return self
    async def __aexit__(self, *exc) -> None: await self.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._tokens.access_token}",
            "content-type": "application/json",
        }

    async def _request(self, method: str, path: str, *,
                       json_body: Optional[dict] = None,
                       params: Optional[dict] = None) -> httpx.Response:
        async with self._sem:
            r = await self._client.request(
                method, path, json=json_body, params=params,
                headers=self._auth_headers(),
            )
        if r.status_code == 401:
            log.info("pp: 401 — refreshing tokens and retrying once")
            self._tokens = refresh_tokens(self._tokens)
            async with self._sem:
                r = await self._client.request(
                    method, path, json=json_body, params=params,
                    headers=self._auth_headers(),
                )
        return r

    async def airline_search(self, spec: SearchSpec, airline: str) -> AirlineSearchResponse:
        r = await self._request("POST", "/api/airline-search",
                                json_body=_payload(spec, airline))
        # 204 = airline has nothing for this route+date; return empty model.
        if r.status_code == 204 or not r.content:
            return AirlineSearchResponse()
        if r.status_code >= 400:
            log.warning("pp: airline-search %s → %s %s",
                        airline, r.status_code, r.text[:200])
            return AirlineSearchResponse()
        return AirlineSearchResponse.model_validate(r.json())

    async def airline_search_many(self, spec: SearchSpec,
                                   airlines: tuple[str, ...]) -> dict[str, AirlineSearchResponse]:
        """Fan out one request per airline; concurrency-bounded by the semaphore."""
        async def one(a: str) -> tuple[str, AirlineSearchResponse]:
            return a, await self.airline_search(spec, a)
        results = await asyncio.gather(*(one(a) for a in airlines), return_exceptions=True)
        out: dict[str, AirlineSearchResponse] = {}
        for r in results:
            if isinstance(r, BaseException):
                log.warning("pp: airline-search exception: %r", r)
                continue
            out[r[0]] = r[1]
        return out

    async def pricing_info(self, *, force_refresh: bool = False) -> PricingInfoResponse:
        if not force_refresh and PRICING_CACHE.exists():
            age = time.time() - PRICING_CACHE.stat().st_mtime
            if age < PRICING_TTL_SECS:
                return PricingInfoResponse.model_validate(
                    json.loads(PRICING_CACHE.read_text())
                )
        r = await self._request("GET", "/api/pricing-info")
        r.raise_for_status()
        PRICING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PRICING_CACHE.write_text(r.text)
        return PricingInfoResponse.model_validate(r.json())

    async def extension_config(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Fetch /api/extension-config?v=<version>. Cached 7d on disk.

        The shape is large and version-tagged; we only consume `featureFlags`
        currently, but the full body is cached so future code paths can read
        other parts (e.g. valuation overrides) without re-fetching.
        """
        if not force_refresh and EXT_CONFIG_CACHE.exists():
            age = time.time() - EXT_CONFIG_CACHE.stat().st_mtime
            if age < EXT_CONFIG_TTL_SECS:
                return json.loads(EXT_CONFIG_CACHE.read_text())
        r = await self._request(
            "GET", "/api/extension-config",
            params={"v": EXT_CONFIG_VERSION},
        )
        r.raise_for_status()
        EXT_CONFIG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXT_CONFIG_CACHE.write_text(r.text)
        return r.json()


# Match `enable<AirlineName>` exactly, or `enable<AirlineName>V<digits>` (the
# Vn pattern is how PointsPath versions individual airline integrations — e.g.
# `enableAirFranceV2`). Sub-feature flags like `enableDeltaTakeOff15` or
# `enableSpiritSaversClub` carry trailing words and so don't match.
_AIRLINE_FLAG_RE = re.compile(r"^enable(?P<name>[A-Z][A-Za-z]+?)(?:V\d+)?$")


def enabled_airlines(
    pricing: PricingInfoResponse,
    ext_config: dict[str, Any],
) -> tuple[str, ...]:
    """Pick the airline-search call set: pricing-info airlines (universe)
    intersected with extension-config feature flags.

    Rule: an airline is enabled if either (a) no `enable<Airline>` flag exists
    (always-on, e.g. American, Delta, United, JetBlue, Alaska), or (b) the
    flag exists with value 1. Falsy flags (e.g. `enableSingapore=0`) suppress
    the airline.
    """
    flags: dict[str, int] = ext_config.get("featureFlags", {}) or {}

    # Build a name → truthy-or-not lookup, ignoring sub-feature flags.
    flag_for_airline: dict[str, bool] = {}
    for flag, val in flags.items():
        m = _AIRLINE_FLAG_RE.match(flag)
        if not m:
            continue
        # Last writer wins on collisions (e.g. enableAirFrance + enableAirFranceV2).
        # In practice PP uses one form per airline at a time.
        flag_for_airline[m.group("name").lower()] = bool(val)

    enabled: list[str] = []
    for p in pricing.pricingInfos:
        v = flag_for_airline.get(p.airline.lower())
        if v is False:
            continue  # explicitly disabled
        enabled.append(p.airline)
    return tuple(enabled)


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/148.0.0.0 Safari/537.36")
