# pyright: reportPrivateUsage=false
"""Tests for `flight search`'s backend selection logic.

`_pick_backend` is the routing decision: which backend handles a given mix
of user-facing CLI flags. The set of "Matrix-only" flags is the load-
bearing knowledge — get it wrong and either Matrix is invoked when it
needn't be (slow) or gflight is invoked for inexpressible queries (errors
deep in fli).

The second load-bearing fact is the search transport: Google's public page,
whose `tfs=` parameter carries only a stop ceiling today. Anything else goes to
Matrix WITH ITS REASON, because the alternative — post-filtering Google's fixed
~30-row board — answers a constrained search with a plausible-looking "no
results"."""

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
        ("depart_times", "morning"),
        ("return_times", "evening"),
        ("children", 1),
        ("seniors", 1),
        ("youth", 1),
        ("inf_seat", 1),
        ("inf_lap", 1),
        ("origin", "JFK,EWR"),  # airport set — the GF bridge keeps only the first
        ("destination", "LHR,LGW"),
    ],
)
def test_auto_hard_matrix_flag_picks_matrix(flag: str, value: object) -> None:
    """Flags the GF bridge can't map at all always force Matrix."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


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
        ("routing", "LH+"),  # marketing carrier
        ("routing", "F* X:FRA F*"),  # via airport
        ("routing", "O:LH+"),  # operating carrier
        ("extension", "MAXCONNECT 2:00"),  # layover max
        ("extension", "ALLIANCE star-alliance"),
        ("extension", "-CODESHARE"),
        ("extension", "MAXDUR 10:00"),
        ("extension", "F bc=y"),  # fare basis (Tier 3)
        ("extension", "MAXMILES 8000"),  # mileage (Tier 3)
        ("routing", "BA AA"),  # ordered carrier chain
        ("extension", "MINCONNECT 1:00"),
        ("extension", "-REDEYES"),
    ],
)
def test_auto_unencodable_constraint_picks_matrix(flag: str, value: object) -> None:
    """Anything the page's tfs= parameter can't carry goes to Matrix — including
    constraints the old RPC filtered server-side."""
    assert _call(**{flag: value}) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]


def test_auto_mixed_encodable_and_not_still_picks_matrix() -> None:
    """A partially-encodable set is not partially honoured."""
    assert _call(extension="ALLIANCE star-alliance; MAXSTOPS 1") == BACKEND_MATRIX


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
        ({"routing": "DL+"}, "a carrier filter (DL)"),
        ({"children": 1}, "a passenger type beyond adults"),
        ({"origin": "JFK,EWR"}, "a multi-airport origin/destination"),
        ({"slice_specs": ["JFK-LHR:2026-08-15"]}, "a multi-city itinerary"),
        ({"depart_times": "morning"}, "a departure/arrival time window"),
    ],
)
def test_auto_names_whatever_forced_matrix(
    overrides: dict[str, object], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently taking the 45x slower backend leaves the user unable to tell a
    constraint they could drop from one they can't."""
    assert _call(**overrides) == BACKEND_MATRIX  # pyright: ignore[reportArgumentType]
    assert expected in capsys.readouterr().err


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
    with pytest.raises(typer.BadParameter, match=r"a carrier filter \(DL\)"):
        _call(BACKEND_GFLIGHT, routing="DL+")


def test_explicit_gflight_error_names_the_pax_type() -> None:
    with pytest.raises(typer.BadParameter, match="passenger type beyond adults"):
        _call(BACKEND_GFLIGHT, children=1)


def test_explicit_gflight_error_lists_every_reason() -> None:
    with pytest.raises(typer.BadParameter, match=r"beyond adults and a carrier filter \(DL\)"):
        _call(BACKEND_GFLIGHT, children=1, routing="DL+")


def test_explicit_gflight_error_names_the_airport_set() -> None:
    with pytest.raises(typer.BadParameter, match="multi-airport"):
        _call(BACKEND_GFLIGHT, origin="JFK,EWR")


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


def test_gflight_alias_takes_matrix_for_a_child_passenger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page transport can't price a child — the tfs writer emits one adult
    varint per occupant. The alias has no --backend flag, so it resolves like
    `search` on auto rather than erroring on a query it accepts."""
    called, output = _gflight_alias(
        monkeypatch, "JFK", "LAX", "--dep", "2026-10-14", "--children", "1"
    )
    assert called == ["matrix"]
    assert "a passenger type beyond adults" in output


def test_gflight_alias_splits_a_multi_airport_argument_like_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma-separated argument is a list of airports here exactly as it is
    in `flight search`, and a multi-airport query belongs to Matrix. Parsing it
    as one opaque airport code turns a query the CLI answers into a model
    validation panel."""
    called, output = _gflight_alias(monkeypatch, "JFK,LAX", "MIA", "--dep", _future_dep())
    assert called == ["matrix"]
    assert "a multi-airport origin/destination" in output
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


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("", id="empty"),
        pytest.param(",", id="comma-only"),
        pytest.param(" , ", id="blanks"),
    ],
)
def test_gflight_alias_rejects_an_empty_airport_list(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    """`_parse_iata_list` drops empty entries, so these all arrive as an empty
    tuple. A leg with no airports at all is not a query anyone can answer."""
    from typer.testing import CliRunner

    from flight_cli import cli

    def _unreached(**_kw: object) -> None:
        raise AssertionError("a backend ran on a query with no airports")

    monkeypatch.setattr(cli, "_run_gflight_path", _unreached)
    monkeypatch.setattr(cli, "_run_matrix_path", _unreached)
    result = CliRunner().invoke(cli.app, ["gflight", origin, "MIA", "--dep", _future_dep()])

    assert result.exit_code == 2
    assert "origin and destination are required" in result.output
