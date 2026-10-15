"""A GET helper on `MatrixClient` (`airports`, `airport`, `currencies`) that
Matrix answers with HTTP 200 and an error object raises `MatrixApiError`, as a
POST answered that way does. Read as data, the error object holds no
locations, so `flight airport` printed "No matches." and exited 0."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import stamina
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.client import MatrixApiError, MatrixClient, MatrixShapeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_KEY = "AIzaSyTestKeyNotReal0123456789"
_ENVELOPE = {"error": {"code": 500, "message": "Internal error encountered.", "status": "INTERNAL"}}


def _client(tmp_path: Path) -> MatrixClient:
    """A client whose transport answers every request with 200 and `_ENVELOPE`."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ENVELOPE)

    c = MatrixClient(api_key=_KEY, cache_dir=str(tmp_path), rebootstrap=False)
    c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
    return c


def _outcome(tmp_path: Path, call: Callable[[MatrixClient], Awaitable[object]]) -> str:
    """What the helper did: the Matrix error it raised, or what it returned."""

    async def go() -> object:
        async with _client(tmp_path) as c:
            return await call(c)

    try:
        with stamina.set_testing(True, attempts=1):
            answer = anyio.run(go)
    except MatrixApiError as e:
        return f"{type(e).__name__} ({e.kind}): {e}"
    except MatrixShapeError as e:
        return f"{type(e).__name__}: {e}"
    return f"answered {answer!r}"


@pytest.mark.parametrize("helper", ["airports", "airport", "currencies"])
def test_a_get_helper_answered_with_an_error_object_raises_a_matrix_api_error(
    tmp_path: Path, helper: str
) -> None:
    calls: dict[str, Callable[[MatrixClient], Awaitable[object]]] = {
        "airports": lambda c: c.airports("JFK"),
        "airport": lambda c: c.airport("JFK"),
        "currencies": lambda c: c.currencies(),
    }

    assert _outcome(tmp_path, calls[helper]) == (
        "MatrixApiError (INTERNAL): Internal error encountered."
    )


def test_the_airport_command_fails_on_an_error_object_instead_of_printing_no_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def build(**_kw: object) -> MatrixClient:
        return _client(tmp_path)

    monkeypatch.setattr(cli, "MatrixClient", build)

    with stamina.set_testing(True, attempts=1):
        result = CliRunner().invoke(cli.app, ["airport", "JFK"], env={"COLUMNS": "200"})

    shown = " ".join(result.output.split())
    assert (result.exit_code, "No matches." in shown) == (1, False), shown
    assert "Internal error encountered." in shown, shown
