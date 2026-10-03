# pyright: reportPrivateUsage=false
"""Matrix's two date options a slice: a flexible date (`--flex`, the form's "Or
day before", "Or day after", "+/- 1 day" and "+/- 2 days") and an arrival date
(`--arrive` in place of `--dep`). Google Flights takes neither, so each sends the
search to Matrix with its reason named."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import typer

from flight_cli import cli
from flight_cli.domain import Bags, Leg
from test_backend_dispatch import _call


def _dep() -> date:
    return date.today() + timedelta(days=45)


def test_leg_of_takes_the_date_options() -> None:
    leg = Leg.of("JFK", "LHR", _dep(), date_minus=2, date_plus=2, is_arrival_date=True)
    assert (leg.date_minus, leg.date_plus, leg.is_arrival_date) == (2, 2, True)
    plain = Leg.of("JFK", "LHR", _dep())
    assert (plain.date_minus, plain.date_plus, plain.is_arrival_date) == (0, 0, False)


# ──────────────────────────────── the backend ───────────────────────────────


@pytest.mark.parametrize(
    ("option", "reason"),
    [
        ({"flex": (1, 0)}, "a flexible outbound date (or day before)"),
        ({"flex": (0, 1)}, "a flexible outbound date (or day after)"),
        ({"flex": (1, 1)}, "a flexible outbound date (+/- 1 day)"),
        ({"flex": (2, 2)}, "a flexible outbound date (+/- 2 days)"),
        ({"arrive": True}, "an outbound arrival date"),
        ({"return_flex": (0, 1)}, "a flexible return date (or day after)"),
        ({"return_arrive": True}, "a return arrival date"),
    ],
)
def test_auto_names_the_date_option_and_uses_matrix(
    option: dict[str, object], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _call(cli.BACKEND_AUTO, **option) == cli.BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert f"Using Matrix: Google Flights can't serve {reason}." in printed, printed


def test_auto_names_every_date_option_in_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        _call(flex=(1, 1), arrive=True, return_flex=(0, 1), return_arrive=True)
        == cli.BACKEND_MATRIX
    )
    printed = " ".join(capsys.readouterr().err.split())
    assert (
        "Using Matrix: Google Flights can't serve a flexible outbound date (+/- 1 day), an "
        "outbound arrival date, a flexible return date (or day after) and a return arrival "
        "date." in printed
    ), printed


def test_gflight_refuses_a_date_option_naming_it() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(cli.BACKEND_GFLIGHT, flex=(2, 2))
    assert str(excinfo.value) == (
        "--backend gflight can't serve this request: a flexible outbound date (+/- 2 days). "
        "Drop it, or use --backend matrix."
    )


def test_bags_refuse_a_date_option_naming_it() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(bags=Bags(checked=1), return_arrive=True)
    assert str(excinfo.value) == (
        "--bags needs Google Flights, which can't serve a return arrival date. Drop it, or "
        "drop --bags to search Matrix, which prices no bags."
    )
