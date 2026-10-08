# pyright: reportPrivateUsage=false
"""A remote value that ends in a backslash prints as it arrived.

`rich.markup.escape` doubles a lone trailing backslash so a `[` after it cannot
swallow it, and the markup parser collapses a backslash pair only in front of a
`[`. A value that ends a table cell or a printed line therefore showed two
backslashes, and a run of three or more was left alone and then escaped the
closing tag after it."""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.table import Table

from flight_cli import _gf_booking as gb
from flight_cli import cli
from flight_cli._console_text import safe_text

_VALUES = ["Back\\", "Back\\\\", "Back\\\\\\", "\\", "[red]\\", "a\\b\\"]


def _render(markup: str) -> str:
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None, highlight=False).print(markup)
    return buf.getvalue()


@pytest.mark.parametrize("value", _VALUES)
def test_a_value_ending_in_backslash_prints_whole_at_the_end_of_a_line(value: str) -> None:
    assert _render(safe_text(value)) == f"{value}\n"


@pytest.mark.parametrize("value", _VALUES)
def test_a_value_ending_in_backslash_prints_whole_before_text_and_before_a_tag(
    value: str,
) -> None:
    assert _render(f"{safe_text(value)} beats") == f"{value} beats\n"
    assert _render(f"[red]{safe_text(value)}[/] beats") == f"{value} beats\n"


@pytest.mark.parametrize("value", _VALUES)
def test_a_value_ending_in_backslash_fills_a_table_cell_whole(value: str) -> None:
    table = Table(show_header=False, box=None)
    table.add_row(safe_text(value))
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None).print(table)
    assert buf.getvalue().strip() == value


def test_an_exception_named_with_a_trailing_backslash_prints_whole() -> None:
    cls = type("Back\\", (Exception,), {})
    assert _render(safe_text(cls(""))) == "Back\\\n"


def test_a_seller_named_with_a_trailing_backslash_prints_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=250, color_system=None))
    seller = gb.Seller("Back\\", 170.0, None, True, bags=(gb.BagFee("carry-on", 1, 0.0),))
    cli._render_booking_options(
        gb.BookingOptions("USD", (seller,)), n=1, table_prices=[], round_trip=False
    )
    lines = buf.getvalue().splitlines()
    assert any(line.startswith("│") and "│ Back\\ " in line for line in lines), lines
    assert "1 Back\\: carry-on free" in lines, lines
