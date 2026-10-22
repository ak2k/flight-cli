# pyright: reportPrivateUsage=false
"""The guard against resolving an airport code through fli's enum sees each
form such a lookup takes, and passes the ones that cannot hand a code another
airport's member."""

from __future__ import annotations

import ast

import pytest

from test_airport_alias_requests import _SRC, _enum_lookups


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models.airport import Airport as Port\nPort['OKA']",
        "import fli.models\nfli.models.Airport['OKA']",
        "from fli.models import Airport\nAirport.OKA",
        "from fli.models import Airport\nAirport.__members__['OKA']",
        "from fli.models import Airport\nAirport.__members__.get('OKA')",
        "from fli.models import Airport as FA\ngetattr(FA, 'OKA')",
        "from fli.search._decoders import _parse_airport\n_parse_airport('OKA')",
        "from fli.search._decoders import _parse_airport as parse\nparse('OKA')",
        "import fli.search._decoders as decoders\ndecoders._parse_airport('OKA')",
    ],
)
def test_the_airport_guard_sees_the_enum_under_any_name(source: str) -> None:
    """Each of these hands OKA the NAH member."""
    assert _enum_lookups(ast.parse(source)) == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airport\nisinstance(x, Airport)",
        "from fli.models import Airport\n"
        "def fli_airports():\n    return {k: v for k, v in Airport.__members__.items()}",
        "from fli.models import Airport\ndef f(x: Airport) -> tuple[Airport, str]: ...",
        "from fli.search._decoders import _parse_airport\n"
        "def _leg_airport(code):\n    return _parse_airport(code)",
    ],
)
def test_the_airport_guard_passes_the_lookups_that_cannot_alias(source: str) -> None:
    """A type check and an annotation name no code, `fli_airports` is where
    `fli_bridge` builds its own table, and `_leg_airport` decodes only a code fli
    has no entry for."""
    assert _enum_lookups(ast.parse(source)) == []


_BYPASSES = [
    "from fli.models import Airport\nAirport('Naha Airport')",
    "from fli.models import Airport\nE = Airport\nE['OKA']",
    "from fli.models import Airport\ndict(Airport.__members__.items())['OKA']",
    "from fli.models import Airport\n{k: v for k, v in Airport.__members__.items()}['OKA']",
]


def test_the_airport_guard_sees_a_copied_table_or_a_rebinding() -> None:
    """Calling the enum, rebinding it and copying its table each hand OKA the NAH member."""
    for source in _BYPASSES:
        assert _enum_lookups(ast.parse(source)) == [2], source


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models import Airport\nAirport.__members__.items()",
        "from fli.models import Airport\nx = [Airport]",
        "from fli.models import Airport\ndef g(a=Airport):\n    pass",
        "from fli.models import Airport\nx: Literal[Airport.OKA]",
        "from fli.models import Airport\nx: Annotated[str, AfterValidator(Airport)]",
        "from fli.search._decoders import _parse_airport\nf = _parse_airport\nf('OKA')",
        "from fli.search._decoders import _parse_airport\ndef g(code): return _parse_airport(code)",
    ],
)
def test_the_airport_guard_sees_every_other_load_of_the_enum(source: str) -> None:
    """Only the annotations, `isinstance`, `fli_airports` and `_leg_airport` are exempt."""
    assert _enum_lookups(ast.parse(source)) == [2]


def test_the_allowlist_names_exist_only_where_the_source_defines_them() -> None:
    """The guard exempts `fli_airports` and `_leg_airport` by name, so a second
    definition elsewhere would be a hole."""
    found: dict[str, list[str]] = {"fli_airports": [], "_leg_airport": []}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.FunctionDef) and node.name in found:
                found[node.name].append(path.name)
    assert found == {"fli_airports": ["fli_bridge.py"], "_leg_airport": ["_gflight_ids.py"]}
