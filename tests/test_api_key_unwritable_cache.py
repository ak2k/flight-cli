# pyright: reportPrivateUsage=false
"""A key cache that cannot be written still bounds the SPA scrape to one per
process: the low-row check's client resolves the key a second time, after the
search's client, and that second resolve must not scrape again."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from flight_cli import _api_key
from flight_cli.client import MatrixClient

if TYPE_CHECKING:
    import pathlib

_KEY_A = "AIzaSy" + "A" * 33
_KEY_B = "AIzaSy" + "B" * 33


@pytest.fixture
def unwritable(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> list[str]:
    """A cache path whose parent is a regular file, so no write can succeed, and
    a bootstrap that records each scrape and answers a fresh key every time."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(_api_key, "_CACHE_PATH", blocker / "flight-cli" / ".matrix-key")
    assert tmp_path in _api_key._CACHE_PATH.parents
    monkeypatch.delenv("FLIGHT_API_KEY", raising=False)
    scrapes: list[str] = []

    def bootstrap() -> str:
        scrapes.append("scrape")
        return _KEY_A if len(scrapes) == 1 else _KEY_B

    monkeypatch.setattr(_api_key, "_bootstrap_from_spa", bootstrap)
    return scrapes


def test_two_matrix_clients_scrape_once_when_the_cache_cannot_be_written(
    unwritable: list[str],
) -> None:
    first = MatrixClient()
    second = MatrixClient()
    assert unwritable == ["scrape"]
    assert first._api_key == _KEY_A
    assert second._api_key == _KEY_A
    assert not _api_key._CACHE_PATH.exists()


def test_a_forced_bootstrap_scrapes_again_and_replaces_the_held_key(
    unwritable: list[str],
) -> None:
    assert _api_key.resolve_api_key() == _KEY_A
    assert _api_key.resolve_api_key(force_bootstrap=True) == _KEY_B
    assert _api_key.resolve_api_key() == _KEY_B
    assert unwritable == ["scrape", "scrape"]


def test_invalidating_the_cache_drops_the_held_key(unwritable: list[str]) -> None:
    assert _api_key.resolve_api_key() == _KEY_A
    _api_key.invalidate_cache()
    assert _api_key.resolve_api_key() == _KEY_B
    assert unwritable == ["scrape", "scrape"]


def test_a_key_saved_to_the_cache_is_not_also_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(_api_key, "_CACHE_PATH", tmp_path / "flight-cli" / ".matrix-key")
    assert tmp_path in _api_key._CACHE_PATH.parents
    monkeypatch.delenv("FLIGHT_API_KEY", raising=False)
    scrapes: list[str] = []

    def bootstrap() -> str:
        scrapes.append("scrape")
        return _KEY_A if len(scrapes) == 1 else _KEY_B

    monkeypatch.setattr(_api_key, "_bootstrap_from_spa", bootstrap)
    assert _api_key.resolve_api_key() == _KEY_A
    assert _api_key.resolve_api_key() == _KEY_A
    _api_key._CACHE_PATH.unlink()
    assert _api_key.resolve_api_key() == _KEY_B
    assert scrapes == ["scrape", "scrape"]
