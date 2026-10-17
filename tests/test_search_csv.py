# pyright: reportPrivateUsage=false, reportUnusedImport=false
"""`flight search --format csv`: one row per itinerary the table lists."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.client import MatrixApiError
from flight_cli.models import SearchResult
from test_cap_and_bags import (  # noqa: F401 - the fixture is used by name
    _gbp_body,
    _matrix_cli,
    matrix_gbp,
)
from test_gf_full_board import _DEP, _LAX, _SEARCH, _served

if TYPE_CHECKING:
    from collections.abc import Callable

    import click

_ROOT = Path(__file__).resolve().parent.parent

_HEADER = [
    "idx",
    "total_price",
    "currency",
    "num_stops",
    "total_duration_min",
    "carriers",
    "flight_numbers",
    "origin",
    "destination",
    "dep_date",
]


@pytest.mark.usefixtures("matrix_gbp")
def test_a_matrix_search_writes_one_csv_row_per_itinerary() -> None:
    result = _matrix_cli("--format", "csv")
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert rows[0] == _HEADER
    assert len(rows) == 1 + 25
    first = dict(zip(_HEADER, rows[1], strict=True))
    assert first["idx"] == "1"
    assert first["currency"] == "GBP"
    assert first["num_stops"] == "0"
    assert first["carriers"] == "AA"
    assert first["flight_numbers"] == "AA142;AA107"
    assert (first["origin"], first["destination"]) == ("JFK", "LHR")
    assert first["dep_date"] == "2026-10-20"


@pytest.mark.usefixtures("matrix_gbp")
def test_a_matrix_csv_row_prices_the_trip_and_sums_its_slices() -> None:
    result = _matrix_cli("--format", "csv")
    first = dict(zip(_HEADER, list(csv.reader(io.StringIO(result.stdout)))[1], strict=True))
    assert first["total_price"] == "618.00"
    assert first["total_duration_min"] == str(425 + 480)


def test_a_google_search_writes_the_csv_of_its_board(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *(*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--backend", "gflight", "--fast", "--format", "csv", "-n", "95"),
        ],
    )
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert rows[0] == _HEADER
    assert len(rows) == 1 + 95
    first = dict(zip(_HEADER, rows[1], strict=True))
    assert first["idx"] == "1"
    assert first["currency"] == "USD"
    assert float(first["total_price"]) > 0
    assert (first["origin"], first["destination"]) == ("JFK", "LAX")
    assert first["dep_date"] == _DEP.isoformat()
    assert first["flight_numbers"]


def test_an_answer_with_no_rows_writes_the_header_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = SearchResult.from_api({"solutionList": {"solutions": []}, "solutionCount": 0})

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return empty

    monkeypatch.setattr(cli, "_run", _run)
    result = _matrix_cli("--format", "csv")
    assert result.exit_code == 0, result.output
    assert list(csv.reader(io.StringIO(result.stdout))) == [_HEADER]


def test_csv_sums_stops_and_lists_each_carrier_once_in_flight_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _gbp_body()
    slices = body["solutionList"]["solutions"][0]["itinerary"]["slices"]
    slices[0]["stops"] = [{"code": "BOS"}, {"code": "ORD"}]
    slices[1]["stops"] = [{"code": "MIA"}]
    answer = SearchResult.from_api(body)

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return answer

    monkeypatch.setattr(cli, "_run", _run)
    rows = list(csv.reader(io.StringIO(_matrix_cli("--format", "csv").stdout)))[1:]
    by_idx = {r[0]: dict(zip(_HEADER, r, strict=True)) for r in rows}
    assert by_idx["1"]["num_stops"] == "3"
    assert by_idx["2"]["num_stops"] == "0"
    assert (by_idx["5"]["carriers"], by_idx["5"]["flight_numbers"]) == ("AA;BA", "AA142;BA179")
    assert (by_idx["7"]["carriers"], by_idx["7"]["flight_numbers"]) == ("BA;AA", "BA178;AA107")
    assert by_idx["12"]["carriers"] == "IB;AA"
    assert by_idx["19"]["carriers"] == "AA"
    assert by_idx["25"]["total_price"] == "736.00"


@pytest.mark.usefixtures("matrix_gbp")
def test_csv_lists_no_more_rows_than_the_table_does() -> None:
    rows = list(csv.reader(io.StringIO(_matrix_cli("--format", "csv", "-n", "3").stdout)))
    assert [r[0] for r in rows] == ["idx", "1", "2", "3"]


def test_a_failed_csv_search_writes_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        raise MatrixApiError("Matrix would not answer", kind="input")

    monkeypatch.setattr(cli, "_run", _run)
    result = _matrix_cli("--format", "csv")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""


@pytest.mark.usefixtures("matrix_gbp")
@pytest.mark.parametrize(
    "flags",
    [
        ["--awards-only"],
        ["--cabin", "economy,business"],
        ["--slice", "JFK-LHR:2026-10-20"],
        ["--split"],
        ["--sellers"],
        ["--fare-rules"],
        ["--verify"],
        ["--enrich"],
    ],
)
def test_csv_refuses_what_it_cannot_carry(flags: list[str]) -> None:
    result = _matrix_cli("--format", "csv", *flags)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "--format csv is one table of cash itineraries" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["calendar", "JFK", "LAX", "--start", "2026-10-20"],
        ["detail", "JFK", "LAX", "--dep", "2026-10-20"],
    ],
)
def test_the_other_commands_still_refuse_csv(argv: list[str]) -> None:
    result = CliRunner().invoke(cli.app, [*argv, "--format", "csv"])
    assert result.exit_code == 2, result.output
    assert "--format must be one of" in result.output
    assert "csv" not in result.output.split("one of", 1)[1].split(";", 1)[0]


def test_csv_is_documented_where_the_formats_are_listed() -> None:
    for rel in ("README.md", ".claude/skills/flight-search/SKILL.md"):
        text = " ".join((_ROOT / rel).read_text().split())
        assert "`--format csv` (`search`)" in text, rel
        assert all(column in text for column in _HEADER), rel
        assert "are usage errors (exit 2)" in text, rel


def test_csv_asks_no_award_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `--cash-only` typed: csv itself selects the cash-only providers."""
    seen: list[bool] = []
    real = cli._should_run_awards

    def spy(sel: cli.ProviderSelection) -> bool:
        seen.append(sel.cash_only)
        return real(sel)

    monkeypatch.setattr(cli, "_should_run_awards", spy)
    empty = SearchResult.from_api({"solutionList": {"solutions": []}, "solutionCount": 0})

    def _run(*_a: Any, **_kw: Any) -> SearchResult:
        return empty

    monkeypatch.setattr(cli, "_run", _run)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--backend", "matrix", "--format", "csv"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [True]


@pytest.mark.parametrize(
    ("argv", "listed"),
    [
        (["search", "JFK", "LAX", "--dep", "2026-10-20"], "table/json/envelope/csv"),
        (["calendar", "JFK", "LAX", "--start", "2026-10-20"], "table/json/envelope;"),
    ],
)
def test_a_bad_format_names_the_formats_the_command_takes(argv: list[str], listed: str) -> None:
    result = CliRunner().invoke(cli.app, [*argv, "--format", "yaml"])
    assert result.exit_code == 2, result.output
    assert f"--format must be one of {listed}" in " ".join(result.output.split())


def test_search_help_lists_csv_and_the_other_commands_do_not() -> None:
    group = cast("click.Group", get_command(cli.app))
    helps = {
        name: next(
            cast("click.Option", p).help or ""
            for p in group.commands[name].params
            if p.name == "fmt"
        )
        for name in ("search", "calendar", "detail")
    }
    assert "csv is a header row" in helps["search"]
    assert "csv" not in helps["calendar"] + helps["detail"]


def test_the_envelope_memo_names_the_csv_run() -> None:
    memo = " ".join((_ROOT / "docs/memories/envelope.md").read_text().split())
    assert "`search --format csv` rides the same run" in memo
    assert "_envelope.record_table" in memo
