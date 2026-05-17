"""Seats.aero auth — much simpler than PointsPath.

Just a static API key (`Partner-Authorization: pro_xxxxxxxx` header). Two
source paths, in order:
  1. SEATS_AERO_API_KEY env var — useful for one-off shells / CI / curl
     scripts that already source from sops.
  2. ~/.config/flight-cli/seats.json — persistent, set by
     `flight auth seats key <KEY>`. 0600 perms.

No refresh, no expiry tracking. The Pro tier key is stable until rotated
on the seats.aero side; we surface 401 errors at request time rather than
trying to validate proactively.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

API_KEY_ENV = "SEATS_AERO_API_KEY"
CONFIG_DIR = Path.home() / ".config" / "flight-cli"
KEY_PATH = CONFIG_DIR / "seats.json"


class SeatsAuthError(Exception):
    """No key configured (env unset + on-disk file missing)."""


def load_key() -> str | None:
    """Resolve the API key from env → file → None.

    Returns None when neither source is configured — callers gate on this
    to decide whether to construct the provider (registry's `is_configured`)
    or to error out (e.g. --providers seats explicitly requested)."""
    env = os.environ.get(API_KEY_ENV)
    if env:
        return env.strip()
    if not KEY_PATH.exists():
        return None
    try:
        raw: Any = json.loads(KEY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    key: Any = data.get("api_key")
    if isinstance(key, str) and key:
        return key.strip()
    return None


def save_key(key: str) -> None:
    """Persist the API key to ~/.config/flight-cli/seats.json with 0600 perms.

    The directory is created if missing. Existing contents are replaced
    (no merge — this is a single-key file)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(json.dumps({"api_key": key.strip()}, indent=2))
    KEY_PATH.chmod(0o600)


def clear_key() -> bool:
    """Delete the on-disk key file. Returns True if a file was removed,
    False if nothing was there. Does not touch the env var."""
    if KEY_PATH.exists():
        KEY_PATH.unlink()
        return True
    return False


def is_configured() -> bool:
    """True iff an API key is available from env or file. Used by the
    registry's auto-enable check — provider-blind, parallel to
    pp.provider.is_configured."""
    return load_key() is not None


def get_key() -> str:
    """Like load_key but raises when nothing's configured. Use this from
    code paths that should hard-error if seats isn't set up
    (e.g. inside the provider's search_leg)."""
    key = load_key()
    if key is None:
        msg = (
            f"No Seats.aero API key configured. Set ${API_KEY_ENV} or run "
            "`flight auth seats key <KEY>`."
        )
        raise SeatsAuthError(msg)
    return key
