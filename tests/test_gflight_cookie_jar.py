# pyright: reportPrivateUsage=false
"""Persisted gflight session cookies (work-4bje3).

The cold-session empties are almost entirely a missing Google `NID` cookie.
Persisting the warmed session's NID and re-seeding it on the next one-shot
process is the root-cause fix (the retry-on-empty is the fallback). We persist
ONLY the allowlisted NID cookie, with a TTL so we re-warm a fresh identity
periodically rather than ride one forever.

No network here: a tiny fake client mirrors the curl_cffi session's cookie API
(`_client.cookies.jar` to read, `_client.cookies.set(...)` to seed).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import flight_cli._gflight_ids as gfid

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _JarCookie:
    def __init__(self, name: str, value: str, domain: str, path: str = "/") -> None:
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path


class _FakeCookies:
    def __init__(self, cookies: list[_JarCookie]) -> None:
        self.jar: list[_JarCookie] = list(cookies)
        self.set_calls: list[tuple[str, str, str, str]] = []

    def set(self, name: str, value: str, domain: str = "/", path: str = "/") -> None:
        self.set_calls.append((name, value, domain, path))
        self.jar.append(_JarCookie(name, value, domain, path))


class _FakeSession:
    def __init__(self, cookies: list[_JarCookie]) -> None:
        self.cookies = _FakeCookies(cookies)


class _FakeClient:
    def __init__(self, cookies: list[_JarCookie] | None = None) -> None:
        self._client = _FakeSession(cookies or [])


def _reset(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(gfid, "_cookie_state", {"seeded": False, "persisted": False})


def _write_cache(cache_dir: Path, cookies: list[dict[str, str]], *, saved_at: float) -> None:
    (cache_dir / "gflight-cookies.json").write_text(
        json.dumps({"saved_at": saved_at, "cookies": cookies})
    )


def test_persist_then_seed_round_trips_only_nid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)

    warm = _FakeClient(
        [
            _JarCookie("NID", "532=abc", ".google.com"),
            _JarCookie("AEC", "junk", ".google.com"),  # Google but not allowlisted
            _JarCookie("other", "x", ".example.com"),  # not Google
        ]
    )
    gfid._persist_cookies(warm)

    payload = json.loads((tmp_path / "gflight-cookies.json").read_text())
    assert [c["name"] for c in payload["cookies"]] == ["NID"]  # only NID persisted
    assert payload["saved_at"] <= time.time()  # stamped now

    # A brand-new (cold) process reloads and seeds the NID onto its fresh session.
    gfid._cookie_state["seeded"] = False
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._client.cookies.set_calls == [("NID", "532=abc", ".google.com", "/")]


def test_seed_with_no_saved_file_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)  # first-ever run, no file → no error
    assert fresh._client.cookies.set_calls == []


def test_seed_ignores_stale_cache_past_ttl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
        saved_at=time.time() - gfid._COOKIE_TTL_S - 1,  # just past TTL
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._client.cookies.set_calls == []  # stale → re-warm, don't seed


def test_seed_uses_fresh_cache_within_ttl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
        saved_at=time.time() - 60,  # one minute old
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._client.cookies.set_calls == [("NID", "v", ".google.com", "/")]


def test_seed_runs_only_once_per_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
        saved_at=time.time(),
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    gfid._seed_cookies_once(fresh)  # second call is a no-op
    assert len(fresh._client.cookies.set_calls) == 1


def test_persist_runs_only_once_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient([_JarCookie("NID", "v1", ".google.com")])
    gfid._persist_cookies(warm)
    warm._client.cookies.jar.append(_JarCookie("NID", "v2", ".google.com"))
    gfid._persist_cookies(warm)  # one write per process
    payload = json.loads((tmp_path / "gflight-cookies.json").read_text())
    assert payload["cookies"][0]["value"] == "v1"


def test_seed_ignores_corrupt_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    (tmp_path / "gflight-cookies.json").write_text("{not valid json")
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)  # must not raise
    assert fresh._client.cookies.set_calls == []


def test_persist_skips_when_no_allowlisted_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient([_JarCookie("AEC", "x", ".google.com")])  # Google but not NID
    gfid._persist_cookies(warm)
    assert not (tmp_path / "gflight-cookies.json").exists()


# ───────── the jar must resolve against the REAL fli client ─────────


def test_cookie_jar_resolves_against_the_installed_fli_client() -> None:
    """Regression: this module reached for `Client._client`, which fli 0.9
    replaced with a per-thread `Client._session()`. The AttributeError landed
    in a best-effort `except` and was logged at debug, so NID seeding AND
    persistence became silent no-ops — every process started cold, against a
    documented ~40% cold-start empty rate versus ~0% warm.

    Deliberately exercised against the real installed client rather than a
    stub: a stub would have kept passing through the upstream rename, which is
    exactly how this went unnoticed.
    """
    from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]

    from flight_cli._gflight_ids import _cookie_jar

    jar = _cookie_jar(get_client())  # pyright: ignore[reportUnknownArgumentType]
    assert hasattr(jar, "set")
    assert hasattr(jar, "jar")


def test_cookie_jar_seeding_is_observable() -> None:
    """A seeded cookie must actually be readable back — the property the
    silent no-op destroyed."""
    from fli.search.client import get_client  # pyright: ignore[reportMissingTypeStubs]

    from flight_cli._gflight_ids import _cookie_jar

    jar = _cookie_jar(get_client())  # pyright: ignore[reportUnknownArgumentType]
    jar.set("NID", "sentinel-value", domain=".google.com", path="/")
    names = [str(c.name) for c in jar.jar]
    assert "NID" in names


def test_cookie_jar_raises_when_no_known_shape() -> None:
    """Fails loudly on a future rename instead of degrading to a no-op again."""
    import pytest

    from flight_cli._gflight_ids import _cookie_jar

    class _Alien:
        pass

    with pytest.raises(AttributeError):
        _ = _cookie_jar(_Alien())
