# pyright: reportPrivateUsage=false
"""The line under the Matrix itinerary table that names each carrier code the table shows."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from flight_cli import cli
from flight_cli.models import SearchResult


def _solution(*carriers: tuple[str, str | None]) -> dict[str, Any]:
    """One solution flown by `carriers`, each a (code, shortName); a None name is not sent."""
    return {
        "ext": {"price": "USD421.00"},
        "itinerary": {
            "carriers": [
                {"code": code} if name is None else {"code": code, "shortName": name}
                for code, name in carriers
            ],
            "slices": [],
        },
    }


def _printed(
    monkeypatch: pytest.MonkeyPatch, solutions: list[dict[str, Any]], limit: int = 10
) -> list[str]:
    res = SearchResult.from_api(
        {
            "solutionCount": len(solutions),
            "currencyNotice": {"ext": {"price": "USD421.00"}},
            "solutionList": {"solutions": solutions},
        }
    )
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=300, color_system=None))
    cli._render_search(res, limit)
    return buffer.getvalue().splitlines()


def _legend(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("Carriers:")]


def test_the_legend_names_each_shown_code_once_in_first_shown_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _printed(
        monkeypatch,
        [
            _solution(("DL", "Delta")),
            _solution(("AA", "American"), ("DL", "Delta")),
            _solution(("AA", "American")),
        ],
    )
    assert lines.count("Carriers: DL Delta · AA American") == 1
    assert len(_legend(lines)) == 1
    assert lines.index("Carriers: DL Delta · AA American") > max(
        i for i, ln in enumerate(lines) if ln.startswith("└")
    )


def test_a_code_with_no_short_name_is_left_out_and_a_table_with_none_has_no_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named = _printed(
        monkeypatch, [_solution(("DL", "Delta"), ("XX", None), ("YY", "   "), ("ZZ", ""))]
    )
    assert _legend(named) == ["Carriers: DL Delta"]
    assert _legend(_printed(monkeypatch, [_solution(("XX", None), ("YY", ""))])) == []


def test_a_code_named_only_on_a_later_row_keeps_its_first_shown_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _printed(
        monkeypatch,
        [_solution(("AA", None), ("DL", "Delta")), _solution(("AA", "American"))],
    )
    assert _legend(lines) == ["Carriers: AA American · DL Delta"]


def test_a_code_keeps_the_first_name_a_row_gave_it(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _printed(
        monkeypatch,
        [_solution(("DL", "Delta")), _solution(("DL", "Delta Air Lines"), ("AA", "American"))],
    )
    assert _legend(lines) == ["Carriers: DL Delta · AA American"]


def test_a_row_past_the_limit_adds_no_code_to_the_legend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _printed(
        monkeypatch,
        [_solution(("DL", "Delta")), _solution(("AA", "American"))],
        limit=1,
    )
    assert _legend(lines) == ["Carriers: DL Delta"]


def test_a_short_name_with_markup_prints_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _printed(monkeypatch, [_solution(("DL", "[bold]Delta[/bold]"))])
    assert _legend(lines) == ["Carriers: DL [bold]Delta[/bold]"]


@pytest.mark.parametrize(
    "unseen",
    ["\x1b\x00", "\u200b\u2066", " \x7f\t\u00ad "],
    ids=["control", "invisible", "mixed"],
)
def test_a_name_that_prints_as_nothing_leaves_the_code_out(
    monkeypatch: pytest.MonkeyPatch, unseen: str
) -> None:
    assert _legend(_printed(monkeypatch, [_solution(("DL", unseen))])) == []


def test_an_empty_code_is_left_out_of_the_legend(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _printed(monkeypatch, [_solution(("", "Ghost"), ("DL", "Delta"))])
    assert _legend(lines) == ["Carriers: DL Delta"]
