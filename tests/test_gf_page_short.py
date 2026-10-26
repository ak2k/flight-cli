# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A page whose pins stopped after serving is named short, not missing.

Google is faked as in `test_gf_page_stop`: the Example 6 round trip, four pages,
every GET throttled from page 1's third GET on, which is its second pin."""

from __future__ import annotations

import json
from pathlib import Path
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


def test_a_page_whose_pins_stopped_after_serving_is_named_short_with_its_skipped_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 1 printed its two rows. It is named `is short`, with the return
    boards its pin loop skipped counted as the log line counts them: the first
    of its five pins was served, so four were skipped. Red at the base: its line
    ended at the reason."""

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
    why = "Google Flights rate-limited; 4 of 5 return boards skipped"
    short = _missing(1, why).replace("is missing", "is short")
    assert short in printed, printed
    assert _missing(1, "Google Flights rate-limited") not in printed, printed
    returns = "its returns were not asked after page 1 stopped the search"
    for n in (2, 3, 4):
        assert _missing(n, returns) in printed, printed


def test_the_multi_page_memo_says_a_short_page_counts_its_skipped_return_boards() -> None:
    """The memo that describes the page lines gives the count the line ends with."""
    root = Path(__file__).resolve().parent.parent
    memo = " ".join((root / "docs/memories/gf_multi_page_legs.md").read_text().split())
    assert "`is short: <reason>; N of M return boards skipped`" in memo
