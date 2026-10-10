"""`flight doctor`: one pass, fail or skip per backend, transport and credential.

A check never raises. A probe that fails raises, and the failure is filed under
one cause; a failure no rule knows is `error`. The causes are what a canary
reads to tell flakiness from a shape change, so a throttle, a brownout or an
outage is never `shape`, and `shape` is never retryable.

A live check passes only on an answer the CLI's own parser priced: a page or a
body that parses to nothing is exactly what a moved extract looks like.

Every detail is redacted before it is stored, because the text of an upstream
error is where a credential leaks: httpx quotes the whole request URL, and
Matrix's carries its key.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
import diskcache  # pyright: ignore[reportMissingTypeStubs]  # DIVERGE: no stubs shipped; Profile-B edge
import httpx
import stamina.instrumentation
import structlog
from pydantic import BaseModel, ConfigDict, model_validator

from . import _api_key, _config, _gf_browser
from . import _gflight_ids as gfid
from ._api_key import ApiKeyResolutionError
from ._gf_common import cache_dir
from ._gf_errors import (
    GfBrowserUnavailableError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfSearchServerError,
    GfThrottledError,
    GfTransportError,
    GfUpstreamStatusError,
)
from ._http import CACHE_SIZE_LIMIT_BYTES
from .client import SEARCH_TIMEOUT_S, MatrixApiError, MatrixClient, MatrixShapeError
from .domain import Leg, SearchOptions, SpecificDateSearch
from .fli_bridge import to_fli_filter
from .models import SearchResult
from .pp import auth as pp_auth
from .pp import client as pp_client
from .pp.client import PPApiError, PPClient
from .pp.models import PricingInfoResponse
from .providers.seats_aero import auth as seats_auth
from .providers.seats_aero.client import SeatsAeroClient, SeatsAeroError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from stamina.instrumentation import RetryDetails
    from structlog.stdlib import BoundLogger

    from .providers.seats_aero.client import RateLimit

log: BoundLogger = structlog.get_logger(__name__)  # pyright: ignore[reportAny]

Status = Literal["pass", "fail", "skip"]
Cause = Literal[
    "throttled",
    "unreachable",
    "upstream",
    "brownout",
    "shape",
    "rejected",
    "consent",
    "auth",
    "config",
    "browser",
    "error",
]
# Conditions that lift on their own. Nothing else is retryable: a shape change,
# a refused credential or a missing Chrome fails the same way on the next run.
RETRYABLE: frozenset[Cause] = frozenset({"throttled", "unreachable", "upstream", "brownout"})

CHECK_IDS = (
    "config",
    "matrix-key",
    "cache",
    "google-cookies",
    "matrix-spa-key",
    "matrix-search",
    "google-http",
    "google-browser",
    "pointspath",
    "seats-aero",
)

# sysexits.h's EX_TEMPFAIL, "try again later": a scheduler reads it as a run
# to repeat rather than one to page on.
EX_TEMPFAIL = 75

# A trunk route every backend has priced on every day measured; an empty
# answer on it is a broken extract, not a quiet day.
_ORIGIN, _DESTINATION = "JFK", "LAX"
_DAYS_OUT = 30

_MATRIX_PRICE = re.compile(r"^[A-Z]{3}\d")
_MATRIX_BROWNOUT_KINDS = frozenset({"INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"})
# Matrix answers an overloaded engine with HTTP 200 and this message. It is also
# its answer to a body it rejects, which is why the canary contract reads a
# brownout that persists across runs as a shape suspect.
_MATRIX_INTERNAL_ERROR = re.compile(r"internal (server )?error", re.IGNORECASE)
# `PPAuthError` carries Supabase's answer to a token refresh only in its text.
_SUPABASE_REFRESH_STATUS = re.compile(r"^Supabase refresh failed: HTTP (\d{3})\b")

_SECRET_ENV = ("FLIGHT_API_KEY", "PP_ACCESS_TOKEN", "PP_REFRESH_TOKEN", "SEATS_AERO_API_KEY")
# No service here issues a credential this short, and replacing a shorter
# string everywhere it occurs would garble the detail it sits in.
_MIN_SECRET_LEN = 8
_KEY_PARAM = re.compile(r"(?<=[?&]key=)[^&#\s'\"]+")

_BROWSER_BIN_ENV = "FLIGHT_CLI_GF_BROWSER_BIN"
_PP_PRICING = "/api/pricing-info"
# patchright's own table for `channel="chrome"`: an absolute path on macOS and
# Linux, and on Windows a suffix under each install root below.
_CHROME_CHANNEL = {
    "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "linux": "/opt/google/chrome/chrome",
    "win32": "Google/Chrome/Application/chrome.exe",
}
_WINDOWS_ROOTS = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")


class Check(BaseModel):
    """One row of the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    status: Status
    detail: str
    cause: Cause | None = None
    retryable: bool = False
    # Wall time of a live probe; None for a local check and for a skip.
    seconds: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Check:
        if (self.cause is None) != (self.status != "fail"):
            raise ValueError(f"{self.id}: a cause goes with a failure, and only with one")
        if self.retryable != (self.cause in RETRYABLE):
            raise ValueError(f"{self.id}: retryable follows the cause")
        if not self.detail.strip() or "\n" in self.detail:
            raise ValueError(f"{self.id}: the detail is one non-empty line")
        return self


class Report(BaseModel):
    """Every check, in `CHECK_IDS` order, from one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: dt.date
    checks: tuple[Check, ...]

    @model_validator(mode="after")
    def _every_check_in_order(self) -> Report:
        got = tuple(c.id for c in self.checks)
        if got != CHECK_IDS:
            raise ValueError(f"checks {got}; expected {CHECK_IDS}")
        return self

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def exit_code(self) -> int:
        failed = [c for c in self.checks if c.status == "fail"]
        if not failed:
            return 0
        return EX_TEMPFAIL if all(c.retryable for c in failed) else 1

    def document(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "date": self.date.isoformat(),
            "checks": [c.model_dump(mode="json") for c in self.checks],
        }


def run(*, on_start: Callable[[str], None], today: dt.date | None = None) -> Report:
    """Run every check in order. `on_start` hears one line as each live probe
    starts, so a run that waits minutes on Matrix says what it waits on."""
    return _Doctor(today or dt.date.today(), on_start).report()


type _Outcome = tuple[Literal["pass", "skip"], str]


class _CheckFailedError(Exception):
    """A probe's own verdict on its failure, where the exception alone would
    not say which cause it is."""

    def __init__(self, cause: Cause, detail: str) -> None:
        super().__init__(detail)
        self.cause: Cause = cause
        self.detail = detail


class _Doctor:
    """One run. Holds what a later check needs from an earlier one: the key a
    search would use, the key Matrix's page serves, the settings a search
    resolves, the cause of each failure, and every secret met."""

    def __init__(self, today: dt.date, on_start: Callable[[str], None]) -> None:
        self.today = today
        self.depart = today + dt.timedelta(days=_DAYS_OUT)
        self.on_start = on_start
        self.key_in_use: str | None = None
        self.spa_key: str | None = None
        self.secrets: set[str] = set()
        self.failed: dict[str, Cause] = {}
        # A search's transport settings, the defaults until `config` resolves them.
        self.rps = _config.DEFAULT_RPS
        self.impersonate = _config.DEFAULT_IMPERSONATE

    def report(self) -> Report:
        probes: tuple[tuple[str, Callable[[], _Outcome], bool], ...] = (
            ("config", self.check_config, False),
            ("matrix-key", self.check_matrix_key, False),
            ("cache", self.check_cache, False),
            ("google-cookies", self.check_google_cookies, False),
            ("matrix-spa-key", self.check_matrix_spa_key, True),
            ("matrix-search", self.check_matrix_search, True),
            ("google-http", self.check_google_http, True),
            ("google-browser", self.check_google_browser, True),
            ("pointspath", self.check_pointspath, True),
            ("seats-aero", self.check_seats_aero, True),
        )
        with _retry_log_redacted(self.redact):
            checks = tuple(self._one(cid, probe, live=live) for cid, probe, live in probes)
        return Report(date=self.today, checks=checks)

    def _one(self, cid: str, probe: Callable[[], _Outcome], *, live: bool) -> Check:
        started = time.monotonic()
        cause: Cause | None = None
        try:
            status, detail = probe()
        except _CheckFailedError as f:
            status, cause, detail = "fail", f.cause, f.detail
        except Exception as e:  # noqa: BLE001 — a check reports its failure; it never raises
            status, (cause, detail) = "fail", _classify(e)
        if cause is not None:
            self.failed[cid] = cause
        elapsed = round(time.monotonic() - started, 2)
        return Check(
            id=cid,
            status=status,
            detail=self.redact(_one_line(detail)),
            cause=cause,
            retryable=cause in RETRYABLE,
            seconds=elapsed if live and status != "skip" else None,
        )

    def _starting(self, cid: str, what: str) -> None:
        self.on_start(f"{cid}: {what}")

    def redact(self, text: str) -> str:
        """`text` with every known secret replaced by its fingerprint and every
        `key=` query value by `<redacted>`.

        The stores are read again on every call rather than once, because a
        probe can write a new secret mid-run: a Matrix 403 re-caches the key, a
        stale PointsPath token is refreshed to disk."""
        secrets = sorted(
            (s for s in _stored_secrets() | self.secrets if len(s) >= _MIN_SECRET_LEN),
            key=len,
            reverse=True,
        )
        for secret in secrets:
            text = text.replace(secret, fingerprint(secret))
        for secret in secrets:
            text = _cut_head_redacted(text, secret)
        return _KEY_PARAM.sub("<redacted>", text)

    # ─────────────────────────────── local ────────────────────────────────

    def check_config(self) -> _Outcome:
        path = _config.config_path()
        try:
            cfg = _config.load()
        except (OSError, ValueError) as e:
            raise _CheckFailedError("config", f"{path} could not be read: {e}") from e
        # Resolved as a search resolves them: an rps that is not a number of at
        # least 5.6e-309 stops the search before it sends anything.
        self.impersonate = _config.http_impersonate(config=cfg)
        try:
            self.rps = _config.http_rps(config=cfg)
        except ValueError as e:
            raise _CheckFailedError("config", f"bad rps configuration: {e}") from e
        if not path.exists():
            return "pass", f"no config file at {path}; the defaults apply"
        return "pass", f"{path} parses"

    def check_matrix_key(self) -> _Outcome:
        """Which key a search would send, read without resolving one."""
        env = os.environ.get("FLIGHT_API_KEY")
        if env:
            # What `resolve_api_key` returns, shaped or not, so a search sends it.
            self.key_in_use = key = env.strip()
            if not _api_key._KEY_SHAPE.match(key):  # pyright: ignore[reportPrivateUsage]
                raise _CheckFailedError(
                    "config",
                    f"FLIGHT_API_KEY ({fingerprint(key)}) is not shaped like a Matrix key "
                    "(AIzaSy and 33 more characters)",
                )
            return "pass", f"FLIGHT_API_KEY, {fingerprint(key)}"
        path: Path = _api_key._CACHE_PATH  # pyright: ignore[reportPrivateUsage]
        try:
            age_s = time.time() - path.stat().st_mtime
            cached = path.read_text().strip()
        except FileNotFoundError:
            return "pass", f"none cached at {path}; the next search reads it from Matrix's page"
        except OSError as e:
            # A search reads this file unguarded, so an unreadable one fails every search.
            raise _CheckFailedError("config", f"{path} could not be read: {e}") from e
        self.secrets.add(cached)
        ttl_days = _api_key._CACHE_TTL_SECS / 86400  # pyright: ignore[reportPrivateUsage]
        age = f"{age_s / 86400:.1f} of {ttl_days:.0f} days old"
        if not _api_key._KEY_SHAPE.match(cached):  # pyright: ignore[reportPrivateUsage]
            return "pass", f"{path} holds no Matrix-shaped key; the next search reads a new one"
        if age_s >= _api_key._CACHE_TTL_SECS:  # pyright: ignore[reportPrivateUsage]
            return (
                "pass",
                f"cached at {path}, past its {ttl_days:.0f} days; the next search reads a new one",
            )
        self.key_in_use = cached
        return "pass", f"cached at {path}, {age}, {fingerprint(cached)}"

    def check_cache(self) -> _Outcome:
        """Open and close the response cache exactly as `HttpTransport` does."""
        root = cache_dir()
        try:
            root.mkdir(parents=True, exist_ok=True)
            cache: Any = diskcache.Cache(  # pyright: ignore[reportUnknownMemberType]
                str(root / "http"), size_limit=CACHE_SIZE_LIMIT_BYTES
            )
            try:
                entries = len(cache)
            finally:
                cache.close()
        # Broad: diskcache raises sqlite3 and OS errors alike.
        except Exception as e:
            raise _CheckFailedError(
                "config", f"the response cache in {root} could not be opened: {e}"
            ) from e
        return "pass", f"{root / 'http'} opens, {entries} entries"

    def check_google_cookies(self) -> _Outcome:
        path = gfid._cookie_path()  # pyright: ignore[reportPrivateUsage]
        try:
            text = path.read_text()
        except FileNotFoundError:
            return "pass", f"none saved at {path}; the first Google search warms a session"
        except OSError as e:
            raise _CheckFailedError("config", f"{path} could not be read: {e}") from e
        try:
            payload = json.loads(text)
            saved_at = float(payload["saved_at"])
            nids = sum(1 for c in payload["cookies"] if c["name"] == "NID")
        except (ValueError, TypeError, KeyError) as e:
            raise _CheckFailedError(
                "config", f"{path} is not a cookie jar this CLI wrote: {e!r}"
            ) from e
        ttl_s: int = gfid._COOKIE_TTL_S  # pyright: ignore[reportPrivateUsage]
        age_s = time.time() - saved_at
        stale = "; past its TTL, so the next search warms a new one" if age_s > ttl_s else ""
        return (
            "pass",
            f"{nids} NID cookie(s), {age_s / 86400:.1f} of {ttl_s / 86400:.0f} days old{stale}",
        )

    # ─────────────────────────────── Matrix ───────────────────────────────

    def check_matrix_spa_key(self) -> _Outcome:
        """The key Matrix's page serves today, read as `_bootstrap_from_spa`
        reads it but status first, and cached nowhere.

        The bootstrap reads the body of whatever answered, so an outage
        there surfaces as "no bundle in the page" — a shape change. Reading the
        status first is what keeps a 503 an outage."""
        self._starting("matrix-spa-key", "reading the key Matrix's web page serves")
        with _spa_client() as c:
            home = _spa_get(c, _api_key._HOMEPAGE, "Matrix's homepage")  # pyright: ignore[reportPrivateUsage]
            bundle = _api_key._BUNDLE_PATTERN.search(home)  # pyright: ignore[reportPrivateUsage]
            if bundle is None:
                raise _CheckFailedError(
                    "shape", "Matrix's homepage names no SPA bundle; the page changed"
                )
            url = bundle.group(1)
            js = _spa_get(c, f"https:{url}" if url.startswith("//") else url, "Matrix's SPA bundle")
        found = _api_key._KEY_PATTERN_MATRIX_PROD.search(js)  # pyright: ignore[reportPrivateUsage]
        if found is None:
            raise _CheckFailedError(
                "shape", "Matrix's SPA bundle has no key tagged 'matrix'; the bundle changed"
            )
        self.spa_key = key = found.group(1) or found.group(2)
        self.secrets.add(key)
        if self.key_in_use is None:
            return "pass", f"{fingerprint(key)}; no key is in use, so a search would cache this one"
        if key == self.key_in_use:
            return "pass", f"{fingerprint(key)}, the key in use"
        return "pass", f"{fingerprint(key)}, not the key in use ({fingerprint(self.key_in_use)})"

    def check_matrix_search(self) -> _Outcome:
        key = self.key_in_use or self.spa_key
        if key is None:
            return "skip", "no key to search with: none in use, and matrix-spa-key found none"
        self._starting(
            "matrix-search",
            f"one Matrix search, {_ORIGIN}-{_DESTINATION} {self.depart} (can take minutes)",
        )
        search = SpecificDateSearch(
            legs=(Leg.of(_ORIGIN, _DESTINATION, self.depart),), options=SearchOptions(page_size=5)
        )

        # No timeout of its own: one shorter than a search's would fail a slow
        # Matrix that a search still gets its answer from.
        async def go() -> object:
            async with MatrixClient(api_key=key, rps=self.rps, impersonate=self.impersonate) as c:
                return await c.execute(search, cache=False)

        try:
            res = anyio.run(go)
        except httpx.TimeoutException as e:
            raise _CheckFailedError(
                "brownout",
                f"Matrix did not answer within {SEARCH_TIMEOUT_S:.0f} s, "
                "the limit a search waits on each attempt",
            ) from e
        except ApiKeyResolutionError as e:
            if e.__cause__ is not None:
                raise
            # Matrix refused the key, and the client read Matrix's page for a
            # new one by its body alone, so a 503 there reads as a page with no
            # key. matrix-spa-key read that page status first moments ago: its
            # verdict is this one, and a page that served it a key is failing,
            # not changed.
            spa = self.failed.get("matrix-spa-key")
            seen = "matrix-spa-key failed on it too" if spa else "it served matrix-spa-key one"
            raise _CheckFailedError(
                spa or "upstream",
                f"Matrix refused the key in use, and its page served no new one; {seen}",
            ) from e
        except MatrixShapeError as e:
            raise _CheckFailedError("shape", f"{e.detail}; the response shape changed") from e
        if not isinstance(res, SearchResult):
            raise TypeError(f"a specific-date search answered with {type(res).__name__}")
        if "solutionList" not in (res.raw or {}):
            raise _CheckFailedError(
                "shape", "Matrix answered without a solutionList; the response shape changed"
            )
        if not res.solutions:
            raise _CheckFailedError(
                "brownout",
                "Matrix's solutionList holds no solution on a route that always has them",
            )
        for s in res.solutions:
            price = s.price or ""
            slices = s.itinerary.slices if s.itinerary else []
            if _MATRIX_PRICE.match(price) and slices and slices[0].flights:
                flights = "/".join(slices[0].flights)
                return "pass", f"{res.solution_count} solutions; first priced {price} on {flights}"
        raise _CheckFailedError(
            "shape", f"none of Matrix's {len(res.solutions)} solutions has a price and a flight"
        )

    # ─────────────────────────────── Google ───────────────────────────────

    def check_google_http(self) -> _Outcome:
        self._starting(
            "google-http",
            f"one Google Flights page over http, {_ORIGIN}-{_DESTINATION} {self.depart}",
        )
        return self._google_page(gfid.GfTransport(mode="http"))

    def check_google_browser(self) -> _Outcome:
        override = os.environ.get(_BROWSER_BIN_ENV)
        # Checked here rather than left to the launch, so a bad override is
        # named as the setting it is and no Chrome is started to find out; and
        # before the extra, so the setting is not hidden until it is installed.
        if override and not (Path(override).is_file() and os.access(override, os.X_OK)):
            raise _CheckFailedError(
                "config",
                f"{_BROWSER_BIN_ENV} names {override}, which is not an executable file",
            )
        if not _patchright_installed():
            return "skip", "patchright is not installed. " + _gf_browser._INSTALL_HINT  # pyright: ignore[reportPrivateUsage]
        if not override and _chrome_channel_path() is None:
            return (
                "skip",
                f"no Chrome where patchright looks for it; install Chrome, or point "
                f"{_BROWSER_BIN_ENV} at one",
            )
        self._starting(
            "google-browser",
            f"one Google Flights page in Chrome, {_ORIGIN}-{_DESTINATION} {self.depart}",
        )
        # Main thread, guarded and scoped, as the CLI reads the browser rung:
        # a Ctrl-C must reach Chrome, and the session must close on every path.
        with _gf_browser.interrupt_guard(), _gf_browser.session_scope():
            return self._google_page(gfid.GfTransport(mode="browser"))

    def _google_page(self, transport: gfid.GfTransport) -> _Outcome:
        filters = to_fli_filter(
            SpecificDateSearch(legs=(Leg.of(_ORIGIN, _DESTINATION, self.depart),))
        )
        board = gfid.search_with_ids(filters, top_n=1, transport=transport)
        rows = [r for r in board or () if isinstance(r, gfid.GFlightWithId)]
        if not rows:
            raise _CheckFailedError(
                "shape",
                f"Google served an empty board for {_ORIGIN}-{_DESTINATION} on {self.depart}, "
                "a route that always has flights",
            )
        for r in rows:
            price = r.flight.price
            if price is not None and price > 0:
                return (
                    "pass",
                    f"{len(rows)} rows; first priced {r.flight.currency or 'USD'} {price:,.0f}",
                )
        raise _CheckFailedError("shape", f"none of Google's {len(rows)} rows has a price")

    # ────────────────────────────── providers ─────────────────────────────

    def check_pointspath(self) -> _Outcome:
        # Stored, not `is_configured()`: that reads a missing and an unreadable
        # store alike as not configured — a skip where the user needs a fail.
        if pp_auth.load_tokens() is None:
            if pp_auth.TOKENS_PATH.exists():
                raise _unreadable_store(pp_auth.TOKENS_PATH, "`flight auth pp login`")
            return "skip", "no PointsPath tokens stored; run `flight auth pp login`"
        self._starting("pointspath", "one PointsPath request")
        tokens = pp_auth.get_valid_tokens()
        self._remember(tokens.access_token, tokens.refresh_token)

        async def go() -> tuple[int, pp_auth.Tokens]:
            c = PPClient(tokens)
            try:
                # The request `pricing_info` sends, without its cache write: that
                # writes a good answer over the catalog every search reads.
                r = await c._request("GET", _PP_PRICING)  # pyright: ignore[reportPrivateUsage]
                pp_client._raise_for_status(r, _PP_PRICING)  # pyright: ignore[reportPrivateUsage]
                # The client refreshes and retries on a 401, so the token that
                # answered can be newer than the one it was handed.
                return _pricing_programs(r), c._tokens  # pyright: ignore[reportPrivateUsage]
            finally:
                await c.aclose()

        programs, answered = anyio.run(go)
        self._remember(answered.access_token, answered.refresh_token)
        expires = dt.datetime.fromtimestamp(answered.expires_at, tz=dt.UTC)
        return (
            "pass",
            f"token valid until {expires:%Y-%m-%d %H:%M} UTC; "
            f"pricing-info lists {programs} programs",
        )

    def _remember(self, *secrets: object) -> None:
        # `Tokens.from_json` keeps a stored field as it finds it, null included,
        # and a secret that is not a string would break every later redaction.
        self.secrets.update(s for s in secrets if isinstance(s, str))

    def check_seats_aero(self) -> _Outcome:
        key = seats_auth.load_key()
        if key is None:
            if seats_auth.KEY_PATH.exists():
                raise _unreadable_store(seats_auth.KEY_PATH, "`flight auth seats-aero key <KEY>`")
            return "skip", "no seats.aero key stored; run `flight auth seats-aero key <KEY>`"
        self.secrets.add(key)
        self._starting("seats-aero", "one seats.aero request, one unit of its daily quota")
        today = dt.datetime.now(dt.UTC).date().isoformat()

        async def go() -> RateLimit | None:
            async with SeatsAeroClient(api_key=key) as c:
                await c.search(
                    origin="JFK",
                    destination="LHR",
                    start_date=today,
                    end_date=today,
                    include_trips=False,
                    take=1,
                )
                return c.last_rate_limit

        quota = anyio.run(go)
        if quota is None:
            return "pass", "the key works; seats.aero sent no quota headers"
        return (
            "pass",
            f"the key works; {quota.remaining} of {quota.limit} requests left in the quota",
        )


# ─────────────────────────────── helpers ──────────────────────────────────


def fingerprint(secret: str) -> str:
    """Enough of a secret to tell two apart, and nothing to use one with."""
    # A stored credential can hold a lone surrogate: `json.loads` keeps an
    # escaped one, and the environment decodes a non-UTF-8 byte into one.
    return "sha256:" + hashlib.sha256(secret.encode(errors="surrogatepass")).hexdigest()[:8]


def _stored_secrets() -> set[str]:
    """Every credential this machine stores for the CLI: the four env vars,
    the cached Matrix key, the PointsPath tokens and the seats.aero key."""
    found = {os.environ.get(v, "") for v in _SECRET_ENV}
    found.add(_read_quietly(_api_key._CACHE_PATH))  # pyright: ignore[reportPrivateUsage]
    for path, fields in (
        (pp_auth.TOKENS_PATH, ("access_token", "refresh_token")),
        (seats_auth.KEY_PATH, ("api_key",)),
    ):
        try:
            data = json.loads(_read_quietly(path) or "{}")
        except ValueError:
            continue
        if isinstance(data, dict):
            found.update(str(data.get(f) or "") for f in fields)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return {s.strip() for s in found} | found


def _unreadable_store(path: Path, login: str) -> _CheckFailedError:
    # The loaders read a store they cannot parse as no store at all, so every
    # search leaves the provider out; only the file's presence tells them apart.
    return _CheckFailedError(
        "config", f"{path} holds no credential this CLI can read; run {login} again"
    )


def _pricing_programs(r: httpx.Response) -> int:
    try:
        body: Any = r.json()
        info = PricingInfoResponse.model_validate(body)
    except ValueError as e:  # JSONDecodeError and ValidationError alike
        raise _CheckFailedError(
            "shape",
            f"PointsPath's pricing-info answer does not parse ({type(e).__name__}); "
            "the response shape changed",
        ) from e
    # The model ignores unknown keys and defaults a missing list to empty, so
    # an error object would read as a catalog of no programs.
    if not (isinstance(body, dict) and "pricingInfos" in body):
        raise _CheckFailedError(
            "shape",
            "PointsPath's pricing-info answer has no pricingInfos; the response shape changed",
        )
    return len(info.pricingInfos)


def _cut_head_redacted(text: str, secret: str) -> str:
    """`text` with a head of `secret` that ends it replaced by the fingerprint.

    The providers' errors quote the first 200 characters of the body, so a
    secret the body echoes can be cut short, and what is left ends the text."""
    for n in range(len(secret) - 1, _MIN_SECRET_LEN - 1, -1):
        if text.endswith(secret[:n]):
            return text[:-n] + fingerprint(secret)
    return text


def _read_quietly(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _one_line(text: str) -> str:
    """The first non-blank line: `_help_text` and httpx both put the fact
    first and a paragraph of advice after it."""
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "no detail given")


def _classify(e: Exception) -> tuple[Cause, str]:  # noqa: PLR0911, PLR0912 — one arm per failure type
    """The cause of a failure a probe did not name itself, and its detail."""
    match e:
        case GfThrottledError():
            return "throttled", str(e)
        case GfTransportError():
            return "unreachable", str(e)
        case GfUpstreamStatusError() | GfSearchServerError():
            return "upstream", str(e)
        case GfConsentError():
            return "consent", str(e)
        case GfPageShapeError() | GfPinIgnoredError():
            return "shape", str(e)
        case GfBrowserUnavailableError():
            return "browser", str(e)
        case MatrixApiError():
            # Both fields are copied untyped from Matrix's error body, so either
            # can be null or a number.
            kind, message = str(e.kind), str(e.message)
            said = f"Matrix answered {kind}: {message}"
            brownout = kind.upper() in _MATRIX_BROWNOUT_KINDS or _MATRIX_INTERNAL_ERROR.search(
                message
            )
            return ("brownout" if brownout else "rejected"), said
        case ApiKeyResolutionError():
            # A network failure fetching the key is an outage. Anything else is
            # Matrix refusing the key it was sent, which the next run meets again.
            if isinstance(e.__cause__, httpx.TransportError):
                return "unreachable", str(e)
            return "auth", str(e)
        case httpx.TransportError():
            return "unreachable", f"{type(e).__name__}: {e}"
        case httpx.HTTPStatusError():
            return _status_cause(e.response.status_code), str(e)
        case pp_auth.PPAuthError():
            # A throttle or an outage at Supabase lifts on its own; any other
            # refusal of the refresh is a login the user has to redo.
            refresh = _SUPABASE_REFRESH_STATUS.match(str(e))
            cause = _status_cause(int(refresh.group(1))) if refresh else "auth"
            return (cause if cause in RETRYABLE else "auth"), str(e)
        case PPApiError():
            return _status_cause(e.status), str(e)
        case SeatsAeroError():
            return _status_cause(e.status), str(e)
        case _:
            return "error", f"{type(e).__name__}: {e}"


def _status_cause(status: int | None) -> Cause:
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return "auth"
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return "throttled"
    if status is not None and status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return "upstream"
    return "error"


def _spa_client() -> httpx.Client:
    """The client `_bootstrap_from_spa` uses, built the same way."""
    return httpx.Client(
        headers={"User-Agent": _api_key._UA},  # pyright: ignore[reportPrivateUsage]
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=True,
    )


def _spa_get(c: httpx.Client, url: str, what: str) -> str:
    r = c.get(url)
    if r.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        raise _CheckFailedError("throttled", f"{what} answered HTTP {r.status_code}")
    if r.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise _CheckFailedError("upstream", f"{what} answered HTTP {r.status_code}")
    if not r.is_success:
        raise _CheckFailedError("error", f"{what} answered HTTP {r.status_code}")
    return r.text


def _patchright_installed() -> bool:
    # A spec lookup, not an import: the import is what the launcher seam does,
    # and a check must not start down that path.
    return importlib.util.find_spec("patchright") is not None


def _chrome_channel_path() -> Path | None:
    """The Chrome `channel="chrome"` would launch, found where patchright
    looks for it, or None."""
    suffix = _CHROME_CHANNEL.get(sys.platform)
    if suffix is None:
        return None
    roots = [os.environ.get(v) for v in _WINDOWS_ROOTS] if sys.platform == "win32" else [""]
    for root in roots:
        if root is None:
            continue
        candidate = Path(root) / suffix
        if os.access(candidate, os.X_OK):
            return candidate
    return None


@contextlib.contextmanager
def _retry_log_redacted(redact: Callable[[str], str]) -> Generator[None]:
    """Log retries without the request URL in them, for the run's length.

    stamina's own hook logs `repr` of the exception that caused a retry, and
    httpx's status error quotes the request URL, key and all, to stderr."""
    previous = stamina.instrumentation.get_on_retry_hooks()

    def logged(details: RetryDetails) -> None:
        log.warning(
            "retry_scheduled",
            retry_num=details.retry_num,
            caused_by=redact(_one_line(repr(details.caused_by))),
            wait_for=round(details.wait_for, 2),
        )

    stamina.instrumentation.set_on_retry_hooks([logged])
    try:
        yield
    finally:
        stamina.instrumentation.set_on_retry_hooks(previous)
