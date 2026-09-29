"""Multi-cabin search orchestration: join N single-cabin SearchResults
into rows with one price-per-cabin.

The domain `Search` stays single-cabin — multi-cabin is an orchestration
concept. The CLI fires N parallel single-cabin queries (one per requested
cabin), then this module joins them on a stable per-itinerary key and
produces rows the renderer can iterate.

Join key: per-slice `(normalized first flight number, YYYY-MM-DD)` across
all slices. Same shape as `pp.match.cash_match_key`, generalized to the
whole itinerary so round-trips don't collide on outbound alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import Cabin
    from .models import Itinerary, SearchResult


SliceKey = tuple[str, str]  # (FLIGHT_NUMBER_UPPER_NOSPACE, "YYYY-MM-DD")
ItineraryKey = tuple[SliceKey, ...]

_CASH_NUM_RE = re.compile(r"[\d,]*\d+(?:\.\d+)?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")


def _norm_fn(fn: str | None) -> str:
    return (fn or "").upper().replace(" ", "")


def _iso_date(s: str | None) -> str:
    if not s:
        return ""
    return s.replace(" ", "T")[:10]


def itinerary_key(itin: Itinerary) -> ItineraryKey | None:
    """Per-slice (first-flight#, date) tuple across all slices of an
    itinerary. None if any slice is missing both anchors — such itineraries
    can't be safely joined and are skipped.
    """
    details = itin.itinerary
    if not details or not details.slices:
        return None
    slice_keys: list[SliceKey] = []
    for s in details.slices:
        flights = s.flights or []
        fn = _norm_fn(flights[0]) if flights else ""
        dep = _iso_date(s.departure)
        if not fn or not dep:
            return None
        slice_keys.append((fn, dep))
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

    `itinerary` is the first occurrence we saw (used for render: slices,
    carriers, legroom). `prices` maps each cabin we have a price for to
    its raw price string (e.g. 'USD623.00'). Cabins with no price are
    absent from the dict — renderer treats absence as '—'.
    """

    itinerary: Itinerary
    prices: dict[Cabin, str] = field(default_factory=dict)


def merge(
    results_by_cabin: dict[Cabin, SearchResult],
    *,
    sort_by: Cabin,
    top_n: int,
    currency: str,
) -> list[MultiCabinRow]:
    """Join itineraries across cabins. Sorted by `sort_by`'s price under
    `price_rank`: rows priced in `currency` first by amount, any other
    currency after them; rows missing the sort cabin's price sink to the
    bottom. Truncated to `top_n` rows, so the trim never drops a fare for a
    smaller number in another currency.

    Itineraries that can't be keyed (missing flight# or departure on any
    slice) are skipped.
    """
    by_key: dict[ItineraryKey, MultiCabinRow] = {}
    for cabin, res in results_by_cabin.items():
        for it in res.solutions:
            key = itinerary_key(it)
            if key is None:
                continue
            row = by_key.get(key)
            if row is None:
                row = MultiCabinRow(itinerary=it)
                by_key[key] = row
            price = it.price
            if price:
                row.prices[cabin] = price

    def sort_value(row: MultiCabinRow) -> tuple[int, str, float]:
        price = row.prices.get(sort_by)
        return price_rank(price, parse_price(price), currency=currency)

    rows = sorted(by_key.values(), key=sort_value)
    return rows[:top_n]
