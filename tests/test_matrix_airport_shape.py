"""`flight airport` reports an airport answer the models refuse as a shape change:
the typed line with the report URL and the kept body, exit 1, never the pydantic
traceback."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import _http, cli
from flight_cli.client import REPORT_URL, MatrixClient, MatrixShapeError

if TYPE_CHECKING:
    import pathlib

    from flight_cli.models import Location

_KEY = "AIzaSy" + "k" * 33
_JFK = {"locations": [{"code": "JFK", "displayName": "John F. Kennedy"}]}


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: object) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)


async def _airports() -> list[Location]:
    async with MatrixClient(cache_read=False, cache_write=False) as c:
        return await c.airports("JFK")


async def _airport() -> Location | None:
    async with MatrixClient(cache_read=False, cache_write=False) as c:
        return await c.airport("JFK")


def test_a_drifted_airport_answer_is_a_typed_shape_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {"locations": [42]})

    result = CliRunner().invoke(cli.app, ["airport", "JFK"])

    assert result.exit_code == 1
    assert not isinstance(result.exception, ValueError), repr(result.exception)
    shown = " ".join(result.stderr.split())
    assert "Matrix airport lookup failed" in shown
    assert REPORT_URL in shown
    assert len(list((tmp_path / "shape-changes").glob("matrix-*.json"))) == 1


@pytest.mark.parametrize("body", [{"locations": [42]}, {"locations": 5}, {"locations": None}])
def test_airports_refuses_a_drifted_list_as_a_shape_error_with_the_body_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: dict[str, object]
) -> None:
    _matrix_answers(monkeypatch, tmp_path, body)

    with pytest.raises(MatrixShapeError) as caught:
        anyio.run(_airports)

    err = caught.value
    assert err.detail.startswith("Matrix's answer does not parse at locations")
    assert err.captured is not None
    assert err.captured.parent == tmp_path / "shape-changes"
    assert json.loads(err.captured.read_text()) == body
    assert REPORT_URL in str(err)


def test_airport_refuses_a_drifted_location_as_a_shape_error_with_the_body_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {"displayName": "no code"})

    with pytest.raises(MatrixShapeError) as caught:
        anyio.run(_airport)

    err = caught.value
    assert err.detail.startswith("Matrix's answer does not parse at code: ")
    assert err.captured is not None
    assert json.loads(err.captured.read_text()) == {"displayName": "no code"}


def test_an_airport_answer_that_parses_keeps_no_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _JFK)

    found = anyio.run(_airports)
    result = CliRunner().invoke(cli.app, ["airport", "JFK"])

    assert [loc.code for loc in found] == ["JFK"]
    assert result.exit_code == 0, result.output
    assert "John F. Kennedy" in result.stdout
    assert not (tmp_path / "shape-changes").exists()


def test_an_answer_with_no_locations_is_no_matches_not_a_shape_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {})

    result = CliRunner().invoke(cli.app, ["airport", "JFK"])

    assert result.exit_code == 0, result.output
    assert "No matches." in result.stdout
    assert not (tmp_path / "shape-changes").exists()


def test_a_transport_failure_is_the_typed_airport_line_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(**_kw: object) -> NoReturn:
        raise OSError("no route to host")

    monkeypatch.setattr(cli, "MatrixClient", unreachable)

    result = CliRunner().invoke(cli.app, ["airport", "JFK"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Matrix airport lookup failed: no route to host" in " ".join(result.stderr.split())
