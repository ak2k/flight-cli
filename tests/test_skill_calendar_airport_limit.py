"""The skill's calendar row states how many airports a leg Google's price graph serves."""

from __future__ import annotations

from pathlib import Path

from flight_cli._metro import MAX_GF_LEG_AIRPORTS

_SKILL = (
    Path(__file__).resolve().parent.parent / ".claude" / "skills" / "flight-search" / "SKILL.md"
)


def _calendar_row() -> str:
    (row,) = [ln for ln in _SKILL.read_text().splitlines() if ln.startswith("| `flight calendar ")]
    return " ".join(row.split())


def test_the_calendar_row_states_the_airport_limit_of_the_price_graph() -> None:
    assert (
        "each date priced at the set's cheapest for comma-lists or metro codes up to "
        f"{MAX_GF_LEG_AIRPORTS} airports a leg; at most 8 page loads)"
    ) in _calendar_row()
