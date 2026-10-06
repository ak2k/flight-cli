# pyright: reportPrivateUsage=false
"""The guard against resolving an airline code through fli's enum flags every
load of the enum, so a form nobody listed cannot pass, and exempts only the
uses that name no code."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

import test_airline_alias_requests as guard
from test_airline_alias_requests import _enum_lookups

if TYPE_CHECKING:
    import pathlib


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


def test_the_guard_sees_the_enum_renamed_in_one_module_and_used_in_another(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard reads one module at a time, and each of these passes it alone."""
    (tmp_path / "carriers.py").write_text("from fli.models.airline import Airline as Carrier\n")
    (tmp_path / "use.py").write_text("from flight_cli.carriers import Carrier\nCarrier['W9']\n")
    monkeypatch.setattr(guard, "_SRC", tmp_path)
    with pytest.raises(AssertionError, match=r"\['carriers\.py:1'\]"):
        guard.test_no_code_resolves_an_airline_through_flis_enum()


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airline\n"
        "def f(c: Annotated[str, Option(callback=lambda v: Airline[v])]) -> None: ...",
        "from fli.models import Airline\nx: Annotated[str, AfterValidator(Airline)]",
    ],
)
def test_the_guard_sees_the_enum_in_annotation_metadata_that_runs(source: str) -> None:
    """Typer and pydantic call what `Annotated` carries, so that is code, not a type."""
    assert _enum_lookups(ast.parse(source)) == [2]


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("from fli.models import Airline\ndef f(c: Literal[Airline.W9]) -> None: ...", [2]),
        ("from fli.models import Airline\ndef f() -> Literal[Airline['W9']]: ...", [2]),
        ("from fli.models import Airline\nx: Annotated[str, Airline.W9]", [2]),
        (
            "from fli.models import Airline\n"
            "class M(BaseModel):\n    carrier: Literal[Airline.W9] = Airline.W6",
            [3, 3],
        ),
        ("from fli.models import Airline\ndef f(x: Airline) -> tuple[Airline, str]: ...", []),
        ("import fli.models\nx: fli.models.Airline | None", []),
    ],
)
def test_the_guard_sees_a_member_the_enum_names_inside_a_type(
    source: str, lines: list[int]
) -> None:
    """`Literal[Airline.W9]` names a code, and pydantic hands it the W6 member."""
    assert _enum_lookups(ast.parse(source)) == lines
