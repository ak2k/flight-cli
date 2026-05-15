"""Token store + Supabase JWT refresh + login-flow plumbing for PointsPath.

Storage layout
  ~/.config/flight-cli/pp.json  →  {access_token, refresh_token, expires_at, user_email}
  PP_ACCESS_TOKEN env var       →  takes precedence (CI / scripting / one-shot)
  PP_SUPABASE_ANON_KEY env var  →  override the bundled anon key (default works)

The Supabase anon key below is the public key shipped in PointsPath's browser
extension and Next.js bundle — anyone who installs the extension sees it. It is
not a secret and rotates on the order of years (the JWT exp is in 2034).
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

import httpx

_JWT_PARTS = 3  # header.payload.signature
_JsonDict = dict[str, Any]

SUPABASE_URL = "https://hxjqzkcirzhjvtubefie.supabase.co"

# Public anon JWT extracted from extension chunk (role=anon, exp=2028+).
# Override with PP_SUPABASE_ANON_KEY if PointsPath rotates it.
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh4anF6a2NpcnpoanZ0dWJlZmllIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MTI5MDM2NTEsImV4cCI6MjAyODQ3OTY1MX0."
    "eTj23z4l_XxbVLhdaeJzXJzTvR06j_CdGsl9atohvj0"
)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "flight-cli"
TOKENS_PATH = CONFIG_DIR / "pp.json"

# Refresh when token has < 60s left, to avoid mid-request expiry.
REFRESH_LEEWAY_SECS = 60


class PPAuthError(Exception):
    """Auth failure (no tokens, refresh failed, expired without refresh token)."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int  # Unix seconds
    user_email: str | None = None

    def to_json(self) -> _JsonDict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_email": self.user_email,
        }

    @classmethod
    def from_json(cls, d: _JsonDict) -> Tokens:
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_at=int(d.get("expires_at") or 0),
            user_email=d.get("user_email"),
        )

    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_LEEWAY_SECS

    def jwt_claims(self) -> _JsonDict:
        """Decode the access_token JWT payload. No signature verification —
        we only use this to display issuer/email/expiry, never for trust."""
        parts = self.access_token.split(".")
        if len(parts) != _JWT_PARTS:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return cast("_JsonDict", json.loads(base64.urlsafe_b64decode(payload)))


def _anon_key() -> str:
    return os.environ.get("PP_SUPABASE_ANON_KEY") or DEFAULT_SUPABASE_ANON_KEY


# ─────────────────────────────── store ─────────────────────────────────────


def load_tokens() -> Tokens | None:
    """Load tokens from env override, then disk. Returns None if neither present."""
    env_access = os.environ.get("PP_ACCESS_TOKEN")
    if env_access:
        # Env-override mode: no refresh, no expiry tracking. Caller must
        # provide a fresh token. We surface this as a Tokens with expires_at=0
        # so needs_refresh() returns True; refresh() will then try to use
        # PP_REFRESH_TOKEN if present.
        return Tokens(
            access_token=env_access,
            refresh_token=os.environ.get("PP_REFRESH_TOKEN", ""),
            expires_at=0,
            user_email=None,
        )
    if not TOKENS_PATH.exists():
        return None
    try:
        return Tokens.from_json(cast("_JsonDict", json.loads(TOKENS_PATH.read_text())))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def save_tokens(t: Tokens) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(t.to_json(), indent=2))
    # 0600 — contains a bearer token to a paid user account.
    TOKENS_PATH.chmod(0o600)


def clear_tokens() -> bool:
    """Remove the on-disk store. Returns True if a file was removed."""
    if TOKENS_PATH.exists():
        TOKENS_PATH.unlink()
        return True
    return False


# ─────────────────────────────── refresh ───────────────────────────────────


def refresh(tokens: Tokens) -> Tokens:
    """Exchange refresh_token for a new access_token via Supabase auth.

    Mutates the on-disk store as a side effect when tokens load from disk.
    Raises PPAuthError if refresh_token is missing or refresh fails.
    """
    if not tokens.refresh_token:
        raise PPAuthError("No refresh_token available — re-run `flight-cli auth pp login`.")
    r = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={
            "apikey": _anon_key(),
            "content-type": "application/json",
        },
        json={"refresh_token": tokens.refresh_token},
        timeout=20,
    )
    if r.status_code != HTTPStatus.OK:
        raise PPAuthError(f"Supabase refresh failed: HTTP {r.status_code} {r.text[:200]}")
    data: _JsonDict = r.json()
    new = Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or tokens.refresh_token,
        expires_at=int(time.time()) + int(data.get("expires_in", 3600)),
        user_email=(cast("_JsonDict", data.get("user") or {})).get("email") or tokens.user_email,
    )
    # Only persist if the on-disk store is the source of truth (not env override).
    if not os.environ.get("PP_ACCESS_TOKEN"):
        save_tokens(new)
    return new


def get_valid_tokens() -> Tokens:
    """Return tokens that are good for at least the next ~minute. Refreshes
    on the fly if stale. Raises PPAuthError if no tokens are available."""
    t = load_tokens()
    if t is None:
        raise PPAuthError(
            "No PointsPath tokens. Run `flight-cli auth pp login --tokens-file ...` "
            "or set PP_ACCESS_TOKEN."
        )
    if t.needs_refresh():
        t = refresh(t)
    return t


# ──────────────────────────── login helpers ────────────────────────────────

# Supabase splits the auth-token cookie across `sb-<ref>-auth-token.0` and
# `.1` to dodge the 4KB cookie limit. The reassembled value is either raw
# JSON or "base64-" + base64 of JSON, depending on gotrue-js version.
SUPABASE_PROJECT_REF = "hxjqzkcirzhjvtubefie"
SUPABASE_AUTH_COOKIE_PREFIX = f"sb-{SUPABASE_PROJECT_REF}-auth-token"


def _tokens_from_supabase_payload(payload: _JsonDict) -> Tokens:
    """Build Tokens from the JSON object Supabase stores in its session cookie
    or returns from /auth/v1/token: {access_token, refresh_token, user, ...}.
    Falls back to JWT exp claim if expires_at is absent."""
    access: str = payload["access_token"]
    parts = access.split(".")
    if len(parts) != _JWT_PARTS:
        raise PPAuthError("access_token is not a JWT (expected 3 dot-separated parts)")
    claims_payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims: _JsonDict = json.loads(base64.urlsafe_b64decode(claims_payload))
    user: _JsonDict = payload.get("user") or {}
    expires_at = int(payload.get("expires_at") or claims.get("exp") or 0)
    return Tokens(
        access_token=access,
        refresh_token=payload.get("refresh_token") or "",
        expires_at=expires_at,
        user_email=user.get("email") or claims.get("email"),
    )


def tokens_from_supabase_cookies(cookies: list[dict[str, Any]]) -> Tokens:
    """Reassemble Supabase auth tokens from a list of cookie dicts.

    Each dict needs `name` and `value` keys (the shape produced by rookiepy
    and Playwright's BrowserContext.cookies()). Filters to `sb-<ref>-auth-
    token[.N]` cookies, concatenates by index, decodes optional `base64-`
    prefix, parses the JSON, and returns Tokens.
    """
    by_idx: dict[int, str] = {}
    for c in cookies:
        name = c.get("name", "")
        if not name.startswith(SUPABASE_AUTH_COOKIE_PREFIX):
            continue
        if "verifier" in name:  # PKCE-flow verifier cookie; not the session
            continue
        if name == SUPABASE_AUTH_COOKIE_PREFIX:
            by_idx[0] = c.get("value", "")
        else:
            suffix = name.removeprefix(SUPABASE_AUTH_COOKIE_PREFIX + ".")
            try:
                by_idx[int(suffix)] = c.get("value", "")
            except ValueError:
                continue
    if not by_idx:
        raise PPAuthError(
            f"No {SUPABASE_AUTH_COOKIE_PREFIX}.* cookies found "
            "(is the browser logged into PointsPath?)",
        )
    joined = "".join(by_idx[i] for i in sorted(by_idx))
    if joined.startswith("base64-"):
        decoded = base64.b64decode(joined.removeprefix("base64-") + "==", validate=False)
        raw_json = decoded.decode("utf-8")
    else:
        raw_json = joined
    payload: _JsonDict = json.loads(raw_json)
    t = _tokens_from_supabase_payload(payload)
    save_tokens(t)
    return t


def import_from_tokens_file(path: Path) -> Tokens:
    """Import tokens from a JSON file (e.g., one captured via CDP cookie sniffing).

    Accepts the shape we already produce in /tmp/pp_tokens.json:
      {access_token, refresh_token, supabase_url?, user?}
    """
    raw: _JsonDict = json.loads(Path(path).read_text())
    t = _tokens_from_supabase_payload(raw)
    save_tokens(t)
    return t


# Pointspath login URL — the homepage redirects unauthenticated users here.
_PP_HOME_URL = "https://pointspath.com/"

# Convenience login mode default: leave the browser open long enough for the
# user to click through email confirmation / 2FA if needed.
_DEFAULT_BROWSER_LOGIN_TIMEOUT_SECS = 300


def login_from_chrome() -> Tokens:
    """Import a PointsPath session from a local Chrome profile via rookiepy.

    Convenience path: piggybacks on whatever Chrome session you already have
    open. Inherits Chrome's refresh chain, so a CLI refresh here can race
    against Chrome's gotrue-js. Prefer the headed Playwright path
    (`login_via_browser`) for an independent session.
    """
    try:
        import rookiepy  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as e:  # pragma: no cover — install-time concern
        raise PPAuthError(
            "rookiepy isn't installed (used for --from-chrome). "
            "Install with: uv pip install rookiepy",
        ) from e

    cookies: list[_JsonDict] = rookiepy.chrome(domains=["pointspath.com"])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return tokens_from_supabase_cookies(cookies)


# Persistent browser profile for the PP login flow. Cloudflare uses cf_clearance
# cookies that are bound to the browser fingerprint; persisting them across runs
# means the user only has to clear the human-check once.
BROWSER_PROFILE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    / "flight-cli"
    / "browser-profile"
)


def login_via_browser(
    *,
    timeout_secs: int = _DEFAULT_BROWSER_LOGIN_TIMEOUT_SECS,
    poll_interval_secs: float = 1.0,
) -> Tokens:
    """Open a headed Patchright Chromium for the user to log into PointsPath.

    Uses `patchright` (a Playwright fork that patches the CDP `Runtime.enable`
    leak and `navigator.webdriver` exposure) so Cloudflare's bot fingerprint
    check passes. Launches with `channel="chrome"` to use the real Chrome
    binary (whose JA3/JA4 TLS fingerprint matches real Chrome traffic) and a
    persistent context under ~/.cache/flight-cli/browser-profile so cf_clearance
    cookies survive between login sessions.

    Independent of any user-facing Chrome session. Polls for the Supabase
    auth-token cookie; returns as soon as it appears, or raises PPAuthError
    on timeout / browser close.
    """
    try:
        from patchright.sync_api import Error as PlaywrightError  # noqa: PLC0415
        from patchright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover — install-time concern
        raise PPAuthError(
            "patchright isn't installed (needed for Cloudflare-resistant browser "
            "login). Quickest path: rerun this command via "
            "`uv run --with patchright flight auth pp login` "
            "(plus a one-time `uvx --from patchright patchright install chrome`). "
            "Alternatives: --from-chrome (reads cookies from your real Chrome) "
            "or --tokens-file PATH.",
        ) from e

    import time  # noqa: PLC0415

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # launch_persistent_context (not launch + new_context) is the
        # recommended Patchright pattern for Cloudflare: it persists cookies +
        # storage between runs, and lets the browser appear as a fully real
        # Chrome session.
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.new_page()
            page.goto(_PP_HOME_URL)

            deadline = time.time() + timeout_secs
            while time.time() < deadline:
                try:
                    raw_cookies = context.cookies([_PP_HOME_URL])
                except PlaywrightError as e:
                    raise PPAuthError(f"Browser closed before login completed: {e}") from e
                cookies: list[_JsonDict] = [dict(c) for c in raw_cookies]
                try:
                    return tokens_from_supabase_cookies(cookies)
                except PPAuthError:
                    # Cookies aren't ready yet; user is still authenticating.
                    pass
                time.sleep(poll_interval_secs)
            raise PPAuthError(
                f"Timed out after {timeout_secs}s waiting for PointsPath login. "
                "Re-run `flight auth pp login` and complete sign-in before the timeout.",
            )
        finally:
            context.close()
