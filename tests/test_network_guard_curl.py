"""The suite-wide network guard in `conftest.py` also covers libcurl, the path
both production HTTP clients take: a request to a remote host fails the test at
the URL it is given, and a loopback request still goes through."""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
from typing import TYPE_CHECKING, Any, cast, override

import anyio
import httpx
import pytest
from curl_cffi import Curl, CurlOpt, requests
from httpx_curl_cffi import AsyncCurlTransport

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_REACHED = "reached the network"
_REMOTE = "http://192.0.2.1:9/"

# Captured at import, before any fixture patches the class.
_REAL_SETOPT = Curl.setopt


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextlib.contextmanager
def _loopback_server() -> Generator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextlib.contextmanager
def _dead_proxy() -> Generator[str]:
    """A loopback port that is bound and never listens: a request the guard
    misses goes here and fails, instead of leaving this machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


def _curl_session(url: str, proxy: str | None) -> int:
    session = cast("Any", requests.Session(trust_env=False))
    with session:
        return cast("int", session.get(url, proxy=proxy, timeout=1).status_code)


def _httpx_curl_transport(url: str, proxy: str | None) -> int:
    options: dict[CurlOpt, Any] = {CurlOpt.PROXY: proxy.encode()} if proxy else {}

    async def get() -> int:
        transport = AsyncCurlTransport(curl_options=options)
        async with httpx.AsyncClient(transport=transport, timeout=1) as client:
            return (await client.get(url)).status_code

    return anyio.run(get)


_SENDERS = [
    pytest.param(_curl_session, id="curl-session"),
    pytest.param(_httpx_curl_transport, id="httpx-curl-transport"),
]


@pytest.mark.parametrize("send", _SENDERS)
def test_a_curl_request_to_a_remote_host_fails_the_test(
    send: Callable[[str, str | None], int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # libcurl reads these itself; a dead one would hide a request the guard missed.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    with _loopback_server() as url:
        assert send(url, None) == 200
    with _dead_proxy() as proxy, pytest.raises(BaseException, match=_REACHED) as caught:
        send(_REMOTE, proxy)
    assert caught.type is pytest.fail.Exception


@pytest.mark.parametrize(
    ("option", "value"),
    [
        pytest.param(CurlOpt.URL, b"http://192.0.2.1:9/", id="url-bytes"),
        pytest.param(CurlOpt.URL, "http://example.invalid/", id="url-str"),
        pytest.param(CurlOpt.URL, b"192.0.2.1:9/path", id="url-without-scheme"),
        pytest.param(CurlOpt.PROXY, b"http://192.0.2.1:3128", id="proxy"),
        pytest.param(CurlOpt.PROXY, b"192.0.2.1:3128", id="proxy-without-scheme"),
        pytest.param(CurlOpt.URL, b"http:///path", id="url-without-host"),
    ],
)
def test_setopt_of_a_remote_url_or_proxy_fails_the_test(
    option: CurlOpt, value: bytes | str
) -> None:
    handle = Curl()
    try:
        with pytest.raises(BaseException, match=_REACHED) as caught:
            handle.setopt(option, value)
    finally:
        handle.close()
    assert caught.type is pytest.fail.Exception


@pytest.mark.parametrize(
    ("option", "value"),
    [
        pytest.param(CurlOpt.URL, b"http://127.0.0.1:9/", id="url-loopback"),
        pytest.param(CurlOpt.URL, b"http://localhost:9/", id="url-localhost"),
        pytest.param(CurlOpt.URL, b"[::1]:9/", id="url-ipv6-without-scheme"),
        pytest.param(CurlOpt.PROXY, b"", id="empty-proxy"),
        pytest.param(CurlOpt.PROXY, b"http://127.0.0.1:3128", id="proxy-loopback"),
    ],
)
def test_setopt_of_a_loopback_or_empty_value_goes_through(option: CurlOpt, value: bytes) -> None:
    handle = Curl()
    try:
        assert handle.setopt(option, value) == 0
    finally:
        handle.close()


@pytest.mark.network
def test_the_network_marker_leaves_curl_alone() -> None:
    assert Curl.setopt is _REAL_SETOPT
