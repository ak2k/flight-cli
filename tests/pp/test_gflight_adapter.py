# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportCallIssue=false
# DIVERGE: pydantic alias fields (e.g. displayTotal, solutionCount) trip
# basedpyright into thinking aliases are required kwargs even with
# populate_by_name=True. Same posture as tests/pp/test_match.py.
"""Tests for the fli → SearchResult adapter.

The adapter is the bridge that lets match.py join PP awards against
Google-Flights cash itineraries. The matcher only reads structural fields
(slices[i].flights[0], .departure, .origin.code, .destination.code) plus
the price string — so this test pins those exact fields end-to-end."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from flight_cli.pp.gflight_adapter import fli_results_to_search_result


def _leg(
    flight_number: str,
    dep_iata: str,
    arr_iata: str,
    dep: datetime,
    arr: datetime,
    airline_iata: str = "AA",
) -> SimpleNamespace:
    """Mimic fli FlightLeg with duck-typed attributes (airline/airport are
    Enum-like; we just need .name with optional leading underscore stripping).

    The adapter concatenates the airline IATA with `flight_number` to produce
    Matrix's-format slice.flights (e.g. 'AA100'), so tests pass bare numbers
    here as `flight_number` and the IATA via `airline_iata`."""
    return SimpleNamespace(
        airline=SimpleNamespace(name=airline_iata),
        flight_number=flight_number,
        departure_airport=SimpleNamespace(name=dep_iata),
        arrival_airport=SimpleNamespace(name=arr_iata),
        departure_datetime=dep,
        arrival_datetime=arr,
        duration=(arr - dep).seconds // 60,
    )


def _result(price: float, *legs: Any, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        legs=list(legs),
        price=price,
        currency=currency,
        duration=sum(leg.duration for leg in legs),
        stops=len(legs) - 1,
    )


def test_one_way_single_leg_maps_to_one_slice() -> None:
    fr = _result(
        877.0,
        _leg(
            "100",
            "JFK",
            "LHR",
            datetime(2026, 8, 15, 19, 0),
            datetime(2026, 8, 16, 7, 0),
            airline_iata="AA",
        ),
    )
    sr = fli_results_to_search_result([fr])
    assert sr.solution_count == 1
    it = sr.solutions[0]
    assert it.price == "USD877.00"
    assert it.itinerary is not None
    slices = it.itinerary.slices
    assert len(slices) == 1
    s = slices[0]
    # slice.flights[0] is IATA-prefixed: matches Matrix's format so the
    # (flight#, date) matcher key joins across backends.
    assert s.flights == ["AA100"]
    assert s.departure == "2026-08-15T19:00:00"
    assert s.origin is not None and s.origin.code == "JFK"
    assert s.destination is not None and s.destination.code == "LHR"


def test_round_trip_tuple_maps_to_two_slices() -> None:
    """Prices deliberately UNEQUAL: the members of a round-trip tuple carry
    different fares, and equal ones would make the itinerary's price agree with
    both members and pin neither."""
    out = _result(
        1078.0,
        _leg(
            "100",
            "JFK",
            "LHR",
            datetime(2026, 8, 15, 19, 0),
            datetime(2026, 8, 16, 7, 0),
            airline_iata="AA",
        ),
    )
    ret = _result(
        1421.0,
        _leg(
            "101",
            "LHR",
            "JFK",
            datetime(2026, 8, 22, 11, 0),
            datetime(2026, 8, 22, 14, 0),
            airline_iata="AA",
        ),
    )
    sr = fli_results_to_search_result([(out, ret)])
    assert sr.solution_count == 1
    it = sr.solutions[0]
    assert it.itinerary is not None
    slices = it.itinerary.slices
    assert len(slices) == 2
    assert slices[0].flights == ["AA100"]
    assert slices[1].flights == ["AA101"]
    assert slices[1].origin is not None and slices[1].origin.code == "LHR"
    assert slices[1].destination is not None and slices[1].destination.code == "JFK"
    # The pinned leg's fare is this combination's total; the outbound's is the
    # cheapest total reachable from that outbound, which is a different trip
    # unless this return happens to be the cheapest one.
    assert it.price == "USD1421.00"
    assert sr.cheapest_price == "USD1421.00"


def test_connection_slice_flattens_all_flight_numbers_first_origin_last_dest() -> None:
    """Connecting itinerary: slice.flights lists every leg; origin/destination
    are first/last leg's airports (so the (route, time) fallback key works)."""
    fr = _result(
        450.0,
        _leg(
            "100",
            "JFK",
            "BOS",
            datetime(2026, 8, 15, 9, 0),
            datetime(2026, 8, 15, 10, 30),
            airline_iata="B6",
        ),
        _leg(
            "200",
            "BOS",
            "LHR",
            datetime(2026, 8, 15, 17, 0),
            datetime(2026, 8, 16, 5, 0),
            airline_iata="B6",
        ),
    )
    sr = fli_results_to_search_result([fr])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.flights == ["B6100", "B6200"]
    assert s.origin is not None and s.origin.code == "JFK"
    assert s.destination is not None and s.destination.code == "LHR"
    assert s.departure == "2026-08-15T09:00:00"


def test_underscore_prefixed_airport_codes_stripped() -> None:
    """fli prefixes numeric airport codes with `_` (since Python enum names
    can't start with a digit). Adapter strips that so codes match Matrix."""
    fr = _result(
        300.0,
        _leg(
            "WN100",
            "_4U",  # imagined numeric-leading IATA
            "JFK",
            datetime(2026, 8, 15, 7, 0),
            datetime(2026, 8, 15, 11, 0),
        ),
    )
    sr = fli_results_to_search_result([fr])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.origin is not None and s.origin.code == "4U"


def test_cheapest_price_tracks_min_across_results() -> None:
    a = _result(
        500.0,
        _leg("AA1", "JFK", "LAX", datetime(2026, 8, 15, 8, 0), datetime(2026, 8, 15, 11, 0)),
    )
    b = _result(
        320.0,
        _leg("DL1", "JFK", "LAX", datetime(2026, 8, 15, 9, 0), datetime(2026, 8, 15, 12, 0)),
    )
    sr = fli_results_to_search_result([a, b])
    assert sr.cheapest_price == "USD320.00"


def test_empty_results_yield_empty_search_result() -> None:
    sr = fli_results_to_search_result([])
    assert sr.solution_count == 0
    assert sr.solutions == []
    assert sr.cheapest_price is None


def test_gflightwithid_populates_slice_flight_id() -> None:
    """When given our `GFlightWithId` wrapper, the adapter pulls the opaque
    flight_id through onto each Slice — the data the PP provider then turns
    into a CashFlightHint."""
    g = SimpleNamespace(
        flight=_result(
            877.0,
            _leg(
                "1",
                "JFK",
                "LHR",
                datetime(2026, 8, 15, 19, 0),
                datetime(2026, 8, 16, 7, 0),
                airline_iata="DL",
            ),
        ),
        flight_id="MWRvrf",
    )
    sr = fli_results_to_search_result([g])
    s = sr.solutions[0].itinerary.slices[0]  # type: ignore[union-attr]
    assert s.flight_id == "MWRvrf"
    assert s.flights == ["DL1"]


def test_cash_hints_from_search_result_shape() -> None:
    """The hints generator outputs the exact shape PP's airline-search wants:
    IATA-prefixed firstFlightNumber, human-readable airline, space-separated
    times, opaque flight_id passed through verbatim. Verified empirically
    against the live extension capture."""
    from flight_cli.pp.gflight_adapter import cash_hints_from_search_result

    g = SimpleNamespace(
        flight=_result(
            802.0,
            _leg(
                "6",
                "MIA",
                "LHR",
                datetime(2026, 6, 30, 18, 5),
                datetime(2026, 7, 1, 8, 5),
                airline_iata="VS",
            ),
        ),
        flight_id="NbXSYb",
    )
    sr = fli_results_to_search_result([g])
    hints = cash_hints_from_search_result(sr)
    assert len(hints) == 1
    h = hints[0]
    # Exact PP-expected format (per research/capture/pp_extension_capture.json)
    assert h.flight_id == "NbXSYb"
    assert h.first_flight_number == "VS6"  # IATA-prefixed
    assert h.airline == "Virgin Atlantic"  # human-readable
    assert h.google_airlines == ["Virgin Atlantic"]
    assert h.origin == "MIA"
    assert h.dest == "LHR"
    assert h.start_dt == "2026-06-30 18:05"  # space-separated
    assert h.end_dt == "2026-07-01 08:05"
    assert h.cash_price_usd == 802


def test_cash_hints_skip_slices_without_flight_id() -> None:
    """Matrix-backend SearchResults have Slice.flight_id=None; the hint
    builder skips those (nothing to match on)."""
    from flight_cli.models import (
        Itinerary,
        ItineraryDetails,
        ItineraryExt,
        SearchResult,
        Slice,
        SliceEndpoint,
    )
    from flight_cli.pp.gflight_adapter import cash_hints_from_search_result

    sr = SearchResult(
        solutions=[
            Itinerary(
                ext=ItineraryExt(price="USD500.00"),
                itinerary=ItineraryDetails(
                    slices=[
                        Slice(
                            flights=["UA146"],
                            departure="2026-06-09T22:00:00",
                            origin=SliceEndpoint(code="JFK"),
                            destination=SliceEndpoint(code="LHR"),
                            # flight_id intentionally None — Matrix cash
                        ),
                    ],
                ),
            ),
        ],
    )
    hints = cash_hints_from_search_result(sr)
    assert hints == []


# ───────── a price-less row must not take down the whole search ─────────


def test_price_string_tolerates_unsurfaced_price() -> None:
    """fli types `FlightResult.price` as `NonNegativeFloat | None` — "None when
    not surfaced", which Google does on some premium round-trip rows. The
    absence travels as itself: an empty string parses as no fare too, but a
    reader has to know that, and `ItineraryExt.price` is optional so nothing
    downstream needs the placeholder."""
    from flight_cli.pp.gflight_adapter import _price_string

    assert _price_string(_result(None, currency="USD")) is None  # pyright: ignore[reportArgumentType]
    assert _price_string(_result(877.0)) == "USD877.00"


def test_one_priceless_row_does_not_discard_the_whole_response() -> None:
    """The crash was `TypeError: unsupported format string passed to
    NoneType.__format__`, raised while building the SearchResult — so ONE
    price-less row destroyed every other itinerary in the response, not just
    its own."""
    dep = datetime(2026, 8, 15, 18, 0)
    arr = datetime(2026, 8, 16, 6, 0)
    priceless = _result(None, _leg("100", "JFK", "LHR", dep, arr))  # pyright: ignore[reportArgumentType]
    priced = _result(877.0, _leg("200", "JFK", "LHR", dep, arr))

    sr = fli_results_to_search_result([priceless, priced])  # pyright: ignore[reportArgumentType]

    assert sr.solution_count == 2
    prices = [(s.ext.price if s.ext else None) for s in sr.solutions]
    assert prices == [None, "USD877.00"]
    # The cheapest-price notice ignores the unpriced row rather than crashing.
    assert sr.currency_notice.ext is not None
    assert sr.currency_notice.ext.price == "USD877.00"


def test_a_captured_round_trip_is_priced_at_the_combination_not_the_outbound(
    gf_session: Any, gf_capture: Any, gf_answering: Any
) -> None:
    """Driven through the transport with the matched pair of live captures,
    because this is a fact about Google's two boards and not about the adapter's
    taste.

    The outbound board prices AA144/AA1110 at 6616, and the return board Google
    served with an outbound pinned prices AA1115/AA297 at 7196 — the same 6616
    is that return board's own MINIMUM, which is what the outbound row was
    quoting all along. Pricing the pair from the outbound therefore reports a
    7196 trip as a 6616 one, and the award comparison downstream reads that
    number as the cash fare to beat."""
    from flight_cli._gflight_ids import search_with_ids
    from flight_cli.domain import Cabin, Leg, SearchOptions, SpecificDateSearch
    from flight_cli.fli_bridge import to_fli_filter

    dep = date.today() + timedelta(days=45)
    ret = date.today() + timedelta(days=52)
    # The return capture answers the leg this search asks for: a board for
    # another route or another day is what a page that dropped the pin looks
    # like, and the pin loop refuses one rather than pairing it. Prices, ids and
    # carriers — everything the pricing argument rests on — are the capture's.
    gf_session(
        gf_capture("ds1_metadata_blocks_kept.json"),
        gf_answering(
            "ds1_return_leg_pinned.json",
            origin="MIA",
            destination="HNL",
            date=ret.isoformat(),
        ),
    )
    results = search_with_ids(
        to_fli_filter(
            SpecificDateSearch(
                legs=(Leg.of("HNL", "MIA", dep), Leg.of("MIA", "HNL", ret)),
                options=SearchOptions(cabin=Cabin.COACH),
            )
        ),
        top_n=3,
    )
    assert results is not None
    sr = fli_results_to_search_result(results)

    priced: dict[tuple[str, str], set[str]] = {}
    for it in sr.solutions:
        itn = it.itinerary
        assert itn is not None
        key = ("/".join(itn.slices[0].flights), "/".join(itn.slices[1].flights))
        priced.setdefault(key, set()).add(it.price or "")

    assert priced[("AA144/AA1110", "AA1115/AA297")] == {"USD7196.00"}
    # Per combination, not per outbound: the same outbound against a cheaper
    # return is a cheaper trip, and an outbound-priced board reports one number
    # for all three.
    assert priced[("AA144/AA1110", "AA713/AA297")] == {"USD6616.00"}


def test_a_row_google_did_not_price_sends_no_cash_hint(gf_rows: Any) -> None:
    """No hint carries a fare the board did not have.

    `CashFlightHint.cash_price_usd` is an `int` and `to_payload` always emits
    the key, so an unpriced row can only reach PointsPath as `cashPrice: 0` —
    and matching is switched on by the presence of hints, which makes that zero
    the cash baseline an award is judged against. Nothing rendered shows it:
    the cash map skips a `None` price and no renderer here has a cash column,
    so the fabricated number is visible only on the wire.

    Asserted as "no hint priced at zero" and not as a count alone, because the
    count is right for the wrong reason as soon as a second row goes unpriced.
    Driven over a real board with one row's price head emptied, which is what a
    served page holds for a row Google did not price."""
    from flight_cli.pp.gflight_adapter import cash_hints_from_search_result

    board = gf_rows("ds1_metadata_blocks_kept.json", unpriced=1)
    priced, unpriced = board[0], board[1]
    assert priced.flight.price is not None
    assert unpriced.flight.price is None
    assert priced.flight_id != unpriced.flight_id  # not deduplicated away

    hints = cash_hints_from_search_result(fli_results_to_search_result([priced, unpriced]))

    assert [h.flight_id for h in hints] == [priced.flight_id]
    assert [h.cash_price_usd for h in hints] == [int(priced.flight.price)]
    assert not [h for h in hints if h.cash_price_usd == 0], hints

    # The edge this costs, asserted where it is paid: a leg on which Google
    # priced nothing sends no hints at all, and `enable_matching` is derived
    # from `bool(cash_hints)`, so the matcher is off for that leg. A leg with
    # no priced row has no cash baseline to match an award against, which is
    # why an absent hint beats a fabricated one here.
    assert cash_hints_from_search_result(fli_results_to_search_result([unpriced])) == []


def test_a_row_google_did_not_price_is_carried_with_no_price_and_sorted_last(
    gf_rows: Any,
) -> None:
    """The adapter is the third place the absence has to be answered, and the
    only one where it is compared as well as formatted.

    Two things happen to a price here: it becomes the itinerary's own string,
    and it competes for `cheapest_price`. Fabricating `USD0.00` for the first
    would undercut every cash comparison an award is made against, and reading
    the absence as a number in the second is a type error the moment a priced
    row precedes an unpriced one — which is ordinary, since the board arrives
    in Google's order and not in ours.

    Carried rather than skipped, because the merged table is drawn from this
    list: a solution dropped here is a row the Google table showed and the
    merged one does not."""
    from flight_cli._enrich import merge_results
    from flight_cli.models import SearchResult

    board = gf_rows("ds1_metadata_blocks_kept.json", unpriced=1)
    # Priced FIRST: `cheapest_price` is already a float when the unpriced row
    # arrives, which is the order that makes the comparison a failure.
    sr = fli_results_to_search_result([board[0], board[1], board[2]])

    assert len(sr.solutions) == len(board)
    prices = [s.ext.price if s.ext else None for s in sr.solutions]
    assert prices == [f"USD{board[0].flight.price:.2f}", None, f"USD{board[2].flight.price:.2f}"]
    # The cheapest is the cheapest of the rows that HAVE one.
    assert sr.cheapest_price == f"USD{board[2].flight.price:.2f}"

    # And the merge orders it last: an absent string has no amount to rank on,
    # so the same row the table shows with a dash sits at the bottom of the
    # merged view too.
    merged = merge_results(sr, SearchResult.model_validate({"solutions": []}), currency="USD")
    assert [r.gf_price for r in merged] == [
        f"USD{board[2].flight.price:.2f}",
        f"USD{board[0].flight.price:.2f}",
        None,
    ], merged
