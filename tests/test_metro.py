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
    MAX_GF_PAGES,
    METRO_MEMBERS,
    expand_airports,
    gf_leg_pages,
    gf_leg_refusal,
    gf_pages_refusal,
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


# The skill's Example 6: 8 east-coast origins, 13 European destinations.
_EX6_FROM = ("JFK", "LGA", "EWR", "BOS", "IAD", "DCA", "BWI", "PHL")
_EX6_TO = (
    "LHR",
    "CDG",
    "FRA",
    "AMS",
    "IST",
    "MAD",
    "BCN",
    "FCO",
    "MUC",
    "ZRH",
    "VIE",
    "CPH",
    "DUB",
)
# The skill's "US East Coast" list: 12 airports.
_EAST_COAST = ("JFK", "LGA", "EWR", "BOS", "IAD", "DCA", "BWI", "PHL", "ATL", "MIA", "FLL", "CLT")


def _pairs(pages: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]) -> list[tuple[str, str]]:
    return [(o, d) for os, ds in pages for o in os for d in ds]


def test_example_six_is_four_pages_that_ask_every_pair_once() -> None:
    pages = gf_leg_pages(_EX6_FROM, _EX6_TO)
    assert pages is not None
    assert len(pages) == 4
    assert all(len(os) + len(ds) <= MAX_GF_LEG_AIRPORTS for os, ds in pages)
    pairs = _pairs(pages)
    assert len(pairs) == len(set(pairs)) == 8 * 13 == 104
    assert set(pairs) == {(o, d) for o in _EX6_FROM for d in _EX6_TO}
    # Two origin groups of four against destinations split 7 / 6, order kept.
    assert pages[0] == (_EX6_FROM[:4], _EX6_TO[:7])
    assert pages[1] == (_EX6_FROM[:4], _EX6_TO[7:])
    assert pages[3] == (_EX6_FROM[4:], _EX6_TO[7:])
    assert gf_pages_refusal(_EX6_FROM, _EX6_TO) is None


def test_the_east_coast_to_one_airport_is_two_pages() -> None:
    pages = gf_leg_pages(_EAST_COAST, ["LAX"])
    assert pages == ((_EAST_COAST[:6], ("LAX",)), (_EAST_COAST[6:], ("LAX",)))


def test_a_leg_that_fits_is_one_page_equal_to_the_leg() -> None:
    assert gf_leg_pages(["JFK", "EWR"], ["LHR"]) == ((("JFK", "EWR"), ("LHR",)),)
    # A metro code is planned as its members, as the page is asked for them.
    assert gf_leg_pages(["NYC"], ["LON"]) == (
        (("JFK", "LGA", "EWR"), ("LHR", "LGW", "STN", "LTN", "LCY", "SEN")),
    )
    ten = _EAST_COAST[:10]
    assert gf_leg_pages(ten, ["LAX"]) == ((ten, ("LAX",)),)


def test_a_tie_goes_to_fewer_groups_on_the_larger_side() -> None:
    """7 origins + 14 destinations is 4 pages as 1 x 4 or as 2 x 2; the side
    with more airports keeps fewer groups."""
    codes = [f"Q{i:02d}" for i in range(21)]
    pages = gf_leg_pages(codes[:7], codes[7:])
    assert pages is not None
    assert len(pages) == 4
    assert {ds for _, ds in pages} == {tuple(codes[7:14]), tuple(codes[14:])}
    assert {os for os, _ in pages} == {tuple(codes[:4]), tuple(codes[4:7])}


def test_a_leg_past_the_page_bound_is_refused_naming_why() -> None:
    """15 + 15 needs 3 x 3 = 9 pages of at most 11: one more than the bound."""
    assert MAX_GF_PAGES == 8
    codes = [f"Q{i:02d}" for i in range(30)]
    assert gf_leg_pages(codes[:15], codes[15:]) is None
    assert gf_pages_refusal(codes[:15], codes[15:]) == (
        "30 airports on one leg (more than 8 pages of at most 11)"
    )
    # 15 + 14 is the bound exactly: 4 x 2 = 8 pages of 4 + 7.
    eight = gf_leg_pages(codes[:15], codes[15:29])
    assert eight is not None
    assert len(eight) == MAX_GF_PAGES
    assert gf_pages_refusal(codes[:15], codes[15:29]) is None


def test_the_page_plan_refuses_an_airport_at_both_ends() -> None:
    assert gf_pages_refusal(["NYC"], ["JFK"]) == "an airport at both ends of a leg (JFK)"
    assert gf_pages_refusal([*_EAST_COAST, "LAX"], ["LAX"]) == (
        "an airport at both ends of a leg (LAX)"
    )
