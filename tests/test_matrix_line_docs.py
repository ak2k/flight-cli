"""The JSON memo says what a `Using Matrix:` line means for both ways Matrix answers."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    text = (_ROOT / rel).read_text().replace("\n# ", "\n")
    return " ".join(text.split())


def test_using_matrix_line_has_two_causes() -> None:
    memo = _flat("docs/memories/wire_format_quirks.md")
    assert "says why Matrix answered, and there are two causes" in memo
    assert "Google was asked and handed on" in memo
    assert "Google served no rows for a party with an infant, or the query failed" in memo
    assert (
        "The exception is a party with an infant on auto, whose empty board goes to Matrix" in memo
    )
    assert "Or Google was never asked: a flag Google cannot serve" in memo
    assert "before any Google call" in memo
    assert "Using Matrix: Google Flights can't serve <reason>." in memo
    assert "says the search was handed from Google to Matrix, and why" not in memo


def test_infant_exception_names_what_keeps_the_board_on_google() -> None:
    memo = _flat("docs/memories/wire_format_quirks.md")
    assert (
        "A Google-only flag (an arrival window, `--exclude-basic`), `--verify` or `--sellers` "
        "keeps that board Google's, with a note naming how to ask Matrix." in memo
    )


def test_json_memo_names_three_cases_beside_the_matrix_line() -> None:
    memo = _flat("docs/memories/wire_format_quirks.md")
    assert (
        "or anything else it raised). An award search whose filter left only "
        "separate-ticket rows is handed on the same way, since awards are matched "
        "to one-ticket rows." in memo
    )
    assert (
        "Google Flights can't serve <reason>.` A multi-city search still asks Google "
        "for each slice's one-ways when the table or `--split` shows separate tickets "
        "beside Matrix's answer. The document after either line is Matrix's." in memo
    )
    assert (
        "because the award providers run whatever Google served. Cash-only under "
        "`--verify` or `--sellers`, it exits 1 with stdout empty, since there is no "
        "row to check or open." in memo
    )
