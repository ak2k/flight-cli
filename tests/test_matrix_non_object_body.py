"""A Matrix answer with HTTP 200 whose JSON body is not an object is a
`MatrixShapeError`, is not stored in the HTTP cache, and is asked for again."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest

from flight_cli import _http
from flight_cli._gf_common import cache_dir
from flight_cli._http import is_cacheable_body
from flight_cli.client import SEARCH_URL, MatrixClient, MatrixShapeError
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    import pathlib

_KEY = "AIzaSy" + "k" * 33
_NOT_OBJECTS: list[tuple[Any, str]] = [([], "list"), ("x", "str"), (0, "int"), (None, "NoneType")]


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


def _search() -> SpecificDateSearch:
    depart = dt.date.today() + dt.timedelta(days=30)
    return SpecificDateSearch(legs=(Leg.of("JFK", "LAX", depart),), options=SearchOptions())


def _run(tmp_path: pathlib.Path) -> str:
    async def go() -> str:
        async with MatrixClient(cache_dir=str(tmp_path)) as c:
            try:
                _ = await c.execute(_search())
            except Exception as e:
                return type(e).__name__
            return "parsed"

    return anyio.run(go)


@pytest.mark.parametrize("body", [b for b, _ in _NOT_OBJECTS], ids=[n for _, n in _NOT_OBJECTS])
def test_a_body_that_is_not_an_object_is_refused_and_asked_for_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: Any
) -> None:
    asked = _matrix(monkeypatch, tmp_path, body)
    outcomes = [_run(tmp_path), _run(tmp_path)]
    assert (asked[0], outcomes) == (2, ["MatrixShapeError", "MatrixShapeError"])


@pytest.mark.parametrize(("body", "name"), _NOT_OBJECTS, ids=[n for _, n in _NOT_OBJECTS])
def test_the_refusal_names_the_type_and_keeps_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: Any, name: str
) -> None:
    _ = _matrix(monkeypatch, tmp_path, body)

    async def go() -> None:
        async with MatrixClient(cache_dir=str(tmp_path)) as c:
            _ = await c.execute(_search(), cache=False)

    with pytest.raises(MatrixShapeError) as caught:
        anyio.run(go)

    assert caught.value.detail == f"Matrix's answer is not a JSON object (a {name})"
    assert caught.value.captured is not None
    assert json.loads(caught.value.captured.read_text()) == body


def test_a_follow_up_answer_that_is_not_an_object_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _ = _matrix(monkeypatch, tmp_path, [])

    async def go() -> None:
        async with MatrixClient(cache_read=False, cache_write=False) as c:
            _ = await c.booking_details(session="s", solution_set="t", solution_id="u")

    with pytest.raises(MatrixShapeError, match=r"not a JSON object \(a list\)"):
        anyio.run(go)


def test_only_an_object_without_an_error_may_be_stored() -> None:
    assert [is_cacheable_body(b) for b, _ in _NOT_OBJECTS] == [False] * 4
    assert is_cacheable_body({"solutionList": {}}) is True


def _stored_whatever_it_was(_body: object) -> bool:
    """The cache gate of a release that stored any body."""
    return True


def test_an_entry_stored_before_the_refusal_is_dropped_when_it_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    asked = _matrix(monkeypatch, tmp_path, [])

    async def store() -> None:
        t = _http.HttpTransport(cache_dir=tmp_path, rps=1000.0)
        _ = await t.post_json(
            SEARCH_URL, to_wire(_search()).as_json(), params={"key": _KEY, "alt": "json"}
        )
        await t.aclose()

    with monkeypatch.context() as stored_as_before:
        stored_as_before.setattr(_http, "is_cacheable_body", _stored_whatever_it_was)
        anyio.run(store)
    assert asked[0] == 1

    outcomes = [_run(tmp_path), _run(tmp_path)]
    assert (asked[0], outcomes) == (2, ["MatrixShapeError", "MatrixShapeError"])
