# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A page whose pins stopped after serving is named short, not missing.

Google is faked as in `test_gf_page_stop`: the Example 6 round trip, four pages,
every GET throttled from page 1's third GET on, which is its second pin."""

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


def test_a_page_whose_pins_stopped_after_serving_is_named_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: page 1 printed its two rows and was named `is missing`."""

    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)
    first: Page = (_EX6_FROM[:4], _EX6_TO[:7])
    gets: list[Page] = []

    def refuse(page: Page) -> Exception | None:
        gets.append(page)
        return GfThrottledError("rate-limited") if gets.count(first) >= 3 else None

    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS), refuse=refuse)
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        *("--backend", "gflight", "--fast", "--format", "json"),
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 2  # the pin served before the throttle
    printed = _flat(buf.getvalue())
    short = _missing(1, "Google Flights rate-limited").replace("is missing", "is short")
    assert short in printed, printed
    assert _missing(1, "Google Flights rate-limited") not in printed, printed
    returns = "its returns were not asked after page 1 stopped the search"
    for n in (2, 3, 4):
        assert _missing(n, returns) in printed, printed
