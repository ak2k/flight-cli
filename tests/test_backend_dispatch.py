# pyright: reportPrivateUsage=false
"""Tests for `flight search`'s backend selection logic.

`_pick_backend` is the routing decision: which backend handles a given mix
of user-facing CLI flags. The set of "Matrix-only" flags is the load-
bearing knowledge — get it wrong and either Matrix is invoked when it
needn't be (slow) or gflight is invoked for inexpressible queries (errors
deep in fli).

The second load-bearing fact is the search transport: Google's public page,
whose `tfs=` parameter carries a stop ceiling, a carrier or alliance include, a
maximum duration, layover minutes, a departure-hour window per leg and child
passengers. The page serves its full board, so the Tier-2 carrier predicates
the post-filter evaluates are served there too. Anything else goes to Matrix
WITH ITS REASON."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import typer
from pydantic import ValidationError

from flight_cli.cli import (
    BACKEND_AUTO,
    BACKEND_GFLIGHT,
    BACKEND_MATRIX,
    _pick_backend,
)
from flight_cli.domain import Bags
from flight_cli.routing_predicates import classify, page_can_encode


def _call(backend: str = BACKEND_AUTO, **overrides: object) -> str:
    """Defaults match a plain `flight search JFK LHR --dep 2026-08-15`."""
    defaults: dict[str, object] = {
        "routing": None,
        "extension": None,
        "slice_specs": None,
        "depart_times": None,
        "return_times": None,
        "stops": None,
        "children": 0,
        "seniors": 0,
        "youth": 0,
        "inf_seat": 0,
        "inf_lap": 0,
        "origin": "JFK",
        "destination": "LHR",
        "allow_airport_changes": True,
        "show_only_available": True,
    }
    defaults.update(overrides)
    return _pick_backend(backend=backend, **defaults)  # type: ignore[arg-type]


# ───────────────────────────────── auto ────────────────────────────────────


def test_auto_plain_search_picks_gflight() -> None:
    assert _call() == BACKEND_GFLIGHT


@pytest.mark.parametrize(
    "flag,value",
    [
        ("slice_specs", ["JFK-LHR:2026-08-15"]),  # multi-city
        ("depart_times", "morning,evening"),  # two windows; the page takes one
        ("return_times", "early,afternoon"),
        ("seniors", 1),
        ("youth", 1),
        ("inf_seat", 1),
        ("inf_lap", 1),
        # Neither reaches the search page at all: `fli_bridge`, which the `tfs=`
        # parameter is encoded from, has no field for either, so a query served
        # on Google is served with the constraint simply gone.
        ("allow_airport_changes", False),
        ("show_only_available", False),
    ],
)
def test_auto_hard_matrix_flag_picks_matrix(flag: str, value: object) -> None:
    """Flags the GF bridge can't map at all always force Matrix."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "overrides",
    [
        {"origin": "JFK,EWR"},
        {"destination": "LHR,LGW"},
        # Metro codes reach Google as their member airports. QSF and SAO are in
        # fli's table as other cities' airports, and are served through the
        # member table all the same.
        {"origin": "NYC", "destination": "LAX"},
        {"origin": "QSF"},
        {"destination": "SAO"},
        {"origin": "NYC", "destination": "LON"},
    ],
)
def test_auto_airport_sets_and_metro_codes_stay_on_gflight(
    overrides: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    """Google's page takes a set of airports per leg, so an airport set is
    answered there over every airport, not flattened and not sent to Matrix."""
    assert _call(**overrides) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]
    assert capsys.readouterr().err == ""


# Ten origins: with one destination, a leg of exactly the bound's 11 airports.
_TEN = ("JFK", "LGA", "EWR", "BOS", "IAD", "DCA", "BWI", "PHL", "ATL", "MIA")


def test_a_leg_of_exactly_eleven_airports_stays_on_gflight() -> None:
    assert _call(origin=",".join(_TEN), destination="LAX") == BACKEND_GFLIGHT


def test_a_leg_of_twelve_airports_goes_to_matrix_naming_the_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Google's page declined 15 airports in one leg outright; the bound keeps
    region lists Matrix answered on Matrix rather than failing on Google."""
    assert _call(origin=",".join(_TEN), destination="LAX,SFO") == BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert "12 airports on one leg (its limit is 11)" in printed, printed


def test_the_bound_counts_a_metro_code_as_its_members() -> None:
    # LON is six airports: 6 + 6 = 12.
    assert _call(origin="LON", destination="JFK,LGA,EWR,BOS,IAD,DCA") == BACKEND_MATRIX
    assert _call(origin="LON", destination="JFK,LGA,EWR,BOS,IAD") == BACKEND_GFLIGHT


def test_explicit_gflight_refuses_a_leg_over_the_bound() -> None:
    with pytest.raises(typer.BadParameter, match=r"12 airports on one leg \(its limit is 11\)"):
        _call(BACKEND_GFLIGHT, origin=",".join(_TEN), destination="LAX,SFO")


@pytest.mark.parametrize(
    "origin,destination,shared",
    [
        ("NYC", "JFK", "JFK"),
        ("JFK,EWR", "EWR,LHR", "EWR"),
    ],
)
def test_an_airport_at_both_ends_of_a_leg_goes_to_matrix(
    origin: str, destination: str, shared: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """fli checks only the first airport of each side: `NYC JFK` raised out of
    the bridge, and `JFK,EWR EWR,LHR` would be sent with EWR at both ends."""
    assert _call(origin=origin, destination=destination) == BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert f"an airport at both ends of a leg ({shared})" in printed, printed


def test_auto_stop_ceiling_stays_on_gflight() -> None:
    """The one constraint the search page's tfs= parameter carries natively."""
    assert _call(extension="MAXSTOPS 1") == BACKEND_GFLIGHT
    assert _call(extension="MAXSTOPS 2") == BACKEND_GFLIGHT
    assert _call(routing="N") == BACKEND_GFLIGHT


@pytest.mark.parametrize(
    "overrides",
    [
        {"stops": 3},  # the --stops flag
        {"extension": "MAXSTOPS 3"},  # the routing-language spelling
    ],
)
def test_a_stop_ceiling_above_two_goes_to_matrix_either_spelling(
    overrides: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both spellings hit the same ceiling. Without this the flag bypasses
    `page_can_encode` and encodes byte-identically to no --stops."""
    assert _call(**overrides) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]
    assert "a stop ceiling above 2 (3)" in capsys.readouterr().err


@pytest.mark.parametrize("stops", [0, 1, 2])
def test_an_encodable_stop_ceiling_stays_on_gflight(stops: int) -> None:
    assert _call(stops=stops) == BACKEND_GFLIGHT


@pytest.mark.parametrize(
    "overrides",
    [
        {"stops": 3, "extension": "MAXSTOPS 0"},
        {"stops": 0, "extension": "MAXSTOPS 3"},
        {"stops": 3, "routing": "N"},
    ],
)
def test_only_the_strictest_stop_limit_is_held_to_the_ceiling(
    overrides: dict[str, object],
) -> None:
    """The page is asked for the strictest of `--stops` and every `MAXSTOPS`,
    so a looser limit beside it leaves nothing the page cannot encode."""
    assert _call(**overrides) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]


def test_a_stop_ceiling_above_two_is_named_once_at_its_strictest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _call(stops=4, extension="MAXSTOPS 3") == BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert "can't serve a stop ceiling above 2 (3)." in printed, printed


def test_an_alliance_naming_none_takes_a_bare_alliances_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ALLIANCE |` names no alliance, so the page would be asked for no filter."""
    assert _call(extension="ALLIANCE |") == BACKEND_MATRIX
    printed = " ".join(capsys.readouterr().err.split())
    assert "extension 'ALLIANCE |' not expressible on GF" in printed, printed
    with pytest.raises(typer.BadParameter, match=r"extension 'ALLIANCE \|' not expressible"):
        _call(BACKEND_GFLIGHT, extension="ALLIANCE |")


def test_explicit_gflight_rejects_a_stop_ceiling_above_two() -> None:
    with pytest.raises(typer.BadParameter, match=r"a stop ceiling above 2 \(3\)"):
        _call(BACKEND_GFLIGHT, stops=3)


def test_stop_ceiling_above_two_goes_to_matrix() -> None:
    """fli's MaxStops tops out at "two or fewer", so a higher ceiling maps to
    ANY and the tfs field is omitted — certifying it encodable would drop the
    constraint with neither a native filter nor a reason."""
    assert _call(extension="MAXSTOPS 3") == BACKEND_MATRIX
    encodable, reasons = page_can_encode(classify(None, "MAXSTOPS 3").predicates)
    assert not encodable
    assert reasons == ["a stop ceiling above 2 (3)"]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("routing", "F* X:FRA F*"),  # via airport
        ("routing", "X:FRA"),
        ("extension", "F bc=y"),  # fare basis (Tier 3)
        ("extension", "MAXMILES 8000"),  # mileage (Tier 3)
        ("routing", "BA AA"),  # ordered carrier chain
        ("routing", "~BA"),  # direct, not BA (Tier 3)
        ("extension", "-REDEYES"),
        ("extension", "MAXCONNECT 0:00"),  # fli's layover maximum is positive
        ("extension", "MAXDUR 0:00"),  # and so is its duration maximum
        ("routing", "XX+"),  # no fli member, so no row would come back
        # 3.6 is one include list: Google would answer either.
        ("extension", "ALLIANCE oneworld; ALLIANCE skyteam"),
        ("extension", "AIRLINES AA; ALLIANCE star-alliance"),
        # Post-filterable, but Matrix reads both positionally and the filter
        # does not: bare AS21 is one flight, `F* ~DUB F*` one connection.
        ("routing", "AS21"),
        ("routing", "AS21+"),
        ("routing", "F* ~DUB F*"),
        ("extension", "-CITIES DUB"),
    ],
)
def test_auto_unencodable_constraint_picks_matrix(flag: str, value: object) -> None:
    """Anything the page's tfs= parameter cannot carry and the post-filter does
    not serve goes to Matrix."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("routing", "O:LH+"),  # operating carrier
        ("extension", "-CODESHARE"),
        ("routing", "~LH+"),  # no LH-booked leg
        ("extension", "-AIRLINES LH"),
        ("extension", "OPAIRLINES LH"),
        ("routing", "~BA+"),
    ],
)
def test_auto_serves_post_filterable_tier2_on_google(flag: str, value: object) -> None:
    """The page serves its full board, so a Tier-2 carrier predicate the post-
    filter evaluates is served by Google, on either backend spelling."""
    assert _call(**{flag: value}) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]
    assert _call(BACKEND_GFLIGHT, **{flag: value}) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]


def test_a_post_filterable_predicate_beside_one_that_is_not_still_picks_matrix() -> None:
    assert _call(routing="~BA+", extension="-REDEYES") == BACKEND_MATRIX


def test_auto_mixed_encodable_and_not_still_picks_matrix() -> None:
    """A partially-encodable set is not partially honored."""
    assert _call(extension="ALLIANCE star-alliance; MAXSTOPS 1; -REDEYES") == BACKEND_MATRIX


@pytest.mark.parametrize(
    "overrides",
    [
        {"routing": "AA+"},
        {"routing": "N:AA"},
        {"extension": "AIRLINES AA DL"},
        {"routing": "AA+", "extension": "AIRLINES DL"},  # both checked on the rows
        {"extension": "ALLIANCE oneworld"},
        {"extension": "ALLIANCE oneworld|skyteam"},  # one directive is one union
        {"extension": "MAXDUR 6:20"},
        {"extension": "MINCONNECT 2:00"},
        {"extension": "MINCONNECT 0:00"},
        {"extension": "MAXCONNECT 2:00"},
        {"extension": "MINCONNECT 1:00; MAXCONNECT 3:00; MAXSTOPS 1"},
        {"routing": "~BA+", "extension": "MINCONNECT 1:00"},
        {"depart_times": "morning"},
        {"depart_times": "early,morning,midday"},
        {"depart_times": "night"},
        {"return_times": "evening,night"},
        {"depart_times": "morning", "return_times": "evening"},
        {"children": 1},
        {"children": 2, "adults": 2},
    ],
)
def test_auto_serves_what_the_page_encodes_on_google(
    overrides: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    """Encoded in the page's tfs= and, wherever the rows show it, checked on
    them too, on either backend spelling."""
    assert _call(**overrides) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]
    assert capsys.readouterr().err == ""
    assert _call(BACKEND_GFLIGHT, **overrides) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]


# ─────────────────────────── the printed reason ────────────────────────────


def test_page_can_encode_accepts_a_stop_ceiling() -> None:
    encodable, reasons = page_can_encode(classify(None, "MAXSTOPS 1").predicates)
    assert encodable
    assert reasons == []


def test_page_can_encode_names_the_carrier_constraint() -> None:
    encodable, reasons = page_can_encode(classify("DL+", None).predicates)
    assert not encodable
    assert reasons == ["a carrier filter (DL)"]


def test_page_can_encode_names_every_constraint_it_refuses() -> None:
    _, reasons = page_can_encode(classify("O:LH+", "ALLIANCE oneworld; MAXDUR 10:00").predicates)
    assert reasons == [
        "an operating carrier filter (LH)",
        "an alliance filter (oneworld)",
        "a maximum trip duration (600 min)",
    ]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"routing": "BA AA"}, "routing 'BA AA' not GF-expressible"),
        ({"inf_lap": 1}, "an infant passenger"),
        ({"inf_seat": 1}, "an infant passenger"),
        ({"seniors": 1}, "a senior or youth passenger"),
        ({"adults": 0, "children": 1}, "a child passenger with no adult"),
        ({"adults": 9, "children": 1}, "more than 9 passengers"),
        ({"extension": "MAXCONNECT 0:00"}, "a maximum layover of 0 min"),
        ({"extension": "MAXDUR 0:00"}, "a maximum trip duration (0 min)"),
        ({"routing": "XX+"}, "a carrier Google Flights has no code for (XX)"),
        (
            {"routing": "AA+", "extension": "ALLIANCE oneworld"},
            "an alliance filter combined with another carrier or alliance filter",
        ),
        # A code that is neither an fli airport nor a metro code in the member
        # table: reaching the bridge with one is an AttributeError before any
        # request, and on `--format json` that is exit 1 and an empty document.
        ({"origin": "YTO"}, "a city code rather than an airport (YTO)"),
        ({"origin": "JFK,ZZZ"}, "a city code rather than an airport (ZZZ)"),
        ({"slice_specs": ["JFK-LHR:2026-08-15"]}, "a multi-city itinerary"),
        ({"depart_times": "morning,evening"}, "departure times that are not one window"),
        (
            {"return_times": "early,night,early"},
            "return times that are not one window (early_morning, night)",
        ),
        ({"allow_airport_changes": False}, "a ban on changing airports"),
        ({"show_only_available": False}, "unavailable itineraries included"),
    ],
)
def test_auto_names_whatever_forced_matrix(
    overrides: dict[str, object], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently taking the 45x slower backend leaves the user unable to tell a
    constraint they could drop from one they can't."""
    assert _call(**overrides) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]
    # Whitespace-collapsed: the reason is printed through a console that wraps
    # at its own width, and where a reason long enough to wrap gets broken is
    # not what this asserts. What it asserts is the sentence.
    printed = " ".join(capsys.readouterr().err.split())
    assert expected in printed, printed


def test_auto_leaves_a_code_that_is_its_own_airport_on_gflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`LAX` is a metro code in the same table and also the airport code, and
    fli resolves it to Los Angeles International. Gating on the table rather
    than on the lookup would move a working query to the 45x slower backend for
    a problem it does not have."""
    assert _call(origin="LAX") == BACKEND_GFLIGHT
    assert capsys.readouterr().err == ""


def test_auto_says_nothing_when_gflight_serves_the_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _call(extension="MAXSTOPS 1") == BACKEND_GFLIGHT
    assert capsys.readouterr().err == ""


# ──────────────────────────────── explicit ─────────────────────────────────


def test_explicit_matrix_always_wins() -> None:
    assert _call(BACKEND_MATRIX) == BACKEND_MATRIX
    assert _call(BACKEND_MATRIX, routing="LH+") == BACKEND_MATRIX
    assert _call(BACKEND_MATRIX, extension="F bc=y") == BACKEND_MATRIX


def test_explicit_gflight_with_plain_search() -> None:
    assert _call(BACKEND_GFLIGHT) == BACKEND_GFLIGHT


def test_explicit_gflight_allows_an_encodable_stop_ceiling() -> None:
    assert _call(BACKEND_GFLIGHT, extension="MAXSTOPS 1") == BACKEND_GFLIGHT


def test_explicit_gflight_rejects_unserveable_request() -> None:
    with pytest.raises(typer.BadParameter, match="can't serve"):
        _call(BACKEND_GFLIGHT, extension="F bc=y")
    with pytest.raises(typer.BadParameter, match="can't serve"):
        _call(BACKEND_GFLIGHT, slice_specs=["JFK-LHR:2026-08-15"])


def test_explicit_gflight_error_names_the_constraint() -> None:
    with pytest.raises(typer.BadParameter, match="a red-eye exclusion"):
        _call(BACKEND_GFLIGHT, extension="-REDEYES")


def test_explicit_gflight_error_names_the_pax_type() -> None:
    with pytest.raises(typer.BadParameter, match="an infant passenger"):
        _call(BACKEND_GFLIGHT, inf_seat=1)


def test_explicit_gflight_error_lists_every_reason() -> None:
    with pytest.raises(typer.BadParameter, match="an infant passenger and a red-eye exclusion"):
        _call(BACKEND_GFLIGHT, inf_seat=1, extension="-REDEYES")


def test_an_unknown_time_of_day_is_refused_before_any_backend_is_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(typer.Exit):
        _call(depart_times="brunch")
    printed = capsys.readouterr().err
    assert "bad time-of-day" in printed
    assert "Using Matrix" not in printed


def test_explicit_matrix_does_not_look_carriers_up_in_fli() -> None:
    """The lookup costs fli's import, which a Matrix run should not pay."""
    from flight_cli.cli import _gf_unmappable_reasons

    preds = classify("XX+", None).predicates
    assert _gf_unmappable_reasons(BACKEND_MATRIX, preds) == []
    assert _gf_unmappable_reasons(BACKEND_AUTO, preds) == [
        "a carrier Google Flights has no code for (XX)"
    ]


@pytest.mark.parametrize("overrides", [{"origin": "JFK,EWR"}, {"origin": "NYC"}])
def test_explicit_gflight_serves_an_airport_set_and_a_metro_code(
    overrides: dict[str, object],
) -> None:
    assert _call(BACKEND_GFLIGHT, **overrides) == BACKEND_GFLIGHT  # pyright: ignore[reportArgumentType]


def test_explicit_gflight_refuses_an_unknown_city_code_rather_than_crashing() -> None:
    """Asked for outright, a code that is neither an airport nor in the member
    table is a parameter error: exit 2 with the reason, in place of the
    `AttributeError` the bridge raises when it looks the code up. The failure it
    replaces is silent on stdout — `--format json` and `--fast` both skip the
    enrich path, so the crash was the whole outcome and the document was zero
    bytes."""
    with pytest.raises(typer.BadParameter, match=r"a city code rather than an airport \(YTO\)"):
        _call(BACKEND_GFLIGHT, origin="YTO")


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"allow_airport_changes": False}, "a ban on changing airports"),
        ({"show_only_available": False}, "unavailable itineraries included"),
    ],
)
def test_explicit_gflight_refuses_a_matrix_only_filter(
    overrides: dict[str, object], expected: str
) -> None:
    """Accepting it silently is the worse half of the same defect: the query is
    served without the constraint, and the Matrix deep link printed underneath
    still carries it — so the two surfaces describe different searches."""
    with pytest.raises(typer.BadParameter, match=expected):
        _call(BACKEND_GFLIGHT, **overrides)  # pyright: ignore[reportArgumentType]


# ──────────────── --bags: Google Flights or a refusal, never Matrix ──────────

_ONE_BAG = Bags(checked=1)


def test_auto_serves_bags_on_gflight() -> None:
    assert _call(bags=_ONE_BAG) == BACKEND_GFLIGHT


def test_explicit_matrix_refuses_bags() -> None:
    with pytest.raises(typer.BadParameter, match="Matrix prices no bags"):
        _call(BACKEND_MATRIX, bags=_ONE_BAG)


@pytest.mark.parametrize(
    "flag,value,reason",
    [
        ("slice_specs", ["JFK-LHR:2026-08-15"], "a multi-city itinerary"),
        ("inf_lap", 1, "an infant passenger"),
        ("fare_rules", True, "fare rules"),
    ],
)
def test_auto_refuses_bags_with_whatever_would_have_sent_it_to_matrix(
    flag: str, value: object, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(bags=_ONE_BAG, **{flag: value})  # pyright: ignore[reportArgumentType]
    message = str(excinfo.value)
    assert f"--bags needs Google Flights, which can't serve {reason}" in message
    assert "drop --bags" in message
    assert "use --backend matrix" not in message
    assert "Using Matrix" not in capsys.readouterr().err


def test_explicit_gflight_under_bags_points_at_the_bags_not_at_matrix() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _call(BACKEND_GFLIGHT, inf_lap=1, bags=_ONE_BAG)
    assert "drop --bags" in str(excinfo.value)
    assert "use --backend matrix" not in str(excinfo.value)


def test_unknown_backend_rejected() -> None:
    with pytest.raises(typer.BadParameter, match="--backend must be one of"):
        _call("nope")


# ───────────── deprecated `flight gflight` alias (work-h70kv.5) ─────────────


def _future_dep() -> str:
    """A departure date the model will accept whatever day the suite runs."""
    return (date.today() + timedelta(days=45)).isoformat()


def _gflight_alias(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[list[str], str]:
    """Run the deprecated alias with both backends stubbed, reporting its pick."""
    from typer.testing import CliRunner

    from flight_cli import cli

    called: list[str] = []

    def _gf(**_kw: object) -> None:
        called.append("gflight")

    def _mx(**_kw: object) -> None:
        called.append("matrix")

    monkeypatch.setattr(cli, "_run_gflight_path", _gf)
    monkeypatch.setattr(cli, "_run_matrix_path", _mx)
    result = CliRunner().invoke(cli.app, ["gflight", *args])
    assert result.exit_code == 0, result.output
    return called, result.output


def test_gflight_alias_still_uses_google_flights_for_a_plain_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called, _ = _gflight_alias(monkeypatch, "JFK", "LAX", "--dep", "2026-10-14")
    assert called == ["gflight"]


def test_gflight_alias_uses_google_flights_for_a_child_beside_an_adult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called, output = _gflight_alias(
        monkeypatch, "JFK", "LAX", "--dep", "2026-10-14", "--children", "1"
    )
    assert called == ["gflight"]
    assert "Using Matrix" not in output


def test_gflight_alias_takes_matrix_for_a_child_with_no_adult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias has no --backend flag, so it resolves like `search` on auto
    rather than erroring on a query it accepts."""
    called, output = _gflight_alias(
        monkeypatch, "JFK", "LAX", "--dep", "2026-10-14", "--adults", "0", "--children", "1"
    )
    assert called == ["matrix"]
    assert "a child passenger with no adult" in output


def test_gflight_alias_splits_a_multi_airport_argument_like_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma-separated argument is a list of airports here exactly as it is
    in `flight search`, and Google Flights serves the set. Parsing it as one
    opaque airport code turns a query the CLI answers into a model validation
    panel."""
    called, output = _gflight_alias(monkeypatch, "JFK,LAX", "MIA", "--dep", _future_dep())
    assert called == ["gflight"]
    assert "Using Matrix" not in output
    assert "validation error" not in output.lower()


def test_gflight_alias_validates_airports_before_it_names_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend line is a claim that the query is on its way. An airport the
    model rejects must surface before that line, not after it."""
    from typer.testing import CliRunner

    from flight_cli import cli

    def _unreached(**_kw: object) -> None:
        raise AssertionError("a backend ran on a query that never validated")

    monkeypatch.setattr(cli, "_run_gflight_path", _unreached)
    monkeypatch.setattr(cli, "_run_matrix_path", _unreached)
    # Multi-airport (so the picker WOULD announce Matrix) with one code the
    # model rejects — the ordering is only observable when both are true.
    result = CliRunner().invoke(cli.app, ["gflight", "JFK,XXXX", "MIA", "--dep", _future_dep()])

    assert result.exit_code != 0
    assert "Using Matrix" not in result.output
    assert isinstance(result.exception, ValidationError)
    assert "Not a 3-letter IATA code: 'XXXX'" in str(result.exception)


def _no_backend_runs(monkeypatch: pytest.MonkeyPatch, command: str, origin: str) -> str:
    """Invoke `command` with a blank origin and every backend booby-trapped."""
    from typer.testing import CliRunner

    from flight_cli import cli

    def _unreached(**_kw: object) -> None:
        raise AssertionError("a backend ran on a query with no airports")

    for path in (
        "_run_gflight_path",
        "_run_matrix_path",
        "_run_gflight_path_multi",
        "_run_matrix_path_multi",
        "_run_enriched_path",
    ):
        monkeypatch.setattr(cli, path, _unreached)
    result = CliRunner().invoke(cli.app, [command, origin, "MIA", "--dep", _future_dep()])
    assert result.exit_code == 2, result.output
    return result.output


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(",", id="comma-only"),
        pytest.param(" , ", id="blanks"),
    ],
)
@pytest.mark.parametrize("command", ["gflight", "search", "fare"])
def test_a_command_that_builds_a_leg_rejects_a_blank_airport_list(
    monkeypatch: pytest.MonkeyPatch, command: str, origin: str
) -> None:
    """`_parse_iata_list` drops blank entries, so these arrive as an empty tuple
    while the argument itself stays truthy and satisfies a plain `if origin`.
    Every command that builds a leg has to reject that the same way, or a leg
    with no airports reaches a backend and fails deep inside it with an index
    error instead."""
    assert "origin and destination are required" in _no_backend_runs(monkeypatch, command, origin)


@pytest.mark.parametrize("command", ["gflight", "search", "fare"])
def test_an_absent_origin_is_refused_by_whichever_arm_owns_it(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """An empty string is falsy, so on `search` and `fare` it never reaches the
    airport check — it is a query with no origin at all, and the arm that asks
    for one answers first. `gflight` takes origin positionally and has no such
    arm. Both exits are 2 and both name what is missing; pinned so the
    difference stays a choice."""
    output = _no_backend_runs(monkeypatch, command, "")
    expected = (
        "origin and destination are required"
        if command == "gflight"
        else "origin destination --dep"
    )
    assert expected in output


@pytest.mark.parametrize("n", ["0", "-5"], ids=["zero", "negative"])
@pytest.mark.parametrize("command", ["gflight", "search", "fare"])
def test_a_result_count_below_one_is_refused_before_any_backend_runs(
    monkeypatch: pytest.MonkeyPatch, command: str, n: str
) -> None:
    """`--n` feeds a pin count and a page size, and neither has a meaning below
    one. A negative reached the pin loop as a slice bound and asked for the
    whole board rather than nothing, so the guard is a floor on the option
    itself — which is a keyword argument no test reads unless one asks.

    All three commands, because the floor is written three times and removing
    it from any one of them is invisible from the other two."""
    from typer.testing import CliRunner

    from flight_cli import cli

    def _unreached(**_kw: object) -> None:
        raise AssertionError("a backend ran on a query that asked for no results")

    for path in (
        "_run_gflight_path",
        "_run_matrix_path",
        "_run_gflight_path_multi",
        "_run_matrix_path_multi",
        "_run_enriched_path",
    ):
        monkeypatch.setattr(cli, path, _unreached)
    result = CliRunner().invoke(cli.app, [command, "JFK", "MIA", "--dep", _future_dep(), "-n", n])
    assert result.exit_code == 2, result.output
