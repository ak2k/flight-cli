"""A Matrix answer the response models refuse is not replayed from the HTTP
cache: the next run asks Matrix again."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from flight_cli import _http
from flight_cli._gf_common import cache_dir
from flight_cli.client import MatrixClient, MatrixShapeError
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch

if TYPE_CHECKING:
    import pathlib

    import pytest

_KEY = "AIzaSy" + "k" * 33
_REFUSED: dict[str, Any] = {"solutionList": {"solutions": {}}}
_PARSES: dict[str, Any] = {"solutionList": {"solutions": []}}


def _matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, answers: list[dict[str, Any]]
) -> list[int]:
    """Matrix answers in order, the last for every later request; returns the count."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)
    assert cache_dir() == tmp_path
    asked = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        body = answers[min(asked[0], len(answers) - 1)]
        asked[0] += 1
        return httpx.Response(200, json=body)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    return asked


def _search() -> SpecificDateSearch:
    depart = dt.date.today() + dt.timedelta(days=30)
    return SpecificDateSearch(legs=(Leg.of("JFK", "LAX", depart),), options=SearchOptions())


def _run(tmp_path: pathlib.Path, *, cache: bool = True) -> str:
    async def go() -> str:
        async with MatrixClient(cache_dir=str(tmp_path)) as c:
            try:
                _ = await c.execute(_search(), cache=cache)
            except MatrixShapeError:
                return "refused"
            return "parsed"

    return anyio.run(go)


def test_a_refused_answer_is_asked_for_again_on_the_next_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    asked = _matrix(monkeypatch, tmp_path, [_REFUSED, _PARSES])
    outcomes = [_run(tmp_path), _run(tmp_path)]
    assert (asked[0], outcomes) == (2, ["refused", "parsed"])


def test_a_refused_answer_asked_without_the_cache_leaves_the_stored_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    asked = _matrix(monkeypatch, tmp_path, [_PARSES, _REFUSED])
    outcomes = [_run(tmp_path), _run(tmp_path, cache=False), _run(tmp_path)]
    assert (asked[0], outcomes) == (2, ["parsed", "refused", "parsed"])
