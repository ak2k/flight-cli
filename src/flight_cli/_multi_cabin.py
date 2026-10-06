"""Multi-cabin search orchestration: join N single-cabin SearchResults
into rows with one price-per-cabin.

The domain `Search` stays single-cabin — multi-cabin is an orchestration
concept. The CLI fires N parallel single-cabin queries (one per requested
cabin), then this module joins them on a stable per-itinerary key and
produces rows the renderer can iterate.

Join key: the whole itinerary. Per slice, every flight number, each
flight's date where the answer states one (Google does, Matrix does not), and
the slice's departure and arrival as stated. Trips behind one first flight
connect or land differently at different fares, so a shorter key prints one
trip's fare against another's flights. Nothing cabin-specific is in the key,
so an itinerary both cabins list is one row with both prices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from .domain import Cabin
    from .models import Itinerary, SearchResult


# (flight numbers, per-flight dates, departure, arrival)
SliceKey = tuple[tuple[str, ...], tuple[str, ...], str, str | None]
ItineraryKey = tuple[SliceKey, ...]

_CASH_NUM_RE = re.compile(r"[\d,]*\d+(?:\.\d+)?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")


def _norm_fn(fn: str | None) -> str:
    return (fn or "").upper().replace(" ", "")


def itinerary_key(itin: Itinerary) -> ItineraryKey | None:
    """Per slice: every flight number, the flights' dates where the slice
    states them, and its departure and arrival strings. None when a slice has
    no flight, a blank one, or no departure, since such an itinerary can't be
    told apart from another; a missing arrival is keyed as missing.
    """
    details = itin.itinerary
    if not details or not details.slices:
        return None
    slice_keys: list[SliceKey] = []
    for s in details.slices:
        flights = tuple(_norm_fn(fn) for fn in s.flights)
        if not flights or not all(flights) or not s.departure:
            return None
        slice_keys.append((flights, tuple(s.segment_dates), s.departure, s.arrival))
    return tuple(slice_keys)


def parse_price(s: str | None) -> float | None:
    """Pull the first numeric value out of strings like 'USD530.00',
    '$1,078', '1,078 USD'. Mirrors `pp.cli._parse_cash` — kept local to
    avoid a cross-module dep just for one regex."""
    if not s or s in ("—", "-"):
        return None
    m = _CASH_NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def price_currency(price: str | None) -> str | None:
    """The ISO 4217 prefix of a price string ('USD' of 'USD530.00'); None for a
    price that names none, such as '$1,078'."""
    m = _CURRENCY_RE.match(price or "")
    return m.group(0) if m else None


def price_rank(price: str | None, amount: float | None, *, currency: str) -> tuple[int, str, float]:
    """Sort key for a price that never compares two currencies' numbers.

    Prices in `currency`, the one the search asked for, come first, then each
    other currency in code order, then prices naming none, each group by
    `amount`; a price with no amount goes last. No exchange rate is known here,
    so a fare in another currency ranks after every requested one rather than
    among them by a number in a different unit.

    `amount` is the caller's own parse of `price`, so a list priced in one
    currency keeps exactly the order that parse gives it."""
    if amount is None:
        return (3, "", 0.0)
    code = price_currency(price)
    if code is None:
        return (2, "", amount)
    return (0, "", amount) if code == currency else (1, code, amount)


@dataclass
class MultiCabinRow:
    """One itinerary observed across one or more cabin queries.

    `itinerary` is the first cabin's listing of it (used for render: slices,
    carriers, legroom), its cheapest where that cabin listed it more than once,
    so its own price is the one shown for that cabin. `prices` maps each cabin
    we have a price for to its raw price string (e.g. 'USD623.00'). Cabins
    with no price are absent from the dict — renderer treats absence as '—'.
    `totals` maps each cabin to the party's total price where one is known.
    """

    itinerary: Itinerary
    prices: dict[Cabin, str] = field(default_factory=dict)
    totals: dict[Cabin, str] = field(default_factory=dict)


def merge(
    results_by_cabin: dict[Cabin, SearchResult],
    *,
    sort_by: Cabin,
    top_n: int,
    currency: str,
    total_of: Callable[[Itinerary], str | None] | None = None,
) -> list[MultiCabinRow]:
    """Join itineraries across cabins. Sorted by `sort_by`'s price under
    `price_rank`: rows priced in `currency` first by amount, any other
    currency after them; rows missing the sort cabin's price sink to the
    bottom. Truncated to `top_n` rows, so the trim never drops a fare for a
    smaller number in another currency.

    A cabin that lists one itinerary more than once is priced at the listing
    that ranks first under `price_rank`, the earlier of two equal ones, since
    only the cheaper fare is on offer.

    Itineraries that can't be keyed (no flight, a blank flight or no
    departure on a slice) are skipped.

    `total_of` gives a listing's party total, read off the listing `prices`
    holds. Rows still rank on `prices`: a party's total is one passenger's
    price times a near-constant, so the order holds.
    """

    def rank(it: Itinerary) -> tuple[int, str, float]:
        return price_rank(it.price, parse_price(it.price), currency=currency)

    listings: dict[ItineraryKey, dict[Cabin, Itinerary]] = {}
    for cabin, res in results_by_cabin.items():
        for it in res.solutions:
            key = itinerary_key(it)
            if key is None:
                continue
            by_cabin = listings.setdefault(key, {})
            held = by_cabin.get(cabin)
            if held is None or rank(it) < rank(held):
                by_cabin[cabin] = it
    # Replacing a cabin's listing keeps its place in the dict, so the first
    # value is the first cabin's, and the rows keep the order keys were met.
    joined = [
        MultiCabinRow(
            itinerary=next(iter(by_cabin.values())),
            prices={cabin: it.price for cabin, it in by_cabin.items() if it.price},
            totals={
                cabin: total
                for cabin, it in by_cabin.items()
                if total_of and (total := total_of(it))
            },
        )
        for by_cabin in listings.values()
    ]

    def sort_value(row: MultiCabinRow) -> tuple[int, str, float]:
        price = row.prices.get(sort_by)
        return price_rank(price, parse_price(price), currency=currency)

    rows = sorted(joined, key=sort_value)
    return rows[:top_n]
