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
from pathlib import Path
from typing import Optional

import httpx

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
    user_email: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_email": self.user_email,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Tokens":
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_at=int(d.get("expires_at") or 0),
            user_email=d.get("user_email"),
        )

    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_LEEWAY_SECS

    def jwt_claims(self) -> dict:
        """Decode the access_token JWT payload. No signature verification —
        we only use this to display issuer/email/expiry, never for trust."""
        parts = self.access_token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))


def _anon_key() -> str:
    return os.environ.get("PP_SUPABASE_ANON_KEY") or DEFAULT_SUPABASE_ANON_KEY


# ─────────────────────────────── store ─────────────────────────────────────

def load_tokens() -> Optional[Tokens]:
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
        return Tokens.from_json(json.loads(TOKENS_PATH.read_text()))
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
        raise PPAuthError(
            "No refresh_token available — re-run `flight-cli auth pp login`."
        )
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
    if r.status_code != 200:
        raise PPAuthError(
            f"Supabase refresh failed: HTTP {r.status_code} {r.text[:200]}"
        )
    data = r.json()
    new = Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or tokens.refresh_token,
        expires_at=int(time.time()) + int(data.get("expires_in", 3600)),
        user_email=(data.get("user") or {}).get("email") or tokens.user_email,
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

def import_from_tokens_file(path: Path) -> Tokens:
    """Import tokens from a JSON file (e.g., one captured via CDP cookie sniffing).

    Accepts the shape we already produce in /tmp/pp_tokens.json:
      {access_token, refresh_token, supabase_url?, user?}
    """
    raw = json.loads(Path(path).read_text())
    access = raw["access_token"]
    parts = access.split(".")
    if len(parts) != 3:
        raise PPAuthError("access_token is not a JWT (expected 3 dot-separated parts)")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    user = raw.get("user") or {}
    t = Tokens(
        access_token=access,
        refresh_token=raw.get("refresh_token") or "",
        expires_at=int(claims.get("exp") or 0),
        user_email=user.get("email") or claims.get("email"),
    )
    save_tokens(t)
    return t
