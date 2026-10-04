# pyright: reportPrivateUsage=false
"""Spellings of one expression that read the same both ways: a bare code under
its documented default prefix, and brackets around each token."""

from __future__ import annotations

import pytest
import typer

from flight_cli import cli
from flight_cli.routing_predicates import direction_dependence


def test_default_prefix_and_split_bracket_spellings_read_the_same_both_ways() -> None:
    assert [direction_dependence(r) for r in ("[AA] [AA]", "AA C:AA", "DFW X:DFW")] == [
        None,
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("routing", "dependent"),
    [
        ("O:AA,O:UA O:UA,O:AA", False),
        ("C:AA,UA UA,AA", False),
        ("aa c:AA", False),
        ("DFW,DEN X:DEN,DFW", False),
        ("F* [DFW] F*", False),
        ("DFW,AA X:AA,DFW", True),
        ("AA O:AA", True),
        ("X:AA AA", True),
        ("DFW C:DFW", True),
        ("[AA] [BB]", True),
        ("[BA AA]", True),
        ("UA LH", True),
    ],
)
def test_only_the_documented_default_prefix_folds_onto_a_bare_code(
    routing: str, dependent: bool
) -> None:
    assert (direction_dependence(routing) is not None) is dependent


def test_a_bracketed_flight_number_is_still_named() -> None:
    assert direction_dependence("[AA25] [AA25]") == "names flight 'AA25', which flies one way"


def test_a_default_prefix_spelling_is_copied_onto_the_return() -> None:
    assert cli._return_codes(
        routing="AA C:AA", extension=None, routing_return=None, extension_return=None
    ) == ("AA C:AA", None)
    with pytest.raises(typer.Exit) as exc:
        cli._return_codes(
            routing="AA O:AA", extension=None, routing_return=None, extension_return=None
        )
    assert exc.value.exit_code == 2
