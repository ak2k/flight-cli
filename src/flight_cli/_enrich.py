"""Reconcile a fast Google Flights result with the authoritative Matrix result.

For a GF-serveable query we render GF immediately (~1s) then run Matrix and
repaint a merged table once it lands. This module is the pure reconcile step:
match itineraries across the two cash results and attribute each side's price.

Matching is by flight number + departure date per slice — which works now that
the gflight adapter emits marketing flight numbers (work-fjibi.1), the same
identity Matrix uses. Matched rows carry both prices, attributed; rows of either
side alone are kept and tagged with their source. A Google-only row is often a
trip on a carrier Matrix does price that Matrix's pruned answer left out, so the
tag alone says nothing about why: `_cross_check` states the reason where the two
answers decide one. The Matrix itinerary is authoritative for a matched row's
structure; the Google slice adds the per-flight dates Matrix does not state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ._multi_cabin import parse_price, price_currency, price_rank

if TYPE_CHECKING:
    from .models import Itinerary, SearchResult, Slice

Source = str  # "both" | "matrix" | "gf"


@dataclass(frozen=True, slots=True)
class MergedRow:
    """One row of the reconciled GF+Matrix view.

    `itinerary` is the structure to display (Matrix-authoritative when matched).
    `gf_price` / `matrix_price` are the attributed price strings from each side
    (None when that side didn't have this itinerary), each for the whole party,
    as Google prices it: `matrix_price` is `party_price`. `source` records which
    backend(s) produced it.

    `google` is the Google row whose price is `gf_price`, the board's own
    object. `same_trip` holds only where that row is the Matrix row's own trip
    (`_date_lender`); a Matrix row priced by a Google row left over lands within
    `_LANDING_SKEW` of it but is not known to be its trip."""

    itinerary: Itinerary
    gf_price: str | None
    matrix_price: str | None
    source: Source
    google: Itinerary | None = None
    same_trip: bool = False


def party_price(it: Itinerary, passengers: int) -> str | None:
    """Matrix's price for its solution `it` and a party of `passengers`, the
    one the merged table prints and a price cap on that table reads: the
    listed price, one passenger's rounded up, for one; for more, the total
    Matrix states, or None where it states none."""
    if it.price is None or passengers <= 1:
        return it.price
    return it.display_total


def _rank_price(row: MergedRow, currency: str) -> str | None:
    """The price a row ranks on: the lowest it prints in `currency`, or where
    it prints none in it, the lowest in the currency of Matrix's price, else
    Google's. So the dearer of a row's two prices never sorts it below a
    dearer row, or out of the first `-n`."""
    printed = [p for p in (row.matrix_price, row.gf_price) if p]
    if not printed:
        return None
    codes = [price_currency(p) for p in printed]
    code = currency if currency in codes else codes[0]
    return min((p for p, c in zip(printed, codes, strict=True) if c == code), key=_exact_amount)


def _exact_amount(price: str) -> tuple[bool, float]:
    """Sort key on a price's amount, a price with none after every one with one."""
    amount = parse_price(price)
    return (amount is None, amount or 0.0)


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


# How far apart two sources may state one trip's landing: a few minutes.
_LANDING_SKEW = timedelta(minutes=5)


def _lands_near(m: Itinerary, g: Itinerary) -> bool:
    """Whether Google row `g` may be Matrix row `m`'s trip, the two sharing a
    match key: on no slice do both state a landing and state it more than
    `_LANDING_SKEW` apart. A landing a source leaves unstated rules nothing out."""
    mi, gi = m.itinerary, g.itinerary
    if mi is None or gi is None or len(mi.slices) != len(gi.slices):
        return False
    for ms, gs in zip(mi.slices, gi.slices, strict=True):
        ma, ga = _wall_clock(ms.arrival), _wall_clock(gs.arrival)
        if ma is not None and ga is not None and abs(ma - ga) > _LANDING_SKEW:
            return False
    return True


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


def _priced_by_near(
    ms: list[Itinerary],
    lenders: list[int | None],
    candidates: list[int],
    solutions: list[Itinerary],
) -> list[int | None]:
    """For each Matrix row of a key, the index of the Google row that prices
    it: its `lenders` entry, else, in Matrix order, the first of `candidates`
    not yet claimed that lands near it (`_lands_near`), else None."""
    priced = list(lenders)
    claimed = {i for i in lenders if i is not None}
    for n, m in enumerate(ms):
        if priced[n] is None:
            near = next(
                (i for i in candidates if i not in claimed and _lands_near(m, solutions[i])), None
            )
            if near is not None:
                priced[n] = near
                claimed.add(near)
    return priced


def merge_results(
    gf: SearchResult, matrix: SearchResult, *, currency: str, passengers: int = 1
) -> list[MergedRow]:
    """Reconcile GF + Matrix cash results into price-sorted merged rows, every
    price for the party of `passengers`, as Google's already is.

    Every row of either side is in exactly one merged row. The match key fixes
    only the flights and the first day, so one key can name several trips on
    either side. Each Matrix row of a key first takes the Google row that is
    its own trip (`_date_lender`); only then does each Matrix row that found
    none, in Matrix order, take the first Google row left that lands within
    `_LANDING_SKEW` of it (`_lands_near`), undated. Handing those out first
    would price a Matrix trip with another trip's Google fare, and a Google row
    landing on another day is never one.
    Every Google row left over is a row of its own, and so is every row Google
    sells as separate tickets: it is another booking than Matrix's one ticket
    on the same flights, so the two prices are not one trip's.

    Sorted under `price_rank` on each row's `_rank_price`, the lowest price it
    prints: rows priced in `currency` first by amount, any other currency after
    them, so a caller trimming the list never drops a fare for a smaller number
    in another currency. Rows tied on it keep the order above, a matched row
    ahead of a Google row alone."""
    gf_keyed: dict[object, list[int]] = {}
    gf_unkeyed: list[Itinerary] = []
    for i, it in enumerate(gf.solutions):
        k = _itin_key(it)
        if k is None or it.ticketing is not None:
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
        priced = _priced_by_near(ms, lenders, candidates, gf.solutions)
        taken.update(i for i in priced if i is not None)
        for m, lender, at in zip(ms, lenders, priced, strict=True):
            g = gf.solutions[at] if at is not None else None
            rows.append(
                MergedRow(
                    itinerary=m if lender is None else _with_google_dates(m, gf.solutions[lender]),
                    gf_price=g.price if g else None,
                    matrix_price=party_price(m, passengers),
                    source="both" if g else "matrix",
                    google=g,
                    same_trip=lender is not None,
                )
            )
    left = sorted(i for listed in gf_keyed.values() for i in listed if i not in taken)
    rows.extend(
        MergedRow(itinerary=g, gf_price=g.price, matrix_price=None, source="gf", google=g)
        for g in (gf.solutions[i] for i in left)
    )
    rows.extend(
        MergedRow(
            itinerary=it, gf_price=None, matrix_price=party_price(it, passengers), source="matrix"
        )
        for it in matrix_unkeyed
    )
    rows.extend(
        MergedRow(itinerary=it, gf_price=it.price, matrix_price=None, source="gf", google=it)
        for it in gf_unkeyed
    )

    def rank(row: MergedRow) -> tuple[int, str, float]:
        price = _rank_price(row, currency)
        return price_rank(price, parse_price(price), currency=currency)

    rows.sort(key=rank)
    return rows
