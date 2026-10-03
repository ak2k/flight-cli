# pyright: reportPrivateUsage=false
"""Matrix's two date options a slice: a flexible date (`--flex`, the form's "Or
day before", "Or day after", "+/- 1 day" and "+/- 2 days") and an arrival date
(`--arrive` in place of `--dep`). Google Flights takes neither, so each sends the
search to Matrix with its reason named."""

from __future__ import annotations

from datetime import date, timedelta

from flight_cli.domain import Leg


def _dep() -> date:
    return date.today() + timedelta(days=45)


def test_leg_of_takes_the_date_options() -> None:
    leg = Leg.of("JFK", "LHR", _dep(), date_minus=2, date_plus=2, is_arrival_date=True)
    assert (leg.date_minus, leg.date_plus, leg.is_arrival_date) == (2, 2, True)
    plain = Leg.of("JFK", "LHR", _dep())
    assert (plain.date_minus, plain.date_plus, plain.is_arrival_date) == (0, 0, False)
