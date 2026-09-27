# pyright: reportMissingTypeStubs=false
#   fli ships no stubs; its airport enum is what every member must resolve in.
"""The metro member table: one copy in code, one in the memo, and they agree.

The memo (`docs/memories/airport_groups.md`, "IATA metro codes") is what an
agent reads to pick a metro code, and `_metro.METRO_MEMBERS` is what Google is
asked for when one is typed. Parsed here so neither can drift from the other."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fli.models.airport import Airport

from flight_cli._metro import (
    MAX_GF_LEG_AIRPORTS,
    METRO_MEMBERS,
    expand_airports,
    gf_leg_refusal,
)

_MEMO = Path(__file__).resolve().parents[1] / "docs" / "memories" / "airport_groups.md"


def _memo_section() -> str:
    text = _MEMO.read_text()
    start = text.index("## IATA metro codes")
    return text[start : text.index("\n## ", start + 1)]


def _memo_table() -> dict[str, tuple[str, ...]]:
    """Metro code -> member airports, read off the memo's table rows.

    The code cell may carry a dagger after its backticks, and a member cell a
    parenthetical note (BER, SHA, DPS), so each is read for codes, not as text."""
    table: dict[str, tuple[str, ...]] = {}
    for line in _memo_section().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not (m := re.fullmatch(r"`([A-Z]{3})`\S*", cells[1])):
            continue
        members = re.sub(r"\([^)]*\)", "", cells[2])
        table[m.group(1)] = tuple(a.strip() for a in members.split(",") if a.strip())
    return table


def _memo_one_airport_codes() -> set[str]:
    """The codes the memo's own sentence says stay one airport on Google."""
    m = re.search(r"also an airport code \(([^)]*)\)", " ".join(_memo_section().split()))
    assert m, "airport_groups.md no longer names the metro codes that are also an airport"
    return set(re.findall(r"`([A-Z]{3})`", m.group(1)))


def test_the_memo_table_parses_to_its_known_size() -> None:
    """A parser that read nothing would make the parity test below vacuous."""
    table = _memo_table()
    assert len(table) == 24
    assert sum(len(m) for m in table.values()) == 62
    assert _memo_one_airport_codes() == {"HOU", "LAX", "BER", "SHA", "BKK", "DPS"}


def test_the_code_table_is_the_memo_table_minus_its_one_airport_codes() -> None:
    table = _memo_table()
    one_airport = _memo_one_airport_codes()
    assert one_airport <= table.keys()
    assert dict(METRO_MEMBERS) == {c: m for c, m in table.items() if c not in one_airport}


def test_every_member_is_an_airport_google_can_be_asked_for() -> None:
    missing = [a for members in METRO_MEMBERS.values() for a in members if not hasattr(Airport, a)]
    assert missing == []


def test_no_member_is_itself_a_metro_code() -> None:
    """Expansion is one level deep; a member that expanded again would change the
    set with the order it was written in."""
    assert not {a for m in METRO_MEMBERS.values() for a in m} & METRO_MEMBERS.keys()


@pytest.mark.parametrize("code", ["HOU", "LAX", "BER", "SHA", "BKK", "DPS"])
def test_a_code_that_is_also_an_airport_stays_that_one_airport(code: str) -> None:
    assert expand_airports([code]) == (code,)


def test_expansion_keeps_the_users_order_and_drops_repeats() -> None:
    assert expand_airports(["NYC"]) == ("JFK", "LGA", "EWR")
    assert expand_airports(["BOS", "NYC", "JFK"]) == ("BOS", "JFK", "LGA", "EWR")
    assert expand_airports(["QSF", "SAO"]) == ("SFO", "OAK", "SJC", "GRU", "CGH", "VCP")


def test_the_leg_bound_counts_expanded_airports_on_both_ends() -> None:
    assert MAX_GF_LEG_AIRPORTS == 11
    # LON (6) + NYC (3) = 9, and two more = 11.
    assert gf_leg_refusal(["LON"], ["NYC", "BOS", "PHL"]) is None
    assert gf_leg_refusal(["LON"], ["NYC", "BOS", "PHL", "IAD"]) == (
        "12 airports on one leg (its limit is 11)"
    )


def test_an_airport_at_both_ends_is_refused_after_expansion() -> None:
    assert gf_leg_refusal(["NYC"], ["JFK"]) == "an airport at both ends of a leg (JFK)"
    assert gf_leg_refusal(["NYC"], ["LAX"]) is None
