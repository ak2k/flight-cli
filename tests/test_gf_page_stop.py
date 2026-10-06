# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A throttle that ends a page's pins after some were served stops the later pages.

Google is faked as in `test_gf_chunked_search`: the Example 6 round trip, four
pages, every GET throttled from page 1's third GET on, which is its second pin."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import capture_err
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_errors import GfThrottledError
from test_gf_auto_escalation import _rungs
from test_gf_chunked_search import (
    _EX6_FROM,
    _EX6_PAGES,
    _EX6_PAIRS,
    _EX6_TO,
    Page,
    _flat,
    _Google,
    _missing,
    _numbers,
    _search,
)
from test_gf_full_board import _DEP

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _tab_wall(google: _Google, tagged: list[str]) -> Callable[[str], Callable[..., Any]]:
    """A rung of `google` that is throttled from its first Cheapest-tab request
    on, each request logged to `tagged` as `<rung>` or `<rung>-tab`."""

    def rung(name: str) -> Callable[..., Any]:
        walled: list[bool] = []

        def call(filters: Any, *, currency: str = "USD", cheapest: bool = False) -> Any:
            tagged.append(f"{name}-tab" if cheapest else name)
            if cheapest:
                walled.append(True)
            if walled:
                raise GfThrottledError("rate-limited")
            return google(filters, currency=currency, cheapest=cheapest)

        return call

    return rung


@pytest.mark.parametrize("mode", ["http", "auto"])
def test_a_throttle_on_a_pages_cheapest_tab_stops_the_later_pages(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Every rung is walled from page 1's Cheapest tab on. Page 1's board
    answered, so its rows stand and no page line names it; every later page
    is named as not asked. Red at the tip: the tab's throttle was the tab's
    alone, so page 2 was asked on the walled IP (or Chrome) and took the
    blame."""

    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)
    tagged: list[str] = []
    google = _Google(_EX6_PAIRS)
    rung = _tab_wall(google, tagged)
    _rungs(monkeypatch, [], http=rung("http"), chrome=rung("chrome"))
    buf = capture_err(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "--cash-only", "--no-google-url", "--no-matrix-url"),
            *(",".join(_EX6_FROM), ",".join(_EX6_TO), "--dep", _DEP.isoformat()),
            *("--backend", "gflight", "--fast", "--format", "json", "--gf-transport", mode),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    # Page 1's board, then its tab on each rung the search may use, and nothing after.
    tabs = ["http-tab", "chrome-tab"] if mode == "auto" else ["http-tab"]
    assert tagged[0] == "http", tagged
    assert sorted(set(tagged[1:])) == sorted(tabs), tagged
    assert google.pages() == [_EX6_PAGES[0]]
    first_numbers = {
        i for i, (o, d) in enumerate(_EX6_PAIRS) if o in _EX6_FROM[:4] and d in _EX6_TO[:7]
    }
    assert set(_numbers(json.loads(result.stdout))) <= first_numbers
    printed = _flat(buf.getvalue())
    assert "page 1 of 4" not in printed, printed
    for n in (2, 3, 4):
        assert _missing(n, "not asked after page 1 stopped the search") in printed, printed
    assert (
        printed.count("Itineraries on separate tickets not read: Google Flights rate-limited") == 1
    ), printed


def test_a_throttle_on_a_round_trip_pages_cheapest_tab_asks_no_later_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ten cheapest outbounds are on pages 1 and 2, so pages 3 and 4 hold
    no pin. Page 1's tab is throttled after its pins: page 2's returns are
    named as not asked, and the tab line is the one account of pages 3 and 4's
    tabs. Red at the tip: page 2's pins and every later tab were asked."""

    def no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", no_sleep)
    monkeypatch.setattr(gfid.random, "random", lambda: 0.0)
    google = _Google(
        _EX6_PAIRS,
        refuse_cheapest=lambda page: (
            GfThrottledError("rate-limited") if page == _EX6_PAGES[0] else None
        ),
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
    assert [pin for _, pin in google.calls[:4]] == [None] * 4
    assert {page for page, _ in google.calls[4:]} == {_EX6_PAGES[0]}
    assert set(google.cheapest) == {_EX6_PAGES[0]}
    assert json.loads(result.stdout)
    printed = _flat(buf.getvalue())
    assert "page 1 of 4" not in printed, printed
    assert _missing(2, "its returns were not asked after page 1 stopped the search") in printed, (
        printed
    )
    assert "page 3 of 4" not in printed, printed
    assert "page 4 of 4" not in printed, printed
    assert (
        printed.count("Itineraries on separate tickets not read: Google Flights rate-limited.") == 1
    ), printed
