"""Matrix Alkali client. Single `execute(search)` entry point — translates
the domain Search into a wire body, hits the endpoint, returns a parsed
response. All routing/filtering/mode-dispatch lives in `wire.to_wire()`.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, assert_never, cast

import httpx

from ._api_key import ApiKeyResolutionError, invalidate_cache, resolve_api_key
from ._http import HttpTransport
from .domain import CalendarFollowup, CalendarSearch, Search, SpecificDateSearch
from .models import BookingDetailsResult, CalendarResult, FareRulesResult, Location, SearchResult
from .wire import booking_details_body, fare_rules_body, to_wire

# Re-export so callers can `from flight_cli.client import ApiKeyResolutionError`.
__all__ = [
    "ApiKeyResolutionError",
    "MatrixApiError",
    "MatrixClient",
]

BASE = "https://content-alkalimatrix-pa.googleapis.com"
SEARCH_URL = f"{BASE}/v1/search"
SUMMARIZE_URL = f"{BASE}/v1/summarize"


class MatrixApiError(Exception):
    """Matrix's validation errors come back as HTTP 200 + `{"error": ...}`."""

    def __init__(
        self,
        message: str,
        kind: str = "input",
        request_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.request_id = request_id
        self.raw = raw


def _raise_if_api_error(data: dict[str, Any]) -> None:
    err_raw = data.get("error")
    if isinstance(err_raw, dict) and ("message" in err_raw or "code" in err_raw):
        err = cast("dict[str, Any]", err_raw)
        raise MatrixApiError(
            message=err.get("message", "unknown error"),
            kind=err.get("type") or err.get("status") or "unknown",
            request_id=data.get("id"),
            raw=data,
        )


def _parse_response(search: Search, data: dict[str, Any]) -> SearchResult | CalendarResult:
    """Pick the right response model based on the search variant."""
    match search:
        case SpecificDateSearch() | CalendarFollowup():
            # followup returns the same shape as specific-date search
            return SearchResult.from_api(data)
        case CalendarSearch():
            return CalendarResult.from_api(data)
        case _:
            assert_never(search)


class MatrixClient:
    """Async, context-manager. One `execute()` method covers all search modes."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        impersonate: str = "chrome",
        rps: float = 1.0,
        concurrency: int = 3,
        timeout: float = 180.0,
        cache_dir: str | None = None,
        cache_read: bool = True,
        cache_write: bool = True,
    ) -> None:
        # If not supplied, resolve at construction time:
        # env var → on-disk cache → bootstrap-scrape from Matrix's SPA.
        # Never hardcoded in the source.
        self._api_key = api_key or resolve_api_key()
        self._http = HttpTransport(
            impersonate=impersonate,
            rps=rps,
            concurrency=concurrency,
            timeout=timeout,
            cache_dir=cache_dir,
            cache_read=cache_read,
            cache_write=cache_write,
        )

    async def __aenter__(self) -> MatrixClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─────────────────────────── search execution ──────────────────────────

    async def _post(self, url: str, body: dict[str, Any], *, cache: bool) -> dict[str, Any]:
        """POST a body to Matrix and return its decoded answer, Matrix errors
        raised as `MatrixApiError`.

        On a 403 from Matrix (typically a stale or wrong cached API key),
        invalidate the cache, re-bootstrap once, and retry. If the retry
        also 403s, surface ApiKeyResolutionError with the recovery guidance
        from _api_key._help_text — instead of a raw httpx traceback.
        """
        try:
            data = await self._http.post_json(
                url,
                body,
                params={"key": self._api_key, "alt": "json"},
                cache=cache,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != HTTPStatus.FORBIDDEN:
                raise
            invalidate_cache()
            self._api_key = resolve_api_key(force_bootstrap=True)
            try:
                data = await self._http.post_json(
                    url,
                    body,
                    params={"key": self._api_key, "alt": "json"},
                    cache=cache,
                )
            except httpx.HTTPStatusError as e2:
                if e2.response.status_code == HTTPStatus.FORBIDDEN:
                    raise ApiKeyResolutionError(
                        "Matrix rejected the API key with HTTP 403 even after "
                        "re-bootstrapping. The bootstrap regex may be picking up "
                        "a non-prod key (e.g. matrix-nightly), or Matrix has "
                        "tightened access. Set FLIGHT_API_KEY explicitly."
                    ) from e2
                raise
        _raise_if_api_error(data)
        return data

    async def execute(self, search: Search, *, cache: bool = True) -> SearchResult | CalendarResult:
        """Run any flavor of search. Returns SearchResult or CalendarResult
        depending on the search variant."""
        data = await self._post(SEARCH_URL, to_wire(search).as_json(), cache=cache)
        return _parse_response(search, data)

    # ──────────────────────── follow-ups on a search ───────────────────────
    # Answered from the search's session, which Matrix holds for a while after
    # the search returns, so any client may ask. Never cached: the body names a
    # session, and an answer kept past that session's life is one Matrix would
    # no longer give.

    async def booking_details(
        self, *, session: str, solution_set: str, solution_id: str
    ) -> BookingDetailsResult:
        body = booking_details_body(
            session=session, solution_set=solution_set, solution_id=solution_id
        )
        data = await self._post(SUMMARIZE_URL, body.as_json(), cache=False)
        return BookingDetailsResult.from_api(data)

    async def fare_rules(
        self, *, session: str, solution_set: str, solution_id: str, fare_key: str
    ) -> FareRulesResult:
        body = fare_rules_body(
            session=session, solution_set=solution_set, solution_id=solution_id, fare_key=fare_key
        )
        data = await self._post(SUMMARIZE_URL, body.as_json(), cache=False)
        return FareRulesResult.from_api(data)

    # ───────────────────────── ancillary helpers ───────────────────────────

    async def airports(self, partial: str, page_size: int = 10) -> list[Location]:
        url = f"{BASE}/v1/locationTypes/CITIES_AND_AIRPORTS/partialNames/{partial}/locations"
        data = await self._http.get_json(
            url,
            params={"pageSize": page_size, "key": self._api_key},
        )
        return [Location.model_validate(loc) for loc in data.get("locations", [])]

    async def airport(self, code: str) -> Location | None:
        """Look up a single airport by IATA code. Returns None only on 404
        ('no such airport'); network errors and auth failures propagate so
        callers can distinguish 'doesn't exist' from 'lookup is broken'."""
        url = f"{BASE}/v1/locationTypes/airportOrMultiAirportCity/locationCodes/{code.upper()}"
        try:
            data = await self._http.get_json(url, params={"key": self._api_key})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise
        return Location.model_validate(data)

    async def currencies(self) -> list[dict[str, str]]:
        data = await self._http.get_json(
            f"{BASE}/v1/currencies",
            params={"key": self._api_key},
        )
        return data.get("currencies", [])
