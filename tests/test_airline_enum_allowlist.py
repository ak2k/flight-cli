# pyright: reportPrivateUsage=false
"""The guard against resolving an airline code through fli's enum flags every
load of the enum, so a form nobody listed cannot pass, and exempts only the
uses that name no code."""

from __future__ import annotations

import ast

import pytest

from test_airline_alias_requests import _enum_lookups


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airline\nAirline('Wizz Air')",
        "from fli.models import Airline\nE = Airline\nE['W9']",
        "from fli.models import Airline\ndict(Airline.__members__.items())['W9']",
        "from fli.models import Airline\n{k: v for k, v in Airline.__members__.items()}['W9']",
    ],
)
def test_the_guard_sees_a_copied_table_or_a_rebinding(source: str) -> None:
    """Each of these hands W9, or Wizz Air UK's name, the W6 member."""
    assert _enum_lookups(ast.parse(source)) == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airline\ndef f(x: Airline) -> tuple[Airline, str]: ...",
        "from fli.models import Airline\nclass C:\n    op: tuple[Airline, str] | None = None",
        "from fli.models import Airline\ntype ItineraryKey = tuple[tuple[Airline, str], ...]",
        "from fli.models import Airline\n"
        "def fli_airlines():\n    return {k: v for k, v in Airline.__members__.items()}",
    ],
)
def test_the_guard_passes_the_uses_that_cannot_alias(source: str) -> None:
    """An annotation names a type, and `fli_airlines` builds the table every lookup reads."""
    assert _enum_lookups(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airline\ndict(Airline.__members__.items())",
        "from fli.models import Airline\ntype Other = tuple[Airline, str]",
        "from fli.models import Airline\nisinstance(x, Airline)",
        "from fli.models import Airline\ndef fli_airlines(a=Airline):\n    pass",
        "import fli.models.airline as m\nm.Airline('Wizz Air')",
    ],
)
def test_the_guard_still_sees_what_the_exemptions_do_not_cover(source: str) -> None:
    """Only the annotations, `ItineraryKey` and the body of `fli_airlines` are exempt."""
    assert _enum_lookups(ast.parse(source)) == [2]
