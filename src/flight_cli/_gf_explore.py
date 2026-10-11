"""Google Flights' explore page: where one origin flies, and for how much.

The page lists destinations in its answer to its own `GetExploreDestinations`
request, which the page signs; its HTML carries none of them. So Chrome opens
the explore URL and `GfBrowserSession.capture` hands back the response the page
received. Nothing here writes a request or runs script in the page.

The answer comes in two halves joined on a destination id: an info row per
destination (name, country, dates, the airport the page shows) and, in later
chunks, a price row per destination it priced (fare, carrier, stops, duration,
and the airport the fare flies to). A price cap removes prices, not
destinations, so a destination with no price row is one Google did not price
under this query.
"""

from __future__ import annotations

import math
import urllib.parse
from datetime import date
from http import HTTPStatus
from typing import Any, NamedTuple, cast

from ._gf_rpc_shared import GfPageRpcError, capture, dig, result_payloads, url_currency

_EXPLORE_RPC = "/GetExploreDestinations"
_WHAT = "Google Flights' explore page response"
# How far out the page's month field reaches: the current month and the next
# five, the same six months its "next 6 months" choice spans. The field carries
# no year, so a month past that is read as the same month of another year.
MONTHS_AHEAD = 5


class TripLength(NamedTuple):
    """One of the page's trip lengths. `code` is what its URL writes (None
    where it writes nothing); `nights` is the range its answers were measured
    to span."""

    name: str
    code: int | None
    nights: tuple[int, int]


TRIP_LENGTHS = (
    TripLength("weekend", 1, (1, 4)),
    TripLength("one week", None, (6, 9)),
    TripLength("two weeks", 3, (13, 16)),
)
ONE_WEEK = TRIP_LENGTHS[1]


def trip_lengths_overlapping(lo: int, hi: int) -> list[TripLength]:
    """The trip lengths whose nights overlap `lo`-`hi`."""
    return [t for t in TRIP_LENGTHS if t.nights[0] <= hi and lo <= t.nights[1]]


def months_open(today: date) -> list[date]:
    """The first day of every month the page's month field can name from `today`."""
    first = today.replace(day=1)
    out: list[date] = []
    for step in range(MONTHS_AHEAD + 1):
        year, month = divmod(first.month - 1 + step, 12)
        out.append(date(first.year + year, month + 1, 1))
    return out


class Destination(NamedTuple):
    """One destination. `code` is the airport the fare flies to, which can
    differ from the city's own (Miami's fares fly to FLL); several destinations
    can share one (Dallas and Fort Worth both fly to DFW)."""

    name: str | None
    country: str | None
    code: str | None
    price: float | None
    departure: date | None
    return_date: date | None
    carrier: str | None
    stops: int | None
    duration_min: int | None

    @property
    def nights(self) -> int | None:
        """Nights between the outbound and the return, where both are dated."""
        if self.departure is None or self.return_date is None:
            return None
        return (self.return_date - self.departure).days


class ExploreAnswer(NamedTuple):
    """Every destination the page listed, in `currency`. `priced` is the ones
    it priced, cheapest first."""

    currency: str
    destinations: tuple[Destination, ...]

    @property
    def priced(self) -> list[Destination]:
        """The priced destinations, cheapest first, ties by name."""
        return sorted(
            (d for d in self.destinations if d.price is not None),
            key=lambda d: (d.price or 0.0, d.name or ""),
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise GfPageRpcError(f"{_WHAT} carried a date this reader cannot read: {value!r}") from e


def _rows(block: Any) -> list[list[Any]]:
    """The rows of `[[row, …]]` whose first field is a destination id."""
    listed = dig(block, 0)
    if not isinstance(listed, list):
        return []
    return [
        cast("list[Any]", r)
        for r in cast("list[Any]", listed)
        if isinstance(r, list) and isinstance(dig(r, 0), str)
    ]


def _price(row: list[Any]) -> float | None:
    price = dig(row, 1, 0, 1)
    if isinstance(price, bool) or not isinstance(price, int | float):
        return None
    try:
        finite = math.isfinite(price)
    except OverflowError:  # an integer too large for a float
        return None
    return price if finite and price > 0 else None


def parse_destinations(body: str, *, origin: str, month: date | None) -> list[Destination]:
    """Every destination in one `GetExploreDestinations` response.

    Info rows are `payload[3][0]`: `[id, _, name, _, country, …, [11] departure,
    [12] return, …, [15] airport]`. Price rows are `payload[4][0]`:
    `[id, [[_, price], token], …, [6] = [carrier, carrier name, stops, minutes,
    _, airport, …]]`; the carrier code of a trip on several airlines is
    `multi`, so the name is the field that says who flies it.

    Refuses rather than returning nothing: no destinations, a page answering
    for another origin (without one it geolocates), and a departure outside
    `month` are all `GfPageRpcError`."""
    info: dict[str, list[Any]] = {}
    fares: dict[str, list[Any]] = {}
    served_from: set[str] = set()
    for payload in result_payloads(body, what=_WHAT):
        for row in _rows(dig(payload, 3)):
            info.setdefault(row[0], row)
        for row in _rows(dig(payload, 4)):
            if row[0] not in fares or _price(fares[row[0]]) is None:
                fares[row[0]] = row
        served_from.update(
            code
            for entry in cast("list[Any]", dig(payload, 6) or [])
            if isinstance(code := dig(entry, 3), str)
        )
    if not info and not fares:
        raise GfPageRpcError("Google Flights' explore page listed no destinations")
    if origin not in served_from:
        found = ", ".join(sorted(served_from)) or "no origin"
        raise GfPageRpcError(f"Google Flights' explore page answered for {found}, not {origin}")
    out: list[Destination] = []
    for key in dict.fromkeys([*info, *fares]):
        place, fare = info.get(key, []), fares.get(key, [])
        flight = dig(fare, 6)
        price = _price(fare)
        destination = Destination(
            name=_text(dig(place, 2)),
            country=_text(dig(place, 4)),
            code=_text(dig(flight, 5)),
            price=price,
            departure=_day(dig(place, 11)),
            return_date=_day(dig(place, 12)),
            carrier=_text(dig(flight, 1)),
            stops=_count(dig(flight, 2)) if price is not None else None,
            duration_min=_count(dig(flight, 3)) if price is not None else None,
        )
        departed = destination.departure
        if month is not None and departed is not None and departed.replace(day=1) != month:
            raise GfPageRpcError(
                f"Google Flights' explore page listed a trip departing {departed.isoformat()}, "
                f"outside {month:%Y-%m}"
            )
        out.append(destination)
    return out


def _is_explore_rpc(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.endswith(_EXPLORE_RPC)


def explore(url: str, *, origin: str, month: date | None, headed: bool) -> ExploreAnswer:
    """Open the explore page at `url` and read what it lists.

    The caller arms `interrupt_guard` and holds `session_scope` around this."""
    captured = capture(url, _is_explore_rpc, headed=headed)
    if not HTTPStatus.OK <= captured.status < HTTPStatus.MULTIPLE_CHOICES:
        raise GfPageRpcError(f"Google Flights' explore request returned HTTP {captured.status:d}")
    return ExploreAnswer(
        url_currency(url),
        tuple(parse_destinations(captured.body, origin=origin, month=month)),
    )


def document(answer: ExploreAnswer) -> list[dict[str, Any]]:
    """The `--format json` document: the priced destinations, cheapest first."""
    return [
        {
            "code": d.code,
            "name": d.name,
            "country": d.country,
            "price": d.price,
            "currency": answer.currency,
            "departure": None if d.departure is None else d.departure.isoformat(),
            "return": None if d.return_date is None else d.return_date.isoformat(),
            "nights": d.nights,
            "carrier": d.carrier,
            "stops": d.stops,
            "duration_min": d.duration_min,
        }
        for d in answer.priced
    ]
