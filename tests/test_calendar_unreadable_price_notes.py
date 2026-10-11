# pyright: reportPrivateUsage=false
"""A calendar price Matrix sends as no readable number is a dash in the headline
line and counts as unpriced in the note beside the table.

`USD1e999` is `float` infinity: `_price_amount` reads it, and `USDabc`, as None.
The table already prints that cell as a dash; the `overall cheapest:` line and
the `Matrix priced no fare on` note follow the same rule.
"""

from __future__ import annotations

import pytest

from conftest import LITERAL_DATES_NOW
from test_calendar_formats import _lines, _run, _serve
from test_calendar_split import _result

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_STUB = ["--gf-transport", "http"]
_ROUND_TRIP = ["calendar", "JFK", "LAX", "--start", "2026-10-20", "--end", "2026-10-22", "-d", "7"]
_ONE_WAY = ["calendar", "JFK", "LAX", "--start", "2026-10-20", "--end", "2026-10-22", "--one-way"]


@pytest.mark.parametrize("notice", ["USD1e999", "USDabc"])
def test_overall_cheapest_prints_the_no_price_dash_for_an_unreadable_notice(
    notice: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = {d: ("USD300.00", 2, {7: "USD300.00"}) for d in (20, 21, 22)}
    _serve(monkeypatch, _result({10: days}, notice).raw or {})
    result = _run(*_ROUND_TRIP, *_STUB)
    assert result.exit_code == 0, result.output
    (headline,) = (ln for ln in result.stdout.split("\n") if "overall cheapest:" in ln)
    assert headline.startswith("6 solutions  · overall cheapest: —  · window 2026-10-20")
    assert notice[3:] not in result.stdout


def test_the_note_counts_a_round_trip_day_with_an_unreadable_length_price_as_unpriced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = {
        20: ("USD100.00", 2, {7: "USD1e999", 8: "USD100.00"}),
        21: ("USD300.00", 2, {7: "USD300.00"}),
        22: ("USD300.00", 2, {7: "USD300.00"}),
    }
    _serve(monkeypatch, _result({10: days}, "USD100.00").raw or {})
    result = _run(*_ROUND_TRIP, *_STUB)
    assert result.exit_code == 0, result.output
    assert _lines(result.stderr) == [
        "Matrix priced no fare on 1 of 3 departure dates asked for 7-night trips: 2026-10-20."
    ]


def test_the_note_counts_a_one_way_day_with_an_unreadable_price_as_unpriced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = {
        20: ("USD1e999", 2, dict[int, str]()),
        21: ("USD300.00", 2, dict[int, str]()),
        22: ("USD300.00", 2, dict[int, str]()),
    }
    _serve(monkeypatch, _result({10: days}, "USD300.00").raw or {})
    result = _run(*_ONE_WAY, *_STUB)
    assert result.exit_code == 0, result.output
    assert _lines(result.stderr) == [
        "Matrix priced no fare on 1 of 3 departure dates asked: 2026-10-20."
    ]
