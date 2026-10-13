"""A Matrix GET answer (`airports`, `airport`, `currencies`) with HTTP 200 whose
JSON body is not an object is a `MatrixShapeError` that keeps the body, as a
POST answer is, and is not stored in the HTTP cache."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest

from flight_cli import _http
from flight_cli._gf_common import cache_dir
from flight_cli.client import MatrixClient, MatrixShapeError

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Awaitable, Callable

_KEY = "AIzaSy" + "k" * 33
_NOT_OBJECTS: list[tuple[Any, str]] = [([], "list"), ("x", "str"), (0, "int"), (None, "NoneType")]
_HELPERS: dict[str, Callable[[MatrixClient], Awaitable[object]]] = {
    "airports": lambda c: c.airports("x"),
    "airport": lambda c: c.airport("JFK"),
    "currencies": lambda c: c.currencies(),
}


def _matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: Any) -> list[int]:
    """Matrix answers every request with `body` as JSON text; returns the count."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)
    assert cache_dir() == tmp_path
    asked = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        asked[0] += 1
        return httpx.Response(200, content=json.dumps(body))

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    return asked


def _outcome(tmp_path: pathlib.Path, helper: str) -> tuple[str, pathlib.Path | None]:
    """The exception a helper ends in, as `Name: text`, and the body it kept."""

    async def go() -> tuple[str, pathlib.Path | None]:
        async with MatrixClient(cache_dir=str(tmp_path)) as c:
            try:
                _ = await _HELPERS[helper](c)
            except MatrixShapeError as e:
                return f"MatrixShapeError: {e.detail}", e.captured
            except Exception as e:
                return f"{type(e).__name__}: {e}", None
            return "returned", None

    return anyio.run(go)


@pytest.mark.parametrize("helper", list(_HELPERS))
@pytest.mark.parametrize(("body", "name"), _NOT_OBJECTS, ids=[n for _, n in _NOT_OBJECTS])
def test_a_get_answer_that_is_not_an_object_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, helper: str, body: Any, name: str
) -> None:
    _ = _matrix(monkeypatch, tmp_path, body)

    outcome, _kept = _outcome(tmp_path, helper)

    assert outcome == f"MatrixShapeError: Matrix's answer is not a JSON object (a {name})"


@pytest.mark.parametrize("helper", list(_HELPERS))
@pytest.mark.parametrize(("body", "name"), _NOT_OBJECTS, ids=[n for _, n in _NOT_OBJECTS])
def test_a_refused_get_answer_keeps_its_body_and_is_asked_for_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, helper: str, body: Any, name: str
) -> None:
    asked = _matrix(monkeypatch, tmp_path, body)

    first, kept = _outcome(tmp_path, helper)
    second, _ = _outcome(tmp_path, helper)

    assert first == second == f"MatrixShapeError: Matrix's answer is not a JSON object (a {name})"
    assert kept is not None
    assert json.loads(kept.read_text()) == body
    assert asked[0] == 2
