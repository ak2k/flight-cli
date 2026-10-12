# pyright: reportPrivateUsage=false
"""`flight doctor` names the limit a Matrix timeout hit: the connect limit when
Matrix accepted no connection, the 180 s read limit when it accepted one and
never answered."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import stamina

from test_doctor import World, _fails_as, _matrix_behind_the_real_client, _run, world

if TYPE_CHECKING:
    import pytest

__all__ = ["world"]

_DOCTOR_DOC = Path(__file__).resolve().parent.parent / "docs" / "memories" / "doctor.md"

_CONNECT_LIMIT = (
    "Matrix accepted no connection within 10 s, the limit a search waits to connect on each attempt"
)


def test_a_matrix_connect_timeout_names_the_connect_limit(world: World) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    world.matrix = refuse
    with stamina.set_testing(True, attempts=3):
        report = _run()
    c = _fails_as(report, "matrix-search", "brownout")
    assert len(world.matrix_requests) == 3
    assert "180" not in c.detail
    assert c.detail == _CONNECT_LIMIT
    assert report.exit_code == 75


def test_the_connect_limit_it_names_is_the_one_the_client_sends(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    del world
    connects: list[float] = []

    def refuse(request: httpx.Request, _read: float) -> httpx.Response:
        connects.append(request.extensions["timeout"]["connect"])
        raise httpx.ConnectTimeout("timed out", request=request)

    _matrix_behind_the_real_client(monkeypatch, refuse)
    with stamina.set_testing(True, attempts=3):
        c = _fails_as(_run(), "matrix-search", "brownout")
    assert connects == [10.0, 10.0, 10.0]
    assert f"within {connects[0]:.0f} s" in c.detail


def test_the_doctor_doc_names_both_limits_of_a_matrix_timeout() -> None:
    rows = [ln for ln in _DOCTOR_DOC.read_text().splitlines() if ln.startswith("| `brownout`")]
    assert len(rows) == 1
    assert "10 s" in rows[0]
    assert "180 s" in rows[0]
