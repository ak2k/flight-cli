"""Who sells one Google Flights itinerary, and at what price.

The booking page lists every seller — airlines and agencies — in its answer to
its own `GetBookingResults` request, which the page signs; its HTML carries none
of them. So Chrome opens the booking URL and `GfBrowserSession.capture` hands
back the response the page received. Nothing here writes a request or runs
script in the page.

The cheapest seller is often below the price the search table shows, which is
the point of asking.
"""

from __future__ import annotations

import math
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from ._gf_rpc_shared import GfPageRpcError, capture, dig, result_payloads, url_currency

if TYPE_CHECKING:
    from collections.abc import Sequence

_BOOKING_RPC = "/GetBookingResults"
_WHAT = "Google Flights' booking page response"


class BagFee(NamedTuple):
    """What one bag costs with one seller, in the page's currency; `fee` 0 is
    free. On a round trip the fee covers the whole trip."""

    bag: Literal["carry-on", "checked"]
    nth: int
    fee: float


class Seller(NamedTuple):
    """One seller's offer. `price` is in whole units of the page's currency;
    `fare` is the airline's fare-family name, which agencies and some airlines
    leave out. `link` is Google's redirect to the seller's own page for this
    fare; `bags` holds only the bags the seller states a fee for or calls free."""

    name: str
    price: float | None
    fare: str | None
    airline: bool
    link: str | None = None
    bags: tuple[BagFee, ...] = ()


class BookingOptions(NamedTuple):
    """Every seller of one itinerary, cheapest first, in `currency`."""

    currency: str
    sellers: tuple[Seller, ...]


def _is_booking_rpc(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.endswith(_BOOKING_RPC)


def _flight_key(carrier: Any, number: Any) -> str:
    """`DL1788` for any spelling of the number, so `0178` and `178` agree."""
    digits = str(number)
    return f"{carrier}{int(digits)}" if digits.isdigit() else f"{carrier}{digits}"


def _option_flights(option: list[Any]) -> tuple[str, ...]:
    flights = dig(option, 3)
    if not isinstance(flights, list):
        return ()
    return tuple(
        _flight_key(f[0], f[1])
        for f in cast("list[Any]", flights)
        if isinstance(f, list) and len(cast("list[Any]", f)) >= 2  # noqa: PLR2004 — `[carrier, number]`
    )


def _amount(value: Any) -> float | None:
    """`value` if it is a positive finite number, else None. `json.loads` reads
    `Infinity`, and an integer too long for a float raises when formatted."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    return value if finite and value > 0 else None


def _form(pairs: Any) -> list[tuple[str, str]] | None:
    """`[[name, value], ...]` as `urlencode` takes it; None unless every pair
    is two strings."""
    if pairs is None:
        return []
    if not isinstance(pairs, list):
        return None
    form: list[tuple[str, str]] = []
    for pair in cast("list[Any]", pairs):
        match pair:
            case [str(name), str(value)]:
                form.append((name, value))
            case _:
                return None
    return form


def _link(option: list[Any]) -> str | None:
    """`option[5][2] = [base URL, [[name, value], ...]]`, the form the page
    posts to reach the seller; Google answers the same pairs sent as a query.
    None unless the base is a printable https URL with a host and no query or
    fragment of its own, and every pair is two strings."""
    base, form = dig(option, 5, 2, 0), _form(dig(option, 5, 2, 1))
    if (
        form is None
        or not isinstance(base, str)
        or not (base.isascii() and base.isprintable())
        or any(c in base for c in " ?#")
    ):
        return None
    try:
        parts = urllib.parse.urlsplit(base)
        query = urllib.parse.urlencode(form)
    except ValueError:  # a bracketed host that is no IP address; a lone surrogate
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    return f"{base}?{query}" if query else base


# The slots of `option[18]`, each `[2, [[None, amount]], 1]` for a fee or `[3]`
# for free. `[0]` and `[1]` occur too, with no meaning known, so they add nothing.
_BAG_SLOTS: tuple[tuple[Literal["carry-on", "checked"], int], ...] = (
    ("checked", 1),
    ("checked", 2),
    ("carry-on", 1),
)
_BAG_FEE = 2
_BAG_FREE = 3


def _bags(option: list[Any]) -> tuple[BagFee, ...]:
    fees: list[BagFee] = []
    for slot, (bag, nth) in enumerate(_BAG_SLOTS):
        code = dig(option, 18, slot, 0)
        if code == _BAG_FREE:
            fees.append(BagFee(bag, nth, 0))
        elif code == _BAG_FEE and (fee := _amount(dig(option, 18, slot, 1, 0, 1))) is not None:
            fees.append(BagFee(bag, nth, fee))
    return tuple(fees)


def _seller(option: list[Any]) -> Seller | None:
    """`option[1][0] = [code, name, _, is_airline]`, `[7][0][1]` the price,
    `[21][3]` the fare name, `[5]` the link and `[18]` the bags. None for an
    option without a seller name."""
    name = dig(option, 1, 0, 1)
    if not isinstance(name, str) or not name.strip():
        return None
    fare = dig(option, 21, 3)
    return Seller(
        name=name,
        price=_amount(dig(option, 7, 0, 1)),
        fare=fare if isinstance(fare, str) and fare.strip() else None,
        airline=dig(option, 1, 0, 3) is True,
        link=_link(option),
        bags=_bags(option),
    )


def parse_sellers(body: str, *, flights: Sequence[tuple[str, str]]) -> tuple[Seller, ...]:
    """The sellers in one `GetBookingResults` response, cheapest first; a
    seller with no price goes last.

    `flights` are the itinerary's `(carrier, number)` pairs in order. At least one
    seller must be selling exactly those: sellers of a codeshare list their own
    numbers, but a page that answered for other flights would have none. Every
    way of showing nothing is a refusal — an error row, no sellers, none priced
    — because an empty list here would read as "nobody sells this"."""
    options: list[list[Any]] = []
    for payload in result_payloads(body, what=_WHAT):
        listed = dig(payload, 1, 0)
        if isinstance(listed, list):
            options.extend(
                cast("list[Any]", o) for o in cast("list[Any]", listed) if isinstance(o, list)
            )
    wanted = tuple(_flight_key(carrier, number) for carrier, number in flights)
    sellers = [s for s in map(_seller, options) if s is not None]
    if not sellers:
        raise GfPageRpcError("Google Flights' booking page listed no sellers")
    if not any(_option_flights(o) == wanted for o in options):
        raise GfPageRpcError(
            f"Google Flights' booking page listed sellers for other flights than {' '.join(wanted)}"
        )
    if all(s.price is None for s in sellers):
        raise GfPageRpcError(
            f"Google Flights' booking page listed {len(sellers):d} sellers and priced none"
        )
    return tuple(sorted(sellers, key=lambda s: (s.price is None, s.price or 0)))


def booking_options(
    url: str, *, flights: Sequence[tuple[str, str]], headed: bool
) -> BookingOptions:
    """Open the booking page at `url` and read who sells the itinerary.

    The caller arms `interrupt_guard` and holds `session_scope` around this."""
    captured = capture(url, _is_booking_rpc, headed=headed)
    if not HTTPStatus.OK <= captured.status < HTTPStatus.MULTIPLE_CHOICES:
        raise GfPageRpcError(f"Google Flights' booking request returned HTTP {captured.status:d}")
    return BookingOptions(url_currency(url), parse_sellers(captured.body, flights=flights))


def document(options: BookingOptions) -> list[dict[str, Any]]:
    """The `booking_options` list of the `--sellers --format json` document."""
    return [
        {
            "seller": s.name,
            "price": s.price,
            "currency": options.currency,
            "fare": s.fare,
            "airline": s.airline,
            "booking_url": s.link,
            "bags": [
                {"bag": b.bag, "nth": b.nth, "fee": b.fee, "currency": options.currency}
                for b in s.bags
            ],
        }
        for s in options.sellers
    ]
