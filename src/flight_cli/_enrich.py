"""Reconcile a fast Google Flights result with the authoritative Matrix result.

For a GF-serveable query we render GF immediately (~1s) then run Matrix and
repaint a merged table once it lands. This module is the pure reconcile step:
match itineraries across the two cash results and attribute each side's price.

Matching is by flight number + departure date per slice — which works now that
the gflight adapter emits marketing flight numbers (work-fjibi.1), the same
identity Matrix uses. Matched rows carry both prices (they should agree; we show
both, attributed); Matrix-only rows are added (its fare coverage is broader),
GF-only rows are kept and flagged (ULCC / codeshare inventory Matrix misses).
The Matrix itinerary is authoritative for a matched row's structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Itinerary, SearchResult

_PRICE_DIGITS = re.compile(r"[\d,]*\d+")
_NO_PRICE = 10**12  # sort key for itineraries with no parseable price (last)

Source = str  # "both" | "matrix" | "gf"


@dataclass(frozen=True, slots=True)
class MergedRow:
    """One row of the reconciled GF+Matrix view.

    `itinerary` is the structure to display (Matrix-authoritative when matched).
    `gf_price` / `matrix_price` are the attributed price strings from each side
    (None when that side didn't have this itinerary). `source` records which
    backend(s) produced it."""

    itinerary: Itinerary
    gf_price: str | None
    matrix_price: str | None
    source: Source


def _price_int(price: str | None) -> int:
    """Leading integer dollars from 'USD877.00' / '$877' / '877 USD'; _NO_PRICE
    when absent (sorts such rows last)."""
    if not price:
        return _NO_PRICE
    m = _PRICE_DIGITS.search(price)
    if not m:
        return _NO_PRICE
    try:
        return int(m.group(0).replace(",", "").split(".")[0])
    except ValueError:
        return _NO_PRICE


def _itin_key(it: Itinerary) -> tuple[tuple[tuple[str, ...], str], ...] | None:
    """Match key: per slice, (flight numbers, departure date). None when the
    itinerary lacks the structure to match on (kept as a single-source row)."""
    itn = it.itinerary
    if itn is None or not itn.slices:
        return None
    parts: list[tuple[tuple[str, ...], str]] = []
    for s in itn.slices:
        if not s.flights or not s.departure:
            return None
        parts.append((tuple(s.flights), s.departure[:10]))
    return tuple(parts)


def merge_results(gf: SearchResult, matrix: SearchResult) -> list[MergedRow]:
    """Reconcile GF + Matrix cash results into price-sorted merged rows."""
    gf_keyed: dict[object, Itinerary] = {}
    gf_unkeyed: list[Itinerary] = []
    for it in gf.solutions:
        k = _itin_key(it)
        if k is None:
            gf_unkeyed.append(it)
        else:
            gf_keyed.setdefault(k, it)

    matrix_keyed: dict[object, Itinerary] = {}
    matrix_unkeyed: list[Itinerary] = []
    for it in matrix.solutions:
        k = _itin_key(it)
        if k is None:
            matrix_unkeyed.append(it)
        else:
            matrix_keyed.setdefault(k, it)

    rows: list[MergedRow] = []
    # Matrix keys first (authoritative), then GF-only keys.
    for k, m in matrix_keyed.items():
        g = gf_keyed.get(k)
        rows.append(
            MergedRow(
                itinerary=m,  # Matrix structure authoritative when matched
                gf_price=g.price if g else None,
                matrix_price=m.price,
                source="both" if g else "matrix",
            )
        )
    for k, g in gf_keyed.items():
        if k not in matrix_keyed:
            rows.append(MergedRow(itinerary=g, gf_price=g.price, matrix_price=None, source="gf"))
    rows.extend(
        MergedRow(itinerary=it, gf_price=None, matrix_price=it.price, source="matrix")
        for it in matrix_unkeyed
    )
    rows.extend(
        MergedRow(itinerary=it, gf_price=it.price, matrix_price=None, source="gf")
        for it in gf_unkeyed
    )

    rows.sort(key=lambda r: _price_int(r.matrix_price or r.gf_price))
    return rows
