# pyright: reportPrivateUsage=false
"""`--pax SPEC`: the passengers as one token, such as `2A1C1IL`.

`search`, `calendar` and `detail` read the token as the same `Pax` their six
count flags give. The token is read whole and case-insensitively; a code given
twice, an unknown code, text left over or a count over 9 is a usage error that
names the token, and `--pax` beside a count flag is one that names both."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Pax
from test_cap_and_bags import _PATHS, _search_cli

if TYPE_CHECKING:
    import pytest
    from click.testing import Result

    from flight_cli.domain import CalendarFollowup, CalendarSearch

_ROOT = Path(__file__).resolve().parent.parent
_DAY = date.today() + timedelta(days=60)
_CALENDAR = ["calendar", "JFK", "LAX", "--start", _DAY.isoformat(), "--no-cache"]
_DETAIL = ["detail", "JFK", "LAX", "--dep", _DAY.isoformat(), "--no-matrix-url", "--no-google-url"]
_FLAGS = ("--adults", "--children", "--seniors", "--youth", "--inf-seat", "--inf-lap")


def _flat(output: str) -> str:
    """`output` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def _stub_search(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _record(**kw: Any) -> None:
        calls.append(kw)

    for name in _PATHS:
        monkeypatch.setattr(cli, name, _record)
    return calls


def _stub_calendar(monkeypatch: pytest.MonkeyPatch) -> list[CalendarSearch]:
    seen: list[CalendarSearch] = []

    def _without_fast(search: CalendarSearch, **_kw: Any) -> None:
        seen.append(search)

    monkeypatch.setattr(cli, "_calendar_without_fast", _without_fast)
    return seen


def _stub_detail(monkeypatch: pytest.MonkeyPatch) -> list[CalendarFollowup]:
    seen: list[CalendarFollowup] = []

    def _run(search: CalendarFollowup, *_a: Any) -> None:
        seen.append(search)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_run", _run)
    return seen


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def test_pax_asks_two_adults_a_child_and_a_lap_infant(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_search(monkeypatch)
    result = _search_cli("--pax", "2A1C1IL")
    assert result.exit_code == 0, result.output
    [kw] = calls
    assert kw["opts"].pax == Pax(adults=2, children=1, infants_in_lap=1)


def test_search_reads_each_code_and_leaves_the_rest_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search(monkeypatch)
    asked = {
        "1A": Pax(adults=1),
        "1C": Pax(adults=0, children=1),
        "3a2s1y": Pax(adults=3, seniors=2, youth=1),
        "1A1IS": Pax(adults=1, infants_in_seat=1),
        "1IS1IL1A": Pax(adults=1, infants_in_seat=1, infants_in_lap=1),
        "9A": Pax(adults=9),
        " 2A1C ": Pax(adults=2, children=1),
    }
    for spec, pax in asked.items():
        calls.clear()
        result = _search_cli("--pax", spec)
        assert result.exit_code == 0, (spec, result.output)
        [kw] = calls
        assert kw["opts"].pax == pax, spec


def test_search_pax_is_the_pax_the_six_flags_give(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_search(monkeypatch)
    assert _search_cli("--pax", "2A1C1IL").exit_code == 0
    assert _search_cli("--adults", "2", "--children", "1", "--inf-lap", "1").exit_code == 0
    [by_pax, by_flags] = calls
    assert by_pax["opts"] == by_flags["opts"]


def test_a_spec_that_is_not_whole_is_a_usage_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search(monkeypatch)
    for spec in ("", "1", "A1", "1A1A", "1a1A", "2A1X", "2A 1C", "1A,1C", "1A!", "10A", "1IS1IS"):
        result = _search_cli("--pax", spec)
        assert result.exit_code == 2, (spec, result.output)
        text = _flat(result.output)
        assert "--pax" in text, (spec, text)
        if spec.strip():
            assert spec in text, (spec, text)
    assert calls == []


def test_pax_beside_a_count_flag_is_a_usage_error_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search(monkeypatch)
    for flag in _FLAGS:
        result = _search_cli("--pax", "1A", flag, "1")
        assert result.exit_code == 2, (flag, result.output)
        text = _flat(result.output)
        assert "--pax" in text, (flag, text)
        assert flag in text, (flag, text)
    both = _flat(_search_cli("--pax", "1A", "--adults", "1", "--inf-lap", "1").output)
    assert "--adults" in both, both
    assert "--inf-lap" in both, both
    assert calls == []


def test_calendar_pax_is_the_pax_the_flags_give(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_calendar(monkeypatch)
    assert _invoke(*_CALENDAR, "--pax", "2A1C1S1Y").exit_code == 0
    assert (
        _invoke(
            *_CALENDAR, "--adults", "2", "--children", "1", "--seniors", "1", "--youth", "1"
        ).exit_code
        == 0
    )
    [by_pax, by_flags] = seen
    assert by_pax.options.pax == Pax(adults=2, children=1, seniors=1, youth=1)
    assert by_pax.options == by_flags.options


def test_calendar_and_detail_refuse_an_infant_and_a_flag_beside_pax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_calendar(monkeypatch)
    sent = _stub_detail(monkeypatch)
    for route in (_CALENDAR, _DETAIL):
        infant = _invoke(*route, "--pax", "1A1IL")
        assert infant.exit_code == 2, (route[0], infant.output)
        assert "1A1IL" in _flat(infant.output), (route[0], infant.output)
        beside = _invoke(*route, "--pax", "1A", "--children", "1")
        assert beside.exit_code == 2, (route[0], beside.output)
        text = _flat(beside.output)
        assert "--pax" in text
        assert "--children" in text
    assert seen == []
    assert sent == []


def test_detail_pax_is_the_pax_the_flags_give(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub_detail(monkeypatch)
    assert _invoke(*_DETAIL, "--pax", "0A2C").exit_code == 0
    assert _invoke(*_DETAIL, "--adults", "0", "--children", "2").exit_code == 0
    [by_pax, by_flags] = sent
    assert by_pax.options.pax == Pax(adults=0, children=2)
    assert by_pax.options == by_flags.options


def test_the_help_and_the_docs_name_pax() -> None:
    for command in ("search", "calendar", "detail"):
        result = _invoke(command, "--help")
        assert result.exit_code == 0, result.output
        assert "--pax" in result.output, command
    for path in ("README.md", ".claude/skills/flight-search/SKILL.md"):
        text = (_ROOT / path).read_text(encoding="utf-8")
        assert "--pax" in text, path
        assert "2A1C1IL" in text, path


def test_pax_counts_outside_a_command_counts_no_flag_as_typed() -> None:
    pax_counts = getattr(cli, "_pax_counts", None)
    assert pax_counts is not None
    assert pax_counts("2A1C1IL", (1, 0, 0, 0, 0, 0), infants=True) == (2, 1, 0, 0, 0, 1)


def test_a_spec_with_a_letter_or_digit_outside_ascii_is_a_usage_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search(monkeypatch)
    # Each folds or reads as an ASCII code or digit under Unicode rules: dotted
    # capital I, dotless small i, long s, Arabic-Indic two, fullwidth two.
    for spec in ("1\u0130S", "1A1\u0130L", "1\u0131L", "1\u017f", "\u0662A", "\uff12A"):
        result = _search_cli("--pax", spec)
        assert result.exit_code == 2, (spec, result.output)
        text = _flat(result.output)
        assert "--pax" in text, (spec, text)
        assert spec in text, (spec, text)
    assert calls == []


def test_calendar_and_detail_help_offer_no_infant_code_for_pax() -> None:
    rows: dict[str, str] = {}
    for command in ("search", "calendar", "detail"):
        result = CliRunner().invoke(cli.app, [command, "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        text = _flat(result.stdout)
        start = text.index("--pax")
        rows[command] = text[start : text.index("--adults", start)]
    assert re.search(r"\bIL\b", rows["search"]), rows["search"]
    for command in ("calendar", "detail"):
        assert re.search(r"\bI[SL]\b|infant", rows[command]) is None, rows[command]


def test_pax_beside_all_six_count_flags_names_every_one(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_search(monkeypatch)
    typed = [arg for flag in _FLAGS for arg in (flag, "0")]
    result = _search_cli("--pax", "2A", *typed)
    assert result.exit_code == 2, result.output
    text = _flat(result.output)
    for flag in _FLAGS:
        assert re.search(rf"{flag}(?![\w-])", text), (flag, text)
    assert calls == []


def test_a_count_past_ints_digit_cap_is_a_usage_error_on_each_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search(monkeypatch)
    seen = _stub_calendar(monkeypatch)
    sent = _stub_detail(monkeypatch)
    # `int` refuses a string of more than 4300 digits.
    spec = "9" * 4301 + "A"
    for result in (
        _search_cli("--pax", spec),
        _invoke(*_CALENDAR, "--pax", spec),
        _invoke(*_DETAIL, "--pax", spec),
    ):
        assert result.exit_code == 2, (result.exception, result.output)
        assert "--pax" in _flat(result.output), result.output
    assert calls == []
    assert seen == []
    assert sent == []
    # Leading zeros are not part of the count's length.
    assert _search_cli("--pax", "0" * 4301 + "1A").exit_code == 0
    [kw] = calls
    assert kw["opts"].pax == Pax(adults=1)
