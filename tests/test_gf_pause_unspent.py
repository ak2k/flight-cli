# pyright: reportPrivateUsage=false
"""A later server-error episode of one search is paused for what the earlier left.

A search's pauses fall in one 8 s window, but an 8 s sum that the pages before
it did not spend is still its to use after the window closes, as it was before
the window existed. Each virtual clock is one thread's, from `test_gf_pause_window`.
"""

from __future__ import annotations

from typing import Any

import pytest

from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from test_gf_pause_window import _ThreadTime
from test_gf_rung_parity import _html
from test_gf_server_error import _error_page, _http

_URL = "https://www.google.com/travel/flights"


@pytest.mark.parametrize("gap", [5.0, 20.0])
def test_a_later_server_error_episode_in_the_same_search_still_reads_its_board(
    gap: float, gf_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One thread, one search. Google errs for the first virtual second, then
    again from `2 + gap` to `4 + gap`, and carries the board between. The first
    page spends 2 s of the 8 s sum; `gap` seconds on, a second page meets the
    second episode, after the window closed or with 1 s of it left, and still
    has the 6 s left to outlast it. Red at the base's window alone: the second
    page was refused with a server error after pauses of `[1.0, 0.0]` at 5 s and
    `[0.0, 0.0]` at 20 s."""
    gf_session(_error_page())
    clock = _ThreadTime()
    monkeypatch.setattr(gfid, "time", clock)
    error, board = _error_page(), _html("ds1_nyc_lon_token")

    def fetch(*_a: object, **_k: object) -> PageFetch:
        t = clock.monotonic()
        erring = t < 1.0 or 2.0 + gap <= t < 4.0 + gap
        return PageFetch(error if erring else board, _URL, 200)

    monkeypatch.setattr(gfid, "_fetch_page", fetch)
    outcomes: list[object] = []
    with gfid.search_escalation():
        for step in range(2):
            if step:
                clock.sleep(gap)
            try:
                outcomes.append(len(_http()))
            except gfid.GfBackendError as e:
                outcomes.append(type(e).__name__)
    assert outcomes == [300, 300], (outcomes, clock.sleeps)
