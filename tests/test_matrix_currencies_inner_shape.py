"""`MatrixClient.currencies` on a 200 answer that is a JSON object whose
`currencies` is not a list of string-to-string objects is a `MatrixShapeError`
that keeps the body and is asked for again on the next run, as `airports` and
`airport` answer a body their model refuses."""

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

_KEY = "AIzaSy" + "k" * 33
_WRONG_INNER_SHAPES: list[Any] = [{"currencies": "x"}, {"currencies": 5}, {"currencies": ["x"]}]


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


def _outcome(tmp_path: pathlib.Path) -> tuple[str, pathlib.Path | None]:
    """What `currencies` ends in, as the exception's name or `returned <value>`,
    and the body it kept."""

    async def go() -> tuple[str, pathlib.Path | None]:
        async with MatrixClient(cache_dir=str(tmp_path)) as c:
            try:
                got = await c.currencies()
            except MatrixShapeError as e:
                return "MatrixShapeError", e.captured
            except Exception as e:
                return type(e).__name__, None
            return f"returned {got!r}", None

    return anyio.run(go)


def test_a_currencies_answer_of_the_wrong_inner_shape_is_refused_and_asked_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[tuple[str, str, int, Any]] = []
    for body in _WRONG_INNER_SHAPES:
        asked = _matrix(monkeypatch, tmp_path, body)
        first, kept = _outcome(tmp_path)
        second, _ = _outcome(tmp_path)
        kept_body = json.loads(kept.read_text()) if kept else None
        seen.append((first, second, asked[0], kept_body))

    assert seen == [(("MatrixShapeError",) * 2) + (2, body) for body in _WRONG_INNER_SHAPES]


@pytest.mark.parametrize(
    ("body", "returned"),
    [
        (
            {"currencies": [{"code": "USD", "name": "US dollar"}]},
            [{"code": "USD", "name": "US dollar"}],
        ),
        ({}, []),
    ],
    ids=["rows", "no-key"],
)
def test_a_currencies_answer_of_the_right_shape_is_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: Any, returned: list[Any]
) -> None:
    _ = _matrix(monkeypatch, tmp_path, body)

    assert _outcome(tmp_path) == (f"returned {returned!r}", None)
