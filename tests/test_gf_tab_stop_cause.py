# pyright: reportPrivateUsage=false
"""The throttle that stopped a paged search is named, whichever page's tab met it.

Google is faked as in `test_gf_chunked_search`: the Example 6 one-way search,
four pages, one Cheapest-tab request per page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import capture_err
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import GfThrottledError, GfUpstreamStatusError
from test_gf_chunked_search import (
    _EX6_FROM,
    _EX6_PAGES,
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

_REFUSED = "Google Flights returned HTTP 503"
_WALL = "Google Flights rate-limited"


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)


def test_a_throttled_tab_on_a_later_page_is_named_beside_an_earlier_pages_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 1's tab is refused for its own URL; page 2's tab is throttled, which
    stops the search. The tab line names the throttle and page 1's refusal is
    still said, on its own line. Red at the base: the throttle was said nowhere."""
    _no_backoff(monkeypatch)
    google = _Google(
        _EX6_PAIRS,
        refuse_cheapest=lambda page: (
            GfUpstreamStatusError(503)
            if page == _EX6_PAGES[0]
            else GfThrottledError("rate-limited")
            if page == _EX6_PAGES[1]
            else None
        ),
    )
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        *("--backend", "gflight", "--fast", "--format", "json"),
    )
    assert result.exit_code == 0, result.output
    printed = _flat(buf.getvalue())
    for n in (3, 4):
        assert _missing(n, "not asked after page 2 stopped the search") in printed, printed
    assert printed.count(f"Itineraries on separate tickets not read: {_WALL}.") == 1, printed
    assert printed.count(_WALL) == 1, printed
    assert printed.count("Google Flights page 1 of 4") == 1, printed
    assert f"itineraries on separate tickets not read: {_REFUSED}." in printed, printed


def test_a_throttled_return_is_named_once_beside_an_earlier_pages_refused_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 2's pin is throttled, so its page line names it and pages 3 and 4
    read no tab. Page 1's tab is refused: the throttle is named once and the
    tab line stays page 1's refusal. Green at the base."""
    _no_backoff(monkeypatch)

    def refuse(page: Page) -> Exception | None:
        pinned = google.calls[-1][1] is not None
        return GfThrottledError("rate-limited") if page == _EX6_PAGES[1] and pinned else None

    google = _Google(
        _EX6_PAIRS,
        refuse=refuse,
        refuse_cheapest=lambda page: GfUpstreamStatusError(503) if page == _EX6_PAGES[0] else None,
    )
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        *("--backend", "gflight", "--fast", "--format", "json", "-n", "200"),
        ret=True,
    )
    assert result.exit_code == 0, result.output
    printed = _flat(buf.getvalue())
    assert _missing(2, _WALL) in printed, printed
    assert printed.count(_WALL) == 1, printed
    assert printed.count(f"Itineraries on separate tickets not read: {_REFUSED}.") == 1, printed
