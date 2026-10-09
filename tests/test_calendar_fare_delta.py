"""The calendar's `min` cell is green when the day is well under the window median."""

from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import CalendarResult

if TYPE_CHECKING:
    import pytest

_SGR = re.compile(r"\x1b\[[0-9;]*m")
_GREEN = "\x1b[32m"


def _result(prices: list[str]) -> CalendarResult:
    days: list[dict[str, Any]] = [
        {"date": 10 + i, "solutionCount": 3, "minPrice": price} for i, price in enumerate(prices)
    ]
    return CalendarResult.from_api(
        {
            "solutionCount": 3 * len(days),
            "currencyNotice": {"ext": {"price": prices[0]}},
            "calendar": {"months": [{"month": 10, "weeks": [{"days": days}]}]},
        }
    )


def _render(
    monkeypatch: pytest.MonkeyPatch,
    prices: list[str],
    *,
    no_color: bool = False,
) -> str:
    buffer = io.StringIO()
    # Pinned rather than inherited: `TERM=dumb` and `NO_COLOR` both leave rich
    # emitting no SGR, which would make every assertion below vacuous.
    monkeypatch.setattr(
        cli,
        "console",
        Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            no_color=no_color,
            width=400,
        ),
    )
    cli._render_calendar(  # pyright: ignore[reportPrivateUsage] — the renderer IS the unit
        _result(prices),
        dmin=7,
        dmax=7,
        origin=("JFK",),
        destination=("LAX",),
        sd=date(2026, 10, 10),
        ed=date(2026, 10, 20),
        round_trip=False,
    )
    return buffer.getvalue()


def _green_amounts(written: str) -> list[str]:
    return re.findall(re.escape(_GREEN) + r"([0-9.,]+)\x1b\[0m", written)


def test_a_day_far_under_the_median_is_green_and_no_other_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _render(
        monkeypatch,
        ["USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD70.00"],
    )
    assert _green_amounts(written) == ["70.00"]
    assert written.count(_GREEN) == 1


def test_the_cutoff_is_inclusive_at_twenty_percent_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _render(
        monkeypatch,
        ["USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD80.00", "USD81.00"],
    )
    assert _green_amounts(written) == ["80.00"]


def test_an_even_window_uses_the_mean_of_the_middle_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Median 105, cutoff 84: 70 and 80 are green, 90 is not. The lower middle
    # value (90) would cut at 72 and leave 80 plain.
    written = _render(
        monkeypatch,
        ["USD70.00", "USD80.00", "USD90.00", "USD120.00", "USD130.00", "USD140.00"],
    )
    assert sorted(_green_amounts(written)) == ["70.00", "80.00"]


def test_fewer_than_five_priced_days_colors_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _render(monkeypatch, ["USD100.00", "USD100.00", "USD100.00", "USD50.00"])
    assert _GREEN not in written
    assert "50.00" in written


def test_another_currency_is_neither_a_baseline_nor_colored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Counted in one window, the 10 EUR would drag the median to 100 and read
    # as a deal; it is a different unit and stays plain.
    written = _render(
        monkeypatch,
        ["USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD100.00", "EUR10.00"],
    )
    assert _GREEN not in written


def test_another_currency_stays_out_of_the_median(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The USD days alone have median 100.00, so both 75.00 days are deals; with
    # the EUR day counted the median is 87.50 and neither is.
    written = _render(
        monkeypatch,
        ["USD100.00", "USD100.00", "USD100.00", "USD75.00", "USD75.00", "EUR1.00"],
    )
    assert _green_amounts(written) == ["75.00", "75.00"]
    assert written.count(_GREEN) == 2


def test_a_day_exactly_twenty_percent_under_to_the_cent_is_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As floats 40.20 * 100 > 50.25 * 80; in whole cents the two are equal.
    written = _render(
        monkeypatch,
        ["USD50.25", "USD50.25", "USD50.25", "USD50.25", "USD50.25", "USD40.20"],
    )
    assert _green_amounts(written) == ["40.20"]


def test_no_color_prints_the_same_text_without_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = ["USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD100.00", "USD70.00"]
    colored = _render(monkeypatch, prices)
    plain = _render(monkeypatch, prices, no_color=True)
    assert _GREEN not in plain
    assert _SGR.sub("", colored) == _SGR.sub("", plain)
    assert "70.00" in plain


def test_calendar_help_states_the_rule() -> None:
    result = CliRunner().invoke(cli.app, ["calendar", "--help"], env={"COLUMNS": "200"})
    flat = " ".join(result.stdout.split())
    assert "green" in flat
    assert "20%" in flat
    assert "median" in flat
    assert "5 priced days" in flat


def test_readme_states_the_rule() -> None:
    readme = " ".join((Path(__file__).resolve().parent.parent / "README.md").read_text().split())
    assert "at least 20% under the median of the window's priced days" in readme
    assert "fewer than 5 such days" in readme
