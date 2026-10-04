# pyright: reportPrivateUsage=false
"""The Google table at the console's width.

Rich prints at the width of the first std stream that is a terminal, or
`COLUMNS`, or 80 when neither says, so a captured stdout gets 80 columns. The
table takes the first of three layouts whose natural width fits: each row's
legs on one line with the CO2 column, the legs one per line with it, the legs
one per line without it. No layout splits a flight designator across lines, and
a CO2 column left out is named by a note, its figures still in the JSON.
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
from rich.text import Text
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import Bags, Leg

if TYPE_CHECKING:
    from collections.abc import Callable

_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_LHR = "ds1_jfk_lhr_tfu.json"
_NOTE = "CO2 kg not shown: the table does not fit the output width; --format json carries it."
_LEGEND = "CO2 kg: Google's estimate"
_CO2_FIELDS = (
    "co2_emissions_g",
    "co2_emissions_typical_g",
    "co2_emissions_delta_pct",
    "emissions_tag",
)


def _parsed(name: str) -> list[gfid.GFlightWithId]:
    return [gfid._parse_flight_with_id(r) for r in gfid._rows_from_ds1(json.loads(_ds1(name))).rows]


_LHR_ROWS = _parsed(_LHR)
_ONE_WAY_LHR = (Leg.of("JFK", "LHR", _DEP),)
# The five boards, each with the width of its one-line table with CO2, in
# terminal cells: 📶 takes two.
_BOARDS: dict[str, tuple[list[Any], tuple[Leg, ...], int]] = {
    "lhr-12": (_LHR_ROWS[:12], _ONE_WAY_LHR, 87),
    "lhr-101": (_LHR_ROWS, _ONE_WAY_LHR, 99),
    "jfk-ewr-lhr": (
        _LHR_ROWS,
        (Leg(origins=("JFK", "EWR"), destinations=("LHR",), date=_DEP),),
        115,
    ),
    "lax-95": (_parsed("ds1_jfk_lax_tfu.json"), (Leg.of("JFK", "LAX", _DEP),), 91),
    "hnl-mia-rt": (
        [
            (o, r)
            for o in _parsed("ds1_metadata_blocks_kept.json")[:3]
            for r in _parsed("ds1_return_leg_pinned.json")[:3]
        ],
        (Leg.of("HNL", "MIA", _DEP), Leg.of("MIA", "HNL", _RET)),
        102,
    ),
}


def _render(
    monkeypatch: pytest.MonkeyPatch,
    results: list[Any],
    legs: tuple[Leg, ...],
    width: int,
    *,
    bags: Bags | None = None,
) -> str:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=width, no_color=True))
    cli._render_gflight_table(results, legs=legs, top_n=len(results), bags=bags)
    return buffer.getvalue()


def _table(text: str) -> list[str]:
    """The title and the table, without the lines printed under it."""
    lines = text.splitlines()
    return lines[: next(i for i, ln in enumerate(lines) if ln.startswith("└")) + 1]


def _header(text: str) -> list[str]:
    line = next(ln for ln in text.splitlines() if ln.startswith("┃"))
    return [c.strip() for c in line.strip("┃").split("┃")]


def _legs_cells(text: str) -> list[list[str]]:
    """Each table row's legs column, one entry per printed line, blank lines
    dropped. A row starts at the line whose `#` cell is filled."""
    at = _header(text).index("legs")
    rows: list[list[str]] = []
    for ln in text.splitlines():
        if not ln.startswith("│"):
            continue
        cells = [c.strip() for c in ln.strip("│").split("│")]
        if cells[0]:
            rows.append([])
        if cells[at]:
            rows[-1].append(cells[at])
    return rows


def _legs_beside_legroom(text: str) -> list[list[tuple[str, str]]]:
    """Each table row's printed lines as (legs, legroom) pairs. A row starts at
    the line whose `#` cell is filled."""
    head = _header(text)
    at_legs, at_room = head.index("legs"), head.index("legroom")
    rows: list[list[tuple[str, str]]] = []
    for ln in text.splitlines():
        if not ln.startswith("│"):
            continue
        cells = [c.strip() for c in ln.strip("│").split("│")]
        if cells[0]:
            rows.append([])
        rows[-1].append((cells[at_legs], cells[at_room]))
    return rows


def _members(results: list[Any]) -> list[Any]:
    """Every row the table prints, in its order: price order, a round trip's
    outbound before its return."""
    return [
        g
        for r in cli._price_ordered(results)
        for g in (cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,))
    ]


def _designators(g: Any) -> list[str]:
    return [cli._leg_display(leg, None, frozenset()) for leg in g.flight.legs]


def _stacked(g: Any, legs: tuple[Leg, ...]) -> list[str]:
    """A member's legs cell one part per line: the route on a multi-airport
    search, then each leg, every one but the last ending in an arrow."""
    shown = _designators(g)
    route = [cli._gflight_route(g.flight.legs)] if len(legs[0].origins) > 1 else []
    return route + [f"{d} →" for d in shown[:-1]] + shown[-1:]


def _split(text: str, results: list[Any], legs: tuple[Leg, ...]) -> list[str]:
    """Every designator, and every multi-airport route, not printed whole on one
    line of its own row's legs cell."""
    cells = _legs_cells(text)
    members = _members(results)
    assert len(cells) == len(members)
    multi = len(legs[0].origins) > 1
    split: list[str] = []
    for lines, g in zip(cells, members, strict=True):
        whole = [*_designators(g), *([cli._gflight_route(g.flight.legs)] if multi else [])]
        split += [
            w
            for w in whole
            if not any(re.search(rf"(?<!\S){re.escape(w)}(?!\S)", ln) for ln in lines)
        ]
    return split


def _misplaced(text: str, results: list[Any], legs: tuple[Leg, ...]) -> list[str]:
    """Every designator not printed on the line where its own leg's legroom
    starts, and every multi-airport route printed beside any legroom."""
    misplaced: list[str] = []
    for lines, g in zip(_legs_beside_legroom(text), _members(results), strict=True):
        amenities: list[Any] = g.amenities or []
        for k, (leg, shown) in enumerate(zip(g.flight.legs, _designators(g), strict=True)):
            own = Text.from_markup(cli._fmt_gflight_legroom([leg], amenities[k : k + 1])).plain
            beside = [room for line, room in lines if line.removesuffix(" →") == shown]
            if len(beside) != 1 or bool(beside[0]) != bool(own) or not own.startswith(beside[0]):
                misplaced.append(shown)
        if len(legs[0].origins) > 1:
            route = cli._gflight_route(g.flight.legs)
            if [room for line, room in lines if line == route] != [""]:
                misplaced.append(route)
    return misplaced


def _is_stacked(text: str, results: list[Any], legs: tuple[Leg, ...]) -> bool:
    return _legs_cells(text) == [_stacked(g, legs) for g in _members(results)]


def _is_one_line(text: str, results: list[Any], legs: tuple[Leg, ...]) -> bool:
    return _legs_cells(text) == [[" ".join(_stacked(g, legs))] for g in _members(results)]


@pytest.mark.parametrize("bags", [None, Bags(checked=1)], ids=["no-bags", "bags"])
@pytest.mark.parametrize("board", list(_BOARDS))
def test_no_designator_is_printed_across_two_lines_at_80_columns_or_more(
    monkeypatch: pytest.MonkeyPatch, board: str, bags: Bags | None
) -> None:
    results, legs, _ = _BOARDS[board]
    failures: dict[int, list[str]] = {}
    for width in range(80, 121, 2):
        text = _render(monkeypatch, results, legs, width, bags=bags)
        if split := _split(text, results, legs):
            failures[width] = split
        assert max(len(ln) for ln in _table(text)) <= width
        # A table wider than the console would be cropped at its right edge.
        assert all(ln.endswith("┓") for ln in text.splitlines() if ln.startswith("┏"))
        assert ("bags" in _header(text)) == (bags is not None)
    assert not failures


def _ticketed(results: list[Any]) -> list[Any]:
    """`results` with rows sold as separate tickets: every third one-way row,
    the two marks in turn, or on a round trip three outbounds as rows of their
    own, as Google lists them."""
    if all(isinstance(r, tuple) for r in results):
        outbounds = [cast("tuple[Any, ...]", r)[0] for r in results[::3]]
        return [*results, *((replace(o, ticketing="self_transfer"),) for o in outbounds)]
    return [
        replace(g, ticketing="self_transfer" if k % 2 else "separate_tickets") if k % 3 == 0 else g
        for k, g in enumerate(results)
    ]


def _price_cells(text: str) -> list[tuple[str, str]]:
    """(label, price) of every table line, the label blank on a row's later
    lines."""
    return [
        (cells[1].strip(), cells[2].strip())
        for ln in _table(text)
        if ln.startswith("│") and len(cells := ln.split("│")) > 2
    ]


@pytest.mark.parametrize("bags", [None, Bags(checked=1)], ids=["no-bags", "bags"])
@pytest.mark.parametrize("board", list(_BOARDS))
def test_a_separate_ticket_mark_stays_beside_its_price_at_80_columns_or_more(
    monkeypatch: pytest.MonkeyPatch, board: str, bags: Bags | None
) -> None:
    """The mark widens the price column the layouts measure, and no layout
    wraps it off its price or crops it."""
    results, legs, _ = _BOARDS[board]
    results = _ticketed(results)
    want: list[tuple[str, str]] = []
    for i, r in enumerate(cli._price_ordered(results), 1):
        items = cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,)
        for j, g in enumerate(items):
            label = f"{i}{'a' if j == 0 else 'b'}" if len(items) > 1 else str(i)
            price = "—" if g.flight.price is None else f"{g.flight.currency}{g.flight.price:.2f}"
            mark = {"self_transfer": " ‡", "separate_tickets": " †"}.get(g.ticketing or "", "")
            want.append((label, price + mark))
    assert any(p.endswith("‡") for _, p in want)
    for width in range(80, 121, 2):
        text = _render(monkeypatch, results, legs, width, bags=bags)
        assert max(len(ln) for ln in _table(text)) <= width
        assert all(ln.endswith("┓") for ln in text.splitlines() if ln.startswith("┏"))
        cells = _price_cells(text)
        assert [c for c in cells if c[0]] == want, width
        assert not [p for label, p in cells if not label and p], width


@pytest.mark.parametrize("bags", [None, Bags(checked=1)], ids=["no-bags", "bags"])
@pytest.mark.parametrize("board", list(_BOARDS))
def test_a_stacked_leg_prints_beside_its_own_legroom(
    monkeypatch: pytest.MonkeyPatch, board: str, bags: Bags | None
) -> None:
    """Stacked, a reader takes the legroom beside a designator as that flight's.
    A legroom line Rich wraps, or a route line above the first leg, must not
    move the next leg's designator beside another leg's legroom."""
    results, legs, _ = _BOARDS[board]
    failures: dict[int, list[str]] = {}
    stacked = 0
    for width in range(80, 121, 2):
        text = _render(monkeypatch, results, legs, width, bags=bags)
        if not _is_stacked(text, results, legs):
            continue
        stacked += 1
        if misplaced := _misplaced(text, results, legs):
            failures[width] = misplaced
    assert stacked
    assert not failures


@pytest.mark.parametrize("board", list(_BOARDS))
def test_a_table_that_fits_prints_one_line_legs_with_co2_as_before(
    monkeypatch: pytest.MonkeyPatch, board: str
) -> None:
    """At the one-line table's own width it prints the table a 200-column
    console prints, with the CO2 legend and no note; the lines under it wrap at
    the console's width as before. One column narrower, the legs stack."""
    results, legs, fits = _BOARDS[board]
    wide = _render(monkeypatch, results, legs, 200)
    assert _header(wide)[-2:] == ["legroom", "CO2 kg"]
    assert _is_one_line(wide, results, legs)
    at_fit = _render(monkeypatch, results, legs, fits)
    assert _table(at_fit) == _table(wide)
    assert _LEGEND in " ".join(at_fit.split())
    assert "--format json carries it" not in " ".join(at_fit.split())
    narrower = _render(monkeypatch, results, legs, fits - 1)
    assert _header(narrower)[-2:] == ["legroom", "CO2 kg"]
    assert _is_stacked(narrower, results, legs)


def test_at_80_columns_a_table_too_wide_with_co2_drops_it_with_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 101-row board stacked with CO2 is 82 columns, so the column goes and
    one line under the table says where its figures are."""
    text = _render(monkeypatch, _LHR_ROWS, _ONE_WAY_LHR, 80)
    assert "CO2 kg" not in _header(text)
    assert _is_stacked(text, _LHR_ROWS, _ONE_WAY_LHR)
    assert text.splitlines().count(_NOTE) == 1
    assert _LEGEND not in text


def test_at_80_columns_a_stacked_table_as_wide_as_the_console_keeps_co2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 12-row board stacked with CO2 is exactly 80 columns, and a table as
    wide as the console fits; at 78 it no longer does."""
    rows = _LHR_ROWS[:12]
    at_80 = _render(monkeypatch, rows, _ONE_WAY_LHR, 80)
    assert _header(at_80)[-2:] == ["legroom", "CO2 kg"]
    assert _is_stacked(at_80, rows, _ONE_WAY_LHR)
    assert max(len(ln) for ln in at_80.splitlines()) == 80
    assert _LEGEND in at_80
    assert "--format json carries it" not in at_80
    at_78 = _render(monkeypatch, rows, _ONE_WAY_LHR, 78)
    assert "CO2 kg" not in _header(at_78)
    assert _is_stacked(at_78, rows, _ONE_WAY_LHR)
    assert at_78.splitlines().count(_NOTE) == 1
    assert _LEGEND not in at_78


@pytest.mark.parametrize("width", [20, 40, 60])
def test_a_console_narrower_than_every_layout_still_prints_the_table(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """The last layout prints even when it does not fit, and Rich narrows it
    to the console rather than fail."""
    results, legs, _ = _BOARDS["jfk-ewr-lhr"]
    text = _render(monkeypatch, results, legs, width, bags=Bags(checked=1))
    assert max(len(ln) for ln in _table(text)) <= width
    assert text.splitlines().count(_NOTE) == 1


def test_a_board_without_co2_stacks_its_legs_and_prints_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No row states grams, so there is no column to drop: the one-line table is
    88 columns, the stacked one 71."""
    rows = [
        replace(g, flight=g.flight.model_copy(update=dict.fromkeys(_CO2_FIELDS))) for g in _LHR_ROWS
    ]
    text = _render(monkeypatch, rows, _ONE_WAY_LHR, 80)
    assert "CO2" not in text
    assert "--format json carries it" not in text
    assert _is_stacked(text, rows, _ONE_WAY_LHR)


def test_at_80_columns_the_json_carries_the_co2_the_table_leaves_out(
    gf_session: Callable[..., Any],
) -> None:
    page = _page(_answering(_ds1(_LHR), origin=None, destination=None, date=_DEP.isoformat()))
    args = [
        *("search", "--cash-only", "--no-google-url", "--no-matrix-url"),
        *("JFK", "LHR", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast"),
        *("-n", "101"),
    ]

    def run(*extra: str, columns: int) -> str:
        gf_session(page)
        result = CliRunner().invoke(cli.app, [*args, *extra], env={"COLUMNS": str(columns)})
        assert result.exit_code == 0, result.output
        return result.stdout

    table = run(columns=80)
    assert "CO2 kg" not in _header(table)
    assert max(len(ln) for ln in _table(table)) <= 80
    assert table.splitlines().count(_NOTE) == 1
    narrow = json.loads(run("--format", "json", columns=80))
    assert narrow == json.loads(run("--format", "json", columns=200))
    assert len(narrow) == 101
    assert sum(type(r["co2_emissions_g"]) is int for r in narrow) == 100
