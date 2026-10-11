"""A Matrix 5xx shows the user a status line and never the request URL.

Matrix's API key rides in the query string (`key=`). httpx's status error quotes
the whole URL, so a 5xx printed through `str(exc)` put the key on the terminal,
and stamina's default retry hook logged `repr(exc)` on every retry before that.
The key is public, but AGENTS.md principle 1 says an `httpx.HTTPStatusError`
never reaches a caller untyped.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import stamina
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.client import MatrixClient

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_KEY = "AIzaSyTestKeyNotReal0123456789"
_DEP = date.today() + timedelta(days=45)


def _client_over(handler: Any, tmp_path: Path) -> MatrixClient:
    c = MatrixClient(
        api_key=_KEY,
        cache_dir=str(tmp_path),
        cache_read=False,
        cache_write=False,
        rebootstrap=False,
    )
    c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
    return c


def test_a_matrix_500_prints_the_status_and_never_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "brownout"})

    def build(**_kw: object) -> MatrixClient:
        return _client_over(handler, tmp_path)

    monkeypatch.setattr(cli, "MatrixClient", build)

    with stamina.set_testing(True, attempts=3):
        result = CliRunner().invoke(
            cli.app,
            [
                *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "-n", "3"),
                *("--backend", "matrix", "--cash-only"),
            ],
        )

    seen = result.stdout + result.stderr
    assert calls["n"] == 3, calls
    assert result.exit_code == 1, seen
    assert "Matrix returned an error (http): Matrix answered HTTP 500 Internal Server Error" in seen
    assert "retry_scheduled" in result.stderr and "HTTPStatusError 500" in result.stderr, seen
    assert _KEY not in seen, seen
    assert "key=" not in seen, seen
