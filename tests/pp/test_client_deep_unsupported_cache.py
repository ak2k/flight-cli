"""A PointsPath unsupported-airline cache nested past the JSON decoder's
recursion limit reads as an empty cache, not as a crash."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from flight_cli.pp import client

if TYPE_CHECKING:
    import pathlib

    import pytest

_DEEP = b"[" * 100_000 + b"]" * 100_000


def test_a_deeply_nested_unsupported_cache_reads_as_empty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr(client, "UNSUPPORTED_CACHE", cache)
    assert cache.is_relative_to(tmp_path)
    cache.write_bytes(_DEEP)
    assert client.load_unsupported_airlines() == frozenset()


def test_a_deeply_nested_unsupported_cache_is_replaced_by_the_next_rejection(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "unsupported.json"
    monkeypatch.setattr(client, "UNSUPPORTED_CACHE", cache)
    assert cache.is_relative_to(tmp_path)
    cache.write_bytes(_DEEP)
    client.remember_unsupported_airline("ANA")
    written: object = json.loads(cache.read_text())
    assert isinstance(written, dict)
    assert list(written) == ["ANA"]  # pyright: ignore[reportUnknownArgumentType] -- keys of a parsed JSON object
    assert client.load_unsupported_airlines() == frozenset({"ANA"})
