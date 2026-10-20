"""gf_request_budget.md names every pin outcome that leaves row 1 dearer than the cheapest."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MEMO = _ROOT / "docs" / "memories" / "gf_request_budget.md"
_IDS = _ROOT / "src" / "flight_cli" / "_gflight_ids.py"


def test_the_cheapest_round_trip_claim_names_each_pin_outcome_and_its_warning() -> None:
    text = _MEMO.read_text()
    start = text.index("A single-cabin round trip pins its cheapest outbound first")
    end = text.find("\n\n", start)
    claim = " ".join(text[start : end if end != -1 else None].split())
    exceptions, _, consequence = claim.partition(" Then row 1 ")
    # The empty board has a counted warning, so the memo may not say none does.
    assert "no line counts an empty board" not in claim, (
        "the memo says no line counts an empty return board; _report_pin_outcome warns on it"
    )
    # Each exception is a clause of its own, anchored on words only that clause holds.
    assert re.search(r"\bunless\b[^.]*\breturn board empty\b", exceptions), (
        "the cheapest-round-trip claim must name a return board Google served empty"
    )
    assert re.search(r"\breturn board was refused\b", exceptions), (
        "the cheapest-round-trip claim must name a refused return board"
    )
    assert re.search(r"\bignored the pin\b", exceptions), (
        "the claim must say a refused board is one that ignored the pin"
    )
    assert "different segment than the pin" not in claim, (
        "that wording reads as the inverse of the check, which wants the segment after the pin"
    )
    # The warnings the memo quotes are the ones the code prints.
    source = _IDS.read_text()
    for warning in ("have no return flight on Google", "return boards unavailable"):
        assert warning in exceptions, "the claim must quote the warning: " + warning
        assert warning in source, "the memo quotes a warning the code does not print: " + warning
    sentence = "is the cheapest trip through the pins whose return boards kept a row."
    assert consequence.startswith(sentence), (
        "the claim must end on what row 1 is when a pin's return board is lost"
    )
