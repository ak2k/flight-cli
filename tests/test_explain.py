"""`flight explain ROUTING`: a routing string in plain English, one line a token.

Every case goes through the command, so the module collects wherever the command
is absent and the first test fails on its exit code rather than on an import."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flight_cli import cli


def _explain(routing: str) -> tuple[int, list[str]]:
    result = CliRunner().invoke(cli.app, ["explain", routing])
    return result.exit_code, result.stdout.splitlines()


def test_explain_decodes_an_operated_by_token() -> None:
    code, lines = _explain("O:LH+")
    assert code == 0
    assert lines == ["O:LH+  ->  one or more flights operated by LH"]


@pytest.mark.parametrize(
    ("token", "meaning"),
    [
        ("AA", "one flight marketed by AA"),
        ("c:aa", "one flight marketed by AA"),
        ("AA,CO,DL", "one flight marketed by AA, CO or DL"),
        ("O:AA,UA", "one flight operated by AA or UA"),
        ("O:AA,C:UA", "one flight operated by AA or marketed by UA"),
        ("AA+", "one or more flights marketed by AA"),
        ("AA*", "zero or more flights marketed by AA"),
        ("AA?", "zero or one flight marketed by AA"),
        ("~UA", "one flight not marketed by UA"),
        ("~UA+", "one or more flights not marketed by UA"),
        ("F", "one flight"),
        ("F+", "one or more flights"),
        ("N", "one nonstop flight"),
        ("N:ua", "one nonstop flight on UA"),
        ("X+", "one or more connections"),
        ("DFW", "one connection at DFW"),
        ("x:icn", "one connection at ICN"),
        ("~DFW", "one connection not at DFW"),
        ("DFW,DEN", "one connection at DFW or DEN"),
        ("~l:nUS+", "one or more connections not in country US"),
        ("UA882", "one flight numbered UA882"),
        ("UA1000-2000+", "one or more flights numbered UA1000-2000"),
        ("~UA882+", "one or more flights not numbered UA882"),
    ],
)
def test_each_token_form_reads_as_documented(token: str, meaning: str) -> None:
    assert _explain(token) == (0, [f"{token}  ->  {meaning}"])


def test_a_chain_prints_one_line_per_token_in_order() -> None:
    code, lines = _explain("F+ AA F+")
    assert code == 0
    assert lines == [
        "F+  ->  one or more flights",
        "AA  ->  one flight marketed by AA",
        "F+  ->  one or more flights",
    ]


def test_one_pair_of_brackets_around_the_whole_string_is_dropped() -> None:
    assert _explain("[F* X:LHR F*]") == (
        0,
        [
            "F*  ->  zero or more flights",
            "X:LHR  ->  one connection at LHR",
            "F*  ->  zero or more flights",
        ],
    )


@pytest.mark.parametrize(
    "token",
    [
        "L:US",  # the country form is read only as the documented `l:nUS`
        "AA,DFW",  # a flight and a connection in one group
        "F,AA",  # a placeholder names no code to list
        "~F",
        "~N",
        "~N:UA",
        "~X+",
        "UA2000-1000",
        "O:LHR",
        "XYZW",
        "AA++",
        "AA,,DL",
        "~",
        "\u017fFO",  # U+017F upper-cases to an ASCII S, so it would read as SFO
    ],
)
def test_a_token_outside_the_grammar_exits_1_and_is_not_guessed(token: str) -> None:
    code, lines = _explain(token)
    assert code == 1
    assert lines == [f"{token!r}  ->  not recognized"]


@pytest.mark.parametrize("token", ["12", "C:12", "O:12", "N:12", "N:12+", "12,AA", "1234"])
def test_an_all_digit_designator_is_not_read_in_any_form(token: str) -> None:
    assert _explain(token) == (1, [f"{token!r}  ->  not recognized"])


def test_a_bad_token_leaves_the_others_read() -> None:
    code, lines = _explain("UA bogus LH")
    assert code == 1
    assert lines == [
        "UA  ->  one flight marketed by UA",
        "'bogus'  ->  not recognized",
        "LH  ->  one flight marketed by LH",
    ]


@pytest.mark.parametrize("routing", ["", "   ", "[]"])
def test_an_empty_routing_is_not_recognized(routing: str) -> None:
    code, lines = _explain(routing)
    assert code == 1
    assert len(lines) == 1
    assert lines[0].endswith("->  not recognized")


def test_an_unread_token_is_printed_inert() -> None:
    code, lines = _explain("[red]x\x07\x1b[2J")
    out = "\n".join(lines)
    assert code == 1
    assert "\x07" not in out
    assert "\x1b" not in out
    assert "->  not recognized" in out
