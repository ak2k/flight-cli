"""The routing memory and the README count what a calendar fan-out asks and priced."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    text = (_ROOT / rel).read_text().replace("\n# ", "\n")
    return " ".join(text.split())


def test_memo_l4_measurement_matches_the_recorded_run() -> None:
    memo = _flat("docs/memories/gf_routing_and_carriers.md")
    assert "8 of the 18 pairs priced nothing" in memo
    assert "LGA→STN priced 1 day" in memo
    assert "cheaper on 10-28 to 11-02" in memo
    assert "cheaper on 7 days" not in memo
    assert "the 7 pairs" not in memo


def test_readme_calendar_counts_the_query_as_typed() -> None:
    assert _flat("README.md").count("plus the query as typed on a round trip") == 2
