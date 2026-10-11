"""An airport sits in one region, so no row of the skill's alliance-hub table names an airport
as both a European hub and an Asian hub; SkyTeam's Asian hubs are ICN and PVG."""

from __future__ import annotations

from pathlib import Path

_SKILL = (
    Path(__file__).resolve().parent.parent / ".claude" / "skills" / "flight-search" / "SKILL.md"
)
_HEADER = "| Alliance | European hubs | Asian hubs | US hubs |"


def _hub_rows() -> dict[str, tuple[set[str], set[str]]]:
    lines = _SKILL.read_text().splitlines()
    rows: dict[str, tuple[set[str], set[str]]] = {}
    for ln in lines[lines.index(_HEADER) + 2 :]:
        if not ln.startswith("|"):
            break
        alliance, europe, asia, _us = (c.strip() for c in ln.strip("|").split("|"))
        rows[alliance] = (set(europe.strip("`").split(",")), set(asia.strip("`").split(",")))
    return rows


def test_asian_hub_cell_names_no_european_hub():
    rows = _hub_rows()
    assert set(rows) == {"Star Alliance", "Oneworld", "SkyTeam"}
    for alliance, (europe, asia) in rows.items():
        assert asia & europe == set(), alliance


def test_skyteam_asian_hubs_are_icn_and_pvg():
    assert _hub_rows()["SkyTeam"][1] == {"ICN", "PVG"}
