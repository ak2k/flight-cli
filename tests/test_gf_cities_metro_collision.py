"""A `-CITIES` exclusion of a metro code that fli also has as an airport.

fli decodes QSF as Ain Arnat (Algeria) and SAO as Campo de Marte, so a row that
names either is not the Bay Area or Sao Paulo. `_metro.METRO_MEMBERS` reads
both as metros; the search page's filter would keep every SFO, OAK or SJC
connection, so the exclusion stays on Matrix."""

from __future__ import annotations

import pytest

from flight_cli._gf_postfilter import search_page_reasons
from flight_cli.routing_predicates import classify


@pytest.mark.parametrize("code", ["QSF", "SAO"])
def test_a_metro_code_fli_also_has_as_an_airport_goes_to_matrix(code: str) -> None:
    reasons = search_page_reasons(classify(None, f"-CITIES {code}").predicates)
    assert reasons == [f"a connecting-airport exclusion ({code})"]
