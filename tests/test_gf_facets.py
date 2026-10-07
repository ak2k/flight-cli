# pyright: reportPrivateUsage=false
"""Google's route facets: the filter choices a search page states at `ds:1[7]`.

The fare, trip-length and layover ranges, the alliances and airlines Google's
Airlines filter offers, and the airports a trip may connect at, read off the
page the search already fetched. The block describes the search rather than
the rows served: it is identical on every page of one search, return pages
included, and the airline list is the filter's, not the rows' carriers.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

from conftest import GFLIGHT_PAGE_DIR, _page
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch

_URL = "https://www.google.com/travel/flights?tfs=abc"
_ALLIANCES = (
    ("ONEWORLD", "Oneworld"),
    ("SKYTEAM", "SkyTeam"),
    ("STAR_ALLIANCE", "Star Alliance"),
)
# Per capture: price, trip length, layover, airline count and first airline,
# connecting-airport count and first airport, as the raw block states them.
type _Stated = tuple[
    tuple[float, float],
    tuple[int, int],
    tuple[int, int],
    int,
    tuple[str, str],
    int,
    tuple[str, str],
]
_OPLH: _Stated = (
    (810.0, 2316.0),
    (415, 2835),
    (45, 1875),
    34,
    ("EI", "Aer Lingus"),
    27,
    ("AMS", "Amsterdam"),
)
_BOS_LHR: _Stated = (
    (779.0, 3613.0),
    (390, 2065),
    (45, 1530),
    32,
    ("EI", "Aer Lingus"),
    29,
    ("AMS", "Amsterdam"),
)
_EWR_LGW: _Stated = (
    (488.0, 15069.0),
    (655, 2460),
    (44, 1880),
    23,
    ("A3", "Aegean"),
    31,
    ("AMS", "Amsterdam"),
)
_JFK_LAX: _Stated = (
    (318.0, 3378.0),
    (312, 1223),
    (32, 755),
    14,
    ("AS", "Alaska"),
    20,
    ("ATL", "Atlanta"),
)
_NYC_LON: _Stated = (
    (488.0, 22564.0),
    (410, 3510),
    (35, 2045),
    46,
    ("A3", "Aegean"),
    53,
    ("ALG", "Algiers"),
)
_STATED: dict[str, _Stated] = {
    "ds1_jfk_lhr_tfu.json": (
        (293.0, 918.0),
        (410, 2065),
        (45, 1525),
        35,
        ("EI", "Aer Lingus"),
        30,
        ("AMS", "Amsterdam"),
    ),
    "ds1_jfk_lax_tfu.json": (
        (204.0, 1502.0),
        (363, 1409),
        (28, 974),
        14,
        ("AS", "Alaska"),
        22,
        ("ABQ", "Albuquerque"),
    ),
    "ds1_jfk_lhr_oplh_out.json": _OPLH,
    **{f"ds1_lhr_jfk_oplh_ret{i}.json": _OPLH for i in range(1, 6)},
    "ds1_fll_lga_rt_best.json": (
        (234.0, 2097.0),
        (172, 968),
        (30, 724),
        9,
        ("AA", "American"),
        13,
        ("ATL", "Atlanta"),
    ),
    "ds1_fll_lga_rt_cheapest.json": (
        (220.0, 2097.0),
        (172, 1615),
        (30, 1337),
        11,
        ("TS", "Air Transat"),
        21,
        ("ATL", "Atlanta"),
    ),
    "ds1_metadata_blocks_kept.json": (
        (5055.0, 35822.0),
        (678, 1888),
        (30, 1125),
        6,
        ("AS", "Alaska"),
        26,
        ("ATL", "Atlanta"),
    ),
    "rung_parity/ds1_bos_lhr_chrome": _BOS_LHR,
    "rung_parity/ds1_bos_lhr_curated": _BOS_LHR,
    "rung_parity/ds1_ewr_lgw_curated": _EWR_LGW,
    "rung_parity/ds1_ewr_lgw_token": _EWR_LGW,
    "rung_parity/ds1_jfk_lax_curated": _JFK_LAX,
    "rung_parity/ds1_jfk_lax_token": _JFK_LAX,
    "rung_parity/ds1_nyc_lon_chrome": _NYC_LON,
    "rung_parity/ds1_nyc_lon_curated": _NYC_LON,
    "rung_parity/ds1_nyc_lon_token": _NYC_LON,
}
_UNSTATED = [
    "ds1_flightless_board.json",
    "ds1_zero_rows.json",
    "ds1_single_block_empty.json",
    "ds1_jfk_lax_3rows.json",
    "ds1_brace_in_string.json",
    "ds1_blocks_relocated.json",
    "ds1_return_leg_pinned.json",
]
_RUNG_PAIRS = [
    ("ds1_bos_lhr_chrome", "ds1_bos_lhr_curated"),
    ("ds1_ewr_lgw_curated", "ds1_ewr_lgw_token"),
    ("ds1_jfk_lax_curated", "ds1_jfk_lax_token"),
    ("ds1_nyc_lon_chrome", "ds1_nyc_lon_curated"),
    ("ds1_nyc_lon_curated", "ds1_nyc_lon_token"),
]


def _ds1_text(capture: str) -> str:
    if capture.startswith("rung_parity/"):
        return gzip.decompress((GFLIGHT_PAGE_DIR / f"{capture}.json.gz").read_bytes()).decode()
    return (GFLIGHT_PAGE_DIR / capture).read_text()


def _parsed(ds1_text: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(ds1_text), _URL, 200))


# ─────────────────────────────────── decode ─────────────────────────────────


@pytest.mark.parametrize("capture", list(_STATED))
def test_each_page_states_its_route_facets(capture: str) -> None:
    price, duration, layover, airlines, first_airline, hubs, first_hub = _STATED[capture]
    facets = _parsed(_ds1_text(capture)).facets
    assert facets is not None
    assert facets.currency == "USD"
    assert (facets.price_low, facets.price_high) == price
    assert (facets.duration_low, facets.duration_high) == duration
    assert (facets.layover_low, facets.layover_high) == layover
    assert (len(facets.airlines), facets.airlines[0]) == (airlines, first_airline)
    assert facets.alliances == _ALLIANCES
    assert (len(facets.connections), facets.connections[0]) == (hubs, first_hub)


def test_the_lists_keep_googles_order() -> None:
    facets = _parsed(_ds1_text("ds1_jfk_lhr_tfu.json")).facets
    assert facets is not None
    assert facets.airlines[-1] == ("WS", "WestJet")
    assert facets.connections[-1] == ("ZRH", "Zürich")


@pytest.mark.parametrize("capture", _UNSTATED)
def test_a_page_without_the_block_states_none(capture: str) -> None:
    """Read off the payload: `ds1_blocks_relocated` is refused before any board."""
    assert json.loads(_ds1_text(capture))[7] is None
    assert gfid._route_facets(json.loads(_ds1_text(capture)), []) is None


@pytest.mark.parametrize(("one", "other"), _RUNG_PAIRS)
def test_both_reads_of_one_search_state_the_same_facets(one: str, other: str) -> None:
    """The curated, token and Chrome reads serve different rows for one search
    (72 and 300 on NYC-LON) and one block."""
    first = _parsed(_ds1_text(f"rung_parity/{one}")).facets
    second = _parsed(_ds1_text(f"rung_parity/{other}")).facets
    assert first is not None and first == second


def _short(block: list[Any]) -> None:
    del block[3:]


def _string_price(block: list[Any]) -> None:
    block[0][0][1] = "293"


def _price_reversed(block: list[Any]) -> None:
    block[0] = [block[0][1], block[0][0]]


def _duration_reversed(block: list[Any]) -> None:
    block[3] = block[3][::-1]


def _layover_reversed(block: list[Any]) -> None:
    block[2][1], block[2][2] = block[2][2], block[2][1]


def _three_string_airline(block: list[Any]) -> None:
    block[1][1][0] = [*block[1][1][0], "Ireland"]


def _hubs_without_bounds(block: list[Any]) -> None:
    del block[2][1:]


@pytest.mark.parametrize(
    "mangle",
    [
        _short,
        _string_price,
        _price_reversed,
        _duration_reversed,
        _layover_reversed,
        _three_string_airline,
        _hubs_without_bounds,
    ],
)
def test_a_block_of_another_shape_states_none_and_leaves_the_board(mangle: Any) -> None:
    """All or nothing: no part of a block that does not read whole, and the
    rows, insight and history as the page carries them without it."""
    served = _ds1_text("ds1_jfk_lhr_tfu.json")
    payload: list[Any] = json.loads(served)
    mangle(payload[7])
    board = _parsed(json.dumps(payload))
    base = _parsed(served)
    assert board.facets is None
    assert list(board) == list(base)
    assert (board.insight, board.history) == (base.insight, base.history)
    assert base.insight is not None and base.history is not None
