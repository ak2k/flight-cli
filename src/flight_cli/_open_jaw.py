"""A multi-city trip on separate tickets: one Google Flights one-way per
slice, combined into the cheapest runs a traveler can fly in order.

Each slice is asked alone, so nothing on one board knows about another: the
cheapest one-way of each slice can be a ticket that leaves before the one
before it lands. A combination is kept only when each ticket is flyable after
the one before it, and a total is the sum of its fares in one currency, counted
in cents so that a tie between two totals is a tie.
"""

from __future__ import annotations

import heapq
import math
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence


class Combination(NamedTuple):
    """One one-way ticket per slice, in slice order, all priced in `currency`;
    `total_cents` is their fares' sum."""

    tickets: tuple[Any, ...]
    total_cents: int
    currency: str

    @property
    def total(self) -> float:
        return self.total_cents / 100


def _cents(row: Any) -> int:
    return round(row.flight.price * 100)


def flyable(first: Any, second: Any) -> bool:
    """Whether `second` can be flown after `first`.

    From the airport `first` lands at, `second` leaves after it lands: both
    times are that airport's local clock, so they compare as they are. From
    another airport the traveler has a ground transfer whose length neither
    board states, and the rows carry no UTC offset to compare clocks in two
    places, so `second` must leave on a later local day than `first` lands."""
    lands = first.flight.legs[-1]
    leaves = second.flight.legs[0]
    if leaves.departure_airport == lands.arrival_airport:
        return leaves.departure_datetime > lands.arrival_datetime
    return leaves.departure_datetime.date() > lands.arrival_datetime.date()


def combine(
    *boards: Sequence[Any],
    currency: str,
    limit: int,
    cap: int | None = None,
) -> list[Combination]:
    """The `limit` cheapest combinations of one ticket from each of `boards`,
    priced one-ticket boards in slice order, each ticket flyable after the one
    before it, cheapest first.

    A row priced in another currency than `currency` is passed over, so no
    total adds two currencies; a row that names none is in `currency`, as
    Google answered the request in it. With `cap`, a combination whose total is
    over it is left out. Equal totals keep the first board's order, then the
    next board's.

    The boards' product is never built: three boards of 300 rows make
    27,000,000 combinations. The cheapest flyable run from a row to the last
    board bounds every combination through that row, so a board is walked
    cheapest row first and its walk stops at the first row whose fare alone
    takes the total past the `limit`-th cheapest found. A run whose bound ties
    that total is passed over too once its board positions so far sort after
    that combination's, since it would rank after it: boards of one fare would
    otherwise be walked through their whole product."""
    # Each board as (cents, position on the board, row), cheapest first.
    priced = [
        sorted(
            (
                (_cents(r), j, r)
                for j, r in enumerate(board)
                if (r.flight.currency or currency) == currency
            ),
            key=lambda t: (t[0], t[1]),
        )
        for board in boards
    ]
    if limit <= 0 or not priced or not all(priced):
        return []
    last = len(priced) - 1
    # through[i][p]: the cheapest total from the p-th row of board i through
    # the last board, each ticket flyable after the one before it; inf where
    # none is.
    through: list[list[float]] = [[math.inf] * len(b) for b in priced]
    through[last] = [float(c) for c, _, _ in priced[last]]
    for i in range(last - 1, -1, -1):
        onward = sorted((t, q) for q, t in enumerate(through[i + 1]) if t != math.inf)
        for p, (c, _, row) in enumerate(priced[i]):
            tail = next((t for t, q in onward if flyable(row, priced[i + 1][q][2])), math.inf)
            through[i][p] = c + tail
    ceiling = math.inf if cap is None else cap * 100
    # The cheapest found so far, keyed so that the root is the one a better
    # combination displaces: the dearest, and of equal totals the latest in
    # board order.
    kept: list[tuple[int, tuple[int, ...], tuple[int, ...]]] = []

    def bar() -> float:
        return -kept[0][0] if len(kept) == limit else ceiling

    def beaten(total: float, at: tuple[int, ...]) -> bool:
        """Whether every combination costing at least `total`, at board
        positions starting `at`, ranks after all of those kept."""
        if len(kept) < limit:
            return total > ceiling
        neg_total, neg_at, _ = kept[0]
        if total != -neg_total:
            return total > -neg_total
        return at > tuple(-pos for pos in neg_at[: len(at)])

    def walk(i: int, picked: tuple[int, ...], at: tuple[int, ...], spent: int) -> None:
        for p, (c, j, row) in enumerate(priced[i]):
            if spent + c > bar():
                break  # every later row of the board costs as much or more
            # Bounded before `flyable` is asked, which is the walk's cost.
            if through[i][p] == math.inf or beaten(spent + through[i][p], (*at, j)):
                continue
            if picked and not flyable(priced[i - 1][picked[-1]][2], row):
                continue
            here = (*picked, p)
            if i < last:
                walk(i + 1, here, (*at, j), spent + c)
                continue
            key = (-(spent + c), tuple(-pos for pos in (*at, j)), here)
            if len(kept) < limit:
                heapq.heappush(kept, key)
            elif key > kept[0]:
                heapq.heapreplace(kept, key)

    walk(0, (), (), 0)
    return [
        Combination(tuple(priced[k][q][2] for k, q in enumerate(here)), -neg_total, currency)
        for neg_total, _, here in sorted(kept, reverse=True)
    ]
