# pyright: reportPrivateUsage=false
"""`--sellers`: a non-finite table price is never one a seller beats."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from flight_cli import _gf_booking as gb
from flight_cli import cli


def _options() -> gb.BookingOptions:
    return gb.BookingOptions("USD", (gb.Seller("Kiwi.com", 170, None, False),))


@pytest.mark.parametrize(
    "table",
    [
        pytest.param(["USDnan", "USD300.00"], id="nan-first"),
        pytest.param(["USD300.00", "USDnan"], id="nan-last"),
        pytest.param([None, "USDnan"], id="matrix-only-nan"),
        pytest.param(["USDinf", "USD300.00"], id="inf-first"),
        pytest.param(["USD300.00", "USD-inf"], id="negative-inf-last"),
    ],
)
def test_a_non_finite_table_price_is_beaten_by_no_seller(table: list[str | None]) -> None:
    assert cli._undercut(_options(), table) is None


def test_the_block_prints_no_beat_line_for_a_nan_table_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=250, no_color=True))
    cli._render_booking_options(_options(), n=1, table_prices=[None, "USDnan"], round_trip=False)
    out = buf.getvalue()
    assert "Booking options for #1" in out
    assert "beats the table price" not in out, out
