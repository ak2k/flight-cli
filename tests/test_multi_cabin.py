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

from typing import Any, cast

import pytest
import typer

from flight_cli._multi_cabin import (
    MultiCabinRow,
    itinerary_key,
    merge,
    parse_price,
)
from flight_cli.cli import (
    _MULTI_CABIN_QUERY_BUMP_CAP,
    _MULTI_CABIN_QUERY_BUMP_FACTOR,
    _bumped_query_top_n,
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


# ────────────────────── _bumped_query_top_n ────────────────────────────────


def test_bumped_query_top_n_single_cabin_no_bump():
    """Single cabin keeps the user's top_n verbatim — no bump applies."""
    assert _bumped_query_top_n(5, cabin_count=1) == 5
    assert _bumped_query_top_n(50, cabin_count=1) == 50
    assert _bumped_query_top_n(0, cabin_count=1) == 0


def test_bumped_query_top_n_multi_scales_by_factor():
    assert _bumped_query_top_n(5, cabin_count=2) == 5 * _MULTI_CABIN_QUERY_BUMP_FACTOR


def test_bumped_query_top_n_caps_at_ceiling():
    """A user-bumped -n already at or above the cap doesn't get scaled —
    bound the per-cabin response size regardless of cabin count."""
    assert _bumped_query_top_n(50, cabin_count=2) == _MULTI_CABIN_QUERY_BUMP_CAP
    assert _bumped_query_top_n(200, cabin_count=3) == _MULTI_CABIN_QUERY_BUMP_CAP


def test_bumped_query_top_n_cabin_count_doesnt_compound():
    """Three cabins doesn't widen further than two — overlap is pairwise, so
    5x per cabin is enough regardless of cabin count."""
    assert _bumped_query_top_n(5, cabin_count=3) == _bumped_query_top_n(5, cabin_count=2)
    assert _bumped_query_top_n(5, cabin_count=4) == _bumped_query_top_n(5, cabin_count=2)


# ── multi-cabin gflight: JSON shape + the constraint guard (work-h70kv.5) ──


def _one_gflight_row() -> Any:
    """A real GFlightWithId, parsed from the committed ds:1 capture."""
    import json as _json
    import pathlib as _pathlib

    from flight_cli import _gflight_ids as gfid

    fixture = (
        _pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
    )
    rows = gfid._rows_from_ds1(_json.loads(fixture.read_text())).rows
    return gfid._parse_flight_with_id(rows[0])


def test_multi_cabin_json_carries_legroom_like_the_single_cabin_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both JSON paths must emit the same row shape: a bare `model_dump()` in
    either one loses the legroom/amenities the other carries."""
    import json as _json
    from datetime import date as _date

    from flight_cli import cli
    from flight_cli.domain import Cabin as _Cabin
    from flight_cli.domain import Leg as _Leg
    from flight_cli.domain import SearchOptions as _SearchOptions

    row = _one_gflight_row()

    def _fan_out(**_kw: Any) -> dict[Any, list[Any]]:
        return {_Cabin.COACH: [row]}

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    cli._run_gflight_path_multi(
        legs=(_Leg.of("JFK", "LAX", _date(2026, 10, 14)),),
        opts=_SearchOptions(cabin=_Cabin.COACH),
        cabins=(_Cabin.COACH, _Cabin.BUSINESS),
        sort_by=_Cabin.COACH,
        top_n=5,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    dumped: Any = _json.loads(capsys.readouterr().out)["COACH"][0]
    assert dumped["flight_id"] == row.flight_id
    # Compare through JSON: the helper keeps tuples that a dump turns into lists.
    expected: Any = _json.loads(_json.dumps(cli._gflight_json_row(row), default=str))
    assert dumped == expected
    assert dumped["legs"][0]["legroom_class"]


@pytest.mark.parametrize(
    ("legs_wanted", "cabins_wanted", "shown"),
    [
        pytest.param(2, 2, True, id="a-round-trip-across-two-cabins"),
        pytest.param(1, 2, False, id="one-way-has-no-pinned-fan-out"),
        pytest.param(2, 1, False, id="one-cabin-has-nothing-to-join"),
    ],
)
def test_a_multi_cabin_round_trip_says_what_its_join_is_drawn_from(
    monkeypatch: pytest.MonkeyPatch, legs_wanted: int, cabins_wanted: int, shown: bool
) -> None:
    """`_PINNED_FANOUT_CAP` decides what the cabin join can even see, so a blank
    cabin cell on a round trip means "these ten outbounds had no fare in both
    cabins" and reads as "that fare does not exist". Only where the cap bites:
    a one-way pins nothing and a single cabin joins nothing."""
    import io as _io
    from datetime import date as _date

    from rich.console import Console as _Console

    from flight_cli import cli
    from flight_cli.domain import Cabin as _Cabin
    from flight_cli.domain import Leg as _Leg
    from flight_cli.domain import SearchOptions as _SearchOptions

    buf = _io.StringIO()
    monkeypatch.setattr(cli, "err", _Console(file=buf, width=400, no_color=True, highlight=False))

    row = _one_gflight_row()
    cabins = (_Cabin.COACH, _Cabin.BUSINESS)[:cabins_wanted]

    def _fan_out(**_kw: Any) -> dict[Any, list[Any]]:
        return {cab: [row] for cab in cabins}

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    legs = (_Leg.of("JFK", "LAX", _date(2026, 10, 14)),)
    if legs_wanted > 1:
        legs += (_Leg.of("LAX", "JFK", _date(2026, 10, 21)),)
    cli._run_gflight_path_multi(
        legs=legs,
        opts=_SearchOptions(cabin=_Cabin.COACH),
        cabins=cabins,
        sort_by=_Cabin.COACH,
        top_n=5,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    from flight_cli._gflight_ids import pinned_fanout

    pins = pinned_fanout(cli._bumped_query_top_n(5, len(cabins)))
    assert (f"up to {pins} of each cabin's first-ranked" in buf.getvalue()) is shown, buf.getvalue()


@pytest.mark.parametrize(
    ("top_n", "expected"),
    [
        pytest.param(10, 10, id="the-default-sits-on-the-cap"),
        pytest.param(1, 5, id="a-small-n-pins-fewer-than-the-cap"),
    ],
)
def test_the_join_note_counts_the_outbounds_that_were_actually_pinned(
    monkeypatch: pytest.MonkeyPatch, top_n: int, expected: int
) -> None:
    """The number is the point of the sentence, so it comes from the pin budget
    rather than a literal. Below the cap `-n` decides, and a note still saying
    "10" would explain an empty cell with a number that never happened."""
    import io as _io
    from datetime import date as _date

    from rich.console import Console as _Console

    from flight_cli import cli
    from flight_cli.domain import Cabin as _Cabin
    from flight_cli.domain import Leg as _Leg
    from flight_cli.domain import SearchOptions as _SearchOptions

    buf = _io.StringIO()
    monkeypatch.setattr(cli, "err", _Console(file=buf, width=400, no_color=True, highlight=False))
    row = _one_gflight_row()
    cabins = (_Cabin.COACH, _Cabin.BUSINESS)

    def _fan_out(**_kw: Any) -> dict[Any, list[Any]]:
        return {c: [row] for c in cabins}

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    cli._run_gflight_path_multi(
        legs=(
            _Leg.of("JFK", "LAX", _date(2026, 10, 14)),
            _Leg.of("LAX", "JFK", _date(2026, 10, 21)),
        ),
        opts=_SearchOptions(cabin=_Cabin.COACH),
        cabins=cabins,
        sort_by=_Cabin.COACH,
        top_n=top_n,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    # "up to", because the cap bounds how many outbounds the join can see and
    # the board may hold fewer. The number is still the pin budget's.
    assert f"up to {expected} of each cabin's first-ranked" in buf.getvalue(), buf.getvalue()


def test_multi_cabin_fan_out_honours_an_encodable_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_pick_backend` keeps an encodable constraint on gflight for multi-cabin
    too, which is only correct if the fan-out actually applies it."""
    from datetime import date as _date

    from flight_cli import _gflight_ids as gfid
    from flight_cli import cli
    from flight_cli.domain import Cabin as _Cabin
    from flight_cli.domain import Leg as _Leg
    from flight_cli.domain import SearchOptions as _SearchOptions

    seen: list[Any] = []
    rungs: list[Any] = []

    def _capture(filters: Any, top_n: int, transport: Any) -> list[Any]:
        seen.append(filters)
        rungs.append(transport)
        return []

    monkeypatch.setattr(gfid, "search_with_ids", _capture)
    cli._run_gflight_multi(
        legs=(_Leg.of("JFK", "LAX", _date(2026, 10, 14), extension="MAXSTOPS 1"),),
        opts=_SearchOptions(cabin=_Cabin.COACH),
        cabins=(_Cabin.COACH,),
        top_n=5,
    )
    assert seen, "the fan-out never queried"
    assert seen[0].stops.name == "ONE_STOP_OR_FEWER"
    # The fan-out names rung 1 now instead of omitting the argument. Same value,
    # so nothing about the fan-out changed — and the profile lock means it must
    # stay rung 1: a thread per cabin cannot each hold the one Chrome profile.
    assert rungs == [gfid.HTTP_TRANSPORT]


def _fan_out_over(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> str:
    """Run the real fan-out with every cabin's query raising `failure`, and
    return what the user was told on stderr.

    A wide, colourless console so an assertion cannot fail on rich's wrapping,
    and markup left ON because surviving the markup pass is the point."""
    import io
    from datetime import date as _date

    from rich.console import Console

    from flight_cli import _gflight_ids as gfid
    from flight_cli import cli
    from flight_cli.domain import Cabin as _Cabin
    from flight_cli.domain import Leg as _Leg
    from flight_cli.domain import SearchOptions as _SearchOptions

    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "err", Console(file=buf, width=1000, force_terminal=False, no_color=True)
    )

    def _raise(*_a: Any, **_kw: Any) -> list[Any]:
        raise failure

    monkeypatch.setattr(gfid, "search_with_ids", _raise)
    out = cli._run_gflight_multi(
        legs=(_Leg.of("JFK", "LAX", _date(2026, 10, 14)),),
        opts=_SearchOptions(cabin=_Cabin.COACH),
        cabins=(_Cabin.COACH, _Cabin.BUSINESS),
        top_n=5,
    )
    assert out == {}, "a cabin that raised must not land a column"
    return buf.getvalue()


def test_a_cabin_that_refuses_is_named_and_carries_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed refusal is WHY a cabin's column is missing, and the fan-out has to
    say both halves. Without the cabin name the user cannot tell which column
    went; without the refusal's own note it reads as an unexplained failure and
    Google Flights looks like it simply had nothing in business class."""
    from flight_cli import cli
    from flight_cli._gf_errors import GfThrottledError

    text = _fan_out_over(monkeypatch, GfThrottledError("Google Flights rate-limited the request"))
    assert "COACH" in text
    assert "BUSINESS" in text
    # Compared against the production wording rather than a copy of it, so a
    # reworded refusal does not need this test edited to keep passing.
    assert text.count(cli._gf_refusal(GfThrottledError("x")).note) == 2
    # The note alone would still be found if the typed branch were deleted: the
    # generic handler prints `str(e)`, which contains the same words. What tells
    # the two apart is that only the generic one says "query failed".
    assert "query failed" not in text


def test_a_cabin_crash_carrying_markup_renders_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """fli has no documented exception surface, so this handler prints arbitrary
    text through a markup-mode console. A closing tag the text never opened
    raises `MarkupError` from inside the task group, which turns one cabin's
    failure into the whole fan-out's — including the cabins that succeeded."""
    text = _fan_out_over(monkeypatch, RuntimeError("fli said [/x] no"))
    assert text.count("[/x]") == 2
    assert "COACH" in text
    assert "BUSINESS" in text


def _dispatch(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[list[str], str]:
    """Run the real `flight search` with both multi-cabin backends stubbed, and
    report which one it chose. Driven through CliRunner because calling the
    typer command directly hands every option its OptionInfo sentinel."""
    from typer.testing import CliRunner

    from flight_cli import cli

    called: list[str] = []

    def _gf(**_kw: Any) -> None:
        called.append("gflight")

    def _mx(**_kw: Any) -> None:
        called.append("matrix")

    monkeypatch.setattr(cli, "_run_gflight_path_multi", _gf)
    monkeypatch.setattr(cli, "_run_matrix_path_multi", _mx)
    result = CliRunner().invoke(cli.app, ["search", *args])
    assert result.exit_code == 0, result.output
    return called, result.output


def test_multi_cabin_encodable_constraint_stays_on_gflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multi-cabin guard defers to `_pick_backend`. Re-testing
    `routing or extension` there drops an encodable constraint to Matrix
    silently."""
    called, _ = _dispatch(
        monkeypatch,
        "JFK",
        "LAX",
        "--dep",
        "2026-10-14",
        "--cabin",
        "coach,business",
        "--extension",
        "MAXSTOPS 1",
        "--cash-only",
    )
    assert called == ["gflight"]


def test_multi_cabin_unencodable_constraint_goes_to_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called, output = _dispatch(
        monkeypatch,
        "JFK",
        "LAX",
        "--dep",
        "2026-10-14",
        "--cabin",
        "coach,business",
        "--routing",
        "DL+",
        "--cash-only",
    )
    assert called == ["matrix"]
    assert "a carrier filter (DL)" in output


@pytest.mark.parametrize(
    ("legs_out", "top_n", "expected"),
    [
        pytest.param(True, 30, True, id="a-round-trip-above-the-cap-says-so"),
        pytest.param(True, 4, False, id="below-the-cap-there-is-nothing-to-say"),
        pytest.param(False, 30, False, id="a-one-way-pins-nothing"),
    ],
)
def test_a_round_trip_says_how_many_outbounds_it_will_actually_combine(
    monkeypatch: pytest.MonkeyPatch, legs_out: bool, top_n: int, expected: bool
) -> None:
    """`-n 30` on a round trip searches ten outbounds, not thirty, and said so
    on exactly one path — the multi-cabin one. Everywhere else a user reading a
    short table saw the market rather than the budget.

    The note is stderr, so it reaches a human on `--fast` and under
    `--format json` alike without touching the document on stdout."""
    import io as _io
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from rich.console import Console as _Console

    from flight_cli import cli
    from flight_cli._gflight_ids import pinned_fanout
    from flight_cli.domain import Leg as _Leg

    buf = _io.StringIO()
    monkeypatch.setattr(cli, "err", _Console(file=buf, width=400, no_color=True, highlight=False))
    dep = _date.today() + _timedelta(days=45)
    legs = (_Leg.of("JFK", "LHR", dep),)
    if legs_out:
        legs = (*legs, _Leg.of("LHR", "JFK", dep + _timedelta(days=7)))

    cli._pin_cap_note(legs=legs, top_n=top_n)

    printed = buf.getvalue()
    assert ("first-ranked outbounds" in printed) is expected, printed
    if expected:
        assert f"up to {pinned_fanout(top_n)} first-ranked" in printed, printed
        assert str(top_n) not in printed, "the note must not quote the number it is correcting"
        # Ranked, not cheapest: the pins are the board in page order, and the
        # repository's own capture has its cheapest outbound outside them.
        assert "cheapest" not in printed, printed


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["search", "JFK", "LHR"], id="the-default-enriched-path"),
        pytest.param(["search", "JFK", "LHR", "--fast"], id="fast-skips-matrix"),
        pytest.param(["search", "JFK", "LHR", "--format", "json"], id="json"),
        pytest.param(["search", "JFK", "LHR", "--cabin", "y,j"], id="multi-cabin"),
    ],
)
def test_every_round_trip_surface_says_how_many_outbounds_it_combines(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Every surface, because a unit test on the helper says nothing about which
    commands call it: the requirement is where the sentence appears, so the test
    drives the commands.

    Driven through the real commands, because "which surfaces say it" is the
    whole requirement. `--format json` is here for the second half of it — the
    note is stderr, so the document on stdout stays a document."""
    import json as _json
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from typer.testing import CliRunner

    from flight_cli import cli

    row = _one_gflight_row()

    def _rows(*_a: object, **_kw: object) -> list[Any]:
        return [row]

    def _by_cabin(**kw: Any) -> dict[Any, list[Any]]:
        return {c: [row] for c in kw["cabins"]}

    monkeypatch.setattr(cli, "_gflight_results", _rows)
    monkeypatch.setattr(cli, "_run_gflight_multi", _by_cabin)

    def _gflight_backend(**_kw: object) -> str:
        return cast("str", cli.BACKEND_GFLIGHT)

    monkeypatch.setattr(cli, "_pick_backend", _gflight_backend)

    dep = _date.today() + _timedelta(days=45)
    ret = dep + _timedelta(days=7)
    result = CliRunner().invoke(
        cli.app,
        # `--cash-only` so the JSON branch is reached: award output owns stdout
        # when it runs, and this is about where the NOTE goes.
        [
            *command,
            "--dep",
            dep.isoformat(),
            "--return",
            ret.isoformat(),
            "-n",
            "30",
            "--cash-only",
        ],
    )

    assert result.exit_code == 0, result.output
    # This exact sentence, not merely the words: the multi-cabin path also
    # prints the join note, which says something adjacent about the same cap and
    # would answer for a call site that is no longer there.
    assert "combines returns against up to" in result.stderr, result.stderr
    # stderr on every surface, which is what lets the JSON case have it at all:
    # a document on stdout stays a document.
    if "json" in command:
        _json.loads(result.stdout)  # the assertion is that this does not raise
        assert "first-ranked outbounds" not in result.stdout, result.stdout
