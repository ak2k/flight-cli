"""Explain the Google-vs-Matrix table: each row's delta, or why it has none.

A pure step after `_enrich.merge_results`. It reads the merged rows beside the
two answers they came from and states only what those answers decide, so a
value neither side gives (a flight's day Matrix does not state, the solutions
past Matrix's page) is never read as an answer.

Matrix answers in price order and `solution_count` is its whole answer, so the
answer is complete when the page lists that many. Its answer is pruned rather
than its inventory: a carrier missing from a complete answer is missing from
that answer, which is all a reason says of it. Point of sale is never a reason:
Google is always asked from the US, Matrix is sent no sales city, and no row
says where it was priced.

The rows and the document are plain text and plain JSON; the console sanitizing
belongs to the renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

# The merge's own reading of a landing time, so a pair and a reason compare
# trips the same way, and its own price for the party.
from ._enrich import (
    _wall_clock,  # pyright: ignore[reportPrivateUsage] — see above
    party_price,
)
from ._multi_cabin import price_currency

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .models import Itinerary, SearchResult, Slice

DELTA = "google_minus_matrix"

_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})\d+$", re.IGNORECASE)
_PRICE_RE = re.compile(r"^([A-Z]{3})([\d,]*\d(?:\.\d+)?)$")
_SOURCE = {"both": "both", "gf": "google", "matrix": "matrix"}
_ROUND_TRIP_SLICES = ("out", "back")
# A row whose trip is unstated in part may or may not be on the other side.
_UNMATCHED = ("unmatched", "cannot be matched: a flight, day or landing is unstated")

# (flight numbers, departure day) of one slice: the merge's match key.
_SliceKey = tuple[tuple[str, ...], str]
# The key plus the landing minute, which is as far as both sides state a trip.
_TripSlice = tuple[tuple[str, ...], str, datetime]


@dataclass(frozen=True, slots=True)
class Answers:
    """The two answers a table compares.

    `matrix` is Matrix's whole page after the price cap. `google` is Google's
    board as the merge read it, or None when Google gave no answer. Only its
    one-ticket rows are compared with Matrix, which sells one ticket: a trip
    Google sells as separate tickets is not that trip's one-ticket price.
    `google_filtered` says the row filter removed rows from that board, which
    then cannot show a flight to be absent from Google. `google_partial` says
    the board stops short of what one search would list (a page of a search
    asked as several missing, or a round trip's returns priced page by page),
    so a carrier it lacks may fly where it was not asked. `stop_limit` says a
    stop limit was sent; without one Matrix searches one flight beyond the
    fewest a slice needs. `passengers` is the party: Google prices all of it,
    while the price Matrix lists is one passenger's, rounded up. `uncapped` is
    Matrix's page before the cap, or None where it is `matrix`: a trip the cap
    cut is still one Matrix answered, and the cap cuts fares, not the flights
    Matrix searched. `google_unread` counts the rows Google served that the
    parser could not read: any trip may be one of them, so while it is non-zero
    the board shows no flight absent from Google."""

    matrix: SearchResult
    google: SearchResult | None
    google_filtered: bool
    stop_limit: bool
    round_trip: bool
    currency: str
    passengers: int = 1
    uncapped: SearchResult | None = None
    google_partial: bool = False
    google_unread: int = 0


@dataclass(frozen=True, slots=True)
class Boundary:
    """Where Matrix's page ends, and how much Google listed and left unread:
    `google_listed` one-ticket rows and `google_separate` rows on separate
    tickets."""

    listed: int
    solution_count: int
    last_price: str | None
    google_listed: int
    google_answered: bool
    google_unread: int = 0
    google_separate: int = 0

    @property
    def complete(self) -> bool:
        return self.solution_count <= self.listed


@dataclass(frozen=True, slots=True)
class RowCheck:
    """One row: Google's price minus Matrix's, or the reasons there is none.

    `reasons` are codes and `reason` their text, joined; both are empty where
    there is a delta. `matrix_price` is the row's own, Matrix's price for the
    party, the one `delta` subtracts, or None where Matrix states none."""

    delta: float | None
    reasons: tuple[str, ...]
    reason: str | None
    matrix_price: str | None = None


@dataclass(frozen=True, slots=True)
class CrossCheck:
    currency: str
    boundary: Boundary
    rows: tuple[RowCheck, ...]


def cross_check(rows: Sequence[Any], answers: Answers) -> CrossCheck:
    """`rows` (merged rows, in the table's order) explained against `answers`.

    Row attributes are read with defaults, so a row built without the merge's
    `google` / `same_trip` reads as an unconfirmed pair, never as a confirmed
    one."""
    bnd = _boundary(answers)
    facts = _Facts.of(answers)
    return CrossCheck(
        currency=answers.currency,
        boundary=bnd,
        rows=tuple(_check_row(r, answers, bnd, facts) for r in rows),
    )


def document(rows: Sequence[Any], xc: CrossCheck) -> dict[str, Any]:
    """`xc` as JSON: the boundary, then one entry per row of `rows`, which are
    the rows `xc` explains, in its order. Prices are each side's own strings
    for the party and `delta` a number."""
    b = xc.boundary
    return {
        "currency": xc.currency,
        "delta": DELTA,
        "matrix": {
            "listed": b.listed,
            "solution_count": b.solution_count,
            "complete": b.complete,
            "last_price": b.last_price,
        },
        "google": {
            "listed": b.google_listed,
            "answered": b.google_answered,
            "unread": b.google_unread,
            # Only where Google listed any, as the table's caption says it.
            **({"separate": b.google_separate} if b.google_separate else {}),
        },
        "rows": [_row_document(r, c) for r, c in zip(rows, xc.rows, strict=True)],
    }


def every_matrix_price_in(rows: Sequence[Any], currency: str) -> bool:
    """Whether every row Matrix is on states its price for the party in
    `currency`, so that a price under the lowest of them is under every fare
    in Matrix's answer."""
    return all(
        (m := _money(r.matrix_price)) is not None and m[0] == currency
        for r in rows
        if r.source != "gf"
    )


def lowest_matrix_price(rows: Sequence[Any], currency: str) -> str | None:
    """Matrix's cheapest price for the party in `currency` among `rows`, the
    merged rows of its whole capped page, or None where it prices none in it."""
    low: tuple[Decimal, str] | None = None
    for r in rows:
        m = _money(r.matrix_price)
        if m is not None and m[0] == currency and (low is None or m[1] < low[0]):
            low = (m[1], r.matrix_price)
    return low[1] if low is not None else None


def low_row(
    rows: Sequence[Any],
    matrix_low: str | None,
    currency: str,
    *,
    comparable: Callable[[Any], bool] | None = None,
) -> int | None:
    """The 1-based number, among `rows` in the table's order, of the first
    Google-only row whose price is in `currency` and under `matrix_low`,
    Matrix's cheapest price for the party; where Matrix prices nothing,
    the first Google-only row priced in `currency`. None when no row is.

    A row on both sides is never chosen: Matrix has already priced its
    flights. Nor is a row Google sells as separate tickets: Matrix prices one
    ticket, so its price for those flights is no check on that booking's. Nor
    is a row `comparable` refuses, whose Google price is for another cabin
    than Matrix is asked in."""
    low = _money(matrix_low)
    if low is not None and low[0] != currency:
        return None
    for n, r in enumerate(rows, 1):
        g = _money(r.gf_price)
        if (
            r.source == "gf"
            and getattr(r.itinerary, "ticketing", None) is None
            and g is not None
            and g[0] == currency
            and (low is None or g[1] < low[1])
            and (comparable is None or comparable(r))
        ):
            return n
    return None


def _row_document(row: Any, c: RowCheck) -> dict[str, Any]:
    return {
        "source": _SOURCE.get(row.source, row.source),
        "google_price": row.gf_price,
        "matrix_price": c.matrix_price,
        "delta": c.delta,
        "reasons": list(c.reasons),
        "reason": c.reason,
        "slices": [
            {
                "flights": list(s.flights),
                "departure": s.departure,
                "arrival": s.arrival,
                "segment_dates": list(s.segment_dates),
            }
            for s in _slices(row.itinerary)
        ],
    }


def _boundary(a: Answers) -> Boundary:
    sols = a.matrix.solutions
    google = a.google.solutions if a.google is not None else []
    one_ticket = len(_one_ticket(google))
    return Boundary(
        listed=len(sols),
        solution_count=a.matrix.solution_count,
        last_price=party_price(sols[-1], a.passengers) if sols else None,
        google_listed=one_ticket,
        google_answered=a.google is not None,
        google_unread=a.google_unread,
        google_separate=len(google) - one_ticket,
    )


def _one_ticket(its: Iterable[Itinerary]) -> list[Itinerary]:
    return [it for it in its if it.ticketing is None]


def separate_tickets_reason(ticketing: str) -> str:
    """Why a row Google sells as separate tickets has no Matrix price."""
    sold = "a self transfer on separate tickets" if ticketing == "self_transfer" else None
    return f"Google sells this trip as {sold or 'separate tickets'}; Matrix prices one ticket"


@dataclass(frozen=True, slots=True)
class _Facts:
    """What each whole answer holds, read once for every row."""

    matrix_carriers: frozenset[str]
    # Per slice index, the fewest flights any Matrix row has on that slice,
    # before the price cap.
    matrix_fewest: dict[int, int]
    matrix_trips: frozenset[tuple[_TripSlice, ...]]
    # The trips the price cap cut from Matrix's page, each with Matrix's price
    # for the party.
    matrix_cut: dict[tuple[_TripSlice, ...], str | None]
    google_carriers: frozenset[str]
    google_outbounds: frozenset[_SliceKey]
    google_trips: frozenset[tuple[_TripSlice, ...]]

    @classmethod
    def of(cls, a: Answers) -> _Facts:
        page = a.uncapped if a.uncapped is not None else a.matrix
        fewest: dict[int, int] = {}
        for it in page.solutions:
            for i, s in enumerate(_slices(it)):
                if s.flights:
                    fewest[i] = min(fewest.get(i, len(s.flights)), len(s.flights))
        google = _one_ticket(a.google.solutions) if a.google is not None else []
        board = _slices_of(google)
        # Every carrier Google names on a flight, as seller and as metal: a
        # carrier is absent from the board only where no flight names it.
        named = [
            c.upper()
            for s in board
            for leg in s.legs
            for c in (*leg.marketing_carriers, leg.operating_carrier)
            if c
        ]
        # The carriers Matrix names per itinerary count as listed too, so an
        # absence is never claimed for one its answer does name.
        listed = [
            c.code.upper()
            for it in a.matrix.solutions
            if it.itinerary is not None
            for c in it.itinerary.carriers
            if c.code
        ]
        trips = _trips(a.matrix.solutions)
        cut: dict[tuple[_TripSlice, ...], str | None] = {}
        for it in page.solutions:
            if (t := _trip(_slices(it))) is not None and t not in trips:
                cut.setdefault(t, party_price(it, a.passengers))
        return cls(
            matrix_carriers=frozenset([*_carriers(_slices_of(a.matrix.solutions)), *listed]),
            matrix_fewest=fewest,
            matrix_trips=trips,
            matrix_cut=cut,
            google_carriers=frozenset([*_carriers(board), *named]),
            google_outbounds=frozenset(
                k for it in google if (sl := _slices(it)) and (k := _key(sl[0])) is not None
            ),
            google_trips=_trips(google),
        )


def _check_row(row: Any, a: Answers, bnd: Boundary, facts: _Facts) -> RowCheck:
    slices = _slices(row.itinerary)
    mp = row.matrix_price
    found: list[tuple[str, str]]
    sold: str | None = getattr(row.itinerary, "ticketing", None)
    match row.source:
        case "both":
            return _priced_by_both(row, mp, slices, a.round_trip)
        case "gf" if sold is not None:
            found = [("separate_tickets", separate_tickets_reason(sold))]
        case "gf":
            found = _google_only(slices, a, bnd, facts)
        case "matrix":
            found = _matrix_only(slices, a, bnd, facts)
        case _:
            return RowCheck(delta=None, reasons=(), reason=None, matrix_price=mp)
    return RowCheck(
        delta=None,
        reasons=tuple(code for code, _ in found),
        reason="; ".join(text for _, text in found),
        matrix_price=mp,
    )


def _priced_by_both(row: Any, mp: str | None, slices: list[Slice], round_trip: bool) -> RowCheck:
    """A delta only for the same trip in one currency, from Matrix's price
    for the party `mp`."""
    found: list[tuple[str, str]] = []
    google: Itinerary | None = getattr(row, "google", None)
    if not getattr(row, "same_trip", False):
        found.append(("trip_unconfirmed", _landings(slices, _slices(google), round_trip)))
    gp = row.gf_price
    g, m = _money(gp), _money(mp)
    if not gp or not mp:
        found.append(("unpriced", f"no {'Google' if not gp else 'Matrix'} price to compare"))
    elif g is None or m is None or g[0] != m[0]:
        found.append(
            ("other_currency", f"Matrix in {_currency_name(mp)}, Google in {_currency_name(gp)}")
        )
    elif not found:
        return RowCheck(delta=float(g[1] - m[1]), reasons=(), reason=None, matrix_price=mp)
    return RowCheck(
        delta=None,
        reasons=tuple(code for code, _ in found),
        reason="; ".join(text for _, text in found),
        matrix_price=mp,
    )


def _google_only(
    slices: list[Slice], a: Answers, bnd: Boundary, facts: _Facts
) -> list[tuple[str, str]]:
    trip = _trip(slices)
    if trip is not None and trip in facts.matrix_trips:
        # A Matrix row states these flights, this first day and every landing
        # minute, and the merge did not pair the two: a flight between the
        # ends can fly on another day, and neither side says which, so this
        # row is not shown to be absent from Matrix's answer.
        return [("paired_elsewhere", "Matrix prices these flights on another row")]
    if trip is not None and trip in facts.matrix_cut:
        mp = facts.matrix_cut[trip]
        return [
            ("capped", "the price cap cut Matrix's fare for this trip" + (f", {mp}" if mp else ""))
        ]
    found: list[tuple[str, str]] = []
    if bnd.complete and (
        absent := [c for c in _carriers(slices) if c not in facts.matrix_carriers]
    ):
        found.append(
            ("carrier_absent", f"no {_either(absent)} flight in Matrix's answer of {bnd.listed}")
        )
    if not a.stop_limit and (over := _past_the_stop_window(slices, facts, a.round_trip)):
        found.append(("stops_outside", over))
    if not bnd.complete:
        to = f", to {bnd.last_price}" if bnd.last_price else ""
        found.append(("past_page", f"Matrix listed only {bnd.listed} of {bnd.solution_count}{to}"))
    if trip is None:
        return found or [_UNMATCHED]
    return found or [("not_in_matrix", f"not in Matrix's answer of {bnd.listed}")]


def _matrix_only(
    slices: list[Slice], a: Answers, bnd: Boundary, facts: _Facts
) -> list[tuple[str, str]]:
    if a.google is None:
        return [("no_google_answer", "Google gave no answer")]
    trip = _trip(slices)
    if trip is not None and trip in facts.google_trips:
        return [("paired_elsewhere", "Google prices these flights on another row")]
    found: list[tuple[str, str]] = []
    unread = (
        ("google_unread", f"{a.google_unread} of Google's rows could not be read")
        if a.google_unread
        else None
    )
    # A round trip's board holds combinations only for the outbounds Google
    # pinned, so a carrier is absent from it only beside an outbound it priced.
    # An outbound with no flights or day may be one of those, or not.
    out = _key(slices[0]) if slices else None
    if not a.google_filtered and (out is not None or not a.round_trip):
        if a.round_trip and out not in facts.google_outbounds:
            found.append(("outbound_not_priced", "Google priced no return for this outbound"))
        elif not a.google_partial and (
            absent := [c for c in _carriers(slices) if c not in facts.google_carriers]
        ):
            found.append(
                unread
                or ("carrier_absent_google", f"no {_either(absent)} flight on Google's board")
            )
    if trip is None:
        return found or [_UNMATCHED]
    return found or [unread or ("not_on_google", f"not among Google's {bnd.google_listed} rows")]


def _past_the_stop_window(slices: list[Slice], facts: _Facts, round_trip: bool) -> str | None:
    """Which slices have more flights than Matrix searched: one beyond the
    fewest it listed there. The fewest it listed is at least the route's own
    minimum, so a slice past it is past Matrix's limit too. A slice Matrix
    listed no row on has no minimum to measure from."""
    parts: list[str] = []
    for i, s in enumerate(slices):
        fewest = facts.matrix_fewest.get(i)
        if fewest is not None and len(s.flights) > fewest + 1:
            parts.append(
                f"{len(s.flights)} flights{_where(i, round_trip, ' {}')}; "
                f"Matrix searched up to {fewest + 1}"
            )
    return ", ".join(parts) or None


def _landings(matrix: list[Slice], google: list[Slice], round_trip: bool) -> str:
    """Both sides' landings, on the slices where they differ, or on every
    slice where none does (the flights between the ends are then what is
    unconfirmed)."""
    pairs = [
        (i, _wall_clock(g.arrival), _wall_clock(m.arrival))
        for i, (m, g) in enumerate(zip(matrix, google, strict=False))
    ]
    if not pairs:
        return "trip unconfirmed"
    shown = [p for p in pairs if p[1] != p[2]] or pairs
    parts = [
        f"{_where(i, round_trip, '{}: ')}Google lands {_minute(g)}, Matrix {_minute(m)}"
        for i, g, m in shown
    ]
    return "trip unconfirmed: " + ", ".join(parts)


def _where(i: int, round_trip: bool, shape: str) -> str:
    """Slice `i`'s name in `shape`, or nothing on a one-way."""
    return shape.format(_ROUND_TRIP_SLICES[i]) if round_trip and i < len(_ROUND_TRIP_SLICES) else ""


def _minute(t: datetime | None) -> str:
    return t.strftime("%m-%d %H:%M") if t is not None else "unstated"


def _currency_name(price: str) -> str:
    return price_currency(price) or "no named currency"


def _either(codes: list[str]) -> str:
    return codes[0] if len(codes) == 1 else ", ".join(codes[:-1]) + " or " + codes[-1]


def _money(price: str | None) -> tuple[str, Decimal] | None:
    m = _PRICE_RE.match(price or "")
    if m is None:
        return None
    try:
        return m.group(1), Decimal(m.group(2).replace(",", ""))
    except InvalidOperation:
        return None


def _slices(it: Itinerary | None) -> list[Slice]:
    itn = it.itinerary if it is not None else None
    return list(itn.slices) if itn is not None else []


def _slices_of(its: Iterable[Itinerary]) -> list[Slice]:
    return [s for it in its for s in _slices(it)]


def _carriers(slices: Iterable[Slice]) -> list[str]:
    """The carriers named by the slices' flight numbers, in order, once each.
    A number that does not parse names none, so it never reads as absent."""
    out: list[str] = []
    for s in slices:
        for fn in s.flights:
            m = _FLIGHT_RE.match(fn.strip())
            if m is not None and (c := m.group(1).upper()) not in out:
                out.append(c)
    return out


def _key(s: Slice) -> _SliceKey | None:
    if not s.flights or not s.departure:
        return None
    return tuple(s.flights), s.departure[:10]


def _trip(slices: list[Slice]) -> tuple[_TripSlice, ...] | None:
    """Each slice's flights, first day and landing minute; None where any of
    them is unstated, so an unknown never matches."""
    out: list[_TripSlice] = []
    for s in slices:
        k, lands = _key(s), _wall_clock(s.arrival)
        if k is None or lands is None:
            return None
        out.append((*k, lands))
    return tuple(out) or None


def _trips(its: Iterable[Itinerary]) -> frozenset[tuple[_TripSlice, ...]]:
    return frozenset(t for it in its if (t := _trip(_slices(it))) is not None)
