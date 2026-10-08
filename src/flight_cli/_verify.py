"""Price one Google Flights row on Matrix as exactly that itinerary.

A routing chain of the row's flight numbers fixes the flights and the first
day, not the itinerary: Matrix answers the same flights on other days too,
landing a day later, or with a middle flight a day later and both slice ends
unchanged. So the row's own itinerary is found in two steps. A solution must
have the row's flights and wall-clock slice ends to be a candidate, and a
candidate is the row only when its booking details, the one place Matrix dates
every flight, give each flight the row's day, minute and airports.

Pure: `cli` makes the requests and prints."""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from ._cross_check import separate_tickets_reason
from ._enrich import party_price
from ._multi_cabin import parse_price, price_currency
from .domain import Leg, SearchOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import (
        BookedItinerary,
        BookedLeg,
        BookedSegment,
        BookingDetails,
        Itinerary,
        SearchResult,
        Slice,
        SliceEndpoint,
    )

Outcome = Literal["match", "other-itinerary", "no-solution", "carrier-unseen", "separate-tickets"]


class Flight(NamedTuple):
    """One leg as either side states it. `departure` and `arrival` are the
    airport's wall clock to the minute, `YYYY-MM-DDTHH:MM`."""

    carrier: str
    number: str
    origin: str
    destination: str
    departure: str
    arrival: str

    @property
    def code(self) -> str:
        return f"{self.carrier}{self.number}"


class Row(NamedTuple):
    """A Google row: each slice's legs, the price the table prints, and how
    Google sells it when that is more than one ticket."""

    slices: tuple[tuple[Flight, ...], ...]
    price: str | None
    ticketing: str | None = None


class Verdict(NamedTuple):
    outcome: Outcome
    reason: str | None = None
    solution: Itinerary | None = None  # Matrix's solution that is the row, on a match
    details: BookingDetails | None = None
    missing_carriers: tuple[str, ...] = ()


def _code(airport: Any) -> str:
    """fli's airport enum by IATA code; a code starting with a digit is named
    with a leading underscore."""
    name: str = getattr(airport, "name", "") or ""
    return name.removeprefix("_")


def _endpoint(e: SliceEndpoint | None) -> str:
    return (e.code if e is not None else None) or ""


def _number(n: object) -> str:
    """A flight number without leading zeros, so "0021" and 21 compare equal."""
    return str(n).strip().lstrip("0") or "0"


def _token(code: str) -> str:
    """A Matrix flight token ("AS021") in the form `Flight.code` writes."""
    return f"{code[:2]}{_number(code[2:])}"


def wall_clock(ts: str | datetime | None) -> str:
    """`ts` as the airport's local time to the minute, or "" when unreadable.
    Matrix writes its UTC offset and Google writes none, so only the local time
    compares across the two."""
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ""
    if ts is None:
        return ""
    return ts.replace(tzinfo=None).isoformat(timespec="minutes")


def google_row(r: Any) -> Row:
    """The row `cli` numbered, from its fli result: a one-way row is one
    result, a round trip a tuple of them, priced by the last member as the
    table prices it."""
    members: list[Any] = list(cast("tuple[Any, ...]", r)) if isinstance(r, tuple) else [r]
    results: list[Any] = [getattr(m, "flight", m) for m in members]
    slices = tuple(
        tuple(
            Flight(
                carrier=_code(leg.airline),
                number=_number(leg.flight_number),
                origin=_code(leg.departure_airport),
                destination=_code(leg.arrival_airport),
                departure=wall_clock(leg.departure_datetime),
                arrival=wall_clock(leg.arrival_datetime),
            )
            for leg in fr.legs
        )
        for fr in results
    )
    fare = results[-1]
    price: float | None = fare.price
    kinds = {getattr(m, "ticketing", None) for m in members}
    return Row(
        slices,
        None if price is None else f"{fare.currency or 'USD'}{price:.2f}",
        next((k for k in ("self_transfer", "separate_tickets") if k in kinds), None),
    )


def _folded(codes: Sequence[str]) -> list[str]:
    """Consecutive legs under one number as one token: to Matrix a through
    flight is one flight."""
    out: list[str] = []
    for c in codes:
        if not out or out[-1] != c:
            out.append(c)
    return out


def routing(codes: Sequence[str]) -> str:
    """The routing chain naming exactly these flights, in order. One token per
    flight: a single number answers only itineraries of that one flight."""
    return " ".join(_folded(codes))


def routings(row: Row) -> list[str]:
    return [routing([f.code for f in s]) for s in row.slices]


def matrix_legs(row: Row, *, routed: bool = True) -> tuple[Leg, ...]:
    """Per slice, the first flight's origin and day and the last flight's
    destination; routed, the chain of its flights and nothing else."""
    return tuple(
        Leg.of(
            s[0].origin,
            s[-1].destination,
            date.fromisoformat(s[0].departure[:10]),
            route_language=routing([f.code for f in s]) if routed else None,
        )
        for s in row.slices
    )


def matrix_options(row: Row, opts: SearchOptions, *, max_stops: int | None = None) -> SearchOptions:
    """The search's cabin and travelers, in the row's currency. The page size
    is the default, never `-n`: the row's own itinerary can rank below the
    same flights on other days, so the chain's answer is read whole."""
    return SearchOptions(
        cabin=opts.cabin,
        pax=opts.pax,
        currency=price_currency(row.price) or opts.currency or "USD",
        max_extra_stops=max_stops,
    )


def most_stops(row: Row) -> int:
    return max(len(s) - 1 for s in row.slices)


def _connections(codes: Sequence[str], stops: Sequence[str]) -> list[str]:
    """The airports between two different flight numbers: `stops[i]` lies
    between `codes[i]` and `codes[i + 1]`. A stop inside one through flight is
    left out; either side may write it or not, and booking details state it."""
    return [s for s, (a, b) in zip(stops, pairwise(codes), strict=True) if a != b]


def _same_ends(ms: Slice, legs: tuple[Flight, ...]) -> bool:
    codes = [_token(f) for f in ms.flights]
    stops = [_endpoint(s) for s in ms.stops]
    row_codes = [f.code for f in legs]
    return (
        _folded(codes) == _folded(row_codes)
        and (
            stops == [f.destination for f in legs[:-1]]
            or (
                len(stops) == len(codes) - 1
                and _connections(codes, stops)
                == _connections(row_codes, [f.destination for f in legs[:-1]])
            )
        )
        and _endpoint(ms.origin) == legs[0].origin
        and _endpoint(ms.destination) == legs[-1].destination
        and wall_clock(ms.departure) == legs[0].departure
        and wall_clock(ms.arrival) == legs[-1].arrival
    )


def candidates(row: Row, res: SearchResult) -> list[int]:
    """Indexes of the solutions that can be the row, in Matrix's order: every
    slice has its flights, connections, end airports and wall-clock departure
    and arrival. Two can remain that differ only in a middle flight's day."""
    out: list[int] = []
    for i, sol in enumerate(res.solutions):
        slices = sol.itinerary.slices if sol.itinerary else []
        if len(slices) == len(row.slices) and all(
            _same_ends(ms, legs) for ms, legs in zip(slices, row.slices, strict=True)
        ):
            out.append(i)
    return out


def booked_flights(itinerary: BookedItinerary) -> tuple[tuple[Flight, ...], ...]:
    """Each booked slice's legs, carrier and number taken from the segment
    that flies them. A segment that lists no legs is its own one leg."""
    out: list[tuple[Flight, ...]] = []
    for s in itinerary.slices:
        legs: list[Flight] = []
        for seg in s.segments:
            carrier = (seg.carrier.code if seg.carrier else None) or ""
            number = seg.flight.number if seg.flight else None
            flown: Sequence[BookedLeg | BookedSegment] = seg.legs or [seg]
            legs.extend(
                Flight(
                    carrier=carrier,
                    number=_number(number) if number else "",
                    origin=_endpoint(leg.origin),
                    destination=_endpoint(leg.destination),
                    departure=wall_clock(leg.departure),
                    arrival=wall_clock(leg.arrival),
                )
                for leg in flown
            )
        out.append(tuple(legs))
    return tuple(out)


def _runs(legs: Sequence[Flight]) -> list[list[Flight]]:
    """Consecutive legs grouped by flight number."""
    out: list[list[Flight]] = []
    for leg in legs:
        if out and out[-1][0].code == leg.code:
            out[-1].append(leg)
        else:
            out.append([leg])
    return out


def _same_legs(google: Sequence[Flight], matrix: Sequence[Flight]) -> bool:
    gs, ms = _runs(google), _runs(matrix)
    if len(gs) != len(ms):
        return False
    for g, m in zip(gs, ms, strict=True):
        if g[0].code != m[0].code:
            return False
        if len(g) == len(m):
            if any(
                (a.origin, a.destination, a.departure) != (b.origin, b.destination, b.departure)
                for a, b in zip(g, m, strict=True)
            ):
                return False
        elif min(len(g), len(m)) == 1:
            # One side writes a through flight as a single leg, so its stop
            # dates nothing; the flight's two ends are what both sides state.
            if (g[0].origin, g[0].departure, g[-1].destination, g[-1].arrival) != (
                m[0].origin,
                m[0].departure,
                m[-1].destination,
                m[-1].arrival,
            ):
                return False
        else:
            return False
    return True


def same_flights(row: Row, itinerary: BookedItinerary) -> bool:
    """Whether the booked itinerary is the row: every flight's carrier and
    number, local departure day and minute, and airports, slice by slice, with
    every leg stating all of these and its arrival, so a match never leaves one
    unknown."""
    booked = booked_flights(itinerary)
    return (
        len(booked) == len(row.slices)
        and all(all(leg) for legs in booked for leg in legs)
        and all(_same_legs(g, m) for g, m in zip(row.slices, booked, strict=True))
    )


def _join(items: Sequence[str]) -> str:
    return ", ".join(items)


def on_separate_tickets(row: Row) -> Verdict | None:
    """The verdict on a row Google sells as separate tickets, decided without
    asking Matrix, which prices one ticket and so never this booking; None for
    a one-ticket row."""
    if row.ticketing is None:
        return None
    return Verdict("separate-tickets", separate_tickets_reason(row.ticketing))


def other_itinerary(answered: int) -> Verdict:
    plural = "y" if answered == 1 else "ies"
    return Verdict(
        "other-itinerary",
        f"Matrix prices these flights only on {answered:d} other itinerar{plural}, "
        "with a flight on another day, at another time or between other airports",
    )


def _field(obj: object, key: str) -> Any:
    """`obj[key]` when `obj` is a JSON object, else None."""
    return cast("dict[str, Any]", obj).get(key) if isinstance(obj, dict) else None


def listed_carriers(probe: SearchResult) -> set[str]:
    """Every carrier an unrouted answer names: its carrier-stop matrix columns,
    its carrier filter and the carriers of each itinerary on the page."""
    csm = probe.carrier_stop_matrix
    listed = {
        col.label.code for col in (csm.columns if csm else []) if col.label and col.label.code
    }
    groups = _field(_field(probe.raw, "itineraryCarrierList"), "groups")
    for g in cast("list[Any]", groups) if isinstance(groups, list) else []:
        code = _field(_field(g, "label"), "code")
        if isinstance(code, str):
            listed.add(code)
    for sol in probe.solutions:
        itn = sol.itinerary
        if itn is None:
            continue
        listed.update(c.code for c in itn.carriers if c.code)
        listed.update(f[:2] for s in itn.slices for f in s.flights)
    return listed


def unpriced(row: Row, probe: SearchResult) -> Verdict:
    """Why Matrix has no fare for the row's flights, from the same legs asked
    without the chain and with at most the row's most stops in a slice. A
    carrier is unseen when that answer lists itineraries and names none of
    its, which says nothing about Matrix's other trips."""
    stops = most_stops(row)
    within = f"with at most {stops:d} stop{'' if stops == 1 else 's'}"
    if not probe.solutions:
        return Verdict(
            "no-solution",
            f"Matrix returned no itinerary on these flights, and none {within} "
            "on the route that day",
        )
    listed = listed_carriers(probe)
    carriers = list(dict.fromkeys(f.carrier for s in row.slices for f in s))
    missing = tuple(c for c in carriers if c not in listed)
    if missing:
        read, total = len(probe.solutions), probe.solution_count
        of = f" of {total:d}" if total > read else ""
        return Verdict(
            "carrier-unseen",
            f"none of the {read:d}{of} trips Matrix returned {within} names "
            f"{_join(missing)} for this route and day; they name {_join(sorted(listed))}",
            missing_carriers=missing,
        )
    return Verdict(
        "no-solution",
        f"Matrix returned no fare on these flights, though it lists {_join(carriers)} "
        "on the route that day",
    )


def delta(google: str | None, matrix: str | None) -> float | None:
    """Google's price minus Matrix's, or None when either is missing or the
    two are in different currencies."""
    g, m = parse_price(google), parse_price(matrix)
    if g is None or m is None or price_currency(google) != price_currency(matrix):
        return None
    return round(g - m, 2)


def _slice_document(legs: Sequence[Flight]) -> dict[str, Any]:
    return {
        "flights": [f.code for f in legs],
        "dates": [f.departure[:10] for f in legs],
        "airports": [[f.origin, f.destination] for f in legs],
        "departure": legs[0].departure if legs else None,
        "arrival": legs[-1].arrival if legs else None,
    }


def fares(details: BookingDetails | None) -> list[dict[str, Any]]:
    """Per booked segment: its fare's basis and carrier, its booking code and cabin."""
    if details is None:
        return []
    return [
        {
            "origin": info.segment.origin if info.segment else None,
            "destination": info.segment.destination if info.segment else None,
            "carrier": fare.carrier,
            "fare_basis": fare.code,
            "booking_code": info.booking_code,
            "cabin": info.cabin,
        }
        for fare in details.fares
        for info in fare.booking_infos
    ]


def document(
    n: int, row: Row, verdict: Verdict, fare_rules: dict[str, Any] | None, passengers: int = 1
) -> dict[str, Any]:
    """The `verify` object of `--format json`. Matrix's side, the delta and the
    fares are there only on a match: a price for any other itinerary would be
    read as this row's. Matrix's price is for the party of `passengers`, as
    Google's is."""
    matched = verdict.outcome == "match" and verdict.solution is not None
    matrix: dict[str, Any] | None = None
    if matched and verdict.solution is not None:
        itn = verdict.details.itinerary if verdict.details else None
        matrix = {
            "price": party_price(verdict.solution, passengers),
            "per_traveler": verdict.solution.price,
            "total": verdict.details.display_total if verdict.details else None,
            "slices": [_slice_document(s) for s in booked_flights(itn)] if itn else [],
        }
    return {
        "row": n,
        "outcome": verdict.outcome,
        "reason": verdict.reason,
        "routing": routings(row),
        "google": {"price": row.price, "slices": [_slice_document(s) for s in row.slices]},
        "matrix": matrix,
        "delta": delta(row.price, matrix["price"]) if matrix else None,
        "missing_carriers": list(verdict.missing_carriers),
        "fares": fares(verdict.details) if matched else [],
        "fare_rules": fare_rules if matched else None,
    }
