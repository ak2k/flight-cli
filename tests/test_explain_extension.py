"""`flight explain --ext`: extension codes in plain English, one line a code.

Every case goes through the command, so the module collects wherever the option
is absent and each test fails on its exit code or output rather than on an import."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flight_cli import cli


def _explain(*args: str) -> tuple[int, list[str]]:
    result = CliRunner().invoke(cli.app, ["explain", *args])
    return result.exit_code, result.stdout.splitlines()


def test_explain_decodes_a_routing_and_its_extension_codes() -> None:
    assert _explain("O:LH+", "--ext", "MAXCONNECT 2:00") == (
        0,
        [
            "O:LH+  ->  one or more flights operated by LH",
            "MAXCONNECT 2:00  ->  no layover longer than 2:00",
        ],
    )


@pytest.mark.parametrize(
    ("code", "meaning"),
    [
        ("MAXSTOPS 1", "at most 1 stop in each direction"),
        ("MAXSTOPS 2", "at most 2 stops in each direction"),
        ("MAXDUR 18:00", "no itinerary longer than 18:00 in each direction"),
        ("MAXCONNECT 2:00", "no layover longer than 2:00"),
        ("MINCONNECT 1:00", "no layover shorter than 1:00"),
        ("MAXMILES 2900", "at most 2900 miles in each direction"),
        ("MINMILES 2600", "at least 2600 miles in each direction"),
        ("-REDEYES", "no red-eye flights"),
        ("-OVERNIGHTS", "no overnight stops at connections"),
        ("-CODESHARE", "no codeshare flights"),
    ],
)
def test_each_extension_code_reads_as_documented(code: str, meaning: str) -> None:
    assert _explain("--extension", code) == (0, [f"{code}  ->  {meaning}"])


def test_semicolon_joined_codes_print_one_line_each_keyword_in_any_case() -> None:
    assert _explain("--ext", "maxstops 1; -Redeyes ;;MINCONNECT 0:45;") == (
        0,
        [
            "maxstops 1  ->  at most 1 stop in each direction",
            "-Redeyes  ->  no red-eye flights",
            "MINCONNECT 0:45  ->  no layover shorter than 0:45",
        ],
    )


@pytest.mark.parametrize(
    "code",
    [
        "MAXSTOPS",
        "MAXSTOPS 1 2",
        "MAXSTOPS x",
        "MAXDUR 9",
        "MAXCONNECT 2:00 1:00",
        "-REDEYES 1",
        "LH+",
    ],
)
def test_a_code_outside_the_read_set_exits_1_and_is_not_guessed(code: str) -> None:
    assert _explain("--ext", code) == (1, [f"{code!r}  ->  not recognized"])


@pytest.mark.parametrize(
    "code",
    [
        "MAXSTOPS \u0661",  # an Arabic-Indic digit is not an ASCII count
        "MAXSTOPS 1234567890",  # a count has at most 9 digits
        "MAXDUR 9:60",
        "MAXMILES 2,900",
        "ALLIANCE oneworld",  # a real code this unit does not read
        "+CABIN 2",
        "MAX\u017fTOPS 1",  # U+017F upper-cases to an ASCII S
        "-OVERN\u0131GHTS",  # U+0131 upper-cases to an ASCII I
        "",
        ";",
    ],
)
def test_other_unread_extension_text_exits_1(code: str) -> None:
    assert _explain("--ext", code) == (1, [f"{code!r}  ->  not recognized"])


def test_an_unread_code_leaves_the_others_and_the_routing_read() -> None:
    assert _explain("UA", "--ext", "-REDEYES; ALLIANCE oneworld; MAXSTOPS 0") == (
        1,
        [
            "UA  ->  one flight marketed by UA",
            "-REDEYES  ->  no red-eye flights",
            "'ALLIANCE oneworld'  ->  not recognized",
            "MAXSTOPS 0  ->  at most 0 stops in each direction",
        ],
    )


def test_an_unread_routing_token_exits_1_beside_a_read_extension() -> None:
    assert _explain("bogus", "--ext", "-CODESHARE") == (
        1,
        ["'bogus'  ->  not recognized", "-CODESHARE  ->  no codeshare flights"],
    )


def test_an_unread_extension_code_is_printed_inert() -> None:
    code, lines = _explain("--ext", "[red]x\x07\x1b[2J")
    out = "\n".join(lines)
    assert code == 1
    assert "\x07" not in out
    assert "\x1b" not in out
    assert "->  not recognized" in out


def test_neither_a_routing_nor_extension_codes_is_a_usage_error_naming_ext() -> None:
    result = CliRunner().invoke(cli.app, ["explain"])
    assert result.exit_code == 2
    assert "--ext" in result.output


def test_the_command_list_says_explain_reads_extension_codes() -> None:
    out = CliRunner().invoke(cli.app, ["--help"]).stdout
    row = next(ln for ln in out.splitlines() if ln.startswith("│ explain "))
    assert "extension codes" in row


@pytest.mark.parametrize("space", ["\n", "\r", "\t", "\x0b"])
def test_a_read_code_shows_each_whitespace_character_as_a_space(space: str) -> None:
    assert _explain("--ext", f"MAXSTOPS{space}1") == (
        0,
        ["MAXSTOPS 1  ->  at most 1 stop in each direction"],
    )
