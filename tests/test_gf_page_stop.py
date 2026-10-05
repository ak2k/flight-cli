# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A throttle that ends a page's pins after some were served stops the later pages.

Google is faked as in `test_gf_chunked_search`: the Example 6 round trip, four
pages, every GET throttled from page 1's third GET on, which is its second pin."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from conftest import capture_err
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import GfThrottledError
from test_gf_chunked_search import (
    _EX6_FROM,
    _EX6_PAIRS,
    _EX6_TO,
    Page,
    _flat,
    _Google,
    _missing,
    _search,
)

if TYPE_CHECKING:
    import pytest


def test_a_throttle_after_a_served_pin_stops_the_later_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: page 1's stop counted as an answer, so page 2 spent a
    second throttle ladder and took the blame for the stop."""

    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)
    first: Page = (_EX6_FROM[:4], _EX6_TO[:7])
    gets: list[Page] = []
    throttled: list[bool] = []

    def refuse(page: Page) -> Exception | None:
        gets.append(page)
        if gets.count(first) >= 3:
            throttled.append(True)
        return GfThrottledError("rate-limited") if throttled else None

    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS), refuse=refuse)
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    # Four outbounds, then only page 1's pins: no GET on any other page after.
    assert [page for page, _ in google.calls[4:] if page != first] == []
    first_numbers = {
        i for i, (o, d) in enumerate(_EX6_PAIRS) if o in _EX6_FROM[:4] and d in _EX6_TO[:7]
    }
    doc = json.loads(result.stdout)
    assert len(doc) == 2  # the one pin served before the throttle, its two returns
    assert {int(pair[0]["legs"][0]["flight_number"]) for pair in doc} <= first_numbers
    printed = _flat(buf.getvalue())
    assert _missing(1, "Google Flights rate-limited") in printed, printed
    assert _missing(2, "Google Flights rate-limited") not in printed, printed
    returns = "its returns were not asked after page 1 stopped the search"
    for n in (2, 3, 4):
        assert _missing(n, returns) in printed, printed


def test_a_round_trip_that_is_not_throttled_asks_every_page_and_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base: with no stop, every page is asked and pinned as before."""

    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)
    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS))
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(google.pages()) == 4
    assert [pin for _, pin in google.calls[:4]] == [None] * 4
    # The default `-n 10` pins the ten cheapest outbounds across the pages.
    assert len([pin for _, pin in google.calls if pin is not None]) == gfid.pinned_fanout(10)
    assert "is missing" not in _flat(buf.getvalue())
