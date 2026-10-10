# pyright: reportPrivateUsage=false
"""The http fan-out names the way out of a throttle on each cabin's line.

`_run_gflight_multi` prints one line for every cabin it could not serve, and when
every cabin fails that line is the only text the user is given: the caller adds
"All Google Flights cabin queries failed." and no remedy of its own.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from conftest import LITERAL_DATES_NOW, capture_err
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_errors import (
    BROWSER_DEFAULT_REMEDY,
    GfBackendError,
    GfBrowserUnavailableError,
    GfPageShapeError,
    GfThrottledError,
)
from flight_cli.domain import Bags, Cabin, Leg, SearchOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

# The searches built here carry literal travel dates; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_CABINS = (Cabin.COACH, Cabin.BUSINESS)


def _lines_for(
    monkeypatch: pytest.MonkeyPatch, failure: GfBackendError, *, bags: Bags | None = None
) -> Sequence[str]:
    """What the user is told on stderr when every cabin's http query raises `failure`."""
    buf = capture_err(monkeypatch)

    def _raise(*_a: Any, **_kw: Any) -> list[Any]:
        raise failure

    monkeypatch.setattr(gfid, "search_with_ids", _raise)
    out = cli._run_gflight_multi(
        legs=(Leg.of("JFK", "LAX", date(2026, 10, 14)),),
        opts=SearchOptions(cabin=Cabin.COACH, bags=bags),
        cabins=_CABINS,
        top_n=5,
    )
    assert out == {}, "a cabin that raised must not land a column"
    return sorted(buf.getvalue().splitlines())


def test_a_throttled_cabin_line_names_the_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _lines_for(monkeypatch, GfThrottledError("x"))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights rate-limited. Wait a moment and retry, "
        "use --gf-transport browser, or use --backend matrix."
        for cab in sorted(_CABINS, key=lambda c: c.value)
    ]


def test_a_throttled_cabin_line_under_bags_drops_them(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _lines_for(monkeypatch, GfThrottledError("x"), bags=Bags(checked=1))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights rate-limited. Wait a moment and retry, "
        "use --gf-transport browser, or drop --bags to search Matrix, which prices no bags."
        for cab in sorted(_CABINS, key=lambda c: c.value)
    ]


def test_a_refusal_with_no_remedy_of_its_own_keeps_its_note_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base: only the throttle's note leaves the user without a next move."""
    lines = _lines_for(monkeypatch, GfPageShapeError("x"))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights' page shape changed."
        for cab in sorted(_CABINS, key=lambda c: c.value)
    ]


def test_a_browser_unavailable_cabin_line_under_bags_keeps_its_note_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `--bags` only the remedy changes: a refusal with none prints the same line."""
    lines = _lines_for(monkeypatch, GfBrowserUnavailableError("no chrome"), bags=Bags(checked=1))
    assert lines == [
        f"Google Flights {cab.value}: Google Flights' browser rung is unavailable — "
        f"no chrome {BROWSER_DEFAULT_REMEDY}."
        for cab in sorted(_CABINS, key=lambda c: c.value)
    ]
