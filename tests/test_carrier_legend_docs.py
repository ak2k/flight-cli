"""The routing memory says how the carrier legend keeps to one line."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_memo_says_a_legend_name_cannot_break_the_line() -> None:
    text = (_ROOT / "docs/memories/gf_routing_and_carriers.md").read_text()
    memo = " ".join(text.split())
    assert (
        "collapses the whitespace runs of an entry, a newline and a tab included, to one "
        "space, so a name cannot break the legend over two lines"
    ) in memo
