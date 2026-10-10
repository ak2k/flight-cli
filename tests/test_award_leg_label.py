# pyright: reportPrivateUsage=false
"""An award leg's label names each typed airport once, in typed order.

`search JFK,JFK LAX` is the one airport JFK. Its label is `one-way JFK→LAX`,
what `search JFK LAX` prints; a typed repeat does not reach the leg's label,
the `Awards for` line or the JSON `leg` key. A metro code stays as typed."""

from __future__ import annotations

from datetime import date

import pytest

from flight_cli import cli
from flight_cli.domain import Leg

_DEP = date(2026, 11, 4)
_RET = date(2026, 11, 11)


@pytest.mark.parametrize(
    ("origins", "destinations", "label"),
    [
        (("JFK",), ("LAX",), "one-way JFK→LAX 2026-11-04"),
        (("JFK", "JFK"), ("LAX",), "one-way JFK→LAX 2026-11-04"),
        (("JFK", "EWR", "JFK"), ("LAX",), "one-way JFK,EWR→LAX 2026-11-04"),
        (("JFK",), ("LAX", "SFO", "LAX"), "one-way JFK→LAX,SFO 2026-11-04"),
        (("NYC", "JFK"), ("LAX",), "one-way NYC,JFK→LAX 2026-11-04"),
    ],
    ids=[
        "single",
        "repeated-origin",
        "repeat-among-two",
        "repeated-destination",
        "metro-and-member",
    ],
)
def test_a_repeated_typed_airport_is_named_once(
    origins: tuple[str, ...], destinations: tuple[str, ...], label: str
) -> None:
    """Red at the base, whose label comma-joined every typed token: `JFK,JFK→LAX`."""
    queries = cli._build_pp_legs((Leg.of(origins, destinations, _DEP),))

    assert {q.label for q in queries} == {label}, queries


def test_a_round_trip_names_each_leg_once() -> None:
    legs = (Leg.of(("JFK", "JFK"), ("LHR",), _DEP), Leg.of(("LHR",), ("JFK", "JFK"), _RET))

    queries = cli._build_pp_legs(legs)

    assert [(q.slice_index, q.label) for q in queries] == [
        (0, "outbound JFK→LHR 2026-11-04"),
        (1, "return LHR→JFK 2026-11-11"),
    ]
