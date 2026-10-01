# pyright: reportPrivateUsage=false
"""A party with an infant is priced on Google Flights.

Google has answered a route with flights (JFK-LAX, 2026-10-01) with no rows at
all for any infant, and answered JFK-LHR for the same party with 30. So an
infant search asks Google first, and a board served with no rows is handed to
Matrix under auto, with a note saying why. Where Matrix may not answer, the
empty board is printed with a note naming the way to ask it."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import cli
from flight_cli.models import SearchResult
from test_gf_full_board import _tfs
from test_links_search_tfs import _decode

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_MATRIX_BODY: dict[str, Any] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
    ).read_text()
)
_EMPTY = "ds1_zero_rows.json"
_HAND_OFF = "Using Matrix: Google Flights served no rows for a party with an infant."


def _served(name: str) -> str:
    if name == _EMPTY:  # no row to re-date
        return _page(_ds1(name))
    return _page(_answering(_ds1(name), origin=None, destination=None, date=_DEP.isoformat()))


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    """Matrix answered in process with a non-empty document; one entry per query."""
    calls: list[object] = []
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))

    class _Matrix:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, search: object, **_kw: object) -> SearchResult:
            calls.append(search)
            return SearchResult.from_api(_MATRIX_BODY)

    monkeypatch.setattr(cli, "MatrixClient", _Matrix)
    return calls


def _search(*extra: str) -> Any:
    return CliRunner().invoke(cli.app, [*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), *extra])


def _flat(text: str) -> str:
    return " ".join(text.split())


# ─────────────────────────── the page asks for them ────────────────────────


@pytest.mark.parametrize(("flag", "kind"), [("--inf-lap", 3), ("--inf-seat", 4)])
def test_the_page_is_asked_for_the_infant(
    gf_session: Callable[..., Any], matrix: list[object], flag: str, kind: int
) -> None:
    fake = gf_session(_served("ds1_jfk_lax_tfu.json"))
    result = _search(flag, "1", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert _decode(_tfs(fake.gets[0]))[8] == [1, kind]
    assert json.loads(result.stdout), "Google's rows are the answer"
    assert matrix == []
    assert "Using Matrix" not in result.stderr


# ─────────────────────── an empty board goes to Matrix ─────────────────────


@pytest.mark.parametrize("fast", [[], ["--fast"]], ids=["json", "fast-json"])
def test_under_auto_an_empty_infant_board_goes_to_matrix_with_the_note(
    gf_session: Callable[..., Any], matrix: list[object], fast: list[str]
) -> None:
    gf_session(_served(_EMPTY))
    result = _search("--inf-lap", "1", "--format", "json", *fast)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["solutionList"]["solutions"], "Matrix's document"
    assert len(matrix) == 1
    assert [ln for ln in result.stderr.splitlines() if "Using Matrix" in ln] == [_HAND_OFF]


def test_the_default_table_shows_matrixs_rows_when_googles_are_empty(
    gf_session: Callable[..., Any], matrix: list[object]
) -> None:
    fake = gf_session(_served(_EMPTY))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "--inf-lap",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.gets, "Google was asked first, beside Matrix"
    assert len(matrix) == 1
    assert "AA142" in result.stdout, result.stdout
    assert "can't serve" not in result.stderr


# ─────────────────────── where Matrix may not answer ───────────────────────


def test_on_gflight_an_empty_infant_board_is_googles_answer_with_the_way_to_matrix(
    gf_session: Callable[..., Any], matrix: list[object]
) -> None:
    gf_session(_served(_EMPTY))
    result = _search("--inf-lap", "1", "--backend", "gflight", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert matrix == []
    printed = _flat(result.stderr)
    assert "Google Flights served no rows for a party with an infant" in printed, printed
    assert "use --backend matrix" in printed
    assert "Using Matrix" not in printed


def test_beside_an_arrival_window_the_note_names_the_flag_matrix_cannot_take(
    gf_session: Callable[..., Any], matrix: list[object]
) -> None:
    gf_session(_served(_EMPTY))
    result = _search("--inf-lap", "1", "--arrive-times", "18:00-21:30", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert matrix == []
    printed = _flat(result.stderr)
    assert "Google Flights served no rows for a party with an infant" in printed, printed
    assert "drop --arrive-times to search Matrix, which takes no arrival time" in printed
    assert "Using Matrix" not in printed


def test_a_multi_cabin_compare_with_an_infant_stays_on_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    def _multi(**_kw: object) -> None:
        ran.append("matrix")

    def _no_google(**_kw: object) -> None:
        pytest.fail("a multi-cabin compare with an infant reached Google")

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _multi)
    monkeypatch.setattr(cli, "_run_gflight_path_multi", _no_google)
    monkeypatch.setenv("COLUMNS", "400")
    result = _search("--inf-lap", "1", "--cabin", "economy,business")
    assert result.exit_code == 0, result.output
    assert ran == ["matrix"]
    assert (
        "Using Matrix: Google Flights can't serve an infant passenger on a multi-cabin compare."
        in _flat(result.stderr)
    )


def test_bags_beside_an_infant_are_refused_as_a_second_traveler() -> None:
    result = CliRunner().invoke(
        cli.app,
        [*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), "--bags", "1", "--inf-lap", "1"],
    )
    assert result.exit_code == 2, result.output
    assert "--bags takes one traveler" in _flat(result.stderr)
