# pyright: reportCallIssue=false, reportPrivateUsage=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/pp/test_match.py.
"""Tests for reconciling GF + Matrix cash results (_enrich.merge_results)."""

from __future__ import annotations

import itertools
import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import pytest

from conftest import _ds1
from flight_cli import _gflight_ids as gfid
from flight_cli._enrich import MergedRow, _itin_key, merge_results
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
    SliceEndpoint,
)
from flight_cli.pp.gflight_adapter import fli_results_to_search_result


def _it(price: str, flights: list[str], dep: str = "2026-08-15T08:00") -> Itinerary:
    return Itinerary(
        ext=ItineraryExt(price=price),
        itinerary=ItineraryDetails(slices=[Slice(flights=flights, departure=dep)]),
    )


def _sr(*its: Itinerary) -> SearchResult:
    return SearchResult(solutionCount=len(its), solutions=list(its))


def _first_flight(it: Itinerary) -> str | None:
    itn = it.itinerary
    return itn.slices[0].flights[0] if itn and itn.slices else None


def test_matched_itinerary_carries_both_prices_matrix_authoritative() -> None:
    gf = _sr(_it("USD500.00", ["LH455"], dep="2026-08-15T08:00"))
    # Same flight + date, different time + price -> still a match (flight# + date).
    matrix = _sr(_it("USD505.00", ["LH455"], dep="2026-08-15T09:30"))
    (row,) = merge_results(gf, matrix, currency="USD")
    assert row.source == "both"
    assert row.gf_price == "USD500.00"
    assert row.matrix_price == "USD505.00"
    assert row.itinerary.price == "USD505.00"  # Matrix structure is authoritative


def test_matrix_only_row() -> None:
    (row,) = merge_results(_sr(), _sr(_it("USD600.00", ["AF83"])), currency="USD")
    assert row.source == "matrix"
    assert row.matrix_price == "USD600.00"
    assert row.gf_price is None


def test_gf_only_row() -> None:
    (row,) = merge_results(_sr(_it("USD380.00", ["UA58"])), _sr(), currency="USD")
    assert row.source == "gf"
    assert row.gf_price == "USD380.00"
    assert row.matrix_price is None


def test_merge_sorts_by_best_price_and_tags_sources() -> None:
    gf = _sr(_it("USD380.00", ["UA58"]), _it("USD500.00", ["LH455"]))
    matrix = _sr(_it("USD505.00", ["LH455"]), _it("USD900.00", ["AF83"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [(r.source, _first_flight(r.itinerary)) for r in rows] == [
        ("gf", "UA58"),  # 380 — GF-only (ULCC/codeshare)
        ("both", "LH455"),  # 500/505 — matched
        ("matrix", "AF83"),  # 900 — Matrix-only
    ]


def test_a_fare_in_another_currency_ranks_after_the_requested_ones() -> None:
    """Matrix priced in GBP and Google in USD. By bare numbers GBP1027 ranks
    above USD1043 (about GBP780); ranked in the requested USD, the Google fare
    leads, the matched row follows on its USD price, and the Matrix-only GBP
    fare comes after every USD one."""
    gf = _sr(_it("USD1330.00", ["VS45"]), _it("USD1043.00", ["TK1988", "TK1"]))
    matrix = _sr(_it("GBP1004.00", ["VS45"]), _it("GBP1027.00", ["AA101"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [(r.source, _first_flight(r.itinerary)) for r in rows] == [
        ("gf", "TK1988"),
        ("both", "VS45"),
        ("matrix", "AA101"),
    ]
    assert (rows[1].matrix_price, rows[1].gf_price) == ("GBP1004.00", "USD1330.00")


def test_a_one_currency_merge_orders_by_amount_whatever_was_asked_for() -> None:
    gf = _sr(_it("GBP380.00", ["UA58"]), _it("GBP500.00", ["LH455"]))
    matrix = _sr(_it("GBP505.00", ["LH455"]), _it("GBP90.00", ["AF83"]))
    rows = merge_results(gf, matrix, currency="USD")
    assert [_first_flight(r.itinerary) for r in rows] == ["AF83", "UA58", "LH455"]


def test_unkeyed_itineraries_stay_single_source() -> None:
    # No flights -> unmatchable -> kept as a single-source row, not merged.
    gf = _sr(_it("USD100.00", []))
    matrix = _sr(_it("USD100.00", []))
    rows = merge_results(gf, matrix, currency="USD")
    assert len(rows) == 2
    assert {r.source for r in rows} == {"gf", "matrix"}


def test_empty_inputs() -> None:
    assert merge_results(_sr(), _sr(), currency="USD") == []


# ───────── a matched row takes Google's per-flight dates ─────────

_D, _D1 = "2026-10-20", "2026-10-21"


def _nz(arrival: str = f"{_D}T10:00", segment_dates: list[str] | None = None) -> Slice:
    """NZ104 SYD-AKL, then NZ10 AKL-HNL, which leaves Auckland after midnight
    and lands in Honolulu on the day the trip began."""
    return Slice(
        flights=["NZ104", "NZ10"],
        departure=f"{_D}T18:00",
        arrival=arrival,
        origin=SliceEndpoint(code="SYD"),
        destination=SliceEndpoint(code="HNL"),
        stops=[SliceEndpoint(code="AKL")],
        segment_dates=segment_dates or [],
    )


def _nz_row(price: str, s: Slice, sid: str | None = None) -> Itinerary:
    return Itinerary(id=sid, ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=[s]))


def test_a_matched_matrix_connection_takes_googles_flight_dates() -> None:
    gf = _sr(_nz_row("USD900.00", _nz(segment_dates=[_D, _D1])))
    matrix = _sr(_nz_row("USD880.00", _nz(), sid="sol-1"))
    (row,) = merge_results(gf, matrix, currency="USD")
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    assert row.itinerary.itinerary.slices[0].segment_dates == [_D, _D1]
    # Still Matrix's itinerary: its id pins the Matrix link, its price is Matrix's.
    assert row.itinerary.id == "sol-1"
    assert row.itinerary.price == "USD880.00"


def test_the_merge_leaves_both_results_as_they_were() -> None:
    gf = _sr(_nz_row("USD900.00", _nz(segment_dates=[_D, _D1])))
    matrix = _sr(_nz_row("USD880.00", _nz(), sid="sol-1"))
    before = (gf.model_dump(), matrix.model_dump())
    merge_results(gf, matrix, currency="USD")
    assert (gf.model_dump(), matrix.model_dump()) == before


def test_a_google_row_landing_on_another_day_lends_no_dates() -> None:
    """Same flights leaving the same day share the match key, but a Google row
    whose NZ10 leaves a day later does not date Matrix's NZ10."""
    late = _nz(arrival=f"{_D1}T10:00", segment_dates=[_D, "2026-10-22"])
    (row,) = merge_results(
        _sr(_nz_row("USD900.00", late)), _sr(_nz_row("USD880.00", _nz())), currency="USD"
    )
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    assert row.itinerary.itinerary.slices[0].segment_dates == []


def _ua(arrival: str, segment_dates: list[str] | None = None) -> Slice:
    """UA100 SFO-DEN, then UA200 DEN-EWR, landing on 2026-11-02 either way:
    the same day's red-eye or the next morning's flight."""
    return Slice(
        flights=["UA100", "UA200"],
        departure="2026-11-01T07:00",
        arrival=arrival,
        origin=SliceEndpoint(code="SFO"),
        destination=SliceEndpoint(code="EWR"),
        stops=[SliceEndpoint(code="DEN")],
        segment_dates=segment_dates or [],
    )


def _dates_lent(google_arrival: str, matrix_arrival: str) -> list[str]:
    google = _ua(google_arrival, segment_dates=["2026-11-01", "2026-11-01"])
    (row,) = merge_results(
        _sr(_nz_row("USD500.00", google)),
        _sr(_nz_row("USD480.00", _ua(matrix_arrival))),
        currency="USD",
    )
    assert row.source == "both"
    assert row.itinerary.itinerary is not None
    return row.itinerary.itinerary.slices[0].segment_dates


def test_a_google_row_landing_at_another_time_that_day_lends_no_dates() -> None:
    """Google's UA200 is the red-eye leaving on the 1st; Matrix's lands at
    13:30, so it is the next morning's, and the 1st would pin the red-eye."""
    assert _dates_lent("2026-11-02T02:45:00", "2026-11-02T13:30-05:00") == []


def test_a_google_row_landing_at_the_same_minute_lends_its_dates() -> None:
    """Google writes local time with no offset, Matrix with one."""
    assert _dates_lent("2026-11-02T13:30:00", "2026-11-02T13:30-05:00") == [
        "2026-11-01",
        "2026-11-01",
    ]


def test_a_one_minute_skew_between_the_sources_lends_no_dates() -> None:
    assert _dates_lent("2026-11-02T13:31:00", "2026-11-02T13:30-05:00") == []


def test_the_google_row_that_is_the_same_trip_lends_its_dates_whatever_its_place() -> None:
    """Two Google rows share the Matrix row's match key; only the second is its
    trip. It lends the dates and the Google price, in either order, and the
    other Google row is a row of its own."""
    late = _nz_row("USD900.00", _nz(arrival=f"{_D1}T10:00:00", segment_dates=[_D, "2026-10-22"]))
    same = _nz_row("USD910.00", _nz(arrival=f"{_D}T10:00:00", segment_dates=[_D, _D1]))
    matrix = _sr(_nz_row("USD880.00", _nz(arrival=f"{_D}T10:00-10:00"), sid="sol-1"))
    for google in (_sr(late, same), _sr(same, late)):
        row, other = merge_results(google, matrix, currency="USD")
        assert row.source == "both"
        assert row.itinerary.itinerary is not None
        assert row.itinerary.itinerary.slices[0].segment_dates == [_D, _D1]
        assert row.gf_price == "USD910.00"
        assert (other.source, other.gf_price, other.itinerary) == ("gf", "USD900.00", late)


def _three_flights(segment_dates: list[str] | None = None) -> Slice:
    return Slice(
        flights=["AA1", "AA2", "AA3"],
        departure="2026-11-01T07:00",
        arrival="2026-11-03T09:00",
        segment_dates=segment_dates or [],
    )


def test_two_google_rows_that_are_the_trip_on_other_days_lend_no_dates() -> None:
    """Both land at Matrix's minute, but AA2 leaves on another day in each, and
    Matrix states no day for it: neither row can say which one Matrix's is."""
    a = _nz_row("USD700.00", _three_flights(["2026-11-01", "2026-11-01", "2026-11-03"]))
    b = _nz_row("USD720.00", _three_flights(["2026-11-01", "2026-11-02", "2026-11-03"]))
    matrix = _sr(_nz_row("USD690.00", _three_flights()))
    for google in (_sr(a, b), _sr(b, a)):
        row, other = merge_results(google, matrix, currency="USD")
        assert row.source == "both"
        assert row.itinerary.itinerary is not None
        assert row.itinerary.itinerary.slices[0].segment_dates == []
        # Priced by the first Google row, undated; the second is its own row.
        assert row.gf_price == google.solutions[0].price
        assert (other.source, other.itinerary) == ("gf", google.solutions[1])


# ───────── one match key, several trips ─────────
#
# The key fixes the flights and the first day, not the trip: Icelandair sells
# FI614 then FI450 out of JFK on one evening with the connection in Keflavik
# the next morning or the one after.


def _fi_board() -> SearchResult:
    """The two FI614+FI450 rows of the JFK-LHR capture: USD617 landing the next
    day and USD690 landing a day later."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lhr_tfu.json"))
    board = fli_results_to_search_result(
        [gfid._parse_flight_with_id(r) for r in gfid._rows_from_ds1(payload).rows]
    )
    return _sr(*(it for it in board.solutions if _flights(it) == ("FI614", "FI450")))


def _flights(it: Itinerary) -> tuple[str, ...]:
    itn = it.itinerary
    return tuple(f for s in itn.slices for f in s.flights) if itn else ()


def _as_matrix(g: Itinerary, price: str, *, late: int = 0) -> Itinerary:
    """Google row `g` as Matrix states it: its own price, a UTC offset on the
    landing time and no per-flight dates. Landing `late` minutes later, it is
    no longer `g`'s trip."""
    assert g.itinerary is not None
    (s,) = g.itinerary.slices
    assert s.arrival is not None
    lands = datetime.fromisoformat(s.arrival) + timedelta(minutes=late)
    slc = s.model_copy(update={"arrival": f"{lands.isoformat()}+00:00", "segment_dates": []})
    return _nz_row(price, slc)


def _view(rows: list[MergedRow]) -> list[tuple[str, str | None, str | None, bool]]:
    """(source, Google price, Matrix price, dated) per row."""
    out: list[tuple[str, str | None, str | None, bool]] = []
    for r in rows:
        itn = r.itinerary.itinerary
        out.append(
            (r.source, r.gf_price, r.matrix_price, bool(itn and itn.slices[0].segment_dates))
        )
    return out


def test_every_google_row_of_a_shared_key_is_its_own_row() -> None:
    google = _fi_board()
    assert [it.price for it in google.solutions] == ["USD617.00", "USD690.00"]
    assert _view(merge_results(google, _sr(), currency="USD")) == [
        ("gf", "USD617.00", None, True),
        ("gf", "USD690.00", None, True),
    ]


@pytest.mark.parametrize("trip", [0, 1])
def test_a_matrix_trip_pairs_with_its_own_google_row_and_the_other_stays(trip: int) -> None:
    google = _fi_board()
    matrix = _sr(_as_matrix(google.solutions[trip], "USD884.00"))
    rows = merge_results(google, matrix, currency="USD")
    own, other = google.solutions[trip].price, google.solutions[1 - trip].price
    assert sorted(_view(rows)) == [
        ("both", own, "USD884.00", True),
        ("gf", other, None, True),
    ]


def _fi(price: str, lands: str, segment_dates: list[str] | None = None) -> Itinerary:
    return _nz_row(
        price,
        Slice(
            flights=["FI614", "FI450"],
            departure="2026-10-20T20:30",
            arrival=lands,
            segment_dates=segment_dates or [],
        ),
    )


def test_every_matrix_row_of_a_key_is_kept_and_takes_only_its_own_trip() -> None:
    """Matrix priced FI614/FI450 twice on 2026-10-20, landing the 21st and the
    22nd; Google lists only the second. The first Matrix row has no lender, and
    were it priced by the first Google row left before the second found its own,
    a USD884 fare would show the USD1180 trip's Google price."""
    google = _sr(_fi("USD1180.00", "2026-10-22T11:55:00", ["2026-10-20", "2026-10-22"]))
    matrix = _sr(
        _fi("USD884.00", "2026-10-21T11:55+00:00"), _fi("USD1180.00", "2026-10-22T11:55+00:00")
    )
    assert _view(merge_results(google, matrix, currency="USD")) == [
        ("matrix", None, "USD884.00", False),
        ("both", "USD1180.00", "USD1180.00", True),
    ]


def test_two_matrix_rows_of_a_key_no_google_row_dates_share_one_google_price() -> None:
    """Neither lands when Google's row does: the key's first Matrix row is
    priced by it, undated, and the second has no Google price."""
    google = _sr(_fi("USD900.00", "2026-10-23T11:55:00", ["2026-10-20", "2026-10-23"]))
    matrix = _sr(
        _fi("USD884.00", "2026-10-21T11:55+00:00"), _fi("USD1180.00", "2026-10-22T11:55+00:00")
    )
    assert _view(merge_results(google, matrix, currency="USD")) == [
        ("both", "USD900.00", "USD884.00", False),
        ("matrix", None, "USD1180.00", False),
    ]


# ───────── no key shared by two rows: the list the merge always gave ─────────


def _lax_board() -> SearchResult:
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_tfu.json"))
    return fli_results_to_search_result(
        [gfid._parse_flight_with_id(r) for r in gfid._rows_from_ds1(payload).rows]
    )


def _lax_matrix(board: SearchResult) -> SearchResult:
    """A Matrix answer beside the LAX board: every third Google trip at USD5
    more, every other one of those landing a minute off so it lends no dates,
    two trips Google did not list and one with no flights to key on."""
    its: list[Itinerary] = []
    for i, g in enumerate(board.solutions[::3]):
        assert g.price is not None and g.price.startswith("USD")
        its.append(_as_matrix(g, f"USD{float(g.price[3:]) + 5:.2f}", late=i % 2))
    day = f"{_lax_departure(board)}T09:00"
    its += [_it("USD150.00", ["ZZ1"], dep=day), _it("USD999.00", ["ZZ2", "ZZ3"], dep=day)]
    its.append(_it("USD300.00", []))
    return _sr(*its)


def _lax_departure(board: SearchResult) -> str:
    itn = board.solutions[0].itinerary
    assert itn is not None and itn.slices[0].departure is not None
    return itn.slices[0].departure[:10]


_Compact = tuple[str, str | None, str | None, str, bool]


def _compact(rows: list[MergedRow]) -> list[_Compact]:
    """(source, Google price, Matrix price, flights, dated) per row."""
    return [
        (s, g, m, "+".join(_flights(r.itinerary)), dated)
        for r, (s, g, m, dated) in zip(rows, _view(rows), strict=True)
    ]


def _lowest(row: _Compact) -> float:
    return min(float(p[3:]) for p in row[1:3] if p is not None)


def test_a_board_with_no_shared_key_merges_as_it_always_did() -> None:
    """The rows are what the merge gave before a key could hold several trips
    (`_BASE_LAX_MERGE`, computed once by that code, which ranked a matched row
    on Matrix's price): `both` rows dated and undated, rows of either side
    alone and a Matrix row with no key. Each matched row's Matrix price is
    Google's plus USD5, so it ranks on Google's, first among the rows at that
    price; the matched rows and the others each keep their own order."""
    board = _lax_board()
    matrix = _lax_matrix(board)
    google_keys = [_itin_key(it) for it in board.solutions]
    matrix_keys = [k for it in matrix.solutions if (k := _itin_key(it)) is not None]
    assert len(set(google_keys)) == len(google_keys) == 95
    assert len(set(matrix_keys)) == len(matrix_keys) == 34
    assert len(set(google_keys) & set(matrix_keys)) == 32
    merged = _compact(merge_results(board, matrix, currency="USD"))
    assert Counter(merged) == Counter(_BASE_LAX_MERGE)
    for matched in (True, False):
        assert [r for r in merged if (r[0] == "both") is matched] == [
            r for r in _BASE_LAX_MERGE if (r[0] == "both") is matched
        ]
    assert [_lowest(r) for r in merged] == sorted(map(_lowest, merged))
    assert not [
        (a, b)
        for a, b in itertools.pairwise(merged)
        if _lowest(a) == _lowest(b) and a[0] != "both" and b[0] == "both"
    ]
    assert merged[:3] == [
        ("matrix", None, "USD150.00", "ZZ1", False),
        ("both", "USD204.00", "USD209.00", "DL1788", True),
        ("both", "USD204.00", "USD209.00", "B6123", False),
    ]


_BASE_LAX_MERGE: list[_Compact] = [
    ("matrix", None, "USD150.00", "ZZ1", False),
    ("gf", "USD204.00", None, "B61023", True),
    ("gf", "USD204.00", None, "DL747", True),
    ("gf", "USD204.00", None, "DL742", True),
    ("gf", "USD204.00", None, "B6523", True),
    ("gf", "USD204.00", None, "B6323", True),
    ("gf", "USD204.00", None, "DL773", True),
    ("gf", "USD204.00", None, "DL1905", True),
    ("gf", "USD204.00", None, "B6423", True),
    ("gf", "USD204.00", None, "B61223", True),
    ("gf", "USD204.00", None, "B6923", True),
    ("both", "USD204.00", "USD209.00", "DL1788", True),
    ("both", "USD204.00", "USD209.00", "B6123", False),
    ("both", "USD204.00", "USD209.00", "DL713", True),
    ("both", "USD204.00", "USD209.00", "B6223", False),
    ("both", "USD204.00", "USD209.00", "DL707", True),
    ("gf", "USD214.00", None, "AS21+AS487", True),
    ("gf", "USD214.00", None, "AS21+AS1793", True),
    ("gf", "USD214.00", None, "AS227+AS1831", True),
    ("gf", "USD214.00", None, "AS227+AS308", True),
    ("gf", "USD214.00", None, "AS23+AS492", True),
    ("both", "USD214.00", "USD219.00", "AS21+AS600", False),
    ("both", "USD214.00", "USD219.00", "AS227+AS595", True),
    ("both", "USD214.00", "USD219.00", "AS23+AS1390", False),
    ("gf", "USD222.00", None, "AA466+AA2690", True),
    ("both", "USD222.00", "USD227.00", "AA466+AA6425", True),
    ("gf", "USD249.00", None, "B6623", True),
    ("gf", "USD254.00", None, "AA171", True),
    ("gf", "USD254.00", None, "AA1", True),
    ("gf", "USD254.00", None, "AA2365", True),
    ("gf", "USD254.00", None, "AA302", True),
    ("gf", "USD254.00", None, "AA117", True),
    ("gf", "USD255.00", None, "AS33+AS308", True),
    ("gf", "USD255.00", None, "AS41+AS2415", True),
    ("both", "USD254.00", "USD259.00", "AA33", False),
    ("both", "USD254.00", "USD259.00", "AA3", True),
    ("both", "USD254.00", "USD259.00", "AA300", False),
    ("gf", "USD264.00", None, "AA2045+AA1135", True),
    ("gf", "USD264.00", None, "AA760+AA2793", True),
    ("gf", "USD264.00", None, "AA760+AA2974", True),
    ("gf", "USD264.00", None, "AA449+AA2040", True),
    ("gf", "USD264.00", None, "AA2819+AA832", True),
    ("gf", "USD264.00", None, "AA2819+AA1021", True),
    ("gf", "USD264.00", None, "AA1444+AA1158", True),
    ("gf", "USD264.00", None, "AA2229+AA1687", True),
    ("gf", "USD264.00", None, "AA2848+AA3316", True),
    ("gf", "USD264.00", None, "AA865+AA1759", True),
    ("gf", "USD264.00", None, "AA865+AA854", True),
    ("gf", "USD264.00", None, "AA2680+AA2775", True),
    ("gf", "USD264.00", None, "AA860+AA487", True),
    ("gf", "USD264.00", None, "AA860+AA1777", True),
    ("gf", "USD264.00", None, "AA3162+AA2467", True),
    ("gf", "USD264.00", None, "AA2469+AA1441", True),
    ("both", "USD260.00", "USD265.00", "AS39+AS2486", True),
    ("gf", "USD267.00", None, "AS41+AS345", True),
    ("gf", "USD267.00", None, "AS41+AS412", True),
    ("gf", "USD267.00", None, "AS39+AS345", True),
    ("gf", "USD267.00", None, "AS39+AS412", True),
    ("gf", "USD267.00", None, "AS39+AS372", True),
    ("gf", "USD267.00", None, "AS39+AS595", True),
    ("gf", "USD267.00", None, "AA177+AA6441", True),
    ("both", "USD264.00", "USD269.00", "AA760+AA2968", False),
    ("both", "USD264.00", "USD269.00", "AA2819+AA2791", True),
    ("both", "USD264.00", "USD269.00", "AA1444+AA1135", False),
    ("both", "USD264.00", "USD269.00", "AA2848+AA1021", True),
    ("both", "USD264.00", "USD269.00", "AA865+AA530", False),
    ("both", "USD264.00", "USD269.00", "AA860+AA854", True),
    ("both", "USD264.00", "USD269.00", "AA495+AA2023", False),
    ("both", "USD264.00", "USD269.00", "AA2634+AA2023", True),
    ("both", "USD267.00", "USD272.00", "AS41+AS303", False),
    ("both", "USD267.00", "USD272.00", "AS39+AS303", True),
    ("both", "USD267.00", "USD272.00", "AA177+AA2211", False),
    ("gf", "USD294.00", None, "DL771", True),
    ("gf", "USD294.00", None, "DL1915", True),
    ("both", "USD294.00", "USD299.00", "B61523", True),
    ("matrix", None, "USD300.00", "", False),
    ("gf", "USD312.00", None, "AS41+AS2486", True),
    ("gf", "USD317.00", None, "AA2901+AA2040", True),
    ("gf", "USD321.00", None, "AA1067+AA1979", True),
    ("both", "USD317.00", "USD322.00", "AA2901+AA3122", False),
    ("both", "USD321.00", "USD326.00", "AA1067+AA2710", True),
    ("gf", "USD327.00", None, "AA177+AA6270", True),
    ("gf", "USD327.00", None, "AA177+AA6316", True),
    ("both", "USD327.00", "USD332.00", "AA177+AA6429", False),
    ("gf", "USD339.00", None, "AA255", True),
    ("gf", "USD339.00", None, "AA306", True),
    ("gf", "USD349.00", None, "AA4397+AA1032", True),
    ("both", "USD349.00", "USD354.00", "AA4397+AA1243", True),
    ("gf", "USD487.00", None, "AA15+AA6270", True),
    ("both", "USD509.00", "USD514.00", "AA4397+AA2473", False),
    ("gf", "USD732.00", None, "AA688+AA3255", True),
    ("gf", "USD732.00", None, "AA2017+AA3255", True),
    ("both", "USD732.00", "USD737.00", "AA3060+AA3255", True),
    ("gf", "USD902.00", None, "AA1186+AA3255", True),
    ("gf", "USD902.00", None, "AA475+AA3255", True),
    ("matrix", None, "USD999.00", "ZZ2+ZZ3", False),
    ("gf", "USD1012.00", None, "DL2293+DL575", True),
    ("both", "USD1012.00", "USD1017.00", "DL2286+DL827", False),
]


# ───────── the price a row carries and ranks on ─────────


def test_a_party_ranks_every_row_on_the_partys_price() -> None:
    """Google prices the whole party and Matrix lists one passenger's fare,
    so for two a Matrix row carries the total Matrix states: ZZ1 at USD180 a
    passenger is USD360 for two, dearer than Google's USD300 for XX9."""
    google = _sr(_it("USD457.00", ["DL1"]), _it("USD300.00", ["XX9"]))
    zz1 = _it("USD180.00", ["ZZ1"]).model_copy(update={"display_total": "USD360.00"})
    dl1 = _it("USD229.00", ["DL1"]).model_copy(update={"display_total": "USD456.80"})
    rows = merge_results(google, _sr(zz1, dl1), currency="USD", passengers=2)
    assert [(r.source, _first_flight(r.itinerary), r.gf_price, r.matrix_price) for r in rows] == [
        ("gf", "XX9", "USD300.00", None),
        ("matrix", "ZZ1", None, "USD360.00"),
        ("both", "DL1", "USD457.00", "USD456.80"),
    ]


def test_a_matched_row_ranks_on_the_lowest_price_it_prints() -> None:
    """Matrix's whole answer adds DL1788, Google's first USD204 row, at
    USD999. Matched, it ranks on Google's USD204, so the deeper page keeps
    the first three trips the page of `-n` gave rather than pushing DL1788
    out for the next USD204 row."""
    board = _lax_board()
    day = f"{_lax_departure(board)}T09:00"
    cheaper = [_it("USD100.00", ["ZZ1"], dep=day), _it("USD101.00", ["ZZ2"], dep=day)]
    page = SearchResult(solutionCount=3, solutions=cheaper)
    whole = _sr(*cheaper, _as_matrix(board.solutions[0], "USD999.00"))
    first = merge_results(board, page, currency="USD")[:3]
    deep = merge_results(board, whole, currency="USD")[:3]
    assert [_flights(r.itinerary) for r in first] == [_flights(r.itinerary) for r in deep]
    assert [(r.source, _flights(r.itinerary), r.gf_price, r.matrix_price) for r in deep] == [
        ("matrix", ("ZZ1",), None, "USD100.00"),
        ("matrix", ("ZZ2",), None, "USD101.00"),
        ("both", ("DL1788",), "USD204.00", "USD999.00"),
    ]


def test_a_row_ranks_on_its_exact_amount() -> None:
    """Google's USD456.00 is under Matrix's USD456.80, though both are 456
    whole dollars."""
    rows = merge_results(
        _sr(_it("USD456.00", ["XX9"])), _sr(_it("USD456.80", ["ZZ1"])), currency="USD"
    )
    assert [_first_flight(r.itinerary) for r in rows] == ["XX9", "ZZ1"]


# ───────── the Google row a merged row holds ─────────


def test_a_pair_landing_at_the_same_minute_is_the_same_trip_and_holds_googles_row() -> None:
    gf = _sr(_nz_row("USD500.00", _ua("2026-11-02T13:30:00", ["2026-11-01", "2026-11-01"])))
    matrix = _sr(_nz_row("USD480.00", _ua("2026-11-02T13:30-05:00")))
    (row,) = merge_results(gf, matrix, currency="USD")
    assert (row.source, row.same_trip) == ("both", True)
    assert row.google is gf.solutions[0]


def test_a_matrix_row_priced_by_a_google_row_left_over_is_not_the_same_trip() -> None:
    """The key's first Matrix row lands the 21st and takes the only Google row,
    which lands the 23rd: one key, two trips."""
    google = _sr(_fi("USD900.00", "2026-10-23T11:55:00", ["2026-10-20", "2026-10-23"]))
    matrix = _sr(
        _fi("USD884.00", "2026-10-21T11:55+00:00"), _fi("USD1180.00", "2026-10-22T11:55+00:00")
    )
    first, second = merge_results(google, matrix, currency="USD")
    assert (first.source, first.same_trip) == ("both", False)
    assert first.google is google.solutions[0]
    assert (second.source, second.same_trip, second.google) == ("matrix", False, None)


def test_a_google_only_row_holds_its_own_row() -> None:
    gf = _sr(_it("USD380.00", ["UA58"]), _it("USD100.00", []))
    rows = merge_results(gf, _sr(), currency="USD")
    assert [(r.google is r.itinerary, r.same_trip) for r in rows] == [(True, False)] * 2
