# pyright: reportPrivateUsage=false
"""The guard against resolving an airport code through fli's enum sees each
form such a lookup takes, and passes the ones that cannot hand a code another
airport's member."""

from __future__ import annotations

import ast

import pytest

from test_airport_alias_requests import _enum_lookups


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
        "from fli.models import Airport\nAirport.__members__.items()",
        "from fli.search._decoders import _parse_airport\n"
        "def _leg_airport(code):\n    return _parse_airport(code)",
    ],
)
def test_the_airport_guard_passes_the_lookups_that_cannot_alias(source: str) -> None:
    """A type check names no code, iterating the table is how `fli_bridge`
    builds its own, and `_leg_airport` decodes only a code fli has no entry
    for."""
    assert _enum_lookups(ast.parse(source)) == []
