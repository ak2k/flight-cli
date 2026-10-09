"""The suite-wide network guard in `conftest.py` fails any test that resolves a
name or connects a socket to a non-loopback address, and leaves loopback, Unix
sockets and socket creation alone."""

from __future__ import annotations

import socket

import anyio
import pytest

_REACHED = "reached the network"


def test_resolving_a_name_fails_the_test() -> None:
    with pytest.raises(BaseException, match=_REACHED) as caught:
        socket.getaddrinfo("example.invalid", 80)
    assert caught.type is pytest.fail.Exception


def test_connect_to_a_remote_address_fails_the_test() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(BaseException, match=_REACHED) as caught:
            sock.connect(("192.0.2.1", 9))
    assert caught.type is pytest.fail.Exception


def test_connect_ex_to_a_remote_address_fails_the_test() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(BaseException, match=_REACHED) as caught:
            sock.connect_ex(("192.0.2.1", 9))
    assert caught.type is pytest.fail.Exception


def test_a_hostname_in_connect_fails_the_test() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(BaseException, match=_REACHED) as caught:
            sock.connect(("example.invalid", 80))
    assert caught.type is pytest.fail.Exception


def test_loopback_and_socketpairs_still_work() -> None:
    a, b = socket.socketpair()
    a.close()
    b.close()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port: int = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
            assert second.connect_ex(("127.0.0.1", port)) == 0
        assert socket.getaddrinfo("127.0.0.1", port)
        assert socket.getaddrinfo("localhost", port)
        assert socket.getaddrinfo(None, port)


def test_a_loopback_host_given_as_bytes_still_works() -> None:
    # anyio, and so httpx's async client, hands the resolver an encoded host.
    assert anyio.run(anyio.getaddrinfo, "localhost", 80)
    assert socket.getaddrinfo(b"127.0.0.1", 80)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect((bytearray(b"127.0.0.1"), server.getsockname()[1]))


def test_a_remote_host_given_as_bytes_fails_the_test() -> None:
    with pytest.raises(BaseException, match=_REACHED) as caught:
        socket.getaddrinfo(b"example.invalid", 80)
    assert caught.type is pytest.fail.Exception


def test_a_unix_socket_connect_is_not_a_network_connect() -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock, pytest.raises(OSError):
        sock.connect("no-such-socket-here")
