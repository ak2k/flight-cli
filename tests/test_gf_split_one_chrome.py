# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--split` over `--gf-transport browser` holds one Chrome for the round trip
and both one-ways, as a multi-page search holds one across its pages."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from test_gf_chunked_search import _DEP, _RET, _Google, _search


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_fast_split_over_the_browser_holds_one_session(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    google = _Google([("JFK", "LAX"), ("LAX", "JFK")])
    sessions: list[gfb.GfBrowserSession] = []

    def browser(
        filters: Any, *, headed: bool, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        sessions.append(gfb.session(headed=headed))
        return google(filters, currency=currency, cheapest=cheapest)

    monkeypatch.setattr(gfid, "_one_call_browser", browser)
    result = _search(
        monkeypatch,
        google,
        "JFK",
        "LAX",
        "--backend",
        "gflight",
        "--fast",
        "--split",
        "--format",
        fmt,
        "--gf-transport",
        "browser",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(sessions) >= 3, (_DEP, _RET, len(sessions))  # the round trip, then both one-ways
    assert len({id(s) for s in sessions}) == 1


def test_enriched_split_over_the_browser_holds_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = _Google([("JFK", "LAX"), ("LAX", "JFK")])
    sessions: list[gfb.GfBrowserSession] = []

    def browser(
        filters: Any, *, headed: bool, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        sessions.append(gfb.session(headed=headed))
        return google(filters, currency=currency, cheapest=cheapest)

    async def no_matrix(state: dict[str, Any], *_a: Any) -> None:
        state["matrix_unexpected"] = RuntimeError("no Matrix in this test")

    painted_on_loop: list[bool] = []
    paint = cli._paint_first_gf_table

    def watch_paint(*args: Any, **kwargs: Any) -> None:
        painted_on_loop.append(threading.current_thread() is threading.main_thread())
        paint(*args, **kwargs)

    monkeypatch.setattr(cli, "_paint_first_gf_table", watch_paint)
    monkeypatch.setattr(gfid, "_one_call_browser", browser)
    monkeypatch.setattr(cli, "_matrix_into", no_matrix)
    result = _search(
        monkeypatch,
        google,
        "JFK",
        "LAX",
        "--backend",
        "gflight",
        "--enrich",
        "--split",
        "--gf-transport",
        "browser",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(sessions) >= 3, (_DEP, _RET, len(sessions))  # the round trip, then both one-ways
    assert painted_on_loop == [True]  # the first table is painted by the event loop's thread
    assert len({id(s) for s in sessions}) == 1


def test_a_cancelled_weave_asks_no_one_way(monkeypatch: pytest.MonkeyPatch) -> None:
    google = _Google([("JFK", "LAX"), ("LAX", "JFK")])
    held: list[bool] = []

    def slow(
        filters: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        if not held:  # the round trip is still out when Matrix fails
            held.append(True)
            time.sleep(0.5)
        return google(filters, currency=currency, cheapest=cheapest)

    async def cancel_the_weave(_state: dict[str, Any], *_a: Any) -> None:
        raise RuntimeError("the weave is cancelled")

    monkeypatch.setattr(gfid, "_one_call", slow)
    monkeypatch.setattr(cli, "_matrix_into", cancel_the_weave)
    args = [
        "search",
        "--cash-only",
        "--no-google-url",
        "--no-matrix-url",
        "JFK",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        "--backend",
        "gflight",
        "--enrich",
        "--split",
        "--gf-transport",
        "http",
    ]
    result = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})
    assert "the one-way searches did not finish" in result.output
    assert len(google.calls) == 2, google.calls  # the round trip's outbound and return only


def test_the_throttle_ladder_memo_no_longer_says_the_enriched_split_opens_a_second_chrome() -> None:
    memo = Path(__file__).parent.parent / "docs" / "memories" / "gf_throttle_ladder.md"
    assert "opens a second Chrome" not in " ".join(memo.read_text().split())
