# pyright: reportPrivateUsage=false
"""The rows Google placed on its Top flights board.

`ds:1[2]` is Google's own pick, `[3]` the rest of the board. Every Google row
says which it came from (`top_flight`), through dedupe, the merge of several
pages and every document; the table marks it `★`. A row the Cheapest tab adds
is never one: that page's `[2]` is not the Top flights board.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import GFLIGHT_PAGE_DIR, _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from test_gf_separate_tickets import _FLL_LGA, _fll_lga_pages

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_LAX = "ds1_jfk_lax_tfu.json"
_LHR = "ds1_jfk_lhr_tfu.json"
_RETURN = "ds1_return_leg_pinned.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_LHR_TOP = ["AF9656", "AA6939", "B61107", "AA100", "DL3"]
_LAX_TOP = ["DL1788", "B61023", "DL747"]


def _booked(row: gfid.GFlightWithId) -> str:
    return "+".join(f"{leg.airline.name}{leg.flight_number}" for leg in row.flight.legs)


def _rung_parity(name: str) -> str:
    return gzip.decompress(
        (GFLIGHT_PAGE_DIR / "rung_parity" / f"{name}.json.gz").read_bytes()
    ).decode()


def _board(ds1_json: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(ds1_json), _URL, 200))


def _top(board: list[gfid.GFlightWithId]) -> list[str]:
    return [_booked(r) for r in board if r.top_flight]


def _served(ds1_json: str, *, origin: str | None = None, destination: str | None = None) -> str:
    day = _RET if origin == "LHR" else _DEP
    return _page(_answering(ds1_json, origin=origin, destination=destination, date=day.isoformat()))


def _top_block(ds1_json: str) -> list[gfid.GFlightWithId]:
    """The rows the payload's `[2]` lists, parsed one by one."""
    block = json.loads(ds1_json)[2]
    rows: list[Any] = block[0] if block else []
    return [gfid._parse_flight_with_id(r) for r in rows]


def _top_block_keys(ds1_json: str) -> set[gfid.ItineraryKey]:
    return {gfid._itinerary_key(r) for r in _top_block(ds1_json)}


# ──────────────────────────────── the page ────────────────────────────────


@pytest.mark.parametrize(
    ("name", "top"),
    [
        (_LHR, _LHR_TOP),
        (_LAX, _LAX_TOP),
        ("ds1_metadata_blocks_kept.json", ["AS854+AS305", "AA144+AA1110"]),
        ("ds1_fll_lga_rt_best.json", ["AA2720", "DL1819", "B6272"]),
        ("ds1_jfk_lhr_oplh_out.json", ["AF9656", "DL1", "AA6939", "AY3787", "B61107"]),
    ],
)
def test_a_page_marks_exactly_the_itineraries_its_top_board_lists(
    name: str, top: list[str]
) -> None:
    board = _board(_ds1(name))
    assert _top(board) == top
    assert {gfid._itinerary_key(r) for r in board if r.top_flight} == _top_block_keys(_ds1(name))


@pytest.mark.parametrize(
    "name",
    [
        _RETURN,
        *(f"ds1_lhr_jfk_oplh_ret{i}.json" for i in range(1, 6)),
        "ds1_flightless_board.json",
        "ds1_zero_rows.json",
    ],
)
def test_a_page_with_no_top_board_marks_nothing(name: str) -> None:
    assert json.loads(_ds1(name))[2] in (None, [], [[]])
    assert _top(_board(_ds1(name))) == []


@pytest.mark.parametrize(
    ("token", "chrome"),
    [("ds1_bos_lhr_curated", "ds1_bos_lhr_chrome"), ("ds1_nyc_lon_token", "ds1_nyc_lon_chrome")],
)
def test_both_rungs_mark_the_same_rows(token: str, chrome: str) -> None:
    """One parser reads both rungs' pages, so one board reads one mark."""
    by_curl = _board(_rung_parity(token))
    by_chrome = _board(_rung_parity(chrome))
    assert len(_top(by_curl)) == 5
    assert _top(by_curl) == _top(by_chrome)
    assert {gfid._itinerary_key(r) for r in by_curl if r.top_flight} == _top_block_keys(
        _rung_parity(token)
    )


# ──────────────────────────────── dedupe ─────────────────────────────────


def _lax_with_copy(*, source: int, into: int, price: int, cabin: int | None = None) -> str:
    """The LAX capture with a copy of block `source`'s first row added to block
    `into` at `price`, in `cabin` on its last leg when given."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    copy: list[Any] = json.loads(json.dumps(payload[source][0][0]))
    copy[1][0][1] = price
    if cabin is not None:
        copy[0][2][-1][gfid._LEG_CABIN_IDX] = cabin
    payload[into][0].append(copy)
    return json.dumps(payload)


def test_a_cheaper_copy_of_a_top_row_off_the_top_board_is_kept_and_marked() -> None:
    board = _board(_lax_with_copy(source=2, into=3, price=150))
    (kept,) = [r for r in board if _booked(r) == "DL1788"]
    assert (kept.flight.price, kept.top_flight) == (150, True)
    assert _top(board) == _LAX_TOP


def test_a_row_the_top_board_lists_dearer_is_kept_at_its_own_fare_and_marked() -> None:
    payload: list[Any] = json.loads(_ds1(_LAX))
    rest = gfid._parse_flight_with_id(payload[3][0][0])
    assert rest.flight.price is not None
    board = _board(_lax_with_copy(source=3, into=2, price=int(rest.flight.price) + 50))
    (kept,) = [r for r in board if gfid._itinerary_key(r) == gfid._itinerary_key(rest)]
    assert (kept.flight.price, kept.top_flight) == (rest.flight.price, True)
    assert _top(board) == [*_LAX_TOP, _booked(rest)]


def test_a_listing_in_another_cabin_mix_carries_the_mark() -> None:
    """`_listing` may hand one of `others` on in the kept row's place."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    rest = gfid._parse_flight_with_id(payload[3][0][0])
    assert rest.flight.price is not None
    board = _board(_lax_with_copy(source=3, into=2, price=int(rest.flight.price) + 50, cabin=4))
    (kept,) = [r for r in board if gfid._itinerary_key(r) == gfid._itinerary_key(rest)]
    assert kept.top_flight
    assert [(o.top_flight, o.amenities[-1].cabin) for o in kept.others] == [(True, "FIRST")]


# ─────────────────────────────── documents ───────────────────────────────


def _one_way(*extra: str) -> list[str]:
    return [*_SEARCH, "JFK", "LHR", "--dep", _DEP.isoformat(), "--backend", "gflight", *extra]


def test_the_json_document_marks_the_top_rows_where_price_order_puts_them(
    gf_session: Callable[..., Any],
) -> None:
    """The three USD293 rows come first, then Google's five top flights at
    USD295 in the page's order."""
    fake = gf_session(_served(_ds1(_LHR)))
    result = CliRunner().invoke(cli.app, _one_way("--fast", "-n", "101", "--format", "json"))
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    assert len(rows) == 101
    assert [i for i, r in enumerate(rows) if r["top_flight"] is True] == [3, 4, 5, 6, 7]
    assert all(r["top_flight"] is False for i, r in enumerate(rows) if i not in range(3, 8))
    assert {r["price"] for r in rows[3:8]} == {295.0}
    ordered = cli._price_ordered(list(_board(_ds1(_LHR))))
    assert [r["flight_id"] for r in rows] == [g.flight_id for g in ordered]
    assert len(fake.gets) == 2  # the board, then its Cheapest tab
    gf_session(_served(_ds1(_LHR)))
    enveloped = CliRunner().invoke(cli.app, _one_way("--fast", "-n", "101", "--format", "envelope"))
    assert enveloped.exit_code == 0, enveloped.output
    (cabin,) = json.loads(enveloped.stdout)["results"]
    assert [r["row"]["top_flight"] for r in cabin["rows"]] == [r["top_flight"] for r in rows]


def test_each_cabin_of_a_multi_cabin_document_marks_its_top_rows(
    gf_session: Callable[..., Any],
) -> None:
    page = _served(_ds1(_LAX))
    gf_session(page, page)
    result = CliRunner().invoke(
        cli.app,
        [
            *(*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--cabin", "economy,business", "--backend", "gflight"),
            *("--format", "json", "-n", "5"),
        ],
    )
    assert result.exit_code == 0, result.output
    doc: dict[str, list[dict[str, Any]]] = json.loads(result.stdout)
    assert list(doc) == ["COACH", "BUSINESS"]
    top = [r.flight_id for r in _top_block(_ds1(_LAX))]
    assert [_booked(r) for r in _top_block(_ds1(_LAX))] == _LAX_TOP
    for rows in doc.values():
        assert len(rows) == 5
        assert [r["flight_id"] for r in rows if r["top_flight"]] == top


def _return_with_top(index: int) -> str:
    """The pinned return capture with its row `index` moved onto a top board."""
    payload: list[Any] = json.loads(_ds1(_RETURN))
    rows: list[Any] = payload[3][0]
    payload[2] = [[rows.pop(index)]]
    return json.dumps(payload)


def _round_trip(gf_session: Callable[..., Any], ret: str) -> tuple[list[list[dict[str, Any]]], Any]:
    fake = gf_session(_served(_ds1(_LHR)), _served(ret, origin="LHR", destination="JFK"))
    argv = [*_one_way("--fast", "-n", "1000", "--format", "json"), "--return", _RET.isoformat()]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout), fake


def test_each_round_trip_member_carries_its_own_pages_mark(
    gf_session: Callable[..., Any],
) -> None:
    """An outbound reads the outbound page, a return its pinned return page,
    which on every committed capture carries no top board."""
    doc, fake = _round_trip(gf_session, _ds1(_RETURN))
    assert len(fake.gets) == 12  # the outbound, ten pins, then the Cheapest tab
    assert all(len(r) == 2 for r in doc)
    top = {r.flight_id for r in _top_block(_ds1(_LHR))}
    assert len(top) == 5
    assert all(out["top_flight"] is (out["flight_id"] in top) for out, _ in doc)
    assert any(out["top_flight"] for out, _ in doc)
    assert all(ret["top_flight"] is False for _, ret in doc)


def test_a_return_page_that_lists_a_top_board_marks_that_return_alone(
    gf_session: Callable[..., Any],
) -> None:
    moved = gfid._parse_flight_with_id(json.loads(_ds1(_RETURN))[3][0][1])
    doc, _ = _round_trip(gf_session, _return_with_top(1))
    assert {ret["flight_id"] for _, ret in doc if ret["top_flight"]} == {moved.flight_id}
    assert any(not ret["top_flight"] for _, ret in doc)


# ───────────────────────────── the Cheapest tab ─────────────────────────────


def test_no_row_the_cheapest_tab_adds_is_marked(gf_session: Callable[..., Any]) -> None:
    """The Cheapest page lists its own `[2]`, B6272 on separate tickets among
    it, and that is not Google's Top flights board; AA2720 is on the Best
    page's."""
    fake = gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 11 + 1  # the board, ten pins, then the Cheapest tab
    doc: list[list[dict[str, Any]]] = json.loads(result.stdout)
    alone = [m for r in doc if len(r) == 1 for m in r]
    assert len(alone) == 33
    assert all(m["separate_tickets"] is True for m in alone)
    assert [m["top_flight"] for m in alone].count(True) == 0
    aa2720 = [out for out, *_ in doc if [leg["flight_number"] for leg in out["legs"]] == ["2720"]]
    assert aa2720
    assert all(out["top_flight"] is True for out in aa2720)
    assert all(not out["top_flight"] for out, *_ in doc if out not in aa2720)
