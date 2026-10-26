# pyright: reportPrivateUsage=false
"""The throttle that stopped a paged search is named, whichever page's tab met it.

Google is faked as in `test_gf_chunked_search`: the Example 6 one-way search,
four pages, one Cheapest-tab request per page."""

from __future__ import annotations

from pathlib import Path
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


def test_a_refused_tab_of_a_later_page_is_said_on_a_line_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages 1 and 3 refuse their tabs for their own URLs and nothing stops the
    search. The tab line names page 1's refusal and page 3's is said on a line of
    its own. Red at the base: page 3's refusal was said nowhere."""
    refused: dict[Page, Exception] = {
        _EX6_PAGES[0]: GfUpstreamStatusError(503),
        _EX6_PAGES[2]: GfUpstreamStatusError(503),
    }
    google = _Google(_EX6_PAIRS, refuse_cheapest=refused.get)
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
    assert printed.count(f"Itineraries on separate tickets not read: {_REFUSED}.") == 1, printed
    assert printed.count("Google Flights page 1 of 4") == 0, printed
    assert printed.count("Google Flights page 2 of 4") == 0, printed
    assert printed.count("Google Flights page 3 of 4") == 1, printed
    assert f"itineraries on separate tickets not read: {_REFUSED}." in printed, printed


def test_a_refused_tab_of_a_page_between_two_others_is_said_beside_a_throttled_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages 1 and 2 refuse their tabs and page 3's tab is throttled, which
    stops the search. The tab line names the throttle and pages 1 and 2 each get
    a line of their own. Red at the base: page 2's refusal was said nowhere."""
    _no_backoff(monkeypatch)
    google = _Google(
        _EX6_PAIRS,
        refuse_cheapest=lambda page: (
            GfUpstreamStatusError(503)
            if page in _EX6_PAGES[:2]
            else GfThrottledError("rate-limited")
            if page == _EX6_PAGES[2]
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
    assert printed.count(f"Itineraries on separate tickets not read: {_WALL}.") == 1, printed
    assert printed.count(_WALL) == 1, printed
    assert printed.count("Google Flights page 1 of 4") == 1, printed
    assert printed.count("Google Flights page 2 of 4") == 1, printed
    assert printed.count(f"itineraries on separate tickets not read: {_REFUSED}.") == 2, printed


def test_the_multi_page_note_says_every_unnamed_refused_tab_has_its_own_line() -> None:
    """The memory note on paged searches states the rule `_PageAsk.displaced` keeps."""
    note = Path(__file__).resolve().parent.parent / "docs/memories/gf_multi_page_legs.md"
    text = " ".join(note.read_text(encoding="utf-8").split())
    assert (
        "Every other page's refused tab that no line names is said on a line of its own "
        "(`_PageAsk.displaced`)." in text
    )
