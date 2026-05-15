"""Resolve Matrix's public API key at runtime instead of hardcoding it.

Matrix's SPA bundles a public JS API key (the same one all anonymous web
visitors use). It's not a secret — anyone who opens devtools sees it —
but we'd rather not commit it. So we discover it the same way every web
visitor does: load Matrix's homepage, find the linked SPA bundle, regex
out the key.

Resolution order:
  1. FLIGHT_API_KEY env var (highest precedence)
  2. ~/.cache/flight-cli/.matrix-key  (auto-cached after first bootstrap; 30-day TTL)
  3. Bootstrap: scrape Matrix's SPA bundle live
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from pathlib import Path

import httpx

_CACHE_PATH = Path.home() / ".cache" / "flight-cli" / ".matrix-key"
_CACHE_TTL_SECS = 30 * 86400
# Shape of a real Google API key. Used to reject malformed cached values
# *before* they round-trip through a 403 from Matrix.
_KEY_SHAPE = re.compile(r"^AIzaSy[A-Za-z0-9_-]{33}$")
_HOMEPAGE = "https://matrix.itasoftware.com/search"
# Matrix's homepage references the SPA bundle via a protocol-relative URL
# (`//www.gstatic.com/alkali/...`), not an absolute one. Accept either.
_BUNDLE_PATTERN = re.compile(r'src="((?:https:)?//www\.gstatic\.com/alkali/[^"]+\.js)"')
# The bundle defines MULTIPLE API keys: DEFAULT, matrix, matrix-nightly,
# matrix-uat, People API, WAA, etc. We want specifically the production
# Matrix-search key — tagged in the bundle as `.matrix="AIza..."` or
# `"matrix":"AIza..."` (the minifier output varies but the "matrix" label
# is load-bearing in Google's own dispatch and shouldn't change lightly).
# Must NOT match "matrix-nightly", "matrix-uat", "matrix-dev" etc.
_KEY_PATTERN_MATRIX_PROD = re.compile(
    r'(?:[.])matrix\s*[=:]\s*["\'](AIzaSy[A-Za-z0-9_-]{33})["\']'
    r"|"
    r'["\']matrix["\']\s*:\s*["\'](AIzaSy[A-Za-z0-9_-]{33})["\']'
)
# Looser fallback (any AIzaSy key in the bundle) — used only if the
# targeted pattern fails. Likely returns the wrong key, but better than
# nothing; the error message guides the user to fix manually.
_KEY_PATTERN_ANY = re.compile(r"AIzaSy[A-Za-z0-9_-]{33}")

# Browser-y User-Agent so the homepage / static bundle requests look ordinary.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ApiKeyResolutionError(RuntimeError):
    """Raised when no key could be resolved. Includes guidance on fix."""


def resolve_api_key(*, force_bootstrap: bool = False) -> str:
    """Return Matrix's public API key. See module docstring for order."""
    if key := os.environ.get("FLIGHT_API_KEY"):
        return key.strip()

    if not force_bootstrap and _cache_fresh():
        cached = _CACHE_PATH.read_text().strip()
        if _KEY_SHAPE.match(cached):
            return cached
        # Cached value doesn't look like a Matrix key (truncated, edited,
        # wrong shape from an older bootstrap that captured the People API
        # key by mistake). Fall through to re-bootstrap.

    key = _bootstrap_from_spa()
    _write_cache(key)
    return key


def invalidate_cache() -> None:
    """Delete the cached API key. Call after a 403 from Matrix to force
    re-bootstrap on the next resolve_api_key() call."""
    with contextlib.suppress(OSError):
        _CACHE_PATH.unlink(missing_ok=True)


# ──────────────────────────────── helpers ──────────────────────────────────


def _cache_fresh() -> bool:
    return _CACHE_PATH.exists() and (time.time() - _CACHE_PATH.stat().st_mtime) < _CACHE_TTL_SECS


def _write_cache(key: str) -> None:
    # Cache write failure is non-fatal; we'll just re-bootstrap next time.
    with contextlib.suppress(OSError):
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(key + "\n")


def _bootstrap_from_spa() -> str:
    """Scrape Matrix's homepage + JS bundle for the public API key."""
    try:
        with httpx.Client(
            headers={"User-Agent": _UA},
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
        ) as c:
            home = c.get(_HOMEPAGE).text
            bundle_match = _BUNDLE_PATTERN.search(home)
            if not bundle_match:
                raise ApiKeyResolutionError(
                    _help_text(
                        "Couldn't locate Matrix's SPA bundle URL in the homepage HTML "
                        "(the SPA may have been restructured)."
                    )
                )
            bundle_url = bundle_match.group(1)
            if bundle_url.startswith("//"):
                bundle_url = "https:" + bundle_url
            js = c.get(bundle_url).text
            # Targeted match: the prod Matrix key tagged `.matrix=` or `"matrix":`
            if m := _KEY_PATTERN_MATRIX_PROD.search(js):
                # group(1) or group(2) — only one alternative matches
                return m.group(1) or m.group(2)
            raise ApiKeyResolutionError(
                _help_text(
                    "Found AIzaSy* keys in Matrix's SPA bundle but none tagged "
                    "as the prod 'matrix' key. Bundle structure may have changed."
                )
            )
    except httpx.HTTPError as e:
        raise ApiKeyResolutionError(
            _help_text(f"Network error contacting matrix.itasoftware.com: {e}")
        ) from e


def _help_text(reason: str) -> str:
    return (
        f"{reason}\n\n"
        "Fix one of three ways:\n"
        "  1. Set FLIGHT_API_KEY env var:\n"
        "        export FLIGHT_API_KEY=AIzaSy...\n"
        "     (find it via devtools: visit matrix.itasoftware.com, look at any\n"
        "      googleapis.com request — the 'key=' query param is the value.)\n"
        f"  2. Write the key to {_CACHE_PATH} directly.\n"
        "  3. File an issue if the bootstrap regex needs updating."
    )
