"""Async HTTP client for the Seats.aero Pro API.

Single endpoint matters for award augmentation:
  GET /partnerapi/search   route + date → award availability (+ trips)

Auth: `Partner-Authorization: <api_key>` header (not Bearer). Surface 401
loudly — there's no token refresh dance like PointsPath; a bad key is a
config problem.

Rate limit: Pro tier is 1000 calls/day. Every 200 response includes:
  X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset (seconds).
We expose remaining via the client's `last_rate_limit` attribute so
`flight auth seats whoami` can show it.

Pagination: response has `hasMore` + `cursor`. We auto-paginate inside
the client when callers want all results; for the augmenter, we cap at a
small N since we only need top-N awards per leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog

from .auth import get_key
from .models import CachedSearchResponse, SeatsAvailabilityItem

if TYPE_CHECKING:
    from types import TracebackType

    from structlog.stdlib import BoundLogger

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

API_BASE = "https://seats.aero/partnerapi"


@dataclass
class RateLimit:
    """Snapshot of the most recent response's quota headers. None when no
    request has run yet, or when the headers weren't present (e.g. 401)."""

    limit: int
    remaining: int
    reset_seconds: int


class SeatsAeroError(Exception):
    """Wraps non-2xx responses with status + body excerpt for diagnostics."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Seats.aero HTTP {status}: {body[:200]}")


class SeatsAeroClient:
    """Thin async wrapper. Use as an async context manager:

        async with SeatsAeroClient() as c:
            page = await c.search(origin="JFK", destination="LHR",
                                  start_date="2026-08-15",
                                  end_date="2026-08-15",
                                  include_trips=True)

    The client owns one httpx.AsyncClient internally; close() on exit.
    """

    def __init__(self, *, timeout: float = 30.0, api_key: str | None = None) -> None:
        # Resolve the key lazily at construction so test code can pass a
        # fake key directly without going through env/file machinery.
        self._api_key = api_key if api_key is not None else get_key()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Partner-Authorization": self._api_key,
                "Accept": "application/json",
                "User-Agent": "flight-cli/0 (+https://github.com/ak2k/flight-cli)",
            },
            base_url=API_BASE,
        )
        self.last_rate_limit: RateLimit | None = None

    async def __aenter__(self) -> SeatsAeroClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, tb
        await self._client.aclose()

    async def search(
        self,
        *,
        origin: str,
        destination: str,
        start_date: str,
        end_date: str,
        include_trips: bool = True,
        take: int = 500,
        cursor: int | None = None,
        sources: tuple[str, ...] | None = None,
        cabins: tuple[str, ...] | None = None,
        carriers: tuple[str, ...] | None = None,
        only_direct_flights: bool = False,
    ) -> CachedSearchResponse:
        """One `/search` call. Returns the parsed page; caller decides
        whether to paginate via `search_all` based on `.hasMore`.

        `sources` filters by mileage program (e.g. ("aeroplan", "united"));
        `cabins` restricts cabin classes; `carriers` filters by operating
        carrier IATA. Empty/None tuples mean "no filter".
        """
        params: dict[str, str] = {
            "origin_airport": origin,
            "destination_airport": destination,
            "start_date": start_date,
            "end_date": end_date,
            "include_trips": "true" if include_trips else "false",
            "take": str(take),
        }
        if cursor is not None:
            params["cursor"] = str(cursor)
        if sources:
            params["sources"] = ",".join(sources)
        if cabins:
            params["cabins"] = ",".join(cabins)
        if carriers:
            params["carriers"] = ",".join(carriers)
        if only_direct_flights:
            params["only_direct_flights"] = "true"

        r = await self._client.get("/search", params=params)
        self._record_rate_limit(r)
        if r.status_code != 200:  # noqa: PLR2004 — explicit HTTP 200 check
            raise SeatsAeroError(r.status_code, r.text)
        return CachedSearchResponse.model_validate(r.json())

    async def search_all(
        self,
        *,
        origin: str,
        destination: str,
        start_date: str,
        end_date: str,
        include_trips: bool = True,
        max_pages: int = 10,
        **filters: str | tuple[str, ...] | bool,
    ) -> list[SeatsAvailabilityItem]:
        """Walk pagination via `cursor` until hasMore=False or max_pages hit.

        `max_pages` caps how many pages we'll fetch — Seats.aero's Pro tier
        has a 1000/day quota, and an unbounded loop on a noisy route can
        chew through it fast. The augmenter caller stays at 1-2 pages.
        """
        out: list[SeatsAvailabilityItem] = []
        cursor: int | None = None
        for _ in range(max_pages):
            page = await self.search(
                origin=origin,
                destination=destination,
                start_date=start_date,
                end_date=end_date,
                include_trips=include_trips,
                cursor=cursor,
                **filters,  # type: ignore[arg-type]
            )
            out.extend(page.data)
            if not page.hasMore or page.cursor is None:
                break
            cursor = page.cursor
        return out

    def _record_rate_limit(self, r: httpx.Response) -> None:
        """Parse X-RateLimit-* headers if present. Silently no-op on 401
        or other responses that don't carry the headers."""
        try:
            self.last_rate_limit = RateLimit(
                limit=int(r.headers["x-ratelimit-limit"]),
                remaining=int(r.headers["x-ratelimit-remaining"]),
                reset_seconds=int(r.headers["x-ratelimit-reset"]),
            )
        except (KeyError, ValueError):
            self.last_rate_limit = None
