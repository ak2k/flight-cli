"""The routing memory names every pin outcome that leaves row 1 dearer than the board's cheapest."""

from __future__ import annotations

import re
from pathlib import Path

_MEMO = Path(__file__).resolve().parent.parent / "docs" / "memories" / "gf_routing_and_carriers.md"


def test_the_cheapest_round_trip_claim_names_a_refused_return_board() -> None:
    text = _MEMO.read_text()
    start = text.index("A single-cabin round trip pins its cheapest outbound first")
    end = text.find("\n\n", start)
    claim = " ".join(text[start : end if end != -1 else None].split())
    assert re.search(r"unless[^.]*\brefused\b", claim), (
        "the cheapest-round-trip claim must name a refused return board as an exception"
    )
    assert re.search(r"unless[^.]*\bempty\b", claim), (
        "the cheapest-round-trip claim must name a return board Google served empty"
    )
