"""AZ changed alliance (`_PARTNER_GROUPS` lists it under no group), so FCO, its hub,
is not a SkyTeam hub in either doc that tabulates alliance hubs."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HUB_DOCS = [
    _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md",
    _ROOT / "docs" / "memories" / "airport_groups.md",
]


@pytest.mark.parametrize("doc", _HUB_DOCS, ids=lambda p: p.name)
def test_skyteam_hub_row_omits_fco(doc: Path):
    rows = [ln for ln in doc.read_text().splitlines() if ln.startswith("| SkyTeam")]
    assert rows
    assert [ln for ln in rows if "FCO" in ln] == []
