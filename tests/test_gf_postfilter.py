# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs even though populate_by_name=True is
# set. Same posture as tests/pp/test_match.py + pp/gflight_adapter.py.
"""Tests for the Tier-2 Google Flights post-filter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flight_cli._gf_postfilter import apply_postfilter, can_postfilter, search_page_reasons
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    LegInfo,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.routing_predicates import (
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    ExcludeCodesharePred,
    ExcludeRedeyesPred,
    Predicate,
    SpecificFlightPred,
    classify,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _slice(legs: Sequence[tuple[str, str | None, list[str]]], stops: Sequence[str] = ()) -> Slice:
    """legs = [(flight_number, operating_carrier, marketing_carriers)]."""
    return Slice(
        flights=[f for f, _, _ in legs],
        stops=[SliceEndpoint(code=s) for s in stops],
        legs=[LegInfo(operating_carrier=op, marketing_carriers=list(mk)) for _, op, mk in legs],
    )


def _result(*slices: Slice) -> SearchResult:
    sols = [
        Itinerary(ext=ItineraryExt(price="USD100.00"), itinerary=ItineraryDetails(slices=[s]))
        for s in slices
    ]
    return SearchResult(solutionCount=len(sols), solutions=sols)


def _filter(result: SearchResult, *preds: Predicate) -> list[str]:
    """Apply preds to slice 0 and return the surviving first-flight numbers."""
    out = apply_postfilter(result, [list(preds)])
    flights: list[str] = []
    for it in out.solutions:
        itn = it.itinerary
        if itn is not None:
            flights.append(itn.slices[0].flights[0])
    return flights


# ─────────────────────────── operating carrier ─────────────────────────


def test_operating_include_keeps_only_all_matching() -> None:
    res = _result(
        _slice([("LH400", "LH", ["LH"])]),  # operated by LH
        _slice([("UA100", "UA", ["UA"])]),  # operated by UA
    )
    kept = _filter(res, CarrierPred(frozenset({"LH"}), exclude=False, operating=True))
    assert kept == ["LH400"]


def test_operating_exclude_drops_matching() -> None:
    res = _result(_slice([("LH400", "LH", ["LH"])]), _slice([("UA100", "UA", ["UA"])]))
    kept = _filter(res, CarrierPred(frozenset({"UA"}), exclude=True, operating=True))
    assert kept == ["LH400"]


# ─────────────────────────── marketing exclude ─────────────────────────


def test_marketing_exclude_drops_by_the_booking_carrier_only() -> None:
    """Matrix's `~UA+` keeps a fare booked under another carrier even when UA
    also sells the flight, so the sellers listed beside the booking carrier
    are not the row's fare and do not exclude it."""
    res = _result(
        _slice([("UA100", "UA", ["UA"])]),  # booked UA -> excluded
        _slice([("LH9498", "EN", ["LH"])]),  # LH/Air Dolomiti, no UA -> kept
        _slice([("LH900", "LH", ["UA"])]),  # booked LH, UA also sells it -> kept
        _slice([("UA9000", "LH", ["UA", "LH"])]),  # booked under UA's number -> excluded
    )
    kept = _filter(res, CarrierPred(frozenset({"UA"}), exclude=True, operating=False))
    assert kept == ["LH9498", "LH900"]


# ─────────────────────────── connection airport ────────────────────────


def test_connection_exclude_drops_via_airport() -> None:
    res = _result(
        _slice([("AA1", "AA", ["AA"]), ("AA2", "AA", ["AA"])], stops=["DFW"]),
        _slice([("AA3", "AA", ["AA"]), ("AA4", "AA", ["AA"])], stops=["ORD"]),
    )
    kept = _filter(res, ConnectionAirportPred(frozenset({"DFW"}), exclude=True))
    assert kept == ["AA3"]


# ─────────────────────────── codeshare ─────────────────────────────────


def test_codeshare_exclude_drops_operated_for_legs() -> None:
    res = _result(
        _slice([("LH9498", "EN", ["LH"])]),  # LH marketed, EN operated -> codeshare
        _slice([("LH400", "LH", ["LH"])]),  # LH on LH metal -> not codeshare
    )
    kept = _filter(res, ExcludeCodesharePred())
    assert kept == ["LH400"]


# ─────────────────────────── specific flight ───────────────────────────


def test_specific_flight_number_and_range() -> None:
    res = _result(_slice([("UA882", "UA", ["UA"])]), _slice([("UA999", "UA", ["UA"])]))
    assert _filter(res, SpecificFlightPred("UA", 882, 882)) == ["UA882"]
    res2 = _result(_slice([("UA882", "UA", ["UA"])]), _slice([("UA3000", "UA", ["UA"])]))
    assert _filter(res2, SpecificFlightPred("UA", 1000, 2000)) == []


# ─────────────────────────── per-slice scoping ─────────────────────────


def test_predicates_apply_only_to_their_slice() -> None:
    """Outbound `~UA` must not filter on the return slice."""
    out_clean_ret_ua = Itinerary(
        ext=ItineraryExt(price="USD1.00"),
        itinerary=ItineraryDetails(
            slices=[_slice([("LH1", "LH", ["LH"])]), _slice([("UA9", "UA", ["UA"])])]
        ),
    )
    out_ua = Itinerary(
        ext=ItineraryExt(price="USD2.00"),
        itinerary=ItineraryDetails(
            slices=[_slice([("UA1", "UA", ["UA"])]), _slice([("LH9", "LH", ["LH"])])]
        ),
    )
    res = SearchResult(solutionCount=2, solutions=[out_clean_ret_ua, out_ua])
    out = apply_postfilter(res, [[CarrierPred(frozenset({"UA"}), exclude=True, operating=False)]])
    # only the itinerary with UA on the OUTBOUND slice is dropped
    assert len(out.solutions) == 1
    assert out.solution_count == 1
    itn = out.solutions[0].itinerary
    assert itn is not None
    assert itn.slices[0].flights == ["LH1"]


# ─────────────────────────── gate helpers ──────────────────────────────


def test_can_postfilter_supported_vs_unsupported() -> None:
    assert can_postfilter(CarrierPred(frozenset({"LH"}), exclude=False, operating=True))
    assert can_postfilter(ExcludeCodesharePred())
    assert not can_postfilter(ConnectTimePred(min_minutes=60, max_minutes=None))  # min layover
    assert not can_postfilter(ExcludeRedeyesPred())


def test_the_search_page_serves_encodable_and_post_filterable_predicates() -> None:
    assert search_page_reasons(classify("O:LH+", "-CODESHARE; MAXSTOPS 1").predicates) == []
    assert search_page_reasons(classify("~BA+", "-AIRLINES AF").predicates) == []


def test_every_other_predicate_keeps_its_own_reason() -> None:
    """One reason per predicate the page can't serve, and none for the ones it
    can: the user reads which constraint sent the search to Matrix."""
    reasons = search_page_reasons(classify("~BA+", "MINCONNECT 1:00; -REDEYES").predicates)
    assert reasons == ["a layover-time bound", "a red-eye exclusion"]
    assert search_page_reasons(classify("LH+", "F bc=y").predicates)  # include, Tier 3
    # Evaluable here, but Matrix reads both positionally and the filter does not.
    assert search_page_reasons(classify("AS21", None).predicates)
    assert search_page_reasons(classify("F* ~DUB F*", None).predicates)


def test_apply_postfilter_no_predicates_is_noop() -> None:
    res = _result(_slice([("UA1", "UA", ["UA"])]))
    out = apply_postfilter(res, [[]])
    assert out.solution_count == 1
