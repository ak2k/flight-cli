"""Concurrent first calls to the fli code tables build one table.

`fli_airports` and `fli_airlines` give every aliased code a member of its own.
Two threads that both find the table unbuilt each built one, so the same code
carried two members and one flight had two identities. The build is held open
here until a second thread would join it, which makes the overlap certain."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import fli_bridge

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_JOIN_WAIT = 0.5  # how long the first build waits for a second thread to join it


@pytest.fixture(autouse=True)
def fresh_tables() -> Iterator[None]:
    fli_bridge.fli_airports.cache_clear()
    fli_bridge.fli_airlines.cache_clear()
    yield
    fli_bridge.fli_airports.cache_clear()
    fli_bridge.fli_airlines.cache_clear()


def _concurrent_first_calls(
    monkeypatch: pytest.MonkeyPatch, table: Callable[[], dict[str, Any]]
) -> list[dict[str, Any]]:
    own_member = fli_bridge._own_member  # pyright: ignore[reportPrivateUsage]
    first_in = threading.Event()
    second_in = threading.Event()
    first = threading.Lock()  # held by whichever thread builds first

    def slow_own_member(code: str, canonical: Any) -> Any:
        if first.acquire(blocking=False):
            first_in.set()
            second_in.wait(_JOIN_WAIT)
        else:
            second_in.set()
        return own_member(code, canonical)

    monkeypatch.setattr(fli_bridge, "_own_member", slow_own_member)
    tables: list[dict[str, Any]] = []

    def call() -> None:
        tables.append(table())

    one = threading.Thread(target=call)
    one.start()
    assert first_in.wait(5)
    two = threading.Thread(target=call)
    two.start()
    one.join(10)
    two.join(10)
    assert len(tables) == 2
    return tables


def test_concurrent_first_airport_calls_share_one_table(monkeypatch: pytest.MonkeyPatch) -> None:
    one, two = _concurrent_first_calls(monkeypatch, fli_bridge.fli_airports)
    assert one is two
    assert one["OKA"] is two["OKA"]
    assert fli_bridge.fli_airport("OKA") is one["OKA"]


def test_concurrent_first_airline_calls_share_one_table(monkeypatch: pytest.MonkeyPatch) -> None:
    one, two = _concurrent_first_calls(monkeypatch, fli_bridge.fli_airlines)
    assert one is two
    assert one["W9"] is two["W9"]
    assert fli_bridge.fli_airline("W9") is one["W9"]
