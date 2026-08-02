# pyright: reportPrivateUsage=false
"""Tests for the cookie-reassembly + login-helper code paths in pp/auth.py.

The headed Playwright path can't be unit-tested (it requires a real
PointsPath login), but the shared cookie-reassembly logic — which both
`--from-chrome` and the Playwright capture funnel through — IS testable
with synthetic cookies."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli.pp import auth as auth_mod
from flight_cli.pp.auth import (
    SUPABASE_AUTH_COOKIE_PREFIX,
    PPAuthError,
    login_from_chrome,
    tokens_from_supabase_cookies,
)

if TYPE_CHECKING:
    import pathlib

_Cookie = dict[str, str]
_Payload = dict[str, Any]

# Dummy test refresh token. Bandit's S105/S107 fire on string literals named
# *_token; this isn't a real credential.
_FAKE_REFRESH = "rt-abc"


# A minimal valid JWT — header.payload.signature, payload base64-encoding
# a JSON claims dict with `exp` and `email`.
def _jwt(exp: int = 2000000000, email: str = "test@example.com") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    claims = json.dumps({"exp": exp, "email": email, "sub": "fake-sub"})
    payload = base64.urlsafe_b64encode(claims.encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _session_payload(
    access: str | None = None,
    refresh: str = _FAKE_REFRESH,
    email: str = "test@example.com",
    expires_at: int = 2000000000,
) -> _Payload:
    return {
        "access_token": access or _jwt(exp=expires_at, email=email),
        "refresh_token": refresh,
        "expires_at": expires_at,
        "user": {"email": email, "id": "fake-id"},
    }


def _split_cookie(
    payload: _Payload, *, encode_base64: bool = True, chunks: int = 2
) -> list[_Cookie]:
    """Produce the `.0`+`.1` cookie pair Supabase writes to browsers."""
    raw = json.dumps(payload)
    if encode_base64:
        raw = "base64-" + base64.b64encode(raw.encode()).decode().rstrip("=")
    if chunks == 1:
        return [{"name": SUPABASE_AUTH_COOKIE_PREFIX, "value": raw}]
    mid = len(raw) // chunks
    return [
        {"name": f"{SUPABASE_AUTH_COOKIE_PREFIX}.0", "value": raw[:mid]},
        {"name": f"{SUPABASE_AUTH_COOKIE_PREFIX}.1", "value": raw[mid:]},
    ]


def _redirect_token_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the on-disk token store at tmp_path so tests don't touch real config."""
    monkeypatch.setattr(auth_mod, "TOKENS_PATH", tmp_path / "pp.json")
    monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)


# ─────────────────────── tokens_from_supabase_cookies ───────────────────────


def test_reassembles_split_base64_cookies(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standard shape: two cookies (.0 + .1), payload base64-prefixed."""
    _redirect_token_store(tmp_path, monkeypatch)
    cookies = _split_cookie(_session_payload(email="adam@example.com"))
    t = tokens_from_supabase_cookies(cookies)
    assert t.user_email == "adam@example.com"
    assert t.refresh_token == _FAKE_REFRESH
    assert t.access_token.count(".") == 2  # round-trip preserved JWT shape


def test_handles_raw_json_payload_no_base64_prefix(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older Supabase versions / different gotrue-js configs may write raw JSON."""
    _redirect_token_store(tmp_path, monkeypatch)
    cookies = _split_cookie(_session_payload(), encode_base64=False)
    t = tokens_from_supabase_cookies(cookies)
    assert t.user_email == "test@example.com"


def test_handles_single_unsplit_cookie(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some Supabase versions write a single cookie when the payload fits."""
    _redirect_token_store(tmp_path, monkeypatch)
    cookies = _split_cookie(_session_payload(), chunks=1)
    t = tokens_from_supabase_cookies(cookies)
    assert t.refresh_token == _FAKE_REFRESH


def test_ignores_pkce_verifier_cookie(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `-code-verifier` cookie shares our prefix but isn't part of the session."""
    _redirect_token_store(tmp_path, monkeypatch)
    cookies = _split_cookie(_session_payload())
    cookies.append(
        {"name": f"{SUPABASE_AUTH_COOKIE_PREFIX}-code-verifier", "value": "pkce-junk"},
    )
    t = tokens_from_supabase_cookies(cookies)
    assert t.user_email == "test@example.com"


def test_raises_when_no_auth_cookies_found(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browser isn't logged in / cookies expired and were cleared."""
    _redirect_token_store(tmp_path, monkeypatch)
    cookies: list[_Cookie] = [{"name": "_ga", "value": "GA1.1.xxx"}]
    with pytest.raises(PPAuthError, match=r"No sb-.*-auth-token"):
        tokens_from_supabase_cookies(cookies)


def test_expiry_falls_back_to_jwt_exp_when_payload_missing_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old payloads may lack `expires_at`; we should mine the JWT claim instead."""
    _redirect_token_store(tmp_path, monkeypatch)
    payload = _session_payload(expires_at=1_900_000_000)
    del payload["expires_at"]  # force the JWT-claim fallback
    cookies = _split_cookie(payload)
    t = tokens_from_supabase_cookies(cookies)
    assert t.expires_at == 1_900_000_000


# ──────────────────────────── login_from_chrome ─────────────────────────────


def test_login_from_chrome_uses_rookiepy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the rookiepy plumbing: we ask only for pointspath.com, and the
    returned cookies pass through tokens_from_supabase_cookies."""
    _redirect_token_store(tmp_path, monkeypatch)
    seen_domains: list[list[str]] = []

    class _FakeRookiepy:
        @staticmethod
        def chrome(domains: list[str]) -> list[_Cookie]:
            seen_domains.append(domains)
            return _split_cookie(_session_payload(email="chrome@example.com"))

    import sys

    monkeypatch.setitem(sys.modules, "rookiepy", _FakeRookiepy)

    t = login_from_chrome()
    assert t.user_email == "chrome@example.com"
    assert seen_domains == [["pointspath.com"]], "Should scope read to pointspath.com only"


# ───────── the token file is never briefly world-readable ─────────


def test_token_file_is_created_0600_not_widened_then_tightened(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """`write_text` creates at the process umask (0644 by default), so writing
    then chmod'ing left a bearer token to a paid account world-readable for the
    window between the two calls. The file must be created 0600."""
    import stat

    from flight_cli.pp import auth as auth_mod

    monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKENS_PATH", tmp_path / "pp.json")

    modes: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        if str(path).endswith("pp.json"):
            modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    auth_mod.save_tokens(
        auth_mod.Tokens(
            access_token="A",  # noqa: S106 — dummy test value
            refresh_token="R",  # noqa: S106 — dummy test value
            expires_at=9_999_999_999,
            user_email="x@y.z",
        ),
    )
    # Requested at creation, not applied afterwards.
    assert modes == [0o600]
    final = stat.S_IMODE((tmp_path / "pp.json").stat().st_mode)
    assert final == 0o600


def test_saved_tokens_round_trip(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    from flight_cli.pp import auth as auth_mod

    monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKENS_PATH", tmp_path / "pp.json")
    original = auth_mod.Tokens(
        access_token="ACCESS",  # noqa: S106 — dummy test value
        refresh_token="REFRESH",  # noqa: S106 — dummy test value
        expires_at=9_999_999_999,
        user_email="x@y.z",
    )
    auth_mod.save_tokens(original)
    loaded = auth_mod.load_tokens()
    assert loaded is not None
    assert loaded.access_token == "ACCESS"  # noqa: S105 — asserting a dummy round-trip
    assert loaded.user_email == "x@y.z"
