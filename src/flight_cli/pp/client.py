"""Async PointsPath HTTP client.

Three endpoints matter:
  POST /api/airline-search    one POST per airline (request body holds route + cabin)
  GET  /api/pricing-info      airline catalog + transfer-partner mapping (cached 24h)
  GET  /api/extension-config  per-tier feature flags (cached 7d) — drives airline filter

401 → refresh tokens once → retry. Anything else propagates.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import structlog

from .auth import Tokens, get_valid_tokens
from .auth import refresh as refresh_tokens
from .models import AirlineSearchResponse, PricingInfoResponse

if TYPE_CHECKING:
    from types import TracebackType

    from structlog.stdlib import BoundLogger

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

_JsonDict = dict[str, Any]

API_BASE = "https://api.pointspath.com"


class PPApiError(Exception):
    """A PointsPath endpoint returned an unusable response.

    Exists so `httpx.HTTPStatusError` never reaches a caller (AGENTS.md
    Principle 1 — boundaries fail loudly, in domain terms). Per-airline
    failures are non-fatal and handled inline; this is for the catalog
    endpoints, where a failure means the award overlay cannot be built.
    """

    def __init__(self, message: str, *, endpoint: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.status = status


PRICING_CACHE = Path.home() / ".cache" / "flight-cli" / "pp_pricing.json"
PRICING_TTL_SECS = 24 * 3600

EXT_CONFIG_CACHE = Path.home() / ".cache" / "flight-cli" / "pp_extension_config.json"
EXT_CONFIG_TTL_SECS = 7 * 24 * 3600
EXT_CONFIG_VERSION = "1.10.4"

# Airlines the server has told us it does not support, learned at runtime.
#
# PP's /api/pricing-info advertises a superset of what /api/airline-search will
# actually serve: ~10 of its entries (ANA, BritishAirways, CathayPacific, …)
# have no `enable<Airline>` feature flag, so `enabled_airlines` treats them as
# always-on and every search fans out to them and collects a 400 "unsupported
# airline". That is ~10 wasted round-trips and ~20 warning lines per run.
#
# A hardcoded exclusion list would be wrong: the two conditions look identical
# from the flags alone. AirFrance also has no exact-name flag (only
# `enableAirFranceV2`) and DOES serve results, so "no flag" cannot be the
# predicate. Instead we record the server's own verdict — a 400 naming the
# airline as unsupported — and skip that airline until the TTL expires. The
# list self-heals when PP adds support or the user's tier changes.
UNSUPPORTED_CACHE = Path.home() / ".cache" / "flight-cli" / "pp_unsupported_airlines.json"
UNSUPPORTED_TTL_SECS = 7 * 24 * 3600

# Airlines observed firing in a single GFlights international search. Used as
# the default fan-out set when --pp-airlines isn't provided. Real catalog
# probably differs by Pro tier — the extension's /api/extension-config feature
# flags drive this — but starting with what we observed avoids guessing.
DEFAULT_AIRLINES: tuple[str, ...] = (
    "AirCanada",
    "AirFrance",
    "Alaska",
    "American",
    "Avianca",
    "Delta",
    "Etihad",
    "JetBlue",
    "Qantas",
    "Qatar",
    "United",
    "VirginAtlantic",
    "VirginAustralia",
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
    start_dt: str  # "YYYY-MM-DD HH:MM"
    end_dt: str
    flight_id: str
    airline: str
    google_airlines: list[str]
    num_connections: int
    first_flight_number: str
    cash_price_usd: int
    raw_cash_price: str

    def to_payload(self) -> _JsonDict:
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
    date: str  # YYYY-MM-DD
    return_date: str = ""
    is_round_trip_return: bool = False
    num_passengers: int = 1
    cabin_class: str = "Economy"
    currency: str = "USD"
    enable_matching: bool = False
    cash_hints: tuple[CashFlightHint, ...] = ()
    disable_cache: bool = False


def _payload(spec: SearchSpec, airline: str) -> _JsonDict:
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

    def __init__(
        self,
        tokens: Tokens,
        *,
        timeout: float = 25.0,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._tokens = tokens
        self._sem = anyio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={"user-agent": _UA},
        )

    @classmethod
    async def create(cls, **kw: Any) -> PPClient:
        # get_valid_tokens is sync (httpx.post for refresh) — fine; called once.
        return cls(get_valid_tokens(), **kw)

    async def aclose(self) -> None:
        await self._client.aclose()

    # context-manager sugar
    async def __aenter__(self) -> PPClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._tokens.access_token}",
            "content-type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: _JsonDict | None = None,
        params: _JsonDict | None = None,
    ) -> httpx.Response:
        async with self._sem:
            r = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._auth_headers(),
            )
        if r.status_code == HTTPStatus.UNAUTHORIZED:
            log.info("pp_token_refresh", reason="401_retry_once")
            self._tokens = refresh_tokens(self._tokens)
            async with self._sem:
                r = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    headers=self._auth_headers(),
                )
        return r

    async def airline_search(self, spec: SearchSpec, airline: str) -> AirlineSearchResponse:
        r = await self._request("POST", "/api/airline-search", json_body=_payload(spec, airline))
        # 204 = airline has nothing for this route+date; return empty model.
        if r.status_code == HTTPStatus.NO_CONTENT or not r.content:
            return AirlineSearchResponse()
        if r.status_code >= HTTPStatus.BAD_REQUEST:
            if is_unsupported_airline_response(r.status_code, r.text):
                # A permanent "we don't serve this airline", not a transient
                # failure — remember it so later runs skip the call entirely.
                # Logged at debug: it's an expected steady state, not a problem
                # the user can act on.
                remember_unsupported_airline(airline)
                log.debug("pp_airline_unsupported", airline=airline)
            else:
                log.warning(
                    "pp_airline_search_failed",
                    airline=airline,
                    status=r.status_code,
                    body=r.text[:200],
                )
            return AirlineSearchResponse()
        return AirlineSearchResponse.model_validate(r.json())

    async def airline_search_many(
        self,
        spec: SearchSpec,
        airlines: tuple[str, ...],
    ) -> dict[str, AirlineSearchResponse]:
        """Fan out one request per airline; concurrency-bounded by the semaphore.

        Airlines the server has previously rejected as unsupported are skipped
        without a request — see `UNSUPPORTED_CACHE`.
        """
        out: dict[str, AirlineSearchResponse] = {}
        skip = load_unsupported_airlines()
        to_call = tuple(a for a in airlines if a not in skip)
        if skipped := tuple(a for a in airlines if a in skip):
            log.debug("pp_airline_search_skipped_unsupported", airlines=skipped)

        async def runner(airline: str) -> None:
            try:
                out[airline] = await self.airline_search(spec, airline)
            except Exception as e:  # noqa: BLE001 - per-airline failures are non-fatal
                log.warning("pp_airline_search_exception", airline=airline, error=str(e))

        async with anyio.create_task_group() as tg:
            for a in to_call:
                tg.start_soon(runner, a)
        return out

    async def pricing_info(self, *, force_refresh: bool = False) -> PricingInfoResponse:
        if not force_refresh and PRICING_CACHE.exists():
            age = time.time() - PRICING_CACHE.stat().st_mtime
            if age < PRICING_TTL_SECS:
                return PricingInfoResponse.model_validate(json.loads(PRICING_CACHE.read_text()))
        r = await self._request("GET", "/api/pricing-info")
        _raise_for_status(r, "/api/pricing-info")
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
            "GET",
            "/api/extension-config",
            params={"v": EXT_CONFIG_VERSION},
        )
        _raise_for_status(r, "/api/extension-config")
        EXT_CONFIG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXT_CONFIG_CACHE.write_text(r.text)
        return r.json()


# Match `enable<AirlineName>` exactly, or `enable<AirlineName>V<digits>` (the
# Vn pattern is how PointsPath versions individual airline integrations — e.g.
# `enableAirFranceV2`). Sub-feature flags like `enableDeltaTakeOff15` or
# `enableSpiritSaversClub` carry trailing words and so don't match.
_AIRLINE_FLAG_RE = re.compile(r"^enable(?P<name>[A-Z][A-Za-z]+?)(?:V\d+)?$")


def _raise_for_status(r: httpx.Response, endpoint: str) -> None:
    """Translate a failed response into `PPApiError` at the boundary, so
    `httpx.HTTPStatusError` never escapes this module."""
    try:
        _ = r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise PPApiError(
            f"{endpoint} returned HTTP {r.status_code}",
            endpoint=endpoint,
            status=r.status_code,
        ) from e


def is_unsupported_airline_response(status: int, body: str) -> bool:
    """True when a 4xx says the airline itself isn't served, as opposed to a
    transient/auth/route-specific failure.

    Deliberately narrow: it must be a 400 AND the body must name the airline
    as unsupported. Broadening this to "any 4xx" would let a rate-limit or an
    expired token permanently blacklist a working airline.
    """
    if status != HTTPStatus.BAD_REQUEST:
        return False
    return "unsupported airline" in body.lower()


def _load_unsupported_raw() -> dict[str, float]:
    """`{airline: epoch_seconds_learned}`, unfiltered. {} on any read problem.

    Tolerates the legacy flat-list format written by the first version of this
    cache, dating those entries from the FILE's mtime — the only real timestamp
    they have. Stamping them `now` instead would restamp on every read, so a
    legacy file could never age out and the entries would be immortal.
    """
    try:
        raw: Any = json.loads(UNSUPPORTED_CACHE.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):  # legacy: ["ANA", "Finnair", ...]
        try:
            learned_at = UNSUPPORTED_CACHE.stat().st_mtime
        except OSError:
            return {}
        return {x: learned_at for x in cast("list[Any]", raw) if isinstance(x, str)}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in cast("dict[Any, Any]", raw).items():
        if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    return out


def load_unsupported_airlines() -> frozenset[str]:
    """Airlines the server rejected as unsupported, whose note is still fresh.

    Each entry carries its OWN learned-at timestamp. Keying the TTL off the
    file's mtime instead would mean any new rejection refreshed every existing
    entry — and since a run that learns one airline rewrites the file, entries
    would never expire in steady state, defeating the self-healing the TTL is
    there to provide.

    Any read problem (missing, corrupt, wrong shape) yields the empty set —
    the cost of a stale-empty result is one wasted round-trip per airline,
    versus wrongly suppressing a working airline's awards.
    """
    cutoff = time.time() - UNSUPPORTED_TTL_SECS
    return frozenset(a for a, learned_at in _load_unsupported_raw().items() if learned_at > cutoff)


def remember_unsupported_airline(airline: str) -> None:
    """Stamp one airline as unsupported as of now. Best-effort: a failure here
    only costs the next run a redundant request.

    Existing entries keep their original timestamps so each expires on its own
    schedule.

    NOTE: read-modify-write with no lock. Safe within one process — there is no
    `await` between the read and the write, so anyio's cooperative scheduling
    serializes concurrent callers in `airline_search_many`. Keep it that way:
    introducing an async file API here would open a real lost-update race.
    Across two concurrent CLI invocations a lost update is possible, and costs
    exactly one redundant request next run.
    """
    current = _load_unsupported_raw()
    if airline in current:
        return
    current[airline] = time.time()
    try:
        UNSUPPORTED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write leaves the old file intact
        # rather than a truncated one that reads as an empty cache.
        tmp = UNSUPPORTED_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(sorted(current.items())), indent=2))
        tmp.replace(UNSUPPORTED_CACHE)
    except OSError as e:
        log.debug("pp_unsupported_cache_write_failed", error=str(e))


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


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)
