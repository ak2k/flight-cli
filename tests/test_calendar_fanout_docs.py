"""The date-grid memory and the README count what a calendar fan-out asks and priced."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    text = (_ROOT / rel).read_text().replace("\n# ", "\n")
    return " ".join(text.split())


def test_memo_l4_measurement_matches_the_recorded_run() -> None:
    memo = _flat("docs/memories/gf_date_grid.md")
    assert "8 of the 18 pairs priced nothing" in memo
    assert "LGA→STN priced 1 day" in memo
    assert "cheaper on 10-28 to 11-02" in memo
    assert "cheaper on 7 days" not in memo
    assert "the 7 pairs" not in memo


def test_readme_calendar_counts_the_query_as_typed() -> None:
    assert _flat("README.md").count("plus the query as typed on a round trip") == 2


def test_skill_and_calendar_memo_say_a_round_trip_also_runs_the_query_as_typed() -> None:
    skill = _flat(".claude/skills/flight-search/SKILL.md")
    assert (
        "the calendar asked one query per airport pair, plus the query as typed on a round trip, "
        "and each cell names the pair that priced it"
    ) in skill
    memo = _flat("docs/memories/calendar_two_phase.md")
    assert "BVA, one query per pair and the query as typed on a round trip;" in memo
    assert "is asked one airport pair per query, plus the query as typed on a round trip," in memo
