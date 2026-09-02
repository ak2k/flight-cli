# pyright: reportPrivateUsage=false
"""Persisted gflight session cookies (work-4bje3).

The cold-session empties are almost entirely a missing Google `NID` cookie.
Persisting the warmed session's NID and re-seeding it on the next one-shot
process is the root-cause fix (the retry-on-empty is the fallback). We persist
ONLY the allowlisted NID cookie, with a TTL so we re-warm a fresh identity
periodically rather than ride one forever.

No network here: a tiny fake client mirrors fli's `Client`, whose jar hangs off
`_session()` (`.cookies.jar` to read, `.cookies.set(...)` to seed).
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any, cast, override

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
    """Mirrors fli's `Client`: the jar hangs off `_session()`, which is backed by
    a `threading.local`. There is no `_client` attribute — reaching for one makes
    both helpers silent no-ops, swallowed by their broad excepts."""

    def __init__(self, cookies: list[_JarCookie] | None = None) -> None:
        self._sessions = _FakeSession(cookies or [])

    def _session(self) -> _FakeSession:
        return self._sessions


class _NeverLatched(dict[str, bool]):
    """A `_cookie_state` that always reads as "not yet persisted".

    The once-per-process latch in `_persist_cookies` is an unsynchronised
    check-then-set, so under the multi-cabin fan-out two threads can already be
    inside the write together. Holding the latch open forces that race on every
    iteration instead of once in eight runs."""

    @override
    def __getitem__(self, key: str) -> bool:
        return False


def _reset(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(gfid, "_cookie_state", {"persisted": False})
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())


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
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == [("NID", "532=abc", ".google.com", "/")]


def test_seed_with_no_saved_file_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)  # first-ever run, no file → no error
    assert fresh._session().cookies.set_calls == []


def test_seed_ignores_stale_cache_past_ttl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
        saved_at=time.time() - gfid._COOKIE_TTL_S - 1,  # just past TTL
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == []  # stale → re-warm, don't seed


def test_seed_uses_fresh_cache_within_ttl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
        saved_at=time.time() - 60,  # one minute old
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == [("NID", "v", ".google.com", "/")]


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
    assert len(fresh._session().cookies.set_calls) == 1


def test_persist_runs_only_once_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient([_JarCookie("NID", "v1", ".google.com")])
    gfid._persist_cookies(warm)
    warm._session().cookies.jar.append(_JarCookie("NID", "v2", ".google.com"))
    gfid._persist_cookies(warm)  # one write per process
    payload = json.loads((tmp_path / "gflight-cookies.json").read_text())
    assert payload["cookies"][0]["value"] == "v1"


def test_concurrent_persists_never_leave_a_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader that catches another thread mid-write gets a truncated file, and
    the seeder's own latch is already set by then — so that thread stays cold
    for the rest of the process. Rename-into-place is what removes the window."""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(gfid, "_cookie_state", _NeverLatched())
    path = tmp_path / "gflight-cookies.json"
    writers_done = threading.Event()
    torn: list[str] = []

    def write(tag: str) -> None:
        for i in range(50):
            # A value long enough that a partial write is visibly short.
            cookie = _JarCookie("NID", f"{tag}{i:03d}" * 40, ".google.com")
            gfid._persist_cookies(_FakeClient([cookie]))

    def read() -> None:
        while not writers_done.is_set():
            try:
                text = path.read_text()
            except OSError:
                continue
            try:
                payload = cast("dict[str, Any]", json.loads(text))
            except ValueError:
                torn.append(text[-60:])
                continue
            if payload["cookies"][0]["name"] != "NID":
                torn.append(text[-60:])

    reader = threading.Thread(target=read)
    reader.start()
    writers = [threading.Thread(target=write, args=(tag,)) for tag in ("a", "b")]
    for th in writers:
        th.start()
    for th in writers:
        th.join()
    writers_done.set()
    reader.join()

    assert not torn, f"a reader saw {len(torn)} partial cookie caches, e.g. {torn[:2]}"
    assert json.loads(path.read_text())["cookies"][0]["name"] == "NID"
    assert list(tmp_path.glob("*.tmp")) == [], "scratch files outlived the write"


def test_seed_ignores_corrupt_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    (tmp_path / "gflight-cookies.json").write_text("{not valid json")
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)  # must not raise
    assert fresh._session().cookies.set_calls == []


def test_persist_skips_when_no_allowlisted_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient([_JarCookie("AEC", "x", ".google.com")])  # Google but not NID
    gfid._persist_cookies(warm)
    assert not (tmp_path / "gflight-cookies.json").exists()


def test_persist_then_seed_round_trips_through_fli_s_real_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wiring a hand-rolled fake can't prove: fli's REAL `Client`, with only
    its session stubbed, must expose the jar where these helpers reach for it."""
    from curl_cffi import requests as curl_requests
    from fli.search.client import Client

    _reset(monkeypatch, tmp_path)
    # curl_cffi's Session is generic and unstubbed — Profile-B edge.
    session = cast("Any", curl_requests.Session())
    session.cookies.set("NID", "round-trip-value", domain=".google.com")

    writer = Client()
    monkeypatch.setattr(writer, "_session", lambda: session)
    gfid._persist_cookies(writer)
    assert (tmp_path / "gflight-cookies.json").exists()

    monkeypatch.setattr(gfid, "_seed_latch", threading.local())
    reader_session = cast("Any", curl_requests.Session())
    reader = Client()
    monkeypatch.setattr(reader, "_session", lambda: reader_session)
    gfid._seed_cookies_once(reader)
    assert reader_session.cookies.get("NID") == "round-trip-value"


def test_each_thread_gets_seeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """fli's session is a `threading.local`, so a process-wide seed latch left
    every worker thread but the first cold — which is precisely the fan-out."""
    _reset(monkeypatch, tmp_path)
    (tmp_path / "gflight-cookies.json").write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "cookies": [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
            }
        )
    )
    clients = [_FakeClient([]) for _ in range(3)]

    def seed(c: _FakeClient) -> None:
        gfid._seed_cookies_once(c)

    threads = [threading.Thread(target=seed, args=(c,)) for c in clients]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert all(c._session().cookies.set_calls for c in clients), (
        "a thread ran with a cold, NID-less session"
    )


def test_seeding_still_runs_once_within_one_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    (tmp_path / "gflight-cookies.json").write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "cookies": [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
            }
        )
    )
    first, second = _FakeClient([]), _FakeClient([])
    gfid._seed_cookies_once(first)
    gfid._seed_cookies_once(second)
    assert first._session().cookies.set_calls
    assert second._session().cookies.set_calls == []
