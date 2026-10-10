# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`--split` over `--gf-transport browser` holds one Chrome for the round trip
and both one-ways, as a multi-page search holds one across its pages."""

from __future__ import annotations

from typing import Any

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
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
