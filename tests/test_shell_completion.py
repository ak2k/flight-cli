# pyright: reportPrivateUsage=false
"""Tab completion: the closed-set flags offer the names their parsers accept,
and `flight --install-completion` / `--show-completion` set the shell up.

A completion request exits in Typer's `_main` before any command runs, so no
backend is stubbed."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flight_cli import cli


def _complete(line: str) -> list[str]:
    """What bash is offered for `line`, the word after its last space being
    the one under the cursor."""
    words = line.split(" ")
    result = CliRunner().invoke(
        cli.app,
        [],
        prog_name="flight",
        env={
            "_FLIGHT_COMPLETE": "complete_bash",
            "COMP_WORDS": line,
            "COMP_CWORD": str(len(words) - 1),
        },
    )
    assert result.exit_code == 0, result.output
    return [ln for ln in result.stdout.splitlines() if ln]


def test_cabin_completes_to_the_cabin_names() -> None:
    assert _complete("flight search --cabin ") == ["economy", "premium", "business", "first"]


def test_search_cabin_completes_the_next_item_of_a_comma_list() -> None:
    assert _complete("flight search --cabin economy,") == [
        "economy,premium",
        "economy,business",
        "economy,first",
    ]


_OTHER_BUCKETS = ["early", "midday", "afternoon", "evening", "night"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("flight calendar --cabin b", ["business"]),
        ("flight calendar --cabin economy,", []),
        ("flight detail --cabin economy,", []),
        ("flight search --sort b", ["business"]),
        ("flight search --backend ", ["auto", "matrix", "gflight"]),
        ("flight search --format ", ["table", "json"]),
        ("flight doctor --format j", ["json"]),
        ("flight search --gf-transport ", ["auto", "http", "browser"]),
        ("flight calendar --gf-transport b", ["browser"]),
        ("flight search --depart-times mo", ["morning"]),
        ("flight search --return-times morning,", [f"morning,{b}" for b in _OTHER_BUCKETS]),
        ("flight calendar --depart-times morning,", [f"morning,{b}" for b in _OTHER_BUCKETS]),
        ("flight detail --return-times morning,", [f"morning,{b}" for b in _OTHER_BUCKETS]),
        ("flight search --depart-times 9:30-1", []),
        ("flight search --depart-times bogus,", []),
        ("flight search --depart-times 9:30-13:45,", []),
        ("flight search --arrive-times ev", ["evening"]),
        ("flight search --return-arrive-times morning,", []),
    ],
)
def test_bounded_flags_offer_their_values(line: str, expected: list[str]) -> None:
    assert _complete(line) == expected


def test_every_offered_name_parses() -> None:
    assert len({cli._resolve_cabin(name) for name in cli._CABIN_CHOICES}) == 4
    assert len(set(cli._parse_times(",".join(cli._TIME_OF_DAY_CHOICES)))) == 6


def test_root_offers_completion_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
    shown = CliRunner().invoke(cli.app, ["--show-completion", "bash"], prog_name="flight")
    assert shown.exit_code == 0, shown.output
    assert "_FLIGHT_COMPLETE=complete_bash" in shown.stdout
    assert "--install-completion" in CliRunner().invoke(cli.app, ["--help"]).stdout
