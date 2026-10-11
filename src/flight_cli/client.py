"""Matrix Alkali client. Single `execute(search)` entry point — translates
the domain Search into a wire body, hits the endpoint, returns a parsed
response. All routing/filtering/mode-dispatch lives in `wire.to_wire()`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, assert_never, cast

import httpx
import structlog
from pydantic import ValidationError

from ._api_key import ApiKeyResolutionError, invalidate_cache, resolve_api_key
from ._gf_common import cache_dir
from ._http import HttpTransport
from .domain import CalendarFollowup, CalendarSearch, Search, SpecificDateSearch
from .models import BookingDetailsResult, CalendarResult, FareRulesResult, Location, SearchResult
from .wire import booking_details_body, fare_rules_body, to_wire

if TYPE_CHECKING:
    from pathlib import Path

# Re-export so callers can `from flight_cli.client import ApiKeyResolutionError`.
__all__ = [
    "ApiKeyResolutionError",
    "MatrixApiError",
    "MatrixClient",
    "MatrixHttpError",
    "MatrixShapeError",
]

BASE = "https://content-alkalimatrix-pa.googleapis.com"
SEARCH_URL = f"{BASE}/v1/search"
SUMMARIZE_URL = f"{BASE}/v1/summarize"
# How long a search waits on each attempt for Matrix to answer.
SEARCH_TIMEOUT_S = 180.0
# Where a drifted Matrix answer is reported, and the cache subdirectory that
# keeps its body for that report.
REPORT_URL = "https://github.com/ak2k/flight-cli/issues"
_SHAPE_DIR = "shape-changes"

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]


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


class MatrixHttpError(MatrixApiError):
    """Matrix answered an HTTP error status. The message is the status line
    alone: httpx's own text quotes the request URL, and Matrix's carries the API
    key."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"Matrix answered HTTP {status:d} {reason}".rstrip(), kind="http")
        self.status = status


def _status_error(e: httpx.HTTPStatusError) -> MatrixHttpError:
    return MatrixHttpError(e.response.status_code, e.response.reason_phrase)


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


class MatrixShapeError(RuntimeError):
    """Matrix answered, and the answer does not parse as the response model for
    this search: the backend changed, or this CLI misreads it. Not a
    `MatrixApiError`: Matrix reported no error, so `kind` and `request_id` have
    nothing to carry. `str()` is the whole typed line, report URL and captured
    body included; `detail` is the parse failure alone."""

    def __init__(self, detail: str, *, captured: Path | None) -> None:
        kept = f"attach {captured}" if captured else "its body could not be saved"
        super().__init__(
            f"{detail}; the backend changed shape, which may be a flight-cli bug. "
            f"Report it at {REPORT_URL} ({kept}; re-run with -vv for every field)"
        )
        self.detail = detail
        self.captured = captured


def _capture_body(data: dict[str, Any]) -> Path | None:
    """Write the body Matrix answered under the cache dir, readable by its owner
    alone, or return None: a cache dir that cannot be written must not turn a
    shape report into a second failure."""
    try:
        folder = cache_dir() / _SHAPE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"matrix-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.json"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(data, f)
    except (OSError, TypeError, ValueError):
        return None
    return path


def _parse_response(search: Search, data: dict[str, Any]) -> SearchResult | CalendarResult:
    """Pick the right response model based on the search variant. A body the
    model refuses is kept on disk and raised as `MatrixShapeError`."""
    try:
        return _parse_by_variant(search, data)
    except ValidationError as e:
        first = e.errors(include_url=False)[0]
        where = ".".join(str(p) for p in first["loc"]) or "the top level"
        log.debug("matrix_shape_change", errors=e.errors(include_url=False, include_input=False))
        detail = f"Matrix's answer does not parse at {where}: {first['msg']}"
        raise MatrixShapeError(
            f"{detail} ({e.error_count()} error(s))", captured=_capture_body(data)
        ) from e


def _parse_by_variant(search: Search, data: dict[str, Any]) -> SearchResult | CalendarResult:
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
        timeout: float = SEARCH_TIMEOUT_S,
        cache_dir: str | None = None,
        cache_read: bool = True,
        cache_write: bool = True,
        rebootstrap: bool = True,
    ) -> None:
        # `rebootstrap=False` makes a 403 final: the re-bootstrap below is
        # synchronous, so a caller that bounds its requests in time cannot
        # allow it.
        self._rebootstrap = rebootstrap
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
        raised as `MatrixApiError` and any other HTTP error status as
        `MatrixHttpError`.

        On a 403 from Matrix (typically a stale or wrong cached API key),
        invalidate the cache, re-bootstrap once, and retry. If the retry
        also 403s, surface ApiKeyResolutionError with the recovery guidance
        from _api_key._help_text — instead of a raw httpx traceback. A client
        built with `rebootstrap=False` invalidates the cache and raises on the
        first 403, so the next run bootstraps.
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
                raise _status_error(e) from None
            invalidate_cache()
            if not self._rebootstrap:
                raise ApiKeyResolutionError("Matrix rejected the API key with HTTP 403.") from e
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
                raise _status_error(e2) from None
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
