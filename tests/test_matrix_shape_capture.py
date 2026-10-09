"""The client keeps the body of a Matrix answer its models refuse, and says so in
a `MatrixShapeError`; the doctor reads that error as a shape failure."""

from __future__ import annotations

import datetime as dt
import json
import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from flight_cli import _http, cli, client
from flight_cli.client import REPORT_URL, MatrixClient, MatrixShapeError
from flight_cli.domain import CalendarSearch, Leg, SearchOptions, SpecificDateSearch

if TYPE_CHECKING:
    import pathlib

_KEY = "AIzaSy" + "k" * 33
_DRIFTED = {"solutionCount": 1, "session": "s-1", "solutionList": [{"id": "x"}]}


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: object) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)


def _execute(search: SpecificDateSearch | CalendarSearch) -> object:
    async def go() -> object:
        async with MatrixClient(cache_read=False, cache_write=False) as c:
            return await c.execute(search, cache=False)

    return anyio.run(go)


def _specific() -> SpecificDateSearch:
    depart = dt.date.today() + dt.timedelta(days=30)
    return SpecificDateSearch(legs=(Leg.of("JFK", "LAX", depart),), options=SearchOptions())


def test_the_client_raises_a_shape_error_holding_the_pydantic_cause_and_the_kept_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)

    with pytest.raises(MatrixShapeError) as caught:
        _execute(_specific())

    err = caught.value
    assert err.__cause__ is not None
    assert err.detail.startswith("Matrix's answer does not parse at solutionList: ")
    assert err.captured is not None
    assert err.captured.parent == tmp_path / "shape-changes"
    assert json.loads(err.captured.read_text()) == _DRIFTED
    assert stat.S_IMODE(err.captured.stat().st_mode) == 0o600
    assert REPORT_URL in str(err)
    assert str(err.captured) in str(err)


def test_a_cache_dir_that_cannot_be_written_still_gives_the_shape_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "shape-changes").write_text("")
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)

    with pytest.raises(MatrixShapeError) as caught:
        _execute(_specific())

    assert caught.value.captured is None
    assert "its body could not be saved" in str(caught.value)


def test_a_body_that_parses_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {"solutionCount": 0, "solutionList": {"solutions": []}})

    _execute(_specific())

    assert not (tmp_path / "shape-changes").exists()


def test_vv_logs_every_field_error(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    argv = ["search", "JFK", "LAX", "--dep", depart, "--backend", "matrix", "--cash-only"]

    result = CliRunner().invoke(cli.app, ["-vv", *argv])

    assert "matrix_shape_change" in result.stderr
    assert "'loc': ('solutionList',)" in result.stderr


def test_a_second_capture_in_the_same_microsecond_never_overwrites_the_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)
    stamp = dt.datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=dt.UTC)

    def frozen_now(_tz: object) -> dt.datetime:
        return stamp

    monkeypatch.setattr(client, "datetime", SimpleNamespace(now=frozen_now))
    with pytest.raises(MatrixShapeError) as first:
        _execute(_specific())
    with pytest.raises(MatrixShapeError) as second:
        _execute(_specific())

    assert first.value.captured is not None
    assert second.value.captured is None
    assert "its body could not be saved" in str(second.value)
    assert json.loads(first.value.captured.read_text()) == _DRIFTED


def test_a_refusal_with_no_field_path_names_the_top_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)

    def refuse(_search: object, _data: object) -> object:
        return TypeAdapter(int).validate_python("not a number")

    monkeypatch.setattr(client, "_parse_by_variant", refuse)
    with pytest.raises(MatrixShapeError) as caught:
        _execute(_specific())

    assert caught.value.detail.startswith("Matrix's answer does not parse at the top level: ")


def test_vv_logs_no_input_value(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    argv = ["search", "JFK", "LAX", "--dep", depart, "--backend", "matrix", "--cash-only"]

    result = CliRunner().invoke(cli.app, ["-vv", *argv])

    assert "matrix_shape_change" in result.stderr
    assert "'input'" not in result.stderr
