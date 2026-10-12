"""A Matrix search answer whose `solutionList.solutions` is present but not a list
is a shape change, not an answer with no results."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import _http, cli
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pathlib

_KEY = "AIzaSy" + "k" * 33


@pytest.mark.parametrize("solutions", [{}, "", 0, False], ids=repr)
def test_a_solutions_that_is_present_and_not_a_list_is_refused(solutions: object) -> None:
    try:
        SearchResult.from_api({"solutionList": {"solutions": solutions}})
    except ValueError as e:
        assert "solutionList.solutions" in str(e)
    else:
        raise AssertionError("read as an answer with 0 results")


@pytest.mark.parametrize("listing", [{}, {"solutions": None}, {"solutions": []}], ids=repr)
def test_an_absent_null_or_empty_solutions_is_still_no_results(
    listing: dict[str, object],
) -> None:
    assert SearchResult.from_api({"solutionList": listing}).solutions == []


def test_the_command_reports_the_shape_change_and_keeps_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    body: dict[str, object] = {
        "solutionCount": 0,
        "session": "s-1",
        "solutionList": {"solutions": {}},
    }
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", depart, "--backend", "matrix", "--cash-only"],
    )

    kept = sorted((tmp_path / "shape-changes").glob("matrix-*.json"))
    assert len(kept) == 1, result.output
    assert json.loads(kept[0].read_text()) == body
    assert result.exit_code == 1, result.output
    assert "solutionList.solutions" in result.stderr
