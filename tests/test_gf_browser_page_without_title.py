# pyright: reportPrivateUsage=false
"""A failed click's page sentence survives a title that cannot be read.

A page with no `<title>`, or a renderer that stalls on it, fails the title
read after the snapshot read fine. The URL and the buttons are still what the
page showed, so the sentence keeps them and says the title was unread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

import pytest

from flight_cli._gf_errors import BROWSER_DEFAULT_REMEDY
from test_gf_browser_click_failure import (
    _FINAL_URL,
    _GRAPH,
    _RPC_URL,
    _START_URL,
    _failed_click,
    _Page,
    _session,
)

if TYPE_CHECKING:
    from flight_cli import _gf_browser as gfb


def _stalled(snapshot: str) -> _Page:
    page = _Page(snapshot=snapshot)
    page.title_stalls = True
    return page


def test_a_page_whose_title_cannot_be_read_still_names_its_url_and_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _failed_click(monkeypatch, _stalled('- button "Accept all"\n'))
    text = str(error)
    assert f'The page showed URL {_FINAL_URL}, title unread, visible buttons: "Accept all".' in text
    assert "could not read the page afterward" not in text


def test_the_whole_refusal_keeps_the_click_failure_the_sentence_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _failed_click(monkeypatch, _stalled('- button "Reject all"\n- button "Accept all"\n'))
    assert str(error) == (
        "Chrome could not click 'Price graph' on Google Flights' page: Timeout 20000ms exceeded. "
        f"The page showed URL {_FINAL_URL}, title unread, "
        'visible buttons: "Reject all", "Accept all". '
        f"{BROWSER_DEFAULT_REMEDY}"
    )
    assert error.remedy == BROWSER_DEFAULT_REMEDY


def test_an_unread_title_on_a_page_with_no_buttons_says_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _failed_click(monkeypatch, _stalled('- heading "Oops"\n'))
    assert f"title unread, no visible buttons. {BROWSER_DEFAULT_REMEDY}" in str(error)


class _StoppedTitle:
    """A title read that fails because the driver was killed under it."""

    def __init__(self, page: _StoppedPage) -> None:
        self._page = page

    @property
    def first(self) -> _StoppedTitle:
        return self

    def text_content(self, *, timeout: float) -> str:
        self._page.session._dead = True
        raise RuntimeError("Target closed\ncall log:\n  - reading the title")


class _StoppedPage(_Page):
    session: gfb.GfBrowserSession

    @override
    def locator(self, selector: str) -> Any:
        return _StoppedTitle(self)


def test_a_title_read_that_fails_because_we_stopped_the_driver_is_the_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _StoppedPage(snapshot='- button "Accept all"\n')
    page.session = _session(monkeypatch, page)
    with pytest.raises(KeyboardInterrupt):
        page.session.capture(_START_URL, lambda url: url == _RPC_URL, click=_GRAPH)
