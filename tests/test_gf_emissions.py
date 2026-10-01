# pyright: reportPrivateUsage=false
"""Google's CO2 estimate on every Google Flights row.

Each row's `data[0][22]` carries the row's grams at [7], the route's typical
grams at [8], the signed percent from that typical at [3] and Google's label for
that comparison at [2]; each leg tuple carries the leg's own grams at [31]. The
tests read those slots themselves and hold the parsed rows, the JSON document
and the table to them.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Bags, Leg

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_LAX = "ds1_jfk_lax_tfu.json"
_LHR = "ds1_jfk_lhr_tfu.json"
_OUTBOUND = "ds1_metadata_blocks_kept.json"  # HNL-MIA, the outbound page
_RETURN = "ds1_return_leg_pinned.json"  # its return board, pinned to row 0
_ROW_CAPTURES = (
    "ds1_brace_in_string.json",
    "ds1_jfk_lax_3rows.json",
    _LAX,
    _LHR,
    _OUTBOUND,
    _RETURN,
)
_URL = "https://www.google.com/travel/flights?tfs=abc"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_LABELS = {1: "lower", 2: "typical", 3: "higher"}
_CO2_FIELDS = (
    "co2_emissions_g",
    "co2_emissions_typical_g",
    "co2_emissions_delta_pct",
    "emissions_tag",
)
# The row keys of every Google JSON document, as fli's FlightResult dumps them
# plus the flight id; the CO2 four were there before, all null.
_ROW_KEYS = {
    "booking_token",
    "co2_emissions_delta_pct",
    "co2_emissions_g",
    "co2_emissions_typical_g",
    "currency",
    "duration",
    "emissions_tag",
    "flight_id",
    "layovers",
    "legs",
    "mixed_cabin",
    "price",
    "primary_airline",
    "primary_airline_name",
    "self_transfer",
    "stops",
}


def _served(name: str) -> str:
    return _page(_answering(_ds1(name), origin=None, destination=None, date=_DEP.isoformat()))


def _board(name: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(_ds1(name)), _URL, 200))


def _raw_rows(name: str) -> list[Any]:
    return gfid._rows_from_ds1(json.loads(_ds1(name))).rows


def _parsed(name: str) -> list[gfid.GFlightWithId]:
    return [gfid._parse_flight_with_id(r) for r in _raw_rows(name)]


def _paired(name: str) -> list[tuple[list[Any], gfid.GFlightWithId]]:
    """Each served row beside the raw row it was parsed from, matched on the
    page's own flight id."""
    raw = {r[0][17]: r for r in _raw_rows(name)}
    board = _board(name)
    assert len(raw) == len(board)
    return [(raw[g.flight_id], g) for g in board]


def _booked(row: gfid.GFlightWithId) -> str:
    return "+".join(f"{leg.airline.name}{leg.flight_number}" for leg in row.flight.legs)


def _slots(raw: list[Any]) -> tuple[Any, Any, Any, Any]:
    """(grams, typical, delta, label) as the row's block states them."""
    block = raw[0][22]
    return block[7], block[8], block[3], _LABELS.get(block[2])


# ─────────────────────────────── the decode ───────────────────────────────


@pytest.mark.parametrize(("name", "rows", "with_grams"), [(_LAX, 95, 95), (_LHR, 101, 100)])
def test_every_row_of_a_full_board_carries_the_figures_its_page_states(
    name: str, rows: int, with_grams: int
) -> None:
    pairs = _paired(name)
    assert len(pairs) == rows
    for raw, g in pairs:
        f = g.flight
        decoded = (
            f.co2_emissions_g,
            f.co2_emissions_typical_g,
            f.co2_emissions_delta_pct,
            f.emissions_tag,
        )
        assert decoded == _slots(raw), _booked(g)
    assert sum(isinstance(g.flight.co2_emissions_g, int) for _, g in pairs) == with_grams


def test_a_row_google_gives_no_estimate_for_carries_only_the_typical() -> None:
    """VS46 on JFK-LHR states the route's typical and nothing of its own: no
    grams, no percent, a label of 0 and a leg tuple too short for a leg figure."""
    (vs46,) = [g for g in _board(_LHR) if _booked(g) == "VS46"]
    f = vs46.flight
    assert f.co2_emissions_g is None
    assert f.co2_emissions_delta_pct is None
    assert f.emissions_tag is None
    assert f.co2_emissions_typical_g == 431000
    assert [leg.co2_emissions_g for leg in f.legs] == [None]


def test_the_label_is_googles_for_the_percent_beside_it() -> None:
    """[11] is a label too, against the board's median rather than the route's
    typical; it differs from [2] on 35 JFK-LAX and 40 JFK-LHR rows and calls 13
    rows lower at a percent of 0 to +4. The label printed beside a percent from
    the typical is the one that describes it."""
    differ: dict[str, int] = {}
    for name in _ROW_CAPTURES:
        for raw in _raw_rows(name):
            f = gfid._parse_flight_with_id(raw).flight
            if f.emissions_tag == "lower":
                assert f.co2_emissions_delta_pct is not None
                assert f.co2_emissions_delta_pct < 0
            if f.emissions_tag == "higher":
                assert f.co2_emissions_delta_pct is not None
                assert f.co2_emissions_delta_pct > 0
            block = raw[0][22]
            if block[2] != block[11]:
                assert f.emissions_tag == _LABELS.get(block[2])
                differ[name] = differ.get(name, 0) + 1
    assert differ[_LAX] == 35
    assert differ[_LHR] == 40


@pytest.mark.parametrize("name", _ROW_CAPTURES)
def test_each_leg_carries_its_own_grams_and_they_sum_to_the_rows(name: str) -> None:
    """The row's figure is its legs' own, summed and rounded to the kilogram."""
    raw_rows = _raw_rows(name)
    for raw in raw_rows:
        f = gfid._parse_flight_with_id(raw).flight
        legs = [leg.co2_emissions_g for leg in f.legs]
        assert legs == [fl[31] if len(fl) > 31 else None for fl in raw[0][2]]
        stated = [grams for grams in legs if grams is not None]
        if f.co2_emissions_g is not None and len(stated) == len(legs):
            assert round(sum(stated) / 1000) * 1000 == f.co2_emissions_g
    assert any(fl[31] for raw in raw_rows for fl in raw[0][2])


def _with(edit: Callable[[list[Any]], None]) -> list[Any]:
    """Row 0 of the JFK-LAX board (DL1788: 261000 g, typical 347000, -25%,
    lower) with `edit` applied to a copy of its raw row."""
    raw: list[Any] = json.loads(json.dumps(_raw_rows(_LAX)[0]))
    edit(raw)
    return raw


def _set_block(value: Any) -> Callable[[list[Any]], None]:
    def edit(raw: list[Any]) -> None:
        raw[0][22] = value

    return edit


def _set_slots(value: Any, *slots: int) -> Callable[[list[Any]], None]:
    def edit(raw: list[Any]) -> None:
        for slot in slots:
            raw[0][22][slot] = value

    return edit


def _set_leg_grams(value: Any) -> Callable[[list[Any]], None]:
    def edit(raw: list[Any]) -> None:
        raw[0][2][0][31] = value

    return edit


def _drop_block(raw: list[Any]) -> None:
    raw[0] = raw[0][:22]


def _shorten_leg(raw: list[Any]) -> None:
    raw[0][2][0] = raw[0][2][0][:31]


_NO_FIGURES = {"g": None, "typical": None, "delta": None, "tag": None}


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param(_drop_block, _NO_FIGURES, id="block-absent"),
        pytest.param(_set_block("[/x]"), _NO_FIGURES, id="block-a-string"),
        pytest.param(_set_block([None, None]), _NO_FIGURES, id="block-short"),
        pytest.param(_set_block(None), _NO_FIGURES, id="block-null"),
        pytest.param(_set_slots(True, 2, 3, 7, 8), _NO_FIGURES, id="slots-bool"),
        pytest.param(_set_slots("1", 2, 3, 7, 8), _NO_FIGURES, id="slots-string"),
        pytest.param(
            _set_slots(261000.0, 7, 8),
            {"g": None, "typical": None, "delta": -25, "tag": "lower"},
            id="grams-float",
        ),
        pytest.param(
            _set_slots(1.0, 2, 3),
            {"g": 261000, "typical": 347000, "delta": None, "tag": None},
            id="percent-and-label-float",
        ),
        pytest.param(
            _set_slots(-261000, 7, 8),
            {"g": None, "typical": None, "delta": -25, "tag": "lower"},
            id="grams-negative",
        ),
        pytest.param(
            _set_slots(4, 2),
            {"g": 261000, "typical": 347000, "delta": -25, "tag": None},
            id="label-unknown",
        ),
    ],
)
def test_a_malformed_row_slot_says_nothing_and_the_row_still_parses(
    edit: Callable[[list[Any]], None], expected: dict[str, Any]
) -> None:
    """Only an int is a figure, and grams are never negative; a percent may be.
    Anything else is a slot Google left empty, never a zero and never a reason
    to drop the row."""
    base = gfid._parse_flight_with_id(_raw_rows(_LAX)[0])
    g = gfid._parse_flight_with_id(_with(edit))
    f = g.flight
    assert {
        "g": f.co2_emissions_g,
        "typical": f.co2_emissions_typical_g,
        "delta": f.co2_emissions_delta_pct,
        "tag": f.emissions_tag,
    } == expected
    assert f.model_dump(exclude=set(_CO2_FIELDS)) == base.flight.model_dump(
        exclude=set(_CO2_FIELDS)
    )


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(_set_leg_grams(True), id="bool"),
        pytest.param(_set_leg_grams(261023.0), id="float"),
        pytest.param(_set_leg_grams(-261023), id="negative"),
        pytest.param(_set_leg_grams("261023"), id="string"),
        pytest.param(_set_leg_grams(None), id="null"),
        pytest.param(_shorten_leg, id="tuple-short"),
    ],
)
def test_a_malformed_leg_slot_says_nothing_and_the_row_still_parses(
    edit: Callable[[list[Any]], None],
) -> None:
    base = gfid._parse_flight_with_id(_raw_rows(_LAX)[0]).flight
    f = gfid._parse_flight_with_id(_with(edit)).flight
    assert [leg.co2_emissions_g for leg in f.legs] == [None]
    assert f.co2_emissions_g == base.co2_emissions_g
    exclude = {"legs": {0: {"co2_emissions_g"}}}
    assert f.model_dump(exclude=exclude) == base.model_dump(exclude=exclude)


# ──────────────────────────────── the JSON ────────────────────────────────


def test_every_json_row_of_a_board_carries_its_figures(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *(*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat()),
            *("--backend", "gflight", "--fast", "--format", "json", "-n", "95"),
        ],
    )
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    assert len(rows) == 95
    assert all(set(r) == _ROW_KEYS for r in rows)
    assert all(type(r["co2_emissions_g"]) is int for r in rows)
    assert all(type(leg["co2_emissions_g"]) is int for r in rows for leg in r["legs"])
    assert rows[0]["co2_emissions_g"] == 261000
    assert rows[0]["co2_emissions_typical_g"] == 347000
    assert rows[0]["co2_emissions_delta_pct"] == -25
    assert rows[0]["emissions_tag"] == "lower"


def test_each_member_of_a_round_trip_carries_its_own_directions_figures(
    gf_rows: Callable[..., list[Any]],
) -> None:
    """The outbound page and the return board each state the row's own
    direction; no page states a total, so none is printed."""
    pair = (gf_rows(_OUTBOUND)[0], gf_rows(_RETURN)[0])
    ((out, ret),) = cli._gflight_json_document([pair])
    assert out["co2_emissions_g"] == 4264000
    assert out["co2_emissions_typical_g"] == 4010000
    assert out["co2_emissions_delta_pct"] == 6
    assert out["emissions_tag"] == "higher"
    assert [leg["co2_emissions_g"] for leg in out["legs"]] == [3400644, 863696]
    assert ret["co2_emissions_g"] == 1539000
    assert ret["co2_emissions_typical_g"] == 2377000
    assert ret["co2_emissions_delta_pct"] == -35
    assert ret["emissions_tag"] == "lower"
    assert [leg["co2_emissions_g"] for leg in ret["legs"]] == [766350, 772184]


def test_each_cabin_of_a_multi_cabin_document_carries_the_figures(
    gf_session: Callable[..., Any],
) -> None:
    page = _served(_LAX)
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
    for rows in doc.values():
        assert len(rows) == 5
        assert all(set(r) == _ROW_KEYS for r in rows)
        assert all(type(r["co2_emissions_g"]) is int for r in rows)


# ──────────────────────────────── the table ───────────────────────────────


def _render(
    monkeypatch: pytest.MonkeyPatch,
    results: list[Any],
    legs: tuple[Leg, ...],
    *,
    color: bool = False,
    bags: Bags | None = None,
) -> str:
    buffer = io.StringIO()
    console = (
        Console(
            file=buffer, width=200, force_terminal=True, color_system="truecolor", no_color=False
        )
        if color
        else Console(file=buffer, width=200, no_color=True)
    )
    monkeypatch.setattr(cli, "console", console)
    cli._render_gflight_table(results, legs=legs, top_n=len(results), bags=bags)
    return buffer.getvalue()


def _header(text: str) -> list[str]:
    line = next(ln for ln in text.splitlines() if ln.startswith("┃"))
    return [c.strip() for c in line.strip("┃").split("┃")]


def _body(text: str) -> list[list[str]]:
    """Every table line's cells, continuation lines included."""
    return [
        [c.strip() for c in ln.strip("│").split("│")]
        for ln in text.splitlines()
        if ln.startswith("│")
    ]


def _cell_by(text: str, column: str, key_column: str, key: str) -> str:
    header = _header(text)
    (row,) = [cells for cells in _body(text) if cells[header.index(key_column)] == key]
    return row[header.index(column)]


_ONE_WAY_LAX = (Leg.of("JFK", "LAX", _DEP),)
_ONE_WAY_LHR = (Leg.of("JFK", "LHR", _DEP),)
_ROUND_TRIP = (Leg.of("HNL", "MIA", _DEP), Leg.of("MIA", "HNL", _RET))


def test_the_table_shows_each_rows_kilograms_and_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    lax = _render(monkeypatch, _parsed(_LAX), _ONE_WAY_LAX)
    assert _header(lax)[-2:] == ["legroom", "CO2 kg"]
    assert _cell_by(lax, "CO2 kg", "legs", "DL 1788") == "261 -25%"
    assert _cell_by(lax, "CO2 kg", "legs", "B6 1023") == "414 +19%"
    assert "CO2 kg: Google's estimate" in lax
    lhr = _render(monkeypatch, _parsed(_LHR), _ONE_WAY_LHR)
    assert _cell_by(lhr, "CO2 kg", "legs", "VS 46") == ""
    assert _cell_by(lhr, "CO2 kg", "legs", "BA 174") == "426 -1%"
    with_bags = _render(monkeypatch, _parsed(_LAX), _ONE_WAY_LAX, bags=Bags(checked=1))
    assert _header(with_bags)[-3:] == ["legroom", "CO2 kg", "bags"]


def test_the_cell_is_colored_by_googles_label(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _parsed(_LAX)
    written = _render(monkeypatch, rows, _ONE_WAY_LAX, color=True)
    assert "\x1b[32m261 -25%\x1b[0m" in written  # DL1788, lower
    assert "\x1b[31m414 +19%\x1b[0m" in written  # B6 1023, higher
    typical = next(g.flight for g in rows if g.flight.emissions_tag == "typical")
    grams, delta = typical.co2_emissions_g, typical.co2_emissions_delta_pct
    assert grams is not None
    assert delta is not None
    shown = f"{grams // 1000} {delta:+d}%"
    assert re.search(rf"(?<!m){re.escape(shown)}\x20", written), shown


def _without_co2(g: gfid.GFlightWithId) -> gfid.GFlightWithId:
    return replace(g, flight=g.flight.model_copy(update=dict.fromkeys(_CO2_FIELDS)))


@pytest.mark.parametrize(
    ("results", "legs"),
    [
        pytest.param(_parsed(_LAX), _ONE_WAY_LAX, id="one-way"),
        pytest.param(_parsed(_LHR), _ONE_WAY_LHR, id="one-way-unpriced"),
        pytest.param([(_parsed(_OUTBOUND)[0], _parsed(_RETURN)[0])], _ROUND_TRIP, id="round-trip"),
    ],
)
def test_a_board_without_figures_prints_the_table_it_printed_before(
    monkeypatch: pytest.MonkeyPatch, results: list[Any], legs: tuple[Leg, ...]
) -> None:
    """The column costs width, so it shows only where a row has something to
    put in it; every other cell is the same either way."""
    stripped = [
        tuple(_without_co2(g) for g in cast("tuple[Any, ...]", r))
        if isinstance(r, tuple)
        else _without_co2(r)
        for r in results
    ]
    with_co2 = _render(monkeypatch, results, legs)
    without = _render(monkeypatch, stripped, legs)
    assert "CO2" not in without
    at = _header(with_co2).index("CO2 kg")
    assert [c for i, c in enumerate(_header(with_co2)) if i != at] == _header(without)
    assert [[c for i, c in enumerate(cells) if i != at] for cells in _body(with_co2)] == _body(
        without
    )


def test_a_round_trip_prints_each_members_own_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = (_parsed(_OUTBOUND)[0], _parsed(_RETURN)[0])
    text = _render(monkeypatch, [pair], _ROUND_TRIP)
    assert _cell_by(text, "CO2 kg", "#", "1a") == "4264 +6%"
    assert _cell_by(text, "CO2 kg", "#", "1b") == "1539 -35%"
