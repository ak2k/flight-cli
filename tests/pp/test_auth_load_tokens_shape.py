"""A PointsPath token store of the wrong shape reads as no store, not a crash.

`load_tokens` returns None for a missing or unparseable file; a file that parses
to the wrong JSON type, or holds a field of the wrong type, must do the same
instead of raising TypeError or ValueError into every search and command.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.pp import auth as pp_auth
from flight_cli.providers.pointspath import provider

_GOOD = {"access_token": "a", "refresh_token": "r", "expires_at": 1, "user_email": None}

_BAD_STORES: dict[str, bytes] = {
    "list": b"[]",
    "null": b"null",
    "string": b'"tokens"',
    "number": b"7",
    "expires_at_list": json.dumps({**_GOOD, "expires_at": [1]}).encode(),
    "expires_at_text": json.dumps({**_GOOD, "expires_at": "soon"}).encode(),
    "not_utf8": b"\xff\xfe{",
}


def _write_store(body: bytes) -> None:
    # Before the write, which replaces whatever file this names.
    assert pp_auth.TOKENS_PATH.is_relative_to(pp_auth.CONFIG_DIR), pp_auth.TOKENS_PATH
    pp_auth.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pp_auth.TOKENS_PATH.write_bytes(body)


@pytest.mark.parametrize("body", _BAD_STORES.values(), ids=_BAD_STORES.keys())
def test_a_token_store_of_the_wrong_shape_loads_as_none(
    body: bytes, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_store(body)
    assert pp_auth.load_tokens() is None
    assert provider.is_configured() is False


def test_whoami_on_a_token_store_of_the_wrong_shape_says_not_logged_in(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_store(b"[]")
    result = CliRunner().invoke(cli.app, ["auth", "pp", "whoami"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert result.exit_code == 1
    assert "Not logged in" in result.output


def test_a_complete_token_store_still_loads(tmp_path_factory: pytest.TempPathFactory) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_store(json.dumps(_GOOD).encode())
    assert pp_auth.load_tokens() == pp_auth.Tokens("a", "r", 1, None)
