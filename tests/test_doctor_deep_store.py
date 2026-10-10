# pyright: reportPrivateUsage=false
"""`flight doctor` reads a stored credential file nested past the JSON decoder's
recursion limit as it reads any other file it cannot parse."""

from __future__ import annotations

from flight_cli import _doctor
from flight_cli.pp import auth as pp_auth
from flight_cli.providers.seats_aero import auth as seats_auth
from test_doctor import World, _by_id, _fails_as, _run, world

__all__ = ["world"]

_DEEP = b"[" * 100_000 + b"]" * 100_000


def test_a_deeply_nested_pointspath_store_fails_as_config(world: World) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(world.tmp)
    pp_auth.TOKENS_PATH.write_bytes(_DEEP)
    c = _fails_as(_run(), "pointspath", "config")
    assert str(pp_auth.TOKENS_PATH) in c.detail
    assert world.pp_calls == []


def test_a_deeply_nested_seats_key_file_still_gets_every_check_reported(world: World) -> None:
    assert seats_auth.KEY_PATH.is_relative_to(world.tmp)
    seats_auth.KEY_PATH.write_bytes(_DEEP)
    report = _run()
    assert [c.id for c in report.checks] == list(_doctor.CHECK_IDS)
    assert _by_id(report)["seats-aero"].status == "fail"
