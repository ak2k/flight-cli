"""Reconcile a fast Google Flights result with the authoritative Matrix result.

For a GF-serveable query we render GF immediately (~1s) then run Matrix and
repaint a merged table once it lands. This module is the pure reconcile step:
match itineraries across the two cash results and attribute each side's price.

Matching is by flight number + departure date per slice — which works now that
the gflight adapter emits marketing flight numbers (work-fjibi.1), the same
identity Matrix uses. Matched rows carry both prices (they should agree; we show
both, attributed); Matrix-only rows are added (its fare coverage is broader),
GF-only rows are kept and flagged (ULCC / codeshare inventory Matrix misses).
The Matrix itinerary is authoritative for a matched row's structure; the Google
slice adds the per-flight dates Matrix does not state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ._multi_cabin import price_currency, price_rank

if TYPE_CHECKING:
    from .models import Itinerary, SearchResult, Slice

_PRICE_DIGITS = re.compile(r"[\d,]*\d+")

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


def _price_int(price: str | None) -> int | None:
    """Leading integer units from 'USD877.00' / '$877' / '877 USD'; None when
    absent (`price_rank` sorts such rows last)."""
    if not price:
        return None
    m = _PRICE_DIGITS.search(price)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", "").split(".")[0])
    except ValueError:
        return None


def _rank_price(row: MergedRow, currency: str) -> str | None:
    """The price a row ranks on: its price in `currency`, Matrix's when both
    sides are, else Matrix's or Google's. A matched row priced in two
    currencies then ranks among the requested ones by the price it shows in
    that currency."""
    both = (row.matrix_price, row.gf_price)
    return next((p for p in both if price_currency(p) == currency), None) or (
        row.matrix_price or row.gf_price
    )


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


def _wall_clock(ts: str | None) -> datetime | None:
    """`ts` as the airport's local time. Matrix writes its UTC offset and
    Google writes none, so only the local time compares across the two."""
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None)
    except ValueError:
        return None


def _same_trip(ms: Slice, gs: Slice) -> bool:
    """Whether Google slice `gs` is Matrix slice `ms`'s own trip, so its
    per-flight dates are `ms`'s too.

    The match key fixes only the first flight's day. The landing day does not
    fix the last flight's: one flight number flown at different hours on two
    days can land on the same day both times. The landing minute does. That
    dates every flight of a one-stop slice; a middle flight of a longer one is
    fixed by neither end, and Matrix states no day for it."""
    arrival = _wall_clock(ms.arrival)
    return (
        ms.flights == gs.flights
        and len(gs.segment_dates) == len(ms.flights)
        and arrival is not None
        and arrival == _wall_clock(gs.arrival)
    )


def _date_lender(m: Itinerary, candidates: list[Itinerary]) -> Itinerary | None:
    """The Google row among `candidates`, the rows sharing `m`'s match key,
    that is `m`'s own trip on every slice. None when none is, or when several
    are but date a flight differently: Google's row order then says nothing
    about which of them `m` is."""
    mi = m.itinerary
    if mi is None:
        return None
    trips: list[tuple[Itinerary, list[list[str]]]] = []
    for g in candidates:
        gi = g.itinerary
        if (
            gi is not None
            and len(gi.slices) == len(mi.slices)
            and all(_same_trip(ms, gs) for ms, gs in zip(mi.slices, gi.slices, strict=True))
        ):
            trips.append((g, [gs.segment_dates for gs in gi.slices]))
    if not trips or any(dates != trips[0][1] for _, dates in trips):
        return None
    return trips[0][0]


def _with_google_dates(m: Itinerary, g: Itinerary) -> Itinerary:
    """`m` with each slice's per-flight dates taken from `g`, its
    `_date_lender`, which states them where Matrix states only the slice's
    two ends."""
    mi, gi = m.itinerary, g.itinerary
    if mi is None or gi is None:
        return m
    slices = [
        ms.model_copy(update={"segment_dates": list(gs.segment_dates)})
        for ms, gs in zip(mi.slices, gi.slices, strict=True)
    ]
    return m.model_copy(update={"itinerary": mi.model_copy(update={"slices": slices})})


def merge_results(gf: SearchResult, matrix: SearchResult, *, currency: str) -> list[MergedRow]:
    """Reconcile GF + Matrix cash results into price-sorted merged rows.

    Every row of either side is in exactly one merged row. The match key fixes
    only the flights and the first day, so one key can name several trips on
    either side. Each Matrix row of a key first takes the Google row that is
    its own trip (`_date_lender`); only then does the key's first Matrix row,
    if it found none, take the first Google row left, undated. Handing that
    one out first would price a Matrix trip with another trip's Google fare.
    Every Google row left over is a row of its own.

    Sorted under `price_rank` on each row's `_rank_price`: rows priced in
    `currency` first by amount, any other currency after them, so a caller
    trimming the list never drops a fare for a smaller number in another
    currency."""
    gf_keyed: dict[object, list[int]] = {}
    gf_unkeyed: list[Itinerary] = []
    for i, it in enumerate(gf.solutions):
        k = _itin_key(it)
        if k is None:
            gf_unkeyed.append(it)
        else:
            gf_keyed.setdefault(k, []).append(i)

    matrix_keyed: dict[object, list[Itinerary]] = {}
    matrix_unkeyed: list[Itinerary] = []
    for it in matrix.solutions:
        k = _itin_key(it)
        if k is None:
            matrix_unkeyed.append(it)
        else:
            matrix_keyed.setdefault(k, []).append(it)

    taken: set[int] = set()
    rows: list[MergedRow] = []
    # Matrix keys first (authoritative), then the Google rows none of them took.
    for k, ms in matrix_keyed.items():
        candidates = gf_keyed.get(k, [])
        lenders: list[int | None] = []
        for m in ms:
            free = [i for i in candidates if i not in taken]
            lender = _date_lender(m, [gf.solutions[i] for i in free])
            at = next((i for i in free if gf.solutions[i] is lender), None)
            if at is not None:
                taken.add(at)
            lenders.append(at)
        priced = list(lenders)
        if priced[0] is None and (left := [i for i in candidates if i not in taken]):
            priced[0] = left[0]
            taken.add(left[0])
        for m, lender, at in zip(ms, lenders, priced, strict=True):
            g = gf.solutions[at] if at is not None else None
            rows.append(
                MergedRow(
                    itinerary=m if lender is None else _with_google_dates(m, gf.solutions[lender]),
                    gf_price=g.price if g else None,
                    matrix_price=m.price,
                    source="both" if g else "matrix",
                )
            )
    left = sorted(i for listed in gf_keyed.values() for i in listed if i not in taken)
    rows.extend(
        MergedRow(itinerary=g, gf_price=g.price, matrix_price=None, source="gf")
        for g in (gf.solutions[i] for i in left)
    )
    rows.extend(
        MergedRow(itinerary=it, gf_price=None, matrix_price=it.price, source="matrix")
        for it in matrix_unkeyed
    )
    rows.extend(
        MergedRow(itinerary=it, gf_price=it.price, matrix_price=None, source="gf")
        for it in gf_unkeyed
    )

    def rank(row: MergedRow) -> tuple[int, str, float]:
        price = _rank_price(row, currency)
        return price_rank(price, _price_int(price), currency=currency)

    rows.sort(key=rank)
    return rows
