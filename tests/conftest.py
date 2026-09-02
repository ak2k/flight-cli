"""Suite-wide guards.

The load-bearing one: **no test launches a browser.** Rung 2 of the Google
Flights transport opens a real Chrome — headless, and under 3 s, so the cost is
not what makes this matter. A test that reached it would hit the live network,
and would quietly disprove the property the CLI advertises: that
`--gf-transport http` never consults a browser. So the launcher seam is
replaced, for every test, by a callable that fails whichever test touched it.

`pytest.fail` raises a `BaseException`, deliberately: production code wraps
launch failures in `except Exception`, and a guard the code under test could
swallow would be no guard at all.

Tests that legitimately drive the seam — with a fake playwright, never a real
one — opt out with `@pytest.mark.gf_browser`.
"""

from __future__ import annotations

import threading
from typing import cast

import pytest

from flight_cli import _gf_browser


@pytest.fixture(autouse=True)
def _no_browser_launch(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail any unmarked test that reaches the real patchright launcher, and
    give every test a clean set of the module's once-per-process latches.

    The resets are unconditional: the launch notice and the thread-local
    session are process-wide state, so without them the first browser test to
    run would decide what every later one sees."""
    monkeypatch.setattr(_gf_browser, "_notice_state", {"printed": False})
    monkeypatch.setattr(_gf_browser, "_sessions", threading.local())
    # The MARKER, not `request.keywords`. `keywords` also carries the node's
    # name, its parametrize ids and its containing directory — so a test
    # parametrized with the string "gf_browser", or any test under a directory
    # of that name, silently opted itself out of the guard and could reach the
    # real launcher.
    # pyright: ignore comments — `request.node` is `Any` in pytest's stubs.
    marker = cast(  # pyright: ignore[reportUnknownArgumentType]
        "object | None",
        request.node.get_closest_marker("gf_browser"),  # pyright: ignore[reportUnknownMemberType]
    )
    if marker is not None:
        return

    def _forbidden() -> object:
        pytest.fail("this test reached rung 2's real browser launcher")

    monkeypatch.setattr(_gf_browser, "_playwright_factory", _forbidden)
