# pyright: reportPrivateUsage=false
"""A refused airport answer is not replayed from the GET cache: the next run asks
Matrix again and keeps no second copy of the body."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
import httpx
from typer.testing import CliRunner

from flight_cli import _http, cli
from flight_cli.client import MatrixClient, MatrixShapeError

if TYPE_CHECKING:
    import pathlib

    import pytest

    from flight_cli.models import Location

_KEY = "AIzaSy" + "k" * 33
_JFK = {"locations": [{"code": "JFK", "displayName": "John F. Kennedy"}]}


def test_a_refused_airport_answer_is_asked_again_not_replayed_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)
    answers: list[object] = [{"locations": [42]}, _JFK]
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=answers[min(len(sent), len(answers)) - 1])

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(answer)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)

    first = CliRunner().invoke(cli.app, ["airport", "JFK"])
    second = CliRunner().invoke(cli.app, ["airport", "JFK"])

    assert first.exit_code == 1, first.output
    assert len(sent) == 2, f"Matrix was asked {len(sent)} time(s)"
    assert second.exit_code == 0, second.output
    assert "John F. Kennedy" in second.stdout
    assert len(list((tmp_path / "shape-changes").glob("matrix-*.json"))) == 1


def _matrix_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, answers: list[object]
) -> list[httpx.Request]:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=answers[min(len(sent), len(answers)) - 1])

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(answer)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    return sent


def test_a_refused_location_answer_is_asked_again_not_replayed_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    refused, found = {"displayName": "no code"}, _JFK["locations"][0]
    sent = _matrix_answers(monkeypatch, tmp_path, [refused, found])
    outcomes: list[str] = []

    async def lookup() -> Location | None:
        async with MatrixClient() as c:
            return await c.airport("JFK")

    for _ in range(2):
        try:
            found = anyio.run(lookup)
            outcomes.append(found.code if found else "none")
        except MatrixShapeError:
            outcomes.append("refused")

    assert outcomes == ["refused", "JFK"]
    assert len(sent) == 2, f"Matrix was asked {len(sent)} time(s)"


def test_an_airport_answer_that_parses_is_still_replayed_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    sent = _matrix_answers(monkeypatch, tmp_path, [_JFK])

    runs = [CliRunner().invoke(cli.app, ["airport", "JFK"]) for _ in range(2)]

    assert [r.exit_code for r in runs] == [0, 0], [r.output for r in runs]
    assert len(sent) == 1, f"Matrix was asked {len(sent)} time(s)"


def test_a_forgotten_get_is_fetched_again_and_a_neighbour_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    sent = _matrix_answers(monkeypatch, tmp_path, [{"n": 1}])
    url = "https://x.test/v1/things"

    async def go() -> None:
        t = _http.HttpTransport(cache_dir=tmp_path / "c", rps=1000.0)
        _ = await t.get_json(url, params={"key": "a"})
        _ = await t.get_json(url, params={"key": "b"})
        await t.forget_get(url, params={"key": "a"})
        _ = await t.get_json(url, params={"key": "a"})
        _ = await t.get_json(url, params={"key": "b"})
        await t.aclose()

    anyio.run(go)

    assert len(sent) == 3  # key=a twice, key=b once


def test_a_cache_that_cannot_delete_never_fails_a_forgotten_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _ = _matrix_answers(monkeypatch, tmp_path, [{}])

    def refuse(_key: str) -> bool:
        raise OSError

    async def go() -> None:
        t = _http.HttpTransport(cache_dir=tmp_path / "c", rps=1000.0)
        t._cache = SimpleNamespace(delete=refuse, close=lambda: None)
        await t.forget_get("https://x.test/v1/things", params={"key": "a"})
        await t.aclose()

    anyio.run(go)
