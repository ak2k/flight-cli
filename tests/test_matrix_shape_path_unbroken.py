"""The path a Matrix shape-change line asks the user to attach is printed whole.

The `err` console wraps a line at its width, and a path has no space to wrap at,
so a wrapped failure line cuts the path in the middle, and the path a user copies
from it does not exist. Every line that prints a failure keeps its text whole."""

from __future__ import annotations

import ast
import datetime as dt
import io
import pathlib
from typing import TYPE_CHECKING

import httpx
import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _http, cli
from flight_cli.client import MatrixShapeError

if TYPE_CHECKING:
    from collections.abc import Callable

_KEY = "AIzaSy" + "k" * 33
# `solutionList` is load-bearing and here it is a list, which no model reads.
_DRIFTED = {"solutionCount": 1, "session": "s-1", "solutionList": [{"id": "x"}]}
# Longer than any console width a test can get, and with nothing to wrap at.
_KEPT = pathlib.Path("/kept") / ("a" * 150) / ("b" * 150) / "matrix-20261012T000000.json"


def _narrow_err(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "err", Console(file=buf, width=40, force_terminal=False, no_color=True, emoji=False)
    )
    return buf


def _shape() -> MatrixShapeError:
    return MatrixShapeError("Matrix's answer does not parse", captured=_KEPT)


def _beside_a_stop() -> None:
    with pytest.raises(typer.Exit):
        cli._reraise_if_orderly(  # pyright: ignore[reportPrivateUsage] — the reporter IS the unit
            ExceptionGroup("g", [typer.Exit(2), _shape()]), said="Matrix search failed"
        )


def _calendar_failure() -> None:
    cli._print_calendar_failure(_shape(), "lost; ")  # pyright: ignore[reportPrivateUsage]


def _lost_group() -> None:
    fan = cli._CalendarFanout(  # pyright: ignore[reportPrivateUsage]
        results=[], failures=[_shape()], lost=["JFK-LAX"]
    )
    cli._report_calendar_fanout(fan, 2, merged_empty=False)  # pyright: ignore[reportPrivateUsage]


def _answered_then_matrix_failed() -> None:
    cli._report_weave_aftermath({"matrix_unexpected": _shape()})  # pyright: ignore[reportPrivateUsage]


def _answered_then_weave_failed() -> None:
    cli._report_weave_aftermath({"weave_err": _shape()})  # pyright: ignore[reportPrivateUsage]


def _no_result_matrix_failed() -> None:
    cli._report_search_matrix_failure({"matrix_unexpected": _shape()})  # pyright: ignore[reportPrivateUsage]


def _no_result_weave_failed() -> None:
    cli._report_search_matrix_failure({"weave_err": _shape()})  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "report",
    [
        _beside_a_stop,
        _calendar_failure,
        _lost_group,
        _answered_then_matrix_failed,
        _answered_then_weave_failed,
        _no_result_matrix_failed,
        _no_result_weave_failed,
    ],
)
def test_each_reporter_prints_the_capture_path_unbroken(
    monkeypatch: pytest.MonkeyPatch, report: Callable[[], None]
) -> None:
    buf = _narrow_err(monkeypatch)

    report()

    assert str(_KEPT) in buf.getvalue()


def test_a_lost_currency_route_is_printed_unbroken(monkeypatch: pytest.MonkeyPatch) -> None:
    route = "J" * 90
    buf = _narrow_err(monkeypatch)
    fan = cli._CalendarFanout(  # pyright: ignore[reportPrivateUsage]
        results=[],
        failures=[cli._CalendarCurrencyError(route, ["EUR"], "USD")],  # pyright: ignore[reportPrivateUsage]
        lost=[route],
    )

    cli._report_calendar_fanout(fan, 2, merged_empty=False)  # pyright: ignore[reportPrivateUsage]

    assert f"{route} came back priced in EUR" in buf.getvalue()


def _matrix_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Matrix behind the real client, answering every request with a body no model
    reads, and the cache dir (where the body is kept) at a path longer than 300
    characters, so the kept path fits no console the test can get."""
    cache = tmp_path / ("c" * 150) / ("d" * 150)
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(cache))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=_DRIFTED))

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    return cache


def _search(*extra: str) -> list[str]:
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return ["search", "JFK", "LAX", "--dep", depart, "--backend", "matrix", "--cash-only", *extra]


def test_the_capture_path_on_the_failure_line_is_unbroken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cache = _matrix_answers(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli.app, _search())

    kept = sorted((cache / "shape-changes").glob("matrix-*.json"))
    assert len(kept) == 1, result.output
    assert len(str(kept[0])) > 300
    assert result.exit_code == 1, result.output
    assert str(kept[0]) in result.stderr


def test_a_cabin_that_drifted_prints_the_capture_path_unbroken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cache = _matrix_answers(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli.app, _search("--cabin", "economy,business"))

    kept = sorted((cache / "shape-changes").glob("matrix-*.json"))
    assert kept, result.output
    assert str(kept[0]) in result.stderr


def test_a_multi_cabin_run_that_fails_outside_a_cabin_prints_the_path_unbroken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cache = _matrix_answers(monkeypatch, tmp_path)

    async def close_that_drifts(*_a: object, **_kw: object) -> None:
        raise MatrixShapeError("the close does not parse", captured=cache / "kept.json")

    monkeypatch.setattr(_http.HttpTransport, "aclose", close_that_drifts)

    result = CliRunner().invoke(cli.app, _search("--cabin", "economy,business"))

    assert str(cache / "kept.json") in result.stderr


def test_no_failure_line_is_printed_through_a_wrapping_console() -> None:
    """The backstop for a site no run above reaches: Google Flights' own line."""
    tree = ast.parse(pathlib.Path(cli.__file__).read_text())
    wrapped: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "print"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "err"
        ):
            continue
        prints_a_failure = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "_failure_text"
            for arg in node.args
            for inner in ast.walk(arg)
        )
        soft = any(
            k.arg == "soft_wrap" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords
        )
        if prints_a_failure and not soft:
            wrapped.append(node.lineno)
    assert wrapped == []
