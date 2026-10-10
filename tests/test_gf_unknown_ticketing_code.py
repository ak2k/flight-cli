# pyright: reportPrivateUsage=false
"""A `row[7]` that holds a code nobody has decoded says nothing about ticketing.

Only `[]`, `[1]` and `[2]` have been seen. A slot like `[5]` is neither one
ticket nor separate tickets, so `--format json` reports `separate_tickets: null`
for it, as it does for a slot that is absent.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import Cabin, Leg, SearchOptions

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_LAX = "ds1_jfk_lax_tfu.json"


def _row_with_slot(slot: Any) -> gfid.GFlightWithId:
    raw: list[Any] = json.loads(json.dumps(gfid._rows_from_ds1(json.loads(_ds1(_LAX))).rows[0]))
    raw[7] = slot
    return gfid._parse_flight_with_id(raw)


@pytest.mark.parametrize("slot", [[5], [0], [3, 4]])
def test_an_undecoded_row_seven_code_reads_as_unknown_in_json(slot: list[int]) -> None:
    row = _row_with_slot(slot)
    assert (row.ticketing, row.flight.self_transfer) == (None, None)
    assert cli._gflight_json_row(row)["separate_tickets"] is None


@pytest.mark.parametrize(
    ("slot", "separate_tickets"),
    [([], False), ([1], True), ([2], True), ([5, 1], True), ([5, 2], True)],
)
def test_a_decoded_row_seven_keeps_its_json_answer(slot: list[int], separate_tickets: bool) -> None:
    assert cli._gflight_json_row(_row_with_slot(slot))["separate_tickets"] is separate_tickets


def test_the_search_document_says_null_for_a_row_with_an_undecoded_code(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    payload: list[Any] = json.loads(_ds1(_LAX))
    gfid._rows_from_ds1(payload).rows[0][7] = [5]
    page = _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )
    gf_session(page)
    cli._run_gflight_path(
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=True,
        legs=(Leg.of("JFK", "LAX", _DEP),),
    )
    doc: list[dict[str, Any]] = json.loads(capsys.readouterr().out)
    assert [r["separate_tickets"] for r in doc].count(None) == 1
    assert [r["separate_tickets"] for r in doc].count(False) == len(doc) - 1
