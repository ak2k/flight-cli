# pyright: reportPrivateUsage=false
"""An auto search handed to Matrix still says the Cheapest tab went unread.

The tab is left unread when a return check only the row filter makes, never
Google's query, could not be held to its rows: a separate-ticket round trip
comes without its return. Every pinned return then fails that check, so auto
hands the search to Matrix, and the note is said ahead of the hand-off line."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from test_gf_separate_tickets import _FLL_LGA, _RETURN_CHECKS, _SEARCH, _fll_lga_pages

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(("asked", "checks"), _RETURN_CHECKS)
def test_an_auto_hand_off_still_says_the_cheapest_tab_went_unread(
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    asked: list[str],
    checks: str,
) -> None:
    """Red at the base: the hand-off returned before the note was said."""
    ran: list[bool] = []

    def matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", matrix)
    fake = gf_session(*_fll_lga_pages())
    args = ["auto" if a == "gflight" else a for a in _FLL_LGA]
    result = CliRunner().invoke(cli.app, [*_SEARCH, *args, "--format", "json", *asked])
    assert result.exit_code == 0, result.output
    assert ran == [True]
    assert len(fake.gets) == 11
    said = " ".join(result.stderr.split())
    assert "Using Matrix:" in said
    assert said.count("Itineraries on separate tickets") == 1, said
    note = (
        "Itineraries on separate tickets not read: Google lists no return for them "
        f"to check against {checks}."
    )
    assert note in said, said
    assert said.index(note) < said.index("Using Matrix:")
    gf_session(*_fll_lga_pages())
    hidden = CliRunner().invoke(
        cli.app, [*_SEARCH, *args, "--format", "json", *asked, "--no-separate-tickets"]
    )
    assert hidden.exit_code == 0, hidden.output
    assert "separate tickets" not in hidden.stderr
