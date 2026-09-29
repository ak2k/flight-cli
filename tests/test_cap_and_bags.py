# pyright: reportPrivateUsage=false
"""`search --max-price N` and `--bags CHECKED[,CARRY]`.

A cap is asked of Google Flights (tfs field 12) and checked on every row it
serves; a Matrix answer, which has no such input, is cut to the fares under it.
Bags are asked of Google Flights (field 13), each row says what its price
covers, and a search that only Matrix could answer is refused rather than
answered without them.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flight_cli import _gflight_ids as gfid
from flight_cli.domain import Bags, Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter
from flight_cli.links import matrix_deep_link
from flight_cli.wire import to_wire

# fli's validator rejects a past travel date, so the dates are derived.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_PAGES = Path(__file__).parent / "fixtures" / "gflight_page"
_ABSENT = object()


def _page_rows(name: str) -> list[Any]:
    return gfid._rows_from_ds1(json.loads((_PAGES / f"{name}.json").read_text())).rows


def _raw_row(slot: object = _ABSENT) -> list[Any]:
    """A JFK-LAX row off a committed page, with `row[4][6]` replaced by
    `slot`, or cut off before it."""
    row = copy.deepcopy(_page_rows("ds1_jfk_lax_3rows")[0])
    if slot is _ABSENT:
        row[4] = row[4][:6]
    else:
        row[4][6] = slot
    return row


def _search(**options: object) -> SpecificDateSearch:
    return SpecificDateSearch(
        legs=(Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET)),
        options=SearchOptions(**options),  # pyright: ignore[reportArgumentType]
    )


# ───────────────────────────── domain and bridge ────────────────────────────


def test_the_fli_filter_carries_the_cap_and_the_bags() -> None:
    f = to_fli_filter(_search(max_price=250, bags=Bags(checked=1, carry_on=1)))
    assert f.price_limit.max_price == 250
    assert (f.bags.checked_bags, f.bags.carry_on) == (1, True)


def test_the_fli_filter_carries_neither_unless_asked() -> None:
    f = to_fli_filter(_search())
    assert f.price_limit is None
    assert f.bags is None


def test_matrix_is_sent_the_same_request_with_or_without_them() -> None:
    """Matrix has no input for either, so its body and its link stay the
    unconstrained search's, byte for byte."""
    plain, asked = _search(), _search(max_price=250, bags=Bags(checked=1))
    assert to_wire(asked).as_json() == to_wire(plain).as_json()
    assert matrix_deep_link(asked) == matrix_deep_link(plain)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"checked": 0, "carry_on": 0}, {"carry_on": 2}, {"checked": -1}],
    ids=["nothing", "zeros", "two-carry-ons", "negative"],
)
def test_bags_refuse_a_count_google_cannot_be_asked_for(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Bags(**kwargs)


def test_a_cap_below_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        SearchOptions(max_price=0)


# ─────────────────────────── the row's bag statement ─────────────────────────


def test_each_row_carries_the_bags_google_says_its_price_covers() -> None:
    def stated(name: str) -> Counter[tuple[int | None, int | None]]:
        return Counter(gfid._parse_flight_with_id(r).bags_included for r in _page_rows(name))

    assert stated("ds1_jfk_lax_tfu") == {(0, 1): 95}
    # JFK-LHR states no checked allowance at all, and four rows state nothing.
    assert stated("ds1_jfk_lhr_tfu") == {(None, 1): 97, (None, None): 4}


@pytest.mark.parametrize(
    "slot,stated",
    [
        ([1, 1], (1, 1)),
        ([None, 1], (None, 1)),
        (_ABSENT, (None, None)),
        ([1], (1, None)),
        (None, (None, None)),
        ("[1, 1]", (None, None)),
        ([True, -1], (None, None)),
    ],
    ids=["both", "checked-unstated", "no-slot", "short", "null", "string", "bool-and-negative"],
)
def test_a_missing_short_or_malformed_statement_says_nothing(
    slot: object, stated: tuple[int | None, int | None]
) -> None:
    row = gfid._parse_flight_with_id(_raw_row(slot))
    assert row.bags_included == stated
    assert row.flight.price is not None  # the row itself still parses whole
