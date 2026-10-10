# pyright: reportPrivateUsage=false
"""A capped one-cabin Matrix search that leaves no fare, where Matrix kept a
nonzero count because the cap dropped fares it could not read, says so on
stderr in every format, as the multi-cabin path does; a count of zero stays
silent there."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Cabin
from test_capped_unreadable_matrix import _DEP, _matrix_answers, _solution
from test_multi_cabin_pins import _matrix_search

if TYPE_CHECKING:
    from click.testing import Result

_LINE = (
    "Matrix: no fare that states a USD total is at or under USD 2000; "
    "those that state none are not shown."
)


def _search(*args: str) -> Result:
    return CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cash-only"),
            *("--no-matrix-url", "--no-google-url", "--backend", "matrix"),
            *("--cabin", "business", "--adults", "2", "-n", "2", "--max-price", "2000"),
            *args,
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )


def _untotaled(monkeypatch: pytest.MonkeyPatch) -> None:
    _matrix_answers(
        monkeypatch,
        _solution("s1", "USD750.00", total=None),
        _solution("s2", "USD700.00", total=None),
        _solution("s3", "USD650.00", total=None),
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_a_capped_party_search_names_the_fares_it_could_not_read_on_stderr(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    _untotaled(monkeypatch)
    result = _search("--format", fmt)
    assert result.exit_code == 0, result.output
    assert _flat(result.stderr) == _LINE
    if fmt == "json":
        assert json.loads(result.stdout)["solutionCount"] == 3
    else:
        assert _flat(result.stdout) == "No solutions at or under USD 2000."


def test_a_capped_party_envelope_does_not_say_matrix_returned_no_itinerary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _untotaled(monkeypatch)
    result = _search("--format", "envelope")
    assert result.exit_code == 0, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    assert doc["results"] == [{"cabin": "BUSINESS", "rows": []}]
    notes: list[str] = doc["notes"]
    assert _LINE in [_flat(n) for n in notes]
    assert [n for n in notes if n.startswith("results:")] == [
        "results: no fare that states a USD total is at or under USD 2000"
    ]


def test_a_capped_search_whose_every_fare_is_over_the_cap_stays_silent_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _matrix_answers(
        monkeypatch,
        _solution("s1", "USD2500.00", total="USD2500.00"),
        _solution("s2", "USD2600.00", total="USD2600.00"),
    )
    result = _search("--format", "envelope")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    notes: list[str] = json.loads(result.stdout)["notes"]
    assert [n for n in notes if n.startswith("results:")] == [
        "results: no itinerary in any cabin asked"
    ]


def test_a_capped_multi_cabin_envelope_with_no_total_in_any_cabin_names_why_it_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _matrix_search(
        monkeypatch,
        *("--adults", "2", "--max-price", "2000", "--format", "envelope"),
        party=2,
        untotaled=frozenset({Cabin.COACH, Cabin.BUSINESS}),
    )
    assert result.exit_code == 0, result.output
    notes: list[str] = json.loads(result.stdout)["notes"]
    assert [n for n in notes if n.startswith("results:")] == [
        "results: no fare that states a USD total is at or under USD 2000"
    ]
