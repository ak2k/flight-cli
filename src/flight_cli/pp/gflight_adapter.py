# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs even though populate_by_name=True is
# set. Same posture as tests/pp/test_match.py.
"""Adapt fli's Google Flights output into the SearchResult shape match.py expects.

The matcher reads structural fields only — slices[i].flights[0], .departure,
.origin.code, .destination.code, Itinerary.price — so a thin wrap-and-translate
layer is enough; no matcher changes needed for the basic flight#+date join.

We also populate `Slice.flight_id` from Google's opaque per-flight ID
(`data[0][17]`, captured via `_gflight_ids.GFlightWithId`). That ID flows
downstream as a `CashFlightHint.flight_id` and PP echoes it back as
`matchedGoogleFlightId` — the matcher's primary join key when available,
much more robust than the heuristic fallbacks for codeshares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._airline_names import pp_airline_name
from ..models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    LegInfo,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from .client import CashFlightHint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._gflight_ids import LegAmenities, Ticketing


def _airport_code(a: Any) -> str:
    name: str = getattr(a, "name", "") or ""
    return name.removeprefix("_")


def _flight_id_string(leg: Any) -> str:
    """IATA-prefixed flight number, e.g. 'DL1' — what Matrix's slices.flights
    uses too, so the matcher's flight#+date key matches across backends and
    PP's `firstFlightNumber` hint field gets the format it expects."""
    iata = _airport_code(leg.airline)  # leg.airline is fli's Airline Enum, .name == IATA
    return f"{iata}{leg.flight_number}"


def _leg_info(a: LegAmenities) -> LegInfo:
    return LegInfo(
        aircraft=a.aircraft,
        pitch_inches=a.pitch_inches,
        legroom_class=a.legroom_class,
        cabin=a.cabin,
        wifi=a.wifi,
        power=a.power,
        video=a.video,
        operating_carrier=a.operating_carrier,
        marketing_carriers=list(a.marketing_carriers),
        marketing_flights=list(a.marketing_flights),
    )


def _slice_from_flight_result(
    fr: Any,
    flight_id: str | None = None,
    amenities: list[LegAmenities] | None = None,
) -> Slice:
    legs: list[Any] = list(fr.legs)
    first, last = legs[0], legs[-1]
    leg_infos: list[LegInfo] = [_leg_info(a) for a in (amenities or [])]
    # Intermediate connection airports. For N legs there are N-1 stops:
    # each inter-leg arrival_airport equals the next leg's
    # departure_airport. Both are valid sources; we use arrival_airport.
    stops = [SliceEndpoint(code=_airport_code(leg.arrival_airport)) for leg in legs[:-1]]
    # Per-segment departure dates from each fli FlightResult.legs[i]. The
    # pinned-URL encoder pins a connection only when every flight's date is
    # given, since the slice's two ends do not date the flights between them.
    segment_dates = [leg.departure_datetime.date().isoformat() for leg in legs]
    return Slice(
        flights=[_flight_id_string(leg) for leg in legs],
        departure=first.departure_datetime.isoformat(),
        arrival=last.arrival_datetime.isoformat(),
        duration=fr.duration,
        origin=SliceEndpoint(code=_airport_code(first.departure_airport)),
        destination=SliceEndpoint(code=_airport_code(last.arrival_airport)),
        stops=stops,
        segment_dates=segment_dates,
        flight_id=flight_id,
        legs=leg_infos,
    )


def _price_string(fr: Any) -> str | None:
    """Match Matrix's price format ('USD877.00') so `pp.cli._parse_cash` reads it.

    None where Google surfaced no price for the row. `ItineraryExt.price` is
    optional, so the absence travels as itself: a fabricated `USD0.00` parses
    as a real fare and would undercut every cash comparison an award is made
    against, which is the one number this string exists to carry."""
    if fr.price is None:
        return None
    currency = fr.currency or "USD"
    return f"{currency}{fr.price:.2f}"


def _ticketing(items: Sequence[Any]) -> Ticketing | None:
    """How Google sells the itinerary of `items`: a self transfer when any
    member is one, else separate tickets when any member is sold so."""
    kinds = {getattr(it, "ticketing", None) for it in items}
    if "self_transfer" in kinds:
        return "self_transfer"
    return "separate_tickets" if "separate_tickets" in kinds else None


def _unwrap(item: Any) -> tuple[Any, str | None, list[LegAmenities] | None]:
    """Accept either a raw fli `FlightResult` (no flight_id / amenities) or
    a `GFlightWithId` carrying the captured opaque ID + per-leg amenities."""
    if hasattr(item, "flight_id") and hasattr(item, "flight"):
        return item.flight, item.flight_id, getattr(item, "amenities", None)
    return item, None, None


def fli_results_to_search_result(results: Sequence[Any]) -> SearchResult:
    """Wrap fli's heterogeneous return into a SearchResult.

    Accepts either fli's raw `FlightResult`s or our enriched `GFlightWithId`
    wrappers. When given the enriched form, populates `Slice.flight_id` so
    the matched-id PP join can fire downstream. With raw fli output, the
    flight_id is None and the matcher falls back to flight#+date / route+time.

    fli returns ``list[FlightResult]`` for one-way and ``list[tuple[FlightResult, ...]]``
    for round-trip/multi-city. Each top-level entry maps to one Itinerary; for
    tuples, each FlightResult becomes one Slice in slice-index order.

    A round-trip tuple's members need not carry the same number, and where
    they differ this is why. An outbound row is priced at the cheapest round-trip TOTAL
    reachable from that outbound; the return board fetched with it pinned
    prices each of its rows at THAT combination's total. The itinerary fare is
    therefore the terminal member's; why that is the true one, with the
    measurements, is in `docs/memories/gf_routing_and_carriers.md`.

    A member may carry no price at all — Google does not always surface one —
    and such a row is carried with `price=None` rather than dropped, so the
    solution list stays the board the user was shown.

    The number matters downstream: an award is compared against it, so an
    outbound-priced combination undercuts every cash comparison but the
    cheapest one.
    """
    solutions: list[Itinerary] = []
    cheapest_price: float | None = None
    cheapest_currency: str = "USD"
    for r in results:
        items_raw: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
        if not items_raw:
            continue
        unwrapped = [_unwrap(it) for it in items_raw]
        slices = [_slice_from_flight_result(fr, fid, am) for fr, fid, am in unwrapped]
        # The pinned leg on a round trip, and the only leg on a one-way — one
        # rule, because `unwrapped[-1]` is `unwrapped[0]` when there is one.
        fare_fr = unwrapped[-1][0]
        price_str = _price_string(fare_fr)
        solutions.append(
            Itinerary(
                ext=ItineraryExt(price=price_str),
                itinerary=ItineraryDetails(slices=slices, carriers=[]),
                ticketing=_ticketing(items_raw),
            ),
        )
        # The same member the itineraries are priced from: a cheapest quoted
        # from the outbound boards would name a fare no row in the table shows.
        #
        # A row Google did not price competes for nothing here — it is not a
        # cheaper fare, it is no fare — so it is passed over while its itinerary
        # is still carried above. Reading it as a number instead is what makes
        # the comparison a type error on the first priced row that precedes it.
        p: float | None = fare_fr.price
        if p is not None and (cheapest_price is None or p < cheapest_price):
            cheapest_price = p
            cheapest_currency = fare_fr.currency or "USD"

    sr = SearchResult(
        solutionCount=len(solutions),
        solutions=solutions,
    )
    if cheapest_price is not None:
        sr.currency_notice.ext = ItineraryExt(
            price=f"{cheapest_currency}{cheapest_price:.2f}",
        )
    return sr


def _to_dt_minute(iso: str | None) -> str:
    """Convert an ISO datetime to PP's space-separated 'YYYY-MM-DD HH:MM'."""
    if not iso:
        return ""
    iso = iso.replace("T", " ")
    return iso[:16]


def cash_hints_from_search_result(
    sr: SearchResult,
    *,
    slice_index: int = 0,
    max_hints: int = 50,
) -> list[CashFlightHint]:
    """Build PP cash hints for one leg's worth of `sr.solutions`.

    `slice_index` selects which leg of each Itinerary the hint represents
    (0 outbound, 1 return on a round-trip). Skips itineraries whose slice
    doesn't carry a `flight_id` — that means the matched-id path isn't
    available for them (Matrix cash, or the gflight result was constructed
    from a non-enriched fli call). The matcher's flight#+date / route+time
    keys still apply to those. Skips an unpriced itinerary for the reason
    spelled out at that guard: the hint has no way to carry a missing fare.

    `max_hints` caps the payload — PP's airline-search rejects very large
    `googleFlightDetails` arrays; the extension typically sends 10-30.
    """
    out: list[CashFlightHint] = []
    seen_flight_ids: set[str] = set()
    for it in sr.solutions:
        itn = it.itinerary
        if not itn or slice_index >= len(itn.slices):
            continue
        s = itn.slices[slice_index]
        if not s.flight_id or s.flight_id in seen_flight_ids:
            continue
        if not s.origin or not s.destination or not s.flights or not s.departure:
            continue
        if it.price is None:
            # `cash_price_usd` is an int and `to_payload` always emits the key,
            # so a row Google did not price can only travel as `cashPrice: 0` —
            # a fabricated fare in the one field the award is compared against,
            # and the number `_price_string` refuses to invent one seam earlier.
            # Skipping costs the matched-id key for this row; flight#+date and
            # route+time still join it, so the loss is a missing award row
            # rather than a wrong price. On a board where NO row is priced this
            # sends no hints at all and `enable_matching` is then False for the
            # leg, which is the right answer: a leg with no priced row has no
            # cash baseline for the matcher to compare an award against.
            continue
        first_flight = s.flights[0]  # IATA-prefixed, e.g. "DL1"
        iata_prefix = first_flight[:2]
        airline_name = pp_airline_name(iata_prefix)
        cash_usd = _parse_cash_int(it.price)
        out.append(
            CashFlightHint(
                origin=(s.origin.code or "").upper(),
                dest=(s.destination.code or "").upper(),
                start_dt=_to_dt_minute(s.departure),
                end_dt=_to_dt_minute(s.arrival),
                flight_id=s.flight_id,
                airline=airline_name,
                google_airlines=[airline_name],
                num_connections=max(len(s.flights) - 1, 0),
                first_flight_number=first_flight,
                cash_price_usd=cash_usd,
                raw_cash_price=it.price or "",
            ),
        )
        seen_flight_ids.add(s.flight_id)
        if len(out) >= max_hints:
            break
    return out


def _parse_cash_int(price: str | None) -> int:
    """Extract leading digits from 'USD877.00' / '$877' / '877 USD'. 0 fallback."""
    if not price:
        return 0
    import re as _re  # noqa: PLC0415

    m = _re.search(r"[\d,]*\d+", price)
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", "").split(".")[0])
    except ValueError:
        return 0
