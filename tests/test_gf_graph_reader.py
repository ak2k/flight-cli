"""The price graph reads its response with the reader every captured page shares.

`_gf_rpc_shared.result_payloads` refuses a body it read only part of and any
body holding an error row; the graph parser has to refuse the same bodies.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from flight_cli import _gf_calgraph as cg


def _chunk(*rows: list[object]) -> str:
    text = json.dumps(list(rows), separators=(",", ":"))
    return f"\n{len(text) + 2}\n{text}"


def _graph_row() -> list[object]:
    cells = [["2026-10-13", None, [[None, 249], ""], 1]]
    return ["wrb.fr", None, json.dumps([None, cells], separators=(",", ":"))]


def _body(*chunks: str, tail: str = "") -> str:
    return ")]}'\n" + "".join(chunks) + "\n" + tail


def test_a_graph_body_with_text_after_its_last_chunk_is_refused() -> None:
    body = _body(_chunk(_graph_row()), tail="BROKEN CHUNK\n")
    with pytest.raises(cg.GfPriceGraphError) as caught:
        cg.parse_graph(body, trip_length=None)
    assert str(caught.value) == "Google Flights' price graph could not be read; its shape changed"
    assert caught.value.code is None


def test_an_error_row_after_the_result_row_refuses_the_whole_graph() -> None:
    error_row: list[object] = ["wrb.fr", None, None, None, None, [13, None, []]]
    body = _body(_chunk(_graph_row()), _chunk(error_row))
    with pytest.raises(cg.GfPriceGraphError) as caught:
        cg.parse_graph(body, trip_length=None)
    assert str(caught.value) == (
        "Google Flights' price graph came back with error 13: "
        "Google refused this browser session's request"
    )
    assert caught.value.code == 13


def test_a_clean_graph_body_still_reads() -> None:
    body = _body(_chunk(_graph_row()), _chunk(["di", 70]))
    page = cg.parse_graph(body, trip_length=None)
    assert page.last == date(2026, 10, 13)
    assert [c.price for c in page.cells] == [249.0]
