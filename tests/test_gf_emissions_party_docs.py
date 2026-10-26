"""The CO2 memory states what the recorded two-adult run measured."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    return " ".join((_ROOT / rel).read_text().split())


def test_co2_memo_states_the_two_adult_measurement() -> None:
    memo = _flat("docs/memories/gf_search_transport.md")
    assert "only one adult has been measured" not in memo
    assert "39 of 39 matched rows at exactly 2x" in memo
    assert "DL747 229000 to 458000" in memo
    assert "B6 1523 419000 to 838000" in memo
    assert "percent unchanged on 38 of 39" in memo


def test_co2_memo_says_which_figures_double_at_two_adults() -> None:
    memo = _flat("docs/memories/gf_search_transport.md")
    assert "at two adults every row and leg gram figure doubles" in memo
    assert "every leg ratio 2.0" in memo
    assert "typical 346000 to 693000 (2.003x, not an exact double)" in memo
    assert "The percent and label slots are not grams and do not double" in memo
