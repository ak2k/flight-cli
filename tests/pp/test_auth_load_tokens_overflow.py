"""A PointsPath token store whose `expires_at` overflows `int` reads as no store.

`json.loads` reads `1e400` and `Infinity` as `float("inf")`, and `int(inf)` raises
OverflowError, which is neither a ValueError nor a TypeError. `load_tokens` must
return None for it, like every other store it cannot read, so `whoami` says not
logged in instead of raising.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.pp import auth as pp_auth
from flight_cli.providers.pointspath import provider

# The raw JSON text in the `expires_at` slot; Python's decoder takes all of them.
_OVERFLOWING = ["1e400", "-1e400", "Infinity", "-Infinity"]


def _write_store(expires_at: str) -> None:
    # Before the write, which replaces whatever file this names.
    assert pp_auth.TOKENS_PATH.is_relative_to(pp_auth.CONFIG_DIR), pp_auth.TOKENS_PATH
    pp_auth.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pp_auth.TOKENS_PATH.write_text(
        '{"access_token": "a", "refresh_token": "r", "expires_at": ' + expires_at + "}"
    )


@pytest.mark.parametrize("expires_at", _OVERFLOWING)
def test_an_overflowing_expires_at_loads_as_none(
    expires_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_store(expires_at)
    assert pp_auth.load_tokens() is None
    assert provider.is_configured() is False


@pytest.mark.parametrize("expires_at", _OVERFLOWING)
def test_whoami_on_an_overflowing_expires_at_says_not_logged_in(
    expires_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert pp_auth.TOKENS_PATH.is_relative_to(tmp_path_factory.getbasetemp())
    _write_store(expires_at)
    result = CliRunner().invoke(cli.app, ["auth", "pp", "whoami"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert result.exit_code == 1
    assert "Not logged in" in result.output
