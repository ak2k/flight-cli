# pyright: reportPrivateUsage=false
"""A trip-length option Matrix prices at no readable number is never a day's trip length.

`USDabc` has no digits and `USD1e999` is `float` infinity: `_cheapest_option` raised
`ValueError` on the first and chose the second as the cheapest only when it stood
alone, and the table printed `1e999` in the option's `Nn` cell.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from flight_cli import cli
from flight_cli.models import CalendarDay, CalendarResult, DurationOption

_SD = date(2026, 10, 20)
_ED = date(2026, 10, 26)


def _day(*options: tuple[int, str]) -> CalendarDay:
    return CalendarDay.model_validate(
        {
            "date": 20,
            "minPrice": "USD100.00",
            "solutionCount": 1,
            "tripDuration": {
                "options": [{"tripLength": n, "minPrice": price} for n, price in options]
            },
        }
    )


def _result(day: dict[str, Any] | CalendarDay) -> CalendarResult:
    body = day.model_dump(by_alias=True) if isinstance(day, CalendarDay) else day
    return CalendarResult.from_api(
        {
            "solutionCount": 1,
            "currencyNotice": {"ext": {"price": "USD100.00"}},
            "calendar": {"months": [{"month": 10, "year": 2026, "weeks": [{"days": [body]}]}]},
        }
    )


@pytest.mark.parametrize(
    ("price", "value"),
    [
        ("USD1e999", None),
        ("USD-1e999", None),
        ("USDabc", None),
        ("", None),
        ("USD100.00", 100.0),
    ],
)
def test_an_option_priced_at_no_readable_number_has_no_price_value(
    price: str, value: float | None
) -> None:
    assert DurationOption.model_validate({"tripLength": 7, "minPrice": price}).price_value == value


@pytest.mark.parametrize("bad", ["USDabc", "USD1e999"])
def test_the_cheapest_option_skips_an_option_with_no_readable_price(bad: str) -> None:
    option = cli._cheapest_option(_day((7, bad), (8, "USD100.00")))
    assert option is not None
    assert option.trip_length == 8


def test_a_day_whose_every_option_is_unreadable_has_no_cheapest_option() -> None:
    assert cli._cheapest_option(_day((7, "USDabc"), (8, "USD1e999"))) is None


def test_the_table_prints_the_no_price_cell_for_an_option_priced_at_infinity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._render_calendar(
        _result(_day((7, "USD1e999"), (8, "USD100.00"))),
        dmin=7,
        dmax=8,
        origin=("JFK",),
        destination=("LAX",),
        sd=_SD,
        ed=_ED,
        round_trip=True,
    )
    out = capsys.readouterr().out
    assert "1e999" not in out
    (row,) = (ln for ln in out.split("\n") if ln.startswith("│") and "20" in ln.split("│")[1])
    assert [cell.strip() for cell in row.strip("│").split("│")] == [
        "20",
        "100.00",
        "—",
        "100.00",
        "1",
    ]


def test_the_envelope_returns_on_the_cheapest_readable_length() -> None:
    day = _day((7, "USDabc"), (8, "USD100.00"))
    (row,) = cli._calendar_envelope_rows(_result(day), sd=_SD, ed=_ED)
    assert row.return_date == date(2026, 10, 28)
