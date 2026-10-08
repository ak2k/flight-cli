# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search --split` on a round trip: a one-way board short of rows.

A one-way board that lost a page or holds rows the parser could not read may
lack the leg's cheapest ticket, so the envelope says the answer is narrower
than asked, as it does for an open jaw's one-ways."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from test_envelope import _envelope_of, _notes
from test_split_ticket import _google, _search

if TYPE_CHECKING:
    from flight_cli.domain import Leg


@pytest.mark.parametrize(
    ("origin", "damage", "note"),
    [
        pytest.param(
            "JFK",
            {"unread": 3},
            "Google Flights outbound one-way: 3 rows its pages served could not be read and "
            "are left out of the answer",
            id="outbound-unread",
        ),
        pytest.param(
            "LAX",
            {"unread": 1},
            "Google Flights return one-way: 1 rows its pages served could not be read and "
            "are left out of the answer",
            id="return-unread",
        ),
        pytest.param("LAX", {"partial": True}, None, id="return-partial"),
    ],
)
def test_a_one_way_board_short_of_rows_leaves_the_split_envelope_incomplete(
    monkeypatch: pytest.MonkeyPatch, origin: str, damage: dict[str, Any], note: str | None
) -> None:
    """The pair stands, and the envelope is incomplete: the rows the board lacks
    may be the leg's cheapest tickets. Red at the base, which reads neither
    `unread` nor `partial` off a round trip's one-way board."""
    google = _google(monkeypatch)

    def _short(legs: tuple[Leg, ...], *a: Any, **kw: Any) -> gfid.Board[Any]:
        board = google(legs, *a, **kw)
        if len(legs) == 1 and legs[0].origins[0] == origin:
            for name, value in damage.items():
                setattr(board, name, value)
        return board

    monkeypatch.setattr(cli, "_gflight_results", _short)
    env = _envelope_of(
        _search("--cash-only", "--fast", "--backend", "gflight", "--split", "--format", "envelope")
    )
    assert (env["backend"], env["complete"]) == ("gflight", False)
    assert env["split_ticket"]["total"] == 413
    if note is not None:
        assert note in env["notes"]
    assert _notes(env, "split_ticket") == []
