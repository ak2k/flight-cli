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

import pytest
import typer

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
    assert _call(routing="N") == BACKEND_GFLIGHT


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
