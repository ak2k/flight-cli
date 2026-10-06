"""An open jaw on separate tickets: one Google Flights one-way per slice,
combined into the cheapest pairs a traveler can fly in order.

Each slice is asked alone, so nothing on either board knows about the other:
the cheapest one-way each way can be a second ticket that leaves before the
first lands. A pair is kept only when the second ticket is flyable after the
first, and a total is the sum of two fares in one currency, counted in cents
so that a tie between two totals is a tie.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


class Combination(NamedTuple):
    """Two one-way tickets, the first slice's then the second's, both priced in
    `currency`; `total_cents` is their fares' sum."""

    tickets: tuple[Any, Any]
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
    first: Sequence[Any],
    second: Sequence[Any],
    *,
    currency: str,
    limit: int,
    cap: int | None = None,
) -> list[Combination]:
    """The `limit` cheapest flyable pairs of a ticket from `first` and one from
    `second`, two priced one-ticket boards in price order, cheapest first.

    A row priced in another currency than `currency` is passed over, so no
    total adds two currencies; a row that names none is in `currency`, as
    Google answered the request in it. With `cap`, a pair whose total is over it
    is left out. Pairs with equal totals keep the first board's order, then the
    second's."""

    def priced(board: Sequence[Any]) -> list[Any]:
        return [r for r in board if (r.flight.currency or currency) == currency]

    def pairs() -> Iterator[Combination]:
        for a in priced(first):
            for b in priced(second):
                total = _cents(a) + _cents(b)
                if (cap is None or total <= cap * 100) and flyable(a, b):
                    yield Combination((a, b), total, currency)

    # Stable on equal totals, as `sorted(...)[:limit]` is.
    return heapq.nsmallest(limit, pairs(), key=lambda c: c.total_cents)
