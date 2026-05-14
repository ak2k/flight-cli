"""Matrix Alkali client. Single `execute(search)` entry point — translates
the domain Search into a wire body, hits the endpoint, returns a parsed
response. All routing/filtering/mode-dispatch lives in `wire.to_wire()`.
"""
from __future__ import annotations
from typing import Any
from functools import singledispatch

from .domain import Search, SpecificDateSearch, CalendarSearch, CalendarFollowup
from .models import SearchResult, CalendarResult, Location
from .wire import to_wire
from ._http import HttpTransport
from ._api_key import resolve_api_key, ApiKeyResolutionError  # noqa: F401 (re-export)

BASE = "https://content-alkalimatrix-pa.googleapis.com"
SEARCH_URL = f"{BASE}/v1/search"


class MatrixApiError(Exception):
    """Matrix's validation errors come back as HTTP 200 + `{"error": ...}`."""
    def __init__(self, message: str, kind: str = "input",
                 request_id: str | None = None,
                 raw: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.request_id = request_id
        self.raw = raw


def _raise_if_api_error(data: dict) -> None:
    err = data.get("error")
    if isinstance(err, dict) and ("message" in err or "code" in err):
        raise MatrixApiError(
            message=err.get("message", "unknown error"),
            kind=err.get("type") or err.get("status") or "unknown",
            request_id=data.get("id"),
            raw=data,
        )


@singledispatch
def _parse_response(search: Search, data: dict):
    """Pick the right response model based on the search variant."""
    raise TypeError(f"no response parser for {type(search).__name__}")

@_parse_response.register
def _(s: SpecificDateSearch, data: dict) -> SearchResult:
    return SearchResult.from_api(data)

@_parse_response.register
def _(s: CalendarSearch, data: dict) -> CalendarResult:
    return CalendarResult.from_api(data)

@_parse_response.register
def _(s: CalendarFollowup, data: dict) -> SearchResult:
    # followup returns the same shape as specific-date search
    return SearchResult.from_api(data)


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
            impersonate=impersonate, rps=rps, concurrency=concurrency,
            timeout=timeout, cache_dir=cache_dir,
            cache_read=cache_read, cache_write=cache_write,
        )

    async def __aenter__(self) -> "MatrixClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─────────────────────────── search execution ──────────────────────────

    async def execute(self, search: Search, *, cache: bool = True):
        """Run any flavor of search. Returns SearchResult or CalendarResult
        depending on the search variant."""
        body = to_wire(search).as_json()
        data = await self._http.post_json(
            SEARCH_URL, body, params={"key": self._api_key, "alt": "json"},
            cache=cache,
        )
        _raise_if_api_error(data)
        return _parse_response(search, data)

    # ───────────────────────── ancillary helpers ───────────────────────────

    async def airports(self, partial: str, page_size: int = 10) -> list[Location]:
        url = (f"{BASE}/v1/locationTypes/CITIES_AND_AIRPORTS/"
               f"partialNames/{partial}/locations")
        data = await self._http.get_json(
            url, params={"pageSize": page_size, "key": self._api_key},
        )
        return [Location.model_validate(loc) for loc in data.get("locations", [])]

    async def airport(self, code: str) -> Location | None:
        url = (f"{BASE}/v1/locationTypes/airportOrMultiAirportCity/"
               f"locationCodes/{code.upper()}")
        try:
            data = await self._http.get_json(url, params={"key": self._api_key})
        except Exception:
            return None
        try:
            return Location.model_validate(data)
        except Exception:
            return None

    async def currencies(self) -> list[dict[str, str]]:
        data = await self._http.get_json(
            f"{BASE}/v1/currencies", params={"key": self._api_key},
        )
        return data.get("currencies", [])
