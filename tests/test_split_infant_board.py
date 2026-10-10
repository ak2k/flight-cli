# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search --split` for a party with an infant: a one-way board Google
served no row for is not the leg's answer, as an infant's empty round-trip board
is not the route's. Google is faked as in `test_split_ticket`; no test reaches
Google, Matrix or Chrome."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _envelope
from test_envelope import _envelope_of, _notes
from test_split_ticket import _google, _search, _split_lines

if TYPE_CHECKING:
    from flight_cli._envelope import Backend

_ARGV = ("--cash-only", "--fast", "--backend", "gflight", "--split")
_NO_ROWS = (
    "Google Flights served no rows for a party with an infant on the {which} one-way, "
    "as it has on routes with flights"
)

_WHICH = [
    pytest.param({"back": []}, "return", id="return-board"),
    pytest.param({"out": []}, "outbound", id="outbound-board"),
]


@pytest.mark.parametrize(("legs", "which"), _WHICH)
def test_an_infants_empty_one_way_board_narrows_the_round_trips_envelope(
    monkeypatch: pytest.MonkeyPatch, legs: dict[str, Any], which: str
) -> None:
    """Red at the merge: the board read as a priced-no-one-way answer, the
    envelope said `complete: true` and the line named no infant."""
    reason = _NO_ROWS.format(which=which)
    _google(monkeypatch, **legs)
    result = _search(*_ARGV, "--inf-lap", "1", "--format", "envelope")
    env = _envelope_of(result)
    assert " ".join(result.stderr.split()).count(f"No split tickets: {reason}.") == 1
    assert "priced no" not in result.stderr
    assert "Matrix" not in result.stderr
    assert env["complete"] is False
    assert env["split_ticket"] == {"error": reason}
    assert _notes(env, "split_ticket") == []


@pytest.mark.parametrize(("legs", "which"), _WHICH)
def test_an_infants_empty_one_way_board_is_named_in_the_json_and_the_table(
    monkeypatch: pytest.MonkeyPatch, legs: dict[str, Any], which: str
) -> None:
    reason = _NO_ROWS.format(which=which)
    _google(monkeypatch, **legs)
    as_json = _search(*_ARGV, "--inf-lap", "1", "--format", "json")
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["split_ticket"] == {"error": reason}
    _google(monkeypatch, **legs)
    table = _search(*_ARGV, "--inf-lap", "1")
    assert table.exit_code == 0, table.output
    assert _split_lines(table.stdout) == []
    assert " ".join(table.stderr.split()).count(f"No split tickets: {reason}.") == 1


@pytest.mark.parametrize(("legs", "which"), _WHICH)
def test_an_empty_one_way_board_without_an_infant_is_still_the_boards_answer(
    monkeypatch: pytest.MonkeyPatch, legs: dict[str, Any], which: str
) -> None:
    _google(monkeypatch, **legs)
    result = _search(*_ARGV, "--format", "envelope")
    env = _envelope_of(result)
    assert env["complete"] is True
    assert env["split_ticket"] == {"error": f"Google Flights priced no {which} one-way"}


def test_the_round_trips_narrowing_names_google_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pair is shown beside Google's round-trip answer, so the narrowing
    is Google's (`of="gflight"`), as a failed one-way's is. Matrix is never
    asked a one-way for a `--split` round trip, so no search shows the
    difference in `complete`; an open jaw's, which narrows whoever answers, is
    held by `test_multi_city_search.test_an_infants_empty_board_narrows_the_answer`."""
    named: list[Backend | None] = []
    narrow = _envelope.narrow

    def _spy(note: str | None = None, *, of: Backend | None = None) -> None:
        named.append(of)
        narrow(note, of=of)

    monkeypatch.setattr(_envelope, "narrow", _spy)
    _google(monkeypatch, back=[])
    _envelope_of(_search(*_ARGV, "--inf-lap", "1", "--format", "envelope"))
    assert named == ["gflight"]


def test_the_separate_tickets_memory_says_a_split_round_trip_narrows_the_same_way() -> None:
    memory = Path(__file__).resolve().parent.parent / "docs" / "memories" / "gf_separate_tickets.md"
    text = " ".join(memory.read_text().split())
    at = text.index("is `_NoInfantRows`")
    paragraph = text[at : text.index("`_OneWaysUnpriced`", at)]
    assert "`--split` round trip" in paragraph, paragraph
