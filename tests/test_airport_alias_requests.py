# pyright: reportPrivateUsage=false
"""Every airport code reaches Google as that airport.

fli's `Airport` enum files 48 codes as aliases of another airport's member
(OKA is NAH, Naha in Indonesia), and a request writes a member's name. So the
code a user types has to reach the search page, the calendar graph's page and
the grid filter as itself, and a row Google serves at it has to decode with
it. MLH is the exception: it is BSL's own airport, which Google serves only as
BSL."""

from __future__ import annotations

import ast
import base64
import json
import logging
import pathlib
import urllib.parse
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from fli.models.airport import Airport  # pyright: ignore[reportMissingTypeStubs]
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gf_calgraph, _gf_dategrid, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfPageShapeError
from flight_cli._metro import METRO_MEMBERS
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, SpecificDateSearch
from flight_cli.fli_bridge import apply_gf_native_filters, to_fli_filter
from flight_cli.routing_predicates import classify

if TYPE_CHECKING:
    from collections.abc import Callable

# fli's FlightSegment validator rejects a past travel date.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)

_ALIASES = sorted(code for code, member in Airport.__members__.items() if member.name != code)
_OWN = [code for code in _ALIASES if code != "MLH"]
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_GOOGLE = ["--backend", "gflight", "--fast"]
_JSON = ["--format", "json"]
_OUTBOUND = "ds1_jfk_lax_tfu.json"
_RETURN = "ds1_return_leg_pinned.json"
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "flight_cli"
_URL = "https://www.google.com/travel/flights?tfs=abc"

_VARINT_MASK = 0x7F
_VARINT_CONT = 0x80


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & _VARINT_MASK) << shift
        if not byte & _VARINT_CONT:
            return value, i
        shift += 7


def _decode(buf: bytes) -> dict[int, list[Any]]:
    """Flat {field: [values]} — varints as ints, length-delimited as bytes."""
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 0x07
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i : i + length], i + length
        else:
            raise AssertionError(f"unexpected wire type {wire} on field {field}")
        out.setdefault(field, []).append(value)
    return out


def _tfs(url: str) -> bytes:
    raw = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


Slice = tuple[list[str], list[str], list[tuple[str, str]]]


def _ends(url: str) -> list[Slice]:
    """Per slice: its origins (3.13), destinations (3.14) and the origin and
    destination of each pinned segment (3.4)."""
    out: list[Slice] = []
    for raw in _decode(_tfs(url)).get(3, []):
        sl = _decode(raw)
        origins = [_decode(e)[2][0].decode() for e in sl.get(13, [])]
        dests = [_decode(e)[2][0].decode() for e in sl.get(14, [])]
        pins = [(_decode(s)[1][0].decode(), _decode(s)[3][0].decode()) for s in sl.get(4, [])]
        out.append((origins, dests, pins))
    return out


def _one_way(origin: str, destination: str) -> SpecificDateSearch:
    return SpecificDateSearch(legs=(Leg.of(origin, destination, _DEP),))


def _calendar(origin: str, destination: str) -> CalendarSearch:
    return CalendarSearch(
        legs=(Leg.of(origin, destination),),
        window=CalendarWindow(
            start=_DEP, end=_DEP + timedelta(days=6), duration_min=0, duration_max=0
        ),
    )


def _page_at(fixture: str, origin: str, destination: str, day: date) -> str:
    """A captured board whose every row runs `origin` -> `destination` on `day`."""
    return _page(
        _answering(_ds1(fixture), origin=origin, destination=destination, date=day.isoformat())
    )


def _board_page(destination: str) -> PageFetch:
    """The captured LAX board, every row re-pointed to land at `destination`."""
    return PageFetch(_page_at(_OUTBOUND, "LAX", destination, _DEP), _URL, 200)


# ─────────────────────────────── requests ───────────────────────────────


@pytest.mark.parametrize("code", _OWN)
def test_the_search_page_asks_for_an_aliased_code_by_that_code(code: str) -> None:
    """fli's enum would ask for the airport it aliases the code to."""
    assert _ends(gfid.search_page_url(to_fli_filter(_one_way("LAX", code)))) == [
        (["LAX"], [code], [])
    ]
    assert _ends(gfid.search_page_url(to_fli_filter(_one_way(code, "LAX")))) == [
        ([code], ["LAX"], [])
    ]


def test_mlh_is_asked_for_as_bsl() -> None:
    """MLH is BSL's own airport, which Google serves only as BSL."""
    assert _ends(gfid.search_page_url(to_fli_filter(_one_way("JFK", "MLH")))) == [
        (["JFK"], ["BSL"], [])
    ]
    assert _ends(gfid.search_page_url(to_fli_filter(_one_way("MLH", "JFK")))) == [
        (["BSL"], ["JFK"], [])
    ]


def test_every_other_code_is_asked_for_as_itself() -> None:
    """A code fli names canonically asks for itself. Metro codes expand to
    their members, and LAX is the leg's other end."""
    wrong: list[tuple[str, list[str]]] = []
    for code, member in Airport.__members__.items():
        if member.name != code or code == "LAX" or code in METRO_MEMBERS:
            continue
        got = _ends(gfid.search_page_url(to_fli_filter(_one_way("LAX", code))))[0][1]
        if got != [code]:
            wrong.append((code, got))
    assert wrong == []


def test_two_airports_fli_files_under_one_member_make_a_search() -> None:
    """fli's enum files NTL and NCL under one member, which its segment
    validator would refuse as one airport at both ends."""
    assert _ends(gfid.search_page_url(to_fli_filter(_one_way("NTL", "NCL")))) == [
        (["NTL"], ["NCL"], [])
    ]


def test_a_connection_at_an_aliased_airport_keeps_its_code() -> None:
    """The layover filter asks for the connecting airport typed."""
    filters = to_fli_filter(_one_way("LAX", "HND"))
    assert apply_gf_native_filters(filters, classify("F* X:OKA F*", None).predicates)
    assert [a.name for a in filters.layover_restrictions.airports] == ["OKA"]


def test_the_calendar_graph_page_asks_for_an_aliased_code() -> None:
    """The price graph's page asks for the airport typed."""
    url = _gf_calgraph.page_url(_calendar("LAX", "OKA"), _DEP)
    assert _ends(url)[0][:2] == (["LAX"], ["OKA"])


def test_the_grid_filter_carries_an_aliased_code() -> None:
    """The grid RPC is gated (`_GRID_RPC_GATED`), so no request sends this
    filter; it is the filter lifting the gate would send."""
    filters = _gf_dategrid._grid_filters(
        _calendar("LAX", "OKA"), _DEP.isoformat(), (_DEP + timedelta(days=6)).isoformat(), []
    )
    leg = filters.flight_segments[0]
    assert (leg.departure_airport[0][0].name, leg.arrival_airport[0][0].name) == ("LAX", "OKA")
    assert "'OKA'" in str(filters.format())
    assert "'NAH'" not in str(filters.format())


def test_an_aliased_code_is_not_refused_and_an_unknown_one_is() -> None:
    """The check reads the bridge's own table, so it refuses before any
    request exactly the codes the bridge has no member for."""
    assert cli._gf_unserveable_reasons(cli.BACKEND_GFLIGHT, "LAX", "OKA") == []
    assert cli._gf_unserveable_reasons(cli.BACKEND_GFLIGHT, "LAX", "QQQ") == [
        "a city code rather than an airport (QQQ)"
    ]


# ───────────────────────────────── rows ─────────────────────────────────


def test_rows_landing_at_an_aliased_airport_keep_its_code() -> None:
    """fli's own row decoder has no entry for an aliased code, and would fail
    every row and read the page as re-shaped."""
    board = gfid._rows_from_page_html(_board_page("OKA"))
    assert len(board) == 95
    assert {row.flight.legs[-1].arrival_airport.name for row in board} == {"OKA"}


def test_a_row_at_an_unknown_airport_fails_with_flis_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Such a row fails through fli's decoder, which logs it, and is skipped;
    a board of nothing else is a re-shaped page."""
    payload: list[Any] = json.loads(_ds1(_OUTBOUND))
    row = gfid._rows_from_ds1(payload).rows[0]
    cast("list[list[Any]]", row[0][2])[-1][6] = "QQQ"
    with caplog.at_level(logging.WARNING):
        board = gfid._rows_from_page_html(PageFetch(_page(json.dumps(payload)), _URL, 200))
    assert len(board) == 94
    assert "Unknown airport IATA code 'QQQ'" in caplog.text

    with pytest.raises(GfPageShapeError, match=r"AttributeError\('QQQ'\)"):
        gfid._rows_from_page_html(_board_page("QQQ"))


# ─────────────────────────────── the CLI ────────────────────────────────


@pytest.mark.parametrize("code", ["OKA", "NTL", "TRI"])
def test_a_search_asks_for_and_prints_the_typed_airport(
    gf_session: Callable[..., Any], code: str
) -> None:
    """One GET of the board and one of its Cheapest tab, both for the code
    typed rather than NAH, NCL or PSC, and every row Google serves there
    printed."""
    fake = gf_session(_page_at(_OUTBOUND, "LAX", code, _DEP))
    args = [*_SEARCH, "LAX", code, "--dep", _DEP.isoformat(), *_GOOGLE, *_JSON, "-n", "100"]
    result = CliRunner().invoke(cli.app, args)
    assert [_ends(url) for url in fake.gets] == [[(["LAX"], [code], [])]] * 2
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 95


def test_the_table_routes_to_the_typed_airport(gf_session: Callable[..., Any]) -> None:
    """The route names the airport typed, not the one fli aliases it to."""
    gf_session(_page_at(_OUTBOUND, "LAX", "OKA", _DEP))
    args = [*_SEARCH, "LAX", "OKA", "--dep", _DEP.isoformat(), *_GOOGLE, "-n", "5"]
    result = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "→OKA" in result.stdout
    assert "NAH" not in result.stdout


def test_a_round_trip_pins_and_pairs_at_the_typed_airport(
    gf_session: Callable[..., Any],
) -> None:
    """The first GET asks for both slices at OKA, each return board pins an
    outbound landing there, and the Cheapest tab is asked the first question."""
    fake = gf_session(
        _page_at(_OUTBOUND, "LAX", "OKA", _DEP), _page_at(_RETURN, "OKA", "LAX", _RET)
    )
    dates = ["--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
    args = [*_SEARCH, "LAX", "OKA", *dates, *_GOOGLE, *_JSON, "-n", "2"]
    result = CliRunner().invoke(cli.app, args)
    asked = [_ends(url) for url in fake.gets]
    assert asked[0] == [(["LAX"], ["OKA"], []), (["OKA"], ["LAX"], [])]
    assert len(asked) > 2
    assert asked[-1] == asked[0]
    for out_slice, ret_slice in asked[1:-1]:
        assert out_slice[:2] == (["LAX"], ["OKA"])
        assert out_slice[2]
        assert out_slice[2][-1][1] == "OKA"
        assert ret_slice[:2] == (["OKA"], ["LAX"])
    assert result.exit_code == 0, result.output
    pairs: list[Any] = json.loads(result.stdout)
    assert pairs
    assert all(isinstance(pair, list) and len(cast("list[Any]", pair)) == 2 for pair in pairs)


# ──────────────────────────────── the trap ──────────────────────────────


def _enum_lookups(tree: ast.AST) -> list[int]:
    nodes = list(ast.walk(tree))

    def imported_as(name: str) -> set[str]:
        return {
            alias.asname
            for node in nodes
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == name and alias.asname
        }

    names = {"Airport", "FliAirport"} | imported_as("Airport")
    parsers = {"_parse_airport"} | imported_as("_parse_airport")

    def is_enum(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr == "Airport"
        )

    def is_parser(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in parsers) or (
            isinstance(node, ast.Attribute) and node.attr == "_parse_airport"
        )

    members = set(Airport.__members__)
    # Iterating the whole table is how `fli_bridge` builds its own.
    iterated = {
        id(node.value) for node in nodes if isinstance(node, ast.Attribute) and node.attr == "items"
    }
    # `_leg_airport` decodes only a code missing from fli's table, which cannot alias.
    unaliased = {
        id(inner)
        for node in nodes
        if isinstance(node, ast.FunctionDef) and node.name == "_leg_airport"
        for inner in ast.walk(node)
    }
    lines: list[int] = []
    for node in nodes:
        if (
            (isinstance(node, ast.Subscript) and is_enum(node.value))
            or (isinstance(node, ast.Attribute) and is_enum(node.value) and node.attr in members)
            or (
                isinstance(node, ast.Attribute)
                and node.attr == "__members__"
                and is_enum(node.value)
                and id(node) not in iterated
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr"}
                and node.args
                and is_enum(node.args[0])
            )
            or (isinstance(node, ast.Call) and is_parser(node.func) and id(node) not in unaliased)
        ):
            lines.append(node.lineno)
    return lines


def test_no_code_resolves_an_airport_through_flis_enum() -> None:
    """The enum hands an aliased code another airport's member, so every
    lookup goes through `fli_bridge.fli_airport`: a subscript, a member read,
    `__members__` outside `.items()`, `getattr`/`hasattr`, or a
    `_parse_airport` call outside `_leg_airport`, under any import name. Read
    from the syntax tree, so prose naming the enum does not match."""
    found = [
        f"{path.relative_to(_SRC).as_posix()}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        for line in _enum_lookups(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert not found, f"airport lookups through fli's enum: {found}"
