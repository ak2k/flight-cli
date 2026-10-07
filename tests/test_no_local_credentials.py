"""The suite runs as if on a clean machine: no award-provider credential or
config this machine holds is visible to a test, and none can be written over.

A test that sees the real PointsPath tokens or Seats.aero key passes or fails by
which machine runs it, and one that saves a token rewrites the user's store.
"""

from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

from flight_cli import _config
from flight_cli.pp import auth as pp_auth
from flight_cli.providers.pointspath import provider
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    import pytest


def test_the_credential_stores_sit_in_a_directory_made_for_the_test(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp()
    for path in (
        pp_auth.TOKENS_PATH,
        pp_auth.CONFIG_DIR,
        pp_auth.BROWSER_PROFILE_DIR,
        seats_auth.KEY_PATH,
        seats_auth.CONFIG_DIR,
        _config.DEFAULT_CONFIG_PATH,
        pathlib.Path.home(),
    ):
        assert path.is_relative_to(base), path


def test_no_award_provider_is_configured_inside_the_suite() -> None:
    # Each check precedes `is_configured`, which refreshes a stale stored token
    # over the network and saves the result.
    names = ("PP_ACCESS_TOKEN", "PP_REFRESH_TOKEN", "SEATS_AERO_API_KEY", "FLIGHT_CLI_CONFIG_DIR")
    for name in names:
        assert name not in os.environ, name
    assert pp_auth.load_tokens() is None
    assert seats_auth.load_key() is None
    assert provider.is_configured() is False
    assert seats_auth.is_configured() is False
    assert _config.load() == {}


def test_a_token_save_inside_a_test_leaves_the_real_store_alone(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp()
    # Before the saves, which overwrite whatever file these name.
    assert pp_auth.TOKENS_PATH.is_relative_to(base), pp_auth.TOKENS_PATH
    assert seats_auth.KEY_PATH.is_relative_to(base), seats_auth.KEY_PATH
    saved = pp_auth.Tokens(
        access_token="a",  # noqa: S106 — dummy test value
        refresh_token="r",  # noqa: S106 — dummy test value
        expires_at=1,
    )
    pp_auth.save_tokens(saved)
    seats_auth.save_key("k")
    assert pp_auth.load_tokens() == saved
    assert seats_auth.load_key() == "k"


def test_a_test_can_still_set_its_own_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(seats_auth.API_KEY_ENV, "pro_x")
    assert seats_auth.load_key() == "pro_x"
