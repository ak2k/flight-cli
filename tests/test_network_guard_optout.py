"""`@pytest.mark.network` opts a test out of the suite-wide network guard."""

from __future__ import annotations

import socket

import pytest

# Captured at import, before any fixture patches the module.
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


@pytest.mark.network
def test_the_network_marker_leaves_the_socket_calls_alone() -> None:
    assert socket.getaddrinfo is _REAL_GETADDRINFO
    assert socket.socket.connect is _REAL_CONNECT
    assert socket.socket.connect_ex is _REAL_CONNECT_EX


def test_an_unmarked_test_has_the_guard_in_place() -> None:
    assert socket.getaddrinfo is not _REAL_GETADDRINFO
    assert socket.socket.connect is not _REAL_CONNECT
    assert socket.socket.connect_ex is not _REAL_CONNECT_EX
