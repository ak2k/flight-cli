"""`calendar --fast -d 5-7 --gf-transport http` refuses a range, and names the
rung that serves it."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from flight_cli import cli

_START = date.today() + timedelta(days=60)
_REMEDY = "For the range on Google, run with --gf-transport browser."


def _refusal(*extra: str, days: int = 13, origin: str = "JFK") -> tuple[int, str, str]:
    end = _START + timedelta(days=days)
    args = ["calendar", origin, "LAX", "--start", _START.isoformat(), "--end", end.isoformat()]
    result = CliRunner().invoke(cli.app, [*args, "--no-cache", "--fast", *extra])
    return result.exit_code, result.stdout, " ".join(result.stderr.split())


def test_a_range_refused_over_http_names_the_browser_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_load(*_a: object, **_k: object) -> object:
        raise AssertionError("a refusal loads nothing")

    monkeypatch.setattr(cli, "_run_fast_calendar_grid", _no_load)
    code, out, err = _refusal("--gf-transport", "http", "-d", "5-7")
    assert (code, out) == (1, "")
    assert "this is a trip-length range (5-7 nights). Run without --fast for Matrix." in err
    assert err.endswith(_REMEDY), err


@pytest.mark.parametrize(
    ("extra", "days", "origin"),
    [
        (("-d", "5-7"), 99, "JFK"),
        (("-d", "5-7"), 13, "JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA,FLL,CLT"),
        (("-d", "7", "--routing", "AA+"), 13, "JFK"),
    ],
    ids=["browser-budget-refuses", "browser-airports-refuse", "one-length-filter-is-not-the-range"],
)
def test_the_remedy_is_named_only_where_the_browser_would_serve(
    extra: tuple[str, ...], days: int, origin: str
) -> None:
    code, _, err = _refusal("--gf-transport", "http", *extra, days=days, origin=origin)
    assert code == 1
    assert _REMEDY not in err
