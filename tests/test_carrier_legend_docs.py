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


def test_memo_says_a_wrapped_legend_breaks_only_between_entries() -> None:
    text = (_ROOT / "docs/memories/gf_routing_and_carriers.md").read_text()
    memo = " ".join(text.split())
    assert (
        "When the legend outgrows the console it breaks only between entries, never between a "
        "code and its name, unless one entry is wider than the console; Rich then folds that "
        "entry. Rich breaks at any space, a no-break space included, so "
        "`_print_carrier_legend_line` picks the break itself."
    ) in memo


def test_memo_says_an_entry_wider_than_the_console_is_folded() -> None:
    text = (_ROOT / "docs/memories/gf_routing_and_carriers.md").read_text()
    memo = " ".join(text.split())
    assert (
        "When the legend outgrows the console it breaks only between entries, never between a "
        "code and its name, unless one entry is wider than the console; Rich then folds that "
        "entry. Rich breaks at any space, a no-break space included, so "
        "`_print_carrier_legend_line` picks the break itself."
    ) in memo
