# pyright: reportPrivateUsage=false
"""`flight doctor` names the limit a Matrix timeout hit: the connect limit when
Matrix accepted no connection, the 180 s read limit when it accepted one and
never answered."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import stamina

from flight_cli._http import HttpTransport
from test_doctor import World, _fails_as, _matrix_behind_the_real_client, _run, world

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["world"]

_DOCTOR_DOC = Path(__file__).resolve().parent.parent / "docs" / "memories" / "doctor.md"

_CONNECT_LIMIT = (
    "Matrix accepted no connection within 10 s, the limit a search waits to connect on each attempt"
)
_READ_LIMIT = "Matrix did not answer within 180 s, the limit a search waits on each attempt"


def test_a_matrix_connect_timeout_names_the_connect_limit(world: World) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    world.matrix = refuse
    with stamina.set_testing(True, attempts=3):
        report = _run()
    c = _fails_as(report, "matrix-search", "brownout")
    assert len(world.matrix_requests) == 3
    assert "180" not in c.detail
    assert c.detail == _CONNECT_LIMIT
    assert report.exit_code == 75


def test_the_connect_limit_it_names_is_the_one_the_client_sends(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    del world
    connects: list[float] = []

    def refuse(request: httpx.Request, _read: float) -> httpx.Response:
        connects.append(request.extensions["timeout"]["connect"])
        raise httpx.ConnectTimeout("timed out", request=request)

    _matrix_behind_the_real_client(monkeypatch, refuse)
    with stamina.set_testing(True, attempts=3):
        c = _fails_as(_run(), "matrix-search", "brownout")
    assert connects == [10.0, 10.0, 10.0]
    assert f"within {connects[0]:.0f} s" in c.detail


def test_the_doctor_doc_names_both_limits_of_a_matrix_timeout() -> None:
    rows = [ln for ln in _DOCTOR_DOC.read_text().splitlines() if ln.startswith("| `brownout`")]
    assert len(rows) == 1
    assert "10 s" in rows[0]
    assert "180 s" in rows[0]


def test_the_curl_transport_raises_a_connect_timeout_when_no_connection_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()  # the kernel accepts the TCP connection; nothing answers the TLS hello
    url = f"https://127.0.0.1:{silent.getsockname()[1]}/"

    async def post() -> None:
        async with HttpTransport(connect_timeout=0.3, cache_dir=tmp_path) as http:
            await http.post_json(url, {}, cache=False)

    with silent, stamina.set_testing(True, attempts=1), pytest.raises(httpx.ConnectTimeout):
        anyio.run(post)


def _curl_timeout(message: str) -> Callable[[httpx.Request, float], httpx.Response]:
    """What httpx-curl-cffi raises for every curl timeout: the bare class, with
    curl's message the only record of which limit ran out."""

    def answer(request: httpx.Request, _read: float) -> httpx.Response:
        raise httpx.TimeoutException(
            f"Failed to perform, curl: (28) {message}. See "
            "https://curl.se/libcurl/c/libcurl-errors.html first for more details.",
            request=request,
        )

    return answer


@pytest.mark.parametrize(
    ("curl", "detail"),
    [
        ("Connection timed out after 10000 milliseconds", _CONNECT_LIMIT),
        ("Connection timeout after 10001 ms", _CONNECT_LIMIT),
        ("Resolving timed out after 10000 milliseconds", _CONNECT_LIMIT),
        (
            "Failed to resolve host 'content-alkalimatrix-pa.googleapis.com' "
            "with timeout after 10000 ms",
            _CONNECT_LIMIT,
        ),
        ("Operation too slow. Less than 1 bytes/sec transferred the last 190 seconds", _READ_LIMIT),
        ("Operation timed out after 190000 milliseconds with 0 bytes received", _READ_LIMIT),
    ],
)
def test_a_curl_timeout_names_the_limit_curl_reports(
    world: World, monkeypatch: pytest.MonkeyPatch, curl: str, detail: str
) -> None:
    del world
    _matrix_behind_the_real_client(monkeypatch, _curl_timeout(curl))
    with stamina.set_testing(True, attempts=3):
        report = _run()
    c = _fails_as(report, "matrix-search", "brownout")
    assert c.detail == detail
    assert report.exit_code == 75
