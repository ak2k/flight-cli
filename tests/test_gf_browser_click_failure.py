"""A capture whose click fails says what the page showed.

A click that times out reads the same whether Google drew no "Price graph"
button, drew a consent page, or redirected elsewhere. The refusal therefore
carries the page's final URL, its title and the visible buttons' names, read
from the page after the failure.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_calgraph, cli
from flight_cli._gf_errors import BROWSER_DEFAULT_REMEDY, GfBrowserUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

_START_URL = "https://www.google.com/travel/flights?tfs=abc"
_FINAL_URL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
_RPC_URL = "https://www.google.com/_/FlightsFrontendUi/data/GetCalendarGraph?rpcids=x"
_GRAPH = gfb.Control("button", "Price graph")
_CLICK_TIMEOUT = "Timeout 20000ms exceeded.\ncall log:\n  - waiting for locator"


class _Nav:
    url = _START_URL
    status = 200


class _Locator:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def click(self, *, timeout: float) -> None:
        self._page.click_timeouts.append(timeout)
        if self._page.clicks_fine:
            return
        raise RuntimeError(_CLICK_TIMEOUT)


class _Page:
    """The slice of patchright's page a failed click reads afterward."""

    def __init__(self, *, snapshot: str, title: str = "Before you continue") -> None:
        self.url = _START_URL
        self._snapshot = snapshot
        self._title = title
        self.snapshot_error: Exception | None = None
        self.clicks_fine = False
        self.click_timeouts: list[float] = []
        self.snapshot_timeouts: list[float] = []

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        pass

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None:
        pass

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _Nav:
        self.url = _FINAL_URL
        return _Nav()

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _Locator:
        return _Locator(self)

    def wait_for_timeout(self, timeout: float) -> None:
        time.sleep(timeout / 1000)

    def title(self) -> str:
        return self._title

    def aria_snapshot(self, *, timeout: float) -> str:
        self.snapshot_timeouts.append(timeout)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self._snapshot


def _session(monkeypatch: pytest.MonkeyPatch, page: _Page) -> gfb.GfBrowserSession:

    def _ensure_page(_self: gfb.GfBrowserSession) -> _Page:
        return page

    monkeypatch.setattr(gfb.GfBrowserSession, "_ensure_page", _ensure_page)
    return gfb.GfBrowserSession(headed=False)


def _failed_click(monkeypatch: pytest.MonkeyPatch, page: _Page) -> GfBrowserUnavailableError:
    session = _session(monkeypatch, page)
    with pytest.raises(GfBrowserUnavailableError) as raised:
        session.capture(_START_URL, lambda url: url == _RPC_URL, click=_GRAPH)
    return raised.value


def test_a_failed_click_names_the_url_title_and_buttons_the_page_showed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(
        snapshot='- heading "Before you continue"\n- button "Reject all"\n- button "Accept all"\n'
    )
    error = _failed_click(monkeypatch, page)
    assert str(error) == (
        "Chrome could not click 'Price graph' on Google Flights' page: Timeout 20000ms exceeded. "
        f'The page showed URL {_FINAL_URL}, title "Before you continue", '
        'visible buttons: "Reject all", "Accept all". '
        f"{BROWSER_DEFAULT_REMEDY}"
    )
    assert error.remedy == BROWSER_DEFAULT_REMEDY


def test_the_read_after_a_failed_click_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _Page(snapshot='- button "Accept all"\n')
    _failed_click(monkeypatch, page)
    [timeout] = page.snapshot_timeouts
    assert 0 < timeout <= gfb._SNAPSHOT_TIMEOUT_MS  # pyright: ignore[reportPrivateUsage]


def test_a_click_that_works_reads_nothing_from_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _Page(snapshot='- button "Accept all"\n')
    page.clicks_fine = True
    session = _session(monkeypatch, page)
    with pytest.raises(GfBrowserUnavailableError, match="in time"):
        session.capture(_START_URL, lambda url: url == _RPC_URL, click=_GRAPH, timeout_s=0.05)
    assert page.click_timeouts
    assert page.snapshot_timeouts == []


def test_a_page_that_cannot_be_read_leaves_the_click_failure_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(snapshot="")
    page.snapshot_error = RuntimeError("Target closed\ncall log:\n  - taking snapshot")
    error = _failed_click(monkeypatch, page)
    assert str(error) == (
        "Chrome could not click 'Price graph' on Google Flights' page: Timeout 20000ms exceeded. "
        "Chrome could not read the page afterward: Target closed. "
        f"{BROWSER_DEFAULT_REMEDY}"
    )


def test_a_page_with_no_buttons_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    error = _failed_click(monkeypatch, _Page(snapshot='- heading "Oops"\n- link "Home"\n'))
    assert f'title "Before you continue", no visible buttons. {BROWSER_DEFAULT_REMEDY}' in str(
        error
    )


def test_only_the_first_buttons_are_named_and_the_rest_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = gfb._SHOWN_BUTTONS  # pyright: ignore[reportPrivateUsage]
    snapshot = "".join(f'- button "b{i}"\n' for i in range(shown + 3))
    error = _failed_click(monkeypatch, _Page(snapshot=snapshot))
    assert f'"b{shown - 1}", and 3 more. ' in str(error)
    assert f'"b{shown}"' not in str(error)


def test_names_and_title_are_unquoted_and_kept_to_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = '- button "Say \\"hi\\"": x\n  - button "Nested one" [disabled]\n'
    page = _Page(snapshot=snapshot, title="Before\n  you\tcontinue " + "x" * 300)
    text = str(_failed_click(monkeypatch, page))
    assert 'visible buttons: "Say "hi"", "Nested one".' in text
    # 200 characters: the 20 of "Before you continue ", 179 x's and the ellipsis.
    assert f'title "Before you continue {"x" * 179}…",' in text
    assert "\n" not in text.split("Timeout 20000ms exceeded.", 1)[1]


def test_the_stalled_graph_line_carries_what_the_page_showed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _failed_click(monkeypatch, _Page(snapshot='- button "Accept all"\n'))
    stalled = _gf_calgraph.GfGraphStalledError(error, loads=2)
    assert stalled.reason == error.reason
    assert cli._graph_failure_text(stalled) == (  # pyright: ignore[reportPrivateUsage]
        "Chrome could not click 'Price graph' on Google Flights' page: Timeout 20000ms exceeded. "
        f'The page showed URL {_FINAL_URL}, title "Before you continue", '
        'visible buttons: "Accept all".'
    )
