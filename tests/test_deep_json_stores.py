# pyright: reportPrivateUsage=false
"""A seats.aero key file or a Google cookie jar nested past the JSON decoder's
recursion limit reads as a file this CLI cannot parse, not as a crash."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from flight_cli import _gflight_ids as gfid
from flight_cli.providers.seats_aero import auth as seats_auth
from test_doctor import World, _by_id, _fails_as, _run, world

if TYPE_CHECKING:
    import pathlib

    import pytest

__all__ = ["world"]

_DEEP = b"[" * 100_000 + b"]" * 100_000


def test_a_deeply_nested_seats_key_file_reads_as_no_key(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "seats.json"
    monkeypatch.setattr(seats_auth, "KEY_PATH", key_path)
    monkeypatch.delenv(seats_auth.API_KEY_ENV, raising=False)
    assert key_path.is_relative_to(tmp_path)
    key_path.write_bytes(_DEEP)
    assert seats_auth.load_key() is None


def test_a_deeply_nested_seats_key_file_fails_the_doctor_check_as_config(world: World) -> None:
    assert seats_auth.KEY_PATH.is_relative_to(world.tmp)
    seats_auth.KEY_PATH.write_bytes(_DEEP)
    c = _fails_as(_run(), "seats-aero", "config")
    assert str(seats_auth.KEY_PATH) in c.detail
    assert world.seats_requests == []


def test_a_deeply_nested_cookie_jar_fails_the_doctor_check_as_config(world: World) -> None:
    jar = gfid._cookie_path()
    assert jar.is_relative_to(world.tmp)
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(_DEEP)
    report = _run()
    c = _fails_as(report, "google-cookies", "config")
    assert f"{jar} is not a cookie jar this CLI wrote" in c.detail
    assert _by_id(report)["config"].status == "pass"


def test_a_deeply_nested_cookie_jar_is_ignored_when_a_search_seeds_its_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())
    jar = gfid._cookie_path()
    assert jar.is_relative_to(tmp_path)
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(_DEEP)
    gfid._seed_cookies_once(object())
