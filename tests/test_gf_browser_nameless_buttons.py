# pyright: reportPrivateUsage=false
"""A failed click's page sentence counts the visible buttons that have no name.

patchright's ARIA snapshot writes a button with no name, or one whose name is
over 900 characters, as `- button` or `- button [disabled]`. The sentence must
not say "no visible buttons" while such buttons are on the page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flight_cli import _gf_browser as gfb
from flight_cli._gf_errors import BROWSER_DEFAULT_REMEDY
from test_gf_browser_click_failure import (
    _FINAL_URL,
    _failed_click,
    _Page,
)

if TYPE_CHECKING:
    import pytest


def _text(monkeypatch: pytest.MonkeyPatch, snapshot: str) -> str:
    return str(_failed_click(monkeypatch, _Page(snapshot=snapshot)))


def test_buttons_with_no_name_are_counted_not_reported_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _text(monkeypatch, "- button\n- button [disabled]\n")
    assert "2 visible buttons without a name" in text
    assert "no visible buttons" not in text
    text = _text(monkeypatch, '- button "A"\n- button\n')
    assert 'visible buttons: "A", and 1 without a name' in text


def test_the_whole_sentence_names_the_buttons_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _text(monkeypatch, '- button "A"\n- button [disabled]\n- button\n') == (
        "Chrome could not click 'Price graph' on Google Flights' page: Timeout 20000ms exceeded. "
        f'The page showed URL {_FINAL_URL}, title "Before you continue", '
        'visible buttons: "A", and 2 without a name. '
        f"{BROWSER_DEFAULT_REMEDY}"
    )


def test_one_button_with_no_name_is_singular(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _text(monkeypatch, "- button [disabled]\n")
    assert (
        f'title "Before you continue", 1 visible button without a name. {BROWSER_DEFAULT_REMEDY}'
        in text
    )


def test_the_count_comes_after_the_more_count(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = gfb._SHOWN_BUTTONS
    snapshot = "".join(f'- button "b{i}"\n' for i in range(shown + 3)) + "- button\n- button\n"
    text = _text(monkeypatch, snapshot)
    assert f'"b{shown - 1}", and 3 more, and 2 without a name. ' in text


def test_only_buttons_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A nameless button that holds children, and nodes that only look like one.
    snapshot = '- button:\n  - img "x"\n- link\n- heading [level=1]\n- buttons\n- button-like\n'
    assert 'title "Before you continue", 1 visible button without a name. ' in _text(
        monkeypatch, snapshot
    )
    assert "without a name" not in _text(monkeypatch, '- button "A"\n- link\n- buttons\n')
