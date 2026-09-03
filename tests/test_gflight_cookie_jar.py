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
import os
import pathlib
import stat
import threading
import time
from typing import TYPE_CHECKING, Any, cast, override

import pytest

import flight_cli._gflight_ids as gfid

if TYPE_CHECKING:
    from pathlib import Path


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


def _cookie_file(cache_dir: Path) -> Path:
    """Where the jar lands: a directory of this component's own under the cache
    root, so the root's mode stays whatever its owner chose."""
    return cache_dir / "gflight" / "gflight-cookies.json"


def _strays(cache_dir: Path) -> list[Path]:
    """Scratch files left behind, looked for where the writer actually puts
    them — beside the jar, not in the cache root above it. A glob one directory
    up finds nothing whatever the cleanup does."""
    return list(_cookie_file(cache_dir).parent.glob("*.tmp"))


def _plant_cache(cache_dir: Path, text: str) -> Path:
    """Put `text` where the reader will look for the jar, directory and all."""
    path = _cookie_file(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _write_cache(cache_dir: Path, cookies: list[dict[str, str]], *, saved_at: float) -> None:
    path = _cookie_file(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"saved_at": saved_at, "cookies": cookies}))


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

    payload = json.loads(_cookie_file(tmp_path).read_text())
    assert [c["name"] for c in payload["cookies"]] == ["NID"]  # only NID persisted
    assert payload["saved_at"] <= time.time()  # stamped now

    # A brand-new (cold) process reloads and seeds the NID onto its fresh session.
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == [("NID", "532=abc", ".google.com", "/")]


@pytest.mark.parametrize(
    "domain",
    [
        pytest.param("google.com.evil.example", id="google-com-as-a-prefix"),
        pytest.param("notgoogle.com", id="a-suffix-that-is-not-a-label"),
        pytest.param(".evil.example", id="a-plainly-foreign-domain"),
    ],
)
def test_a_foreign_domains_cookie_is_never_written_to_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: str
) -> None:
    """The WRITE side of the same allowlist the read side applies.

    A live session picks up cookies from wherever it has been, including any
    redirect it followed. Writing an NID for a host we do not talk to puts it in
    a file the next process seeds from, so the read-side check would be the only
    thing standing between a redirect and a cookie on a live session."""
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient(
        [
            _JarCookie("NID", "impostor", domain),
            _JarCookie("NID", "532=abc", ".google.com"),
        ]
    )
    gfid._persist_cookies(warm)

    payload = json.loads(_cookie_file(tmp_path).read_text())
    assert [(c["name"], c["domain"]) for c in payload["cookies"]] == [("NID", ".google.com")]


def test_a_cache_with_nothing_but_a_foreign_cookie_is_not_written_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing allowlisted is nothing to save; an empty cache file would only
    give the next process something to fail to parse."""
    _reset(monkeypatch, tmp_path)
    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "impostor", "google.com.evil.example")]))
    assert not _cookie_file(tmp_path).exists()


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
    payload = json.loads(_cookie_file(tmp_path).read_text())
    assert payload["cookies"][0]["value"] == "v1"


def test_concurrent_persists_never_leave_a_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader that catches another thread mid-write gets a truncated file, and
    the seeder's own latch is already set by then — so that thread stays cold
    for the rest of the process. Rename-into-place is what removes the window."""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(gfid, "_cookie_state", _NeverLatched())
    path = _cookie_file(tmp_path)
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
    assert _strays(tmp_path) == [], "scratch files outlived the write"


def test_the_cache_is_owner_only_even_over_a_world_readable_predecessor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The NID is a live Google session cookie, so the cache carries the same
    0600 the token stores do. The rename puts the temp file's own inode in
    place, so the mode has to be right on the temp — an existing 0644 cache is
    replaced, not chmod'ed."""
    _reset(monkeypatch, tmp_path)
    path = _plant_cache(tmp_path, "{}")
    path.chmod(0o644)

    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["cookies"][0]["name"] == "NID"


def test_a_failed_rename_leaves_no_temp_holding_the_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing sweeps the cache directory, so a temp that survives a failure is
    a stray copy of the NID sitting there until someone notices."""
    _reset(monkeypatch, tmp_path)

    def _boom(self: Path, _target: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(pathlib.Path, "replace", _boom)
    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))

    assert _strays(tmp_path) == []
    assert not _cookie_file(tmp_path).exists()


def test_an_interrupt_mid_write_leaves_no_temp_holding_the_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl-C during a search is ordinary, and it does not come through the
    OSError arm. Cleanup has to sit in a `finally` or the interrupt strands the
    temp with the NID already written into it."""
    _reset(monkeypatch, tmp_path)
    real_dump = json.dump

    def _interrupted(obj: object, fh: Any, **kw: Any) -> None:
        real_dump(obj, fh, **kw)  # the NID really is on disk when this lands
        fh.flush()
        raise KeyboardInterrupt

    monkeypatch.setattr(gfid.json, "dump", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))

    assert _strays(tmp_path) == []


def test_seed_ignores_corrupt_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset(monkeypatch, tmp_path)
    _plant_cache(tmp_path, "{not valid json")
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)  # must not raise
    assert fresh._session().cookies.set_calls == []


def test_persist_skips_when_no_allowlisted_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset(monkeypatch, tmp_path)
    warm = _FakeClient([_JarCookie("AEC", "x", ".google.com")])  # Google but not NID
    gfid._persist_cookies(warm)
    assert not _cookie_file(tmp_path).exists()


def test_persist_then_seed_round_trips_through_fli_s_real_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wiring a hand-rolled fake can't prove: fli's REAL `Client`, with only
    its session stubbed, must expose the jar where these helpers reach for it."""
    from curl_cffi import requests as curl_requests
    from fli.search.client import (  # pyright: ignore[reportMissingTypeStubs]
        Client,  # fli ships no stubs; nothing here reads a typed fli API
    )

    _reset(monkeypatch, tmp_path)
    # curl_cffi's Session is generic and unstubbed — Profile-B edge.
    session = cast("Any", curl_requests.Session())
    session.cookies.set("NID", "round-trip-value", domain=".google.com")

    writer = Client()
    monkeypatch.setattr(writer, "_session", lambda: session)
    gfid._persist_cookies(writer)
    assert _cookie_file(tmp_path).exists()

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
    _plant_cache(
        tmp_path,
        json.dumps(
            {
                "saved_at": time.time(),
                "cookies": [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
            }
        ),
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
    _plant_cache(
        tmp_path,
        json.dumps(
            {
                "saved_at": time.time(),
                "cookies": [{"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}],
            }
        ),
    )
    first, second = _FakeClient([]), _FakeClient([])
    gfid._seed_cookies_once(first)
    gfid._seed_cookies_once(second)
    assert first._session().cookies.set_calls
    assert second._session().cookies.set_calls == []


def test_a_temp_we_did_not_create_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`O_EXCL` failing means the scratch file was already there and belongs to
    another writer, mid-write. Deleting it because our own `finally` runs would
    destroy their data — cleanup owns only what this call created."""
    _reset(monkeypatch, tmp_path)
    jar = _cookie_file(tmp_path)
    jar.parent.mkdir(parents=True, exist_ok=True)
    squatter = jar.with_name(f"{jar.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    squatter.write_text("another writer's half-written file")

    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))

    assert squatter.exists(), "cleanup deleted a temp file it did not create"
    assert squatter.read_text() == "another writer's half-written file"
    assert not _cookie_file(tmp_path).exists()


@pytest.mark.parametrize(
    "domain",
    [
        pytest.param(".google.com", id="the-domain-wide-form-google-sends"),
        pytest.param("google.com", id="host-only"),
        pytest.param("www.google.com", id="a-subdomain"),
    ],
)
def test_an_allowlisted_google_domain_is_still_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: str
) -> None:
    """Tightening the domain check must not stop seeding the cookie it exists
    for. A domain-wide cookie carries the leading dot Google writes."""
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [{"name": "NID", "value": "legitimate", "domain": domain, "path": "/"}],
        saved_at=time.time(),
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == [("NID", "legitimate", domain, "/")]


@pytest.mark.parametrize("premade", [False, True], ids=["fresh", "already-there-and-0755"])
def test_the_cookie_directory_is_owner_only_and_the_cache_root_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, premade: bool
) -> None:
    """`mkdir` takes the umask unless told otherwise, and this directory holds a
    live Google session cookie. So the cookie gets a directory of its own, which
    this code creates and can therefore make private.

    The pre-made case is the one that happens in practice, and it is why the
    cookie cannot simply live in the root: the Matrix response cache shares that
    root and creates it with no mode, so on any machine that has run a search it
    already sits at the umask default. Tightening it from here would change a
    directory this component was handed rather than created — and would take the
    response cache's permissions with it, on a box where the user may have
    widened them deliberately. Both halves are asserted, because the private
    leaf is only half the rule."""
    cache_dir = tmp_path / "fresh"
    _reset(monkeypatch, cache_dir)
    if premade:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.chmod(0o755)
    before = stat.S_IMODE(cache_dir.stat().st_mode) if premade else None
    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))
    assert stat.S_IMODE(_cookie_file(cache_dir).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(_cookie_file(cache_dir).stat().st_mode) == 0o600
    if before is not None:
        assert stat.S_IMODE(cache_dir.stat().st_mode) == before, (
            "the caller's directory was changed"
        )


@pytest.mark.parametrize(
    ("name", "domain"),
    [
        pytest.param("SID", ".google.com", id="a-google-cookie-not-on-the-allowlist"),
        pytest.param("NID", ".evil.example", id="the-right-name-for-another-domain"),
        pytest.param("__Secure-1PSID", ".google.com", id="an-auth-cookie-name"),
        # A substring test accepts both of these. The first is a host anyone can
        # register under a domain they own; the second only LOOKS like a suffix.
        pytest.param("NID", "google.com.evil.example", id="google-com-as-a-prefix"),
        pytest.param("NID", "notgoogle.com", id="a-suffix-that-is-not-a-label"),
    ],
)
def test_a_tampered_cache_cannot_inject_arbitrary_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, domain: str
) -> None:
    """The cache is a plain file in a shared directory. Whatever can edit it
    could otherwise name any cookie for any domain and have the seeder install
    it on a live session, so the read side re-checks the write side's
    allowlist."""
    _reset(monkeypatch, tmp_path)
    _write_cache(
        tmp_path,
        [
            {"name": name, "value": "injected", "domain": domain, "path": "/"},
            {"name": "NID", "value": "legitimate", "domain": ".google.com", "path": "/"},
        ],
        saved_at=time.time(),
    )
    fresh = _FakeClient([])
    gfid._seed_cookies_once(fresh)
    assert fresh._session().cookies.set_calls == [("NID", "legitimate", ".google.com", "/")]


def test_the_temp_file_is_owner_only_while_it_is_being_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Asserting the mode after the rename cannot tell `os.open(..., 0o600)`
    from a chmod applied afterwards — both end at 0600. The window is what
    matters: the NID is on disk from the first byte written, so the mode is
    checked mid-write, before the rename.

    Same interception point as the interrupt test: inside `json.dump`, with the
    file created and the payload written."""
    _reset(monkeypatch, tmp_path)
    real_dump = json.dump
    modes: list[int] = []

    def _inspect(obj: object, fh: Any, **kw: Any) -> None:
        real_dump(obj, fh, **kw)
        fh.flush()
        for temp in _cookie_file(tmp_path).parent.glob("*.tmp"):
            modes.append(stat.S_IMODE(temp.stat().st_mode))

    monkeypatch.setattr(gfid.json, "dump", _inspect)
    gfid._persist_cookies(_FakeClient([_JarCookie("NID", "532=abc", ".google.com")]))

    assert modes == [0o600], f"the temp was readable mid-write: {[oct(m) for m in modes]}"
    assert stat.S_IMODE(_cookie_file(tmp_path).stat().st_mode) == 0o600
