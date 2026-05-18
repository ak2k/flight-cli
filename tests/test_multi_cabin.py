# pyright: reportCallIssue=false, reportPrivateUsage=false, reportOptionalMemberAccess=false
# DIVERGE: pydantic Field(alias=...) confuses basedpyright into thinking
# alias names are required kwargs. The tests rely on populate_by_name=True
# (set on _Loose); silence the rule rather than reformat every constructor.
# Private-usage suppression: tests intentionally drive `_resolve_cabin_list`
# and `_derive_pp_cabins` (module-private helpers) — they're the units we're
# unit-testing. Optional-member-access: the test fixtures always build
# itineraries with `.itinerary` populated, but pydantic's `Optional` typing
# requires a narrow at every access — noise that drowns out real errors.
"""Tests for multi-cabin merge logic, CLI cabin-list parsing, and PP
cabin auto-derivation."""

from __future__ import annotations

import pytest
import typer

from flight_cli._multi_cabin import (
    MultiCabinRow,
    itinerary_key,
    merge,
    parse_price,
)
from flight_cli.cli import (
    _cash_per_cabin_multi,
    _cash_per_cabin_single,
    _derive_pp_cabins,
    _resolve_cabin_list,
)
from flight_cli.domain import Cabin
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
    SliceEndpoint,
)

# ─────────────────────────── itinerary builders ────────────────────────────


def _itin(
    *slices_data: tuple[str, str, str, str],
    price: str = "USD500.00",
) -> Itinerary:
    """Build an Itinerary. Each slices_data tuple is
    (flight_number, departure_iso, origin, destination)."""
    slcs = [
        Slice(
            flights=[fn],
            departure=dep,
            origin=SliceEndpoint(code=o),
            destination=SliceEndpoint(code=d),
        )
        for fn, dep, o, d in slices_data
    ]
    return Itinerary(
        displayTotal=price,
        ext=ItineraryExt(price=price),
        itinerary=ItineraryDetails(slices=slcs, carriers=[]),
    )


def _result(*itins: Itinerary) -> SearchResult:
    return SearchResult(solutionCount=len(itins), solutions=list(itins))


# ─────────────────────────── _resolve_cabin_list ───────────────────────────


def test_resolve_cabin_list_csv_basic():
    assert _resolve_cabin_list("economy,business") == (Cabin.COACH, Cabin.BUSINESS)


def test_resolve_cabin_list_short_aliases():
    assert _resolve_cabin_list("y,j,f") == (Cabin.COACH, Cabin.BUSINESS, Cabin.FIRST)


def test_resolve_cabin_list_dedup_preserves_order():
    assert _resolve_cabin_list("business,economy,business") == (Cabin.BUSINESS, Cabin.COACH)


def test_resolve_cabin_list_strips_whitespace_and_empties():
    assert _resolve_cabin_list(" economy , , business ") == (Cabin.COACH, Cabin.BUSINESS)


def test_resolve_cabin_list_single_cabin_returns_singleton():
    assert _resolve_cabin_list("economy") == (Cabin.COACH,)


def test_resolve_cabin_list_empty_errors():
    with pytest.raises(typer.Exit):
        _resolve_cabin_list(",,")


def test_resolve_cabin_list_unknown_token_errors():
    with pytest.raises(typer.Exit):
        _resolve_cabin_list("economy,nonsense")


# ────────────────────────────── itinerary_key ──────────────────────────────


def test_itinerary_key_one_way():
    it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"))
    assert itinerary_key(it) == (("AA100", "2026-08-15"),)


def test_itinerary_key_round_trip_distinct_keys():
    out = _itin(
        ("AA100", "2026-08-15T09:00", "JFK", "LHR"),
        ("AA200", "2026-08-22T18:00", "LHR", "JFK"),
    )
    ret_swapped = _itin(
        # Same return-first ordering produces a different tuple — the test
        # locks in that slice order matters (outbound + return aren't
        # interchangeable; the round trip is the unit).
        ("AA200", "2026-08-22T18:00", "LHR", "JFK"),
        ("AA100", "2026-08-15T09:00", "JFK", "LHR"),
    )
    assert itinerary_key(out) != itinerary_key(ret_swapped)


def test_itinerary_key_normalizes_flight_numbers():
    it = _itin((" aa 100 ", "2026-08-15T09:00", "JFK", "LHR"))
    assert itinerary_key(it) == (("AA100", "2026-08-15"),)


def test_itinerary_key_missing_flights_returns_none():
    it = Itinerary(
        itinerary=ItineraryDetails(
            slices=[Slice(flights=[], departure="2026-08-15T09:00")], carriers=[]
        ),
    )
    assert itinerary_key(it) is None


def test_itinerary_key_missing_departure_returns_none():
    it = _itin(("AA100", "", "JFK", "LHR"))
    assert itinerary_key(it) is None


# ────────────────────────────── parse_price ────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD530.00", 530.00),
        ("$1,078", 1078.0),
        ("1,078 USD", 1078.0),
        ("EUR99.99", 99.99),
        (None, None),
        ("", None),
        ("—", None),
        ("free", None),
    ],
)
def test_parse_price(raw: str | None, expected: float | None):
    assert parse_price(raw) == expected


# ──────────────────────────────── merge ────────────────────────────────────


def test_merge_full_overlap():
    a = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    b = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD3000.00")
    rows = merge(
        {Cabin.COACH: _result(a), Cabin.BUSINESS: _result(b)},
        sort_by=Cabin.COACH,
        top_n=10,
    )
    assert len(rows) == 1
    assert rows[0].prices == {Cabin.COACH: "USD600.00", Cabin.BUSINESS: "USD3000.00"}


def test_merge_partial_overlap_missing_filled_with_absent_keys():
    a = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    b = _itin(("BA200", "2026-08-15T11:00", "JFK", "LHR"), price="USD3500.00")
    rows = merge(
        {Cabin.COACH: _result(a), Cabin.BUSINESS: _result(b)},
        sort_by=Cabin.COACH,
        top_n=10,
    )
    assert len(rows) == 2
    by_carrier = {row.itinerary.itinerary.slices[0].flights[0]: row for row in rows}
    assert by_carrier["AA100"].prices == {Cabin.COACH: "USD600.00"}
    assert by_carrier["BA200"].prices == {Cabin.BUSINESS: "USD3500.00"}


def test_merge_sort_by_missing_sinks_to_bottom():
    has_econ = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD800.00")
    no_econ = _itin(("BA200", "2026-08-15T11:00", "JFK", "LHR"), price="USD9000.00")
    cheap_econ = _itin(("DL300", "2026-08-15T10:00", "JFK", "LHR"), price="USD600.00")
    # BUSINESS-only result contains the no-econ flight.
    rows = merge(
        {
            Cabin.COACH: _result(has_econ, cheap_econ),
            Cabin.BUSINESS: _result(no_econ),
        },
        sort_by=Cabin.COACH,
        top_n=10,
    )
    flight_nums = [r.itinerary.itinerary.slices[0].flights[0] for r in rows]
    assert flight_nums == ["DL300", "AA100", "BA200"]


def test_merge_top_n_truncates_after_sort():
    a = _itin(("AA1", "2026-08-15T09:00", "JFK", "LHR"), price="USD100.00")
    b = _itin(("BB2", "2026-08-15T10:00", "JFK", "LHR"), price="USD200.00")
    c = _itin(("CC3", "2026-08-15T11:00", "JFK", "LHR"), price="USD300.00")
    rows = merge(
        {Cabin.COACH: _result(c, a, b)},
        sort_by=Cabin.COACH,
        top_n=2,
    )
    assert [r.itinerary.itinerary.slices[0].flights[0] for r in rows] == ["AA1", "BB2"]


def test_merge_skips_unkeyable_itineraries():
    keyed = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    unkeyed = Itinerary(
        ext=ItineraryExt(price="USD400.00"),
        itinerary=ItineraryDetails(slices=[Slice(flights=[], departure="")], carriers=[]),
    )
    rows = merge(
        {Cabin.COACH: _result(keyed, unkeyed)},
        sort_by=Cabin.COACH,
        top_n=10,
    )
    assert len(rows) == 1


def test_merge_preserves_first_itinerary_for_render():
    """When the same key appears in two cabins, the renderer uses the first
    observed itinerary (its slices, carriers, legroom) — locking that in so
    consumers don't see surprise changes if dict iteration order differs."""
    coach_it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    biz_it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD3000.00")
    rows = merge(
        {Cabin.COACH: _result(coach_it), Cabin.BUSINESS: _result(biz_it)},
        sort_by=Cabin.COACH,
        top_n=10,
    )
    # Coach was first; the row's `itinerary` reference must be `coach_it`.
    assert rows[0].itinerary is coach_it


# ────────────────────────── _derive_pp_cabins ──────────────────────────────


def test_derive_pp_cabins_business_promotes_first():
    assert _derive_pp_cabins((Cabin.COACH, Cabin.BUSINESS)) == ("Economy", "Business", "First")


def test_derive_pp_cabins_business_only():
    assert _derive_pp_cabins((Cabin.BUSINESS,)) == ("Business", "First")


def test_derive_pp_cabins_first_present_no_double_add():
    assert _derive_pp_cabins((Cabin.BUSINESS, Cabin.FIRST)) == ("Business", "First")


def test_derive_pp_cabins_first_alone_does_not_promote_business():
    # Asymmetric rule: First → no auto-add of Business. Asking for First is
    # an explicit choice; we don't second-guess it.
    assert _derive_pp_cabins((Cabin.FIRST,)) == ("First",)


def test_derive_pp_cabins_economy_only():
    # Single cabin — preserve order, no promotion.
    assert _derive_pp_cabins((Cabin.COACH,)) == ("Economy",)


def test_derive_pp_cabins_premium_economy_business():
    assert _derive_pp_cabins((Cabin.PREMIUM_COACH, Cabin.BUSINESS)) == (
        "Premium economy",
        "Business",
        "First",
    )


# ────────────────────────── MultiCabinRow construction ─────────────────────


def test_multi_cabin_row_default_prices_empty():
    it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"))
    row = MultiCabinRow(itinerary=it)
    assert row.prices == {}


# ───────────────────────── cash_per_cabin builders ─────────────────────────


def test_cash_per_cabin_single_uses_queried_cabin_name():
    a = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    b = _itin(("BB200", "2026-08-15T10:00", "JFK", "LHR"), price="$1,200")
    res = _result(a, b)
    m = _cash_per_cabin_single(res, Cabin.BUSINESS)
    assert m[id(a)] == {"Business": 600.0}
    assert m[id(b)] == {"Business": 1200.0}


def test_cash_per_cabin_single_skips_unparseable_cash():
    no_price = Itinerary(
        ext=ItineraryExt(price=None),
        itinerary=ItineraryDetails(
            slices=[Slice(flights=["XX1"], departure="2026-08-15T09:00")], carriers=[]
        ),
    )
    has_price = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    m = _cash_per_cabin_single(_result(no_price, has_price), Cabin.COACH)
    assert id(no_price) not in m
    assert m[id(has_price)] == {"Economy": 600.0}


def test_cash_per_cabin_multi_keys_by_pp_cabin_names():
    it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    row = MultiCabinRow(itinerary=it)
    row.prices[Cabin.COACH] = "USD600.00"
    row.prices[Cabin.BUSINESS] = "USD3000.00"
    m = _cash_per_cabin_multi([row])
    assert m[id(it)] == {"Economy": 600.0, "Business": 3000.0}


def test_cash_per_cabin_multi_omits_cabins_without_parseable_cash():
    it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"), price="USD600.00")
    row = MultiCabinRow(itinerary=it)
    row.prices[Cabin.COACH] = "USD600.00"
    row.prices[Cabin.BUSINESS] = "—"  # the "missing" sentinel
    m = _cash_per_cabin_multi([row])
    # Business absent: no business cash → no business CPM in render.
    assert m[id(it)] == {"Economy": 600.0}


def test_cash_per_cabin_multi_skips_rows_with_no_parseable_cash():
    """A row whose every cabin price is unparseable doesn't appear in the
    map at all — callers don't need to defend against empty inner dicts."""
    it = _itin(("AA100", "2026-08-15T09:00", "JFK", "LHR"))
    row = MultiCabinRow(itinerary=it)
    row.prices[Cabin.COACH] = "—"
    row.prices[Cabin.BUSINESS] = ""
    m = _cash_per_cabin_multi([row])
    assert id(it) not in m
