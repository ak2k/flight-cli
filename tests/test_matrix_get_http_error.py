"""The GET helpers on `MatrixClient` (`airports`, `airport`, `currencies`) leave
an HTTP error status as a `MatrixHttpError`, whose text is the status line and
never the request URL, which carries the API key. `airport` still answers a 404
with None: 'no such airport' is not a failure."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import stamina

import flight_cli
from flight_cli import cli
from flight_cli.client import MatrixApiError, MatrixClient, MatrixHttpError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_KEY = "AIzaSyTestKeyNotReal0123456789"


def _client(tmp_path: Path, status: int, seen: list[str]) -> MatrixClient:
    """A client whose transport answers `status` and records each URL asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, text="no")

    c = MatrixClient(api_key=_KEY, cache_dir=str(tmp_path), rebootstrap=False)
    c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
    return c


def _run[T](
    tmp_path: Path, status: int, seen: list[str], call: Callable[[MatrixClient], Awaitable[T]]
) -> T:
    async def go() -> T:
        async with _client(tmp_path, status, seen) as c:
            return await call(c)

    with stamina.set_testing(True, attempts=1):
        return anyio.run(go)


@pytest.mark.parametrize(
    ("status", "phrase"),
    [(403, "Forbidden"), (500, "Internal Server Error"), (503, "Service Unavailable")],
)
@pytest.mark.parametrize("helper", ["airports", "airport", "currencies"])
def test_a_get_helper_http_error_is_a_matrix_http_error_without_the_url(
    tmp_path: Path, helper: str, status: int, phrase: str
) -> None:
    calls: dict[str, Callable[[MatrixClient], Awaitable[object]]] = {
        "airports": lambda c: c.airports("x"),
        "airport": lambda c: c.airport("JFK"),
        "currencies": lambda c: c.currencies(),
    }
    seen: list[str] = []

    with pytest.raises(MatrixHttpError) as caught:
        _run(tmp_path, status, seen, calls[helper])

    e = caught.value
    assert isinstance(e, MatrixApiError)
    assert str(e) == f"Matrix answered HTTP {status:d} {phrase}", str(e)
    assert (e.kind, e.status) == ("http", status)
    assert e.__cause__ is None and e.__suppress_context__
    # The request did carry the key, so its absence below means something.
    assert len(seen) == 1 and f"key={_KEY}" in seen[0], seen
    assert "key=" not in repr(e) and _KEY not in repr(e), repr(e)


def test_airport_answers_a_404_with_none(tmp_path: Path) -> None:
    seen: list[str] = []

    assert _run(tmp_path, 404, seen, lambda c: c.airport("ZZZ")) is None
    assert len(seen) == 1, seen


def test_the_airport_command_fails_with_the_status_line_not_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`flight airport` has no handler of its own: what leaves the client is
    what the user's traceback ends with."""

    def build(**_kw: object) -> MatrixClient:
        return _client(tmp_path, 500, [])

    monkeypatch.setattr(cli, "MatrixClient", build)

    with stamina.set_testing(True, attempts=1), pytest.raises(MatrixHttpError) as caught:
        cli.airport(query="x", impersonate="chrome")

    assert str(caught.value) == "Matrix answered HTTP 500 Internal Server Error"
    assert _KEY not in repr(caught.value) and caught.value.__cause__ is None


def test_the_package_exports_matrix_http_error() -> None:
    assert "MatrixHttpError" in flight_cli.__all__
    assert flight_cli.MatrixHttpError is MatrixHttpError
