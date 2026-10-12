# pyright: reportPrivateUsage=false
"""A booking-details or fare-rules answer the response models refuse is a
`MatrixShapeError` with the body kept, the same as a search answer."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import typer

from flight_cli import _http, cli
from flight_cli.client import REPORT_URL, MatrixClient, MatrixShapeError

if TYPE_CHECKING:
    import pathlib

    from flight_cli.models import BookingDetailsResult, FareRulesResult

_KEY = "AIzaSy" + "k" * 33
_DRIFTED_DETAILS = {"bookingDetails": "x"}
_DRIFTED_RULES = {"fareRules": "x"}


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, body: object) -> None:
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)


async def _booking_details() -> BookingDetailsResult:
    async with MatrixClient(cache_read=False, cache_write=False) as c:
        return await c.booking_details(session="s", solution_set="t", solution_id="u")


async def _fare_rules() -> FareRulesResult:
    async with MatrixClient(cache_read=False, cache_write=False) as c:
        return await c.fare_rules(session="s", solution_set="t", solution_id="u", fare_key="k")


def _assert_kept(err: MatrixShapeError, tmp_path: pathlib.Path, body: object) -> None:
    assert err.__cause__ is not None
    assert err.captured is not None
    assert err.captured.parent == tmp_path / "shape-changes"
    assert json.loads(err.captured.read_text()) == body
    assert stat.S_IMODE(err.captured.stat().st_mode) == 0o600
    assert REPORT_URL in str(err)
    assert str(err.captured) in str(err)


def test_a_drifted_booking_details_answer_is_a_shape_error_with_the_body_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED_DETAILS)

    with pytest.raises(MatrixShapeError) as caught:
        anyio.run(_booking_details)

    assert caught.value.detail.startswith("Matrix's answer does not parse at bookingDetails: ")
    _assert_kept(caught.value, tmp_path, _DRIFTED_DETAILS)


def test_a_drifted_fare_rules_answer_is_a_shape_error_with_the_body_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED_RULES)

    with pytest.raises(MatrixShapeError) as caught:
        anyio.run(_fare_rules)

    assert caught.value.detail.startswith("Matrix's answer does not parse at fareRules: ")
    _assert_kept(caught.value, tmp_path, _DRIFTED_RULES)


def test_a_follow_up_that_parses_keeps_no_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _matrix_answers(monkeypatch, tmp_path, {})

    assert anyio.run(_booking_details).booking_details is None
    assert anyio.run(_fare_rules).fare_rules is None
    assert not (tmp_path / "shape-changes").exists()


def test_a_drifted_follow_up_leaves_the_command_with_the_report_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _matrix_answers(monkeypatch, tmp_path, _DRIFTED_RULES)

    with pytest.raises(typer.Exit) as left:
        cli._run_matrix(_fare_rules, said="Matrix fare rules failed")

    assert left.value.exit_code == 1
    shown = " ".join(capsys.readouterr().err.split())
    assert "Matrix fare rules failed" in shown
    assert REPORT_URL in shown
    assert "ValidationError" not in shown
