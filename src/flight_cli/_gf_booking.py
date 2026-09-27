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

import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from . import _gf_browser
from ._gf_rpc_shared import GfPageRpcError, dig, refuse_a_wall, result_payloads

if TYPE_CHECKING:
    from collections.abc import Sequence

_BOOKING_RPC = "/GetBookingResults"
_WHAT = "Google Flights' booking page response"


class Seller(NamedTuple):
    """One seller's offer. `price` is in whole units of the page's currency;
    `fare` is the airline's fare-family name, which agencies and some airlines
    leave out."""

    name: str
    price: float | None
    fare: str | None
    airline: bool


class BookingOptions(NamedTuple):
    """Every seller of one itinerary, cheapest first, in `currency`."""

    currency: str
    sellers: tuple[Seller, ...]


def url_currency(url: str) -> str:
    """The currency a page prices in: its URL's `curr=`. Neither response
    names one."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["curr"][0]


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


def _seller(option: list[Any]) -> Seller | None:
    """`option[1][0] = [code, name, _, is_airline]`, `[7][0][1]` the price,
    `[21][3]` the fare name. None for an option without a seller name."""
    name = dig(option, 1, 0, 1)
    if not isinstance(name, str) or not name.strip():
        return None
    price = dig(option, 7, 0, 1)
    fare = dig(option, 21, 3)
    return Seller(
        name=name,
        price=price
        if isinstance(price, int | float) and not isinstance(price, bool) and price > 0
        else None,
        fare=fare if isinstance(fare, str) and fare.strip() else None,
        airline=dig(option, 1, 0, 3) is True,
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
    captured = _gf_browser.session(headed=headed).capture(
        url, _is_booking_rpc, check_page=refuse_a_wall
    )
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
        }
        for s in options.sellers
    ]
