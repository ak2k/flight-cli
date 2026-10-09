"""A Matrix answer the response models cannot read is the backend changing shape,
not a traceback and not an empty result. The command says so on one typed line,
points at the issue tracker, and keeps the body on disk for the report."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

import httpx
from typer.testing import CliRunner

from flight_cli import _http, cli

if TYPE_CHECKING:
    import pathlib

    import pytest

_KEY = "AIzaSy" + "k" * 33
_ISSUES = "https://github.com/ak2k/flight-cli/issues"
# `solutionList` is load-bearing and here it is a list, which no model reads.
_DRIFTED = {"solutionCount": 1, "session": "s-1", "solutionList": [{"id": "x"}]}


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: object) -> None:
    """Matrix behind the real client, answering every request with `body`, and the
    cache dir (where a captured body lands) inside `tmp_path`."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)


def _captured(tmp_path: pathlib.Path) -> list[pathlib.Path]:
    return sorted((tmp_path / "shape-changes").glob("matrix-*.json"))


def test_a_drifted_matrix_answer_names_the_change_and_keeps_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED)
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LAX", "--dep", depart, "--backend", "matrix", "--cash-only"],
    )

    kept = _captured(tmp_path)
    assert len(kept) == 1, result.output
    assert json.loads(kept[0].read_text()) == _DRIFTED
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "ValidationError" not in result.output
    # The console wraps a line at its width, mid-path included.
    said = "".join(result.stderr.split())
    assert "solutionList" in said
    assert _ISSUES in said
    assert str(kept[0]) in said
    assert "-vv" in said
