# pyright: reportPrivateUsage=false
"""The series runner names the way out of a throttle on each cabin's line.

`_gflight_cabins_in_series` is rung 2's multi-cabin shape. It prints one line for
every cabin it could not serve, and when every cabin fails that line is the only
text the user is given.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from conftest import LITERAL_DATES_NOW, capture_err
from flight_cli import cli
from flight_cli._gf_errors import (
    BROWSER_DEFAULT_REMEDY,
    GfBackendError,
    GfBrowserUnavailableError,
    GfPageShapeError,
    GfThrottledError,
)
from flight_cli.domain import Bags, Cabin, Leg, SearchOptions

# The searches built here carry literal travel dates; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_CABINS = (Cabin.COACH, Cabin.BUSINESS)


def _lines_for(
    monkeypatch: pytest.MonkeyPatch,
    failure: GfBackendError,
    *,
    bags: Bags | None = None,
    served: bool = False,
) -> list[str]:
    """What the user is told on stderr when rung 2 raises `failure` for each cabin.

    `served` lets the first cabin answer, so a rung that never opened is a
    cabin's missing column rather than the fall-through to rung 1."""
    buf = capture_err(monkeypatch)
    asked: list[Cabin] = []

    def _rung(_legs: Any, opts: SearchOptions, *_a: Any, **_kw: Any) -> list[Any]:
        asked.append(opts.cabin)
        if served and len(asked) == 1:
            return []
        raise failure

    monkeypatch.setattr(cli, "_gflight_results", _rung)
    out = cli._gflight_cabins_in_series(
        legs=(Leg.of("JFK", "LAX", date(2026, 10, 14)),),
        opts=SearchOptions(cabin=Cabin.COACH, bags=bags),
        cabins=_CABINS,
        top_n=5,
        gf_headed=False,
    )
    assert out is not None
    assert asked == list(_CABINS)
    return [ln for ln in buf.getvalue().splitlines() if ln.startswith("Google Flights ")]


def test_a_throttled_cabin_line_names_the_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _lines_for(monkeypatch, GfThrottledError("x"))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights rate-limited the browser rung. "
        "It does not retry, so use --backend matrix."
        for cab in _CABINS
    ]


def test_a_throttled_cabin_line_under_bags_drops_them(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _lines_for(monkeypatch, GfThrottledError("x"), bags=Bags(checked=1))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights rate-limited the browser rung. "
        "It does not retry, so drop --bags to search Matrix, which prices no bags."
        for cab in _CABINS
    ]


def test_a_refusal_with_no_remedy_of_its_own_keeps_its_note_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base: only the throttle's note leaves the user without a next move."""
    lines = _lines_for(monkeypatch, GfPageShapeError("x"))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights' page shape changed." for cab in _CABINS
    ]


def test_a_browser_unavailable_cabin_line_keeps_its_note_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its note carries the reason and the remedy, so no tail is added after it."""
    lines = _lines_for(monkeypatch, GfBrowserUnavailableError("no chrome"), served=True)
    assert lines == [
        "Google Flights BUSINESS: Google Flights' browser rung is unavailable — "
        f"no chrome {BROWSER_DEFAULT_REMEDY}"
    ]


def test_a_browser_unavailable_cabin_line_under_bags_drops_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix prices no bags, so the way out under `--bags` is dropping them, with one full stop."""
    lines = _lines_for(
        monkeypatch, GfBrowserUnavailableError("no chrome"), bags=Bags(checked=1), served=True
    )
    assert lines == [
        "Google Flights BUSINESS: Google Flights' browser rung is unavailable — no chrome "
        "Retry, or use `--gf-transport http` "
        "(or drop `--bags` to search Matrix, which prices no bags)."
    ]


@pytest.mark.parametrize(
    ("bags", "way_out"),
    [
        (False, "use [bold]--backend matrix[/]"),
        (True, "drop [bold]--bags[/] to search Matrix, which prices no bags"),
    ],
)
def test_the_browser_throttle_refusal_carries_its_way_out_as_a_field(
    bags: bool, way_out: str
) -> None:
    refusal = cli._gf_refusal(GfThrottledError("x"), transport=cli.TRANSPORT_BROWSER, bags=bags)
    assert refusal.remedy == f"It does not retry, so {way_out}."
    assert (
        refusal.message
        == f"[yellow]Google Flights rate-limited the browser rung.[/] {refusal.remedy}"
    )
