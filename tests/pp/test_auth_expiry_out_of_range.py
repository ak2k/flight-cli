# pyright: reportPrivateUsage=false
"""A PointsPath expiry no datetime can hold is refused, not shown.

`1e300` and `99999999999999` pass `int()` but not `datetime.fromtimestamp`, which
`whoami`, `login` and `doctor` call on the expiry. A store holding one reads as
logged out; a login or refresh answer holding one, or `Infinity`, is a PPAuthError
and writes no store.
"""

from __future__ import annotations

import base64
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.pp import auth as pp_auth

_OUT_OF_RANGE = [1e300, 99999999999999]
# The last second a datetime holds in UTC is year 10000 in a zone east of UTC; the first
# second it does not hold is year 9999 in a zone west of it. `whoami` prints local time
# and `doctor` prints UTC. POSIX TZ strings, since a libc that finds no zoneinfo for an
# Olson name silently runs in UTC, where both seconds are in range.
_EDGES = [
    pytest.param(253402300799, "<+05>-5", id="253402300799-UTC+5"),
    pytest.param(253402300800, "<-05>5", id="253402300800-UTC-5"),
]


def _jwt() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    claims = base64.urlsafe_b64encode(b'{"exp": 2000000000}').decode().rstrip("=")
    return f"{header}.{claims}.sig"


def _write_store(expires_at: float, tmp_path_factory: pytest.TempPathFactory) -> None:
    # Before the write, which replaces whatever file this names.
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    pp_auth.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pp_auth.TOKENS_PATH.write_text(
        json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": expires_at})
    )


@pytest.mark.parametrize("expires_at", _OUT_OF_RANGE)
def test_a_store_with_an_out_of_range_expires_at_loads_as_none(
    expires_at: float, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _write_store(expires_at, tmp_path_factory)
    assert pp_auth.load_tokens() is None


@pytest.mark.parametrize(("expires_at", "zone"), _EDGES)
def test_a_store_that_local_time_or_utc_cannot_hold_loads_as_none(
    expires_at: int, zone: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _write_store(expires_at, tmp_path_factory)
    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        assert pp_auth.load_tokens() is None
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


@pytest.mark.parametrize("expires_at", _OUT_OF_RANGE)
def test_whoami_on_an_out_of_range_expires_at_says_not_logged_in(
    expires_at: float, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _write_store(expires_at, tmp_path_factory)
    result = CliRunner().invoke(cli.app, ["auth", "pp", "whoami"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert result.exit_code == 1
    assert "Not logged in" in result.output


@pytest.mark.parametrize("expires_at", [float("inf"), *_OUT_OF_RANGE])
def test_a_login_payload_with_an_unusable_expires_at_is_a_pp_auth_error(
    expires_at: float,
) -> None:
    payload = {"access_token": _jwt(), "refresh_token": "r", "expires_at": expires_at}
    with pytest.raises(pp_auth.PPAuthError):
        pp_auth._tokens_from_supabase_payload(payload)


def test_login_with_an_out_of_range_tokens_file_fails_and_saves_nothing(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    tokens_file = tmp_path_factory.mktemp("tokens") / "session.json"
    tokens_file.write_text(
        json.dumps({"access_token": _jwt(), "refresh_token": "r", "expires_at": 1e300})
    )
    result = CliRunner().invoke(cli.app, ["auth", "pp", "login", "--tokens-file", str(tokens_file)])
    assert result.exit_code == 1, result.output
    assert "Login failed" in result.output
    assert not pp_auth.TOKENS_PATH.exists()


@pytest.mark.parametrize("expires_in", [float("inf"), *_OUT_OF_RANGE])
def test_a_refresh_answer_with_an_unusable_expires_in_is_a_pp_auth_error(
    expires_in: float, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())

    def answer(*_a: object, **_kw: object) -> httpx.Response:
        body = {"access_token": "new", "expires_in": expires_in}
        return httpx.Response(200, content=json.dumps(body).encode())

    monkeypatch.setattr(pp_auth, "httpx", SimpleNamespace(post=answer))
    with pytest.raises(pp_auth.PPAuthError):
        pp_auth.refresh(pp_auth.Tokens("old", "r", 1))
    assert not pp_auth.TOKENS_PATH.exists()
