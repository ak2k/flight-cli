# pyright: reportPrivateUsage=false
"""Every airline code reaches Google as that carrier.

fli's `Airline` enum files six codes as aliases of another carrier's member
(W9, Wizz Air UK, is W6, Wizz Air Hungary), a request writes a member's name,
and fli's row decoder has no entry for an alias. So a carrier a user types has
to reach the search page, the calendar graph's page and the grid filter as
itself, and a row Google sells or flies under one has to decode with it."""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fli.models.airline import Airline  # pyright: ignore[reportMissingTypeStubs]
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gf_calgraph, _gf_dategrid, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfPageShapeError
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, SpecificDateSearch
from flight_cli.fli_bridge import apply_gf_native_filters, to_fli_filter, unmappable_codes
from flight_cli.pp.gflight_adapter import _flight_id_string
from flight_cli.routing_predicates import CarrierPred, classify
from test_gf_airport_sets import _tfs
from test_links_search_tfs import _decode, _slices

if TYPE_CHECKING:
    from collections.abc import Callable

# fli's FlightSegment validator rejects a past travel date.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)

# As Google writes them: fli keys a digit-leading code `_1W`.
_ALIASES = sorted(
    key.removeprefix("_") for key, member in Airline.__members__.items() if member.name != key
)
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_GOOGLE = ["--backend", "gflight"]
_OUTBOUND = "ds1_jfk_lax_tfu.json"
_RETURN = "ds1_return_leg_pinned.json"
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "flight_cli"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_FIRST_FLIGHT = "1788"  # the capture's cheapest-listed row, one leg


def _carriers(url: str) -> list[list[str]]:
    """Per slice, the carrier include list (3.6)."""
    return [[code.decode() for code in sl.get(6, [])] for sl in _slices(_tfs(url))]


def _pinned_carriers(url: str) -> list[list[str]]:
    """Per slice, each pinned segment's carrier (3.4.5)."""
    return [[_decode(seg)[5][0].decode() for seg in sl.get(4, [])] for sl in _slices(_tfs(url))]


def _search_page(extension: str | None = None, routing: str | None = None) -> str:
    filters = to_fli_filter(SpecificDateSearch(legs=(Leg.of("LTN", "TIA", _DEP),)))
    assert apply_gf_native_filters(filters, classify(routing, extension).predicates)
    return gfid.search_page_url(filters)


def _calendar(extension: str) -> CalendarSearch:
    return CalendarSearch(
        legs=(Leg.of("LTN", "TIA", extension=extension),),
        window=CalendarWindow(
            start=_DEP, end=_DEP + timedelta(days=6), duration_min=0, duration_max=0
        ),
    )


def _legs(row: list[Any]) -> list[list[Any]]:
    return cast("list[list[Any]]", row[0][2])


def _board(code: str, *, every_row: bool) -> list[Any]:
    """The captured board answering LTN-TIA, its first row (or every row)
    flown by `code` and sold by no other carrier."""
    payload: list[Any] = json.loads(
        _answering(_ds1(_OUTBOUND), origin="LTN", destination="TIA", date=_DEP.isoformat())
    )
    rows = gfid._rows_from_ds1(payload).rows
    for row in rows if every_row else rows[:1]:
        for fl in _legs(row):
            fl[22][0] = code
            fl[15] = None
    return payload


def _parsed(payload: list[Any]) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(json.dumps(payload)), _URL, 200))


def _first_row_flown_by(code: str) -> tuple[list[Any], str]:
    """The board with its first row flown by `code` and sold as BA1234, and
    that row's flight id."""
    payload = _board(code, every_row=False)
    first = gfid._rows_from_ds1(payload).rows[0]
    fl = _legs(first)[0]
    fl[15] = [["BA", "1234", None, "British Airways"]]
    fl[18] = None
    return payload, first[0][17]


# ─────────────────────────────── requests ───────────────────────────────


@pytest.mark.parametrize("code", _ALIASES)
def test_an_aliased_include_asks_for_that_carrier(code: str) -> None:
    """fli's enum would ask for the carrier it aliases the code to, and its
    decoder has none for it, so the base sent such a search to Matrix."""
    predicates = classify(None, f"AIRLINES {code}").predicates
    assert unmappable_codes(predicates) == []
    assert cli._gf_unmappable_reasons(cli.BACKEND_AUTO, predicates) == []
    assert _carriers(_search_page(f"AIRLINES {code}")) == [[code]]


def test_both_carriers_of_a_pair_are_asked_for() -> None:
    assert _carriers(_search_page("AIRLINES W9 W6")) == [["W6", "W9"]]


def test_a_routing_carrier_or_flight_under_an_aliased_code_stays_on_google() -> None:
    for routing in ("MT+", "MT123"):
        predicates = classify(routing, None).predicates
        assert cli._gf_unmappable_reasons(cli.BACKEND_AUTO, predicates) == [], routing
    assert _carriers(_search_page(routing="MT+")) == [["MT"]]


def test_the_grid_gate_admits_an_aliased_include() -> None:
    assert _gf_dategrid.unwritten_constraint(classify(None, "AIRLINES W9").predicates) is None


def test_the_calendar_graph_page_asks_for_an_aliased_include() -> None:
    url = _gf_calgraph.page_url(_calendar("AIRLINES W9"), _DEP)
    assert _carriers(url) == [["W9"]]


def test_the_grid_filter_carries_an_aliased_include() -> None:
    """The grid RPC is gated (`_GRID_RPC_GATED`), so no request sends this
    filter; it is the filter lifting the gate would send."""
    filters = _gf_dategrid._grid_filters(
        _calendar("AIRLINES W9"),
        _DEP.isoformat(),
        (_DEP + timedelta(days=6)).isoformat(),
        list(classify(None, "AIRLINES W9").predicates),
    )
    encoded = filters.encode()
    assert "W9" in encoded
    assert "W6" not in encoded


def test_every_other_code_is_asked_for_by_flis_own_member() -> None:
    """So its request is the base's, byte for byte."""
    codes = sorted(
        key.removeprefix("_")
        for key, member in Airline.__members__.items()
        if member.name == key and len(key.removeprefix("_")) == 2
    )
    filters = SimpleNamespace()
    assert apply_gf_native_filters(
        filters, [CarrierPred(frozenset(codes), exclude=False, operating=False)]
    )
    asked: list[Any] = filters.airlines
    keys = [f"_{code}" if code[0].isdigit() else code for code in codes]
    wrong = [
        key for key, got in zip(keys, asked, strict=True) if got is not Airline.__members__[key]
    ]
    assert wrong == []


def test_an_alliance_asks_for_itself() -> None:
    assert _carriers(_search_page("ALLIANCE oneworld")) == [["ONEWORLD"]]


def test_a_carrier_fli_has_no_entry_for_stays_off_google() -> None:
    predicates = classify(None, "AIRLINES XX").predicates
    assert unmappable_codes(predicates) == ["XX"]
    assert cli._gf_unmappable_reasons(cli.BACKEND_AUTO, predicates) == [
        "a carrier Google Flights has no code for (XX)"
    ]


# ───────────────────────────────── rows ─────────────────────────────────


@pytest.mark.parametrize("code", _ALIASES)
def test_a_row_sold_under_an_aliased_code_is_kept_as_that_code(code: str) -> None:
    """fli's own decoder has no entry for the code and would fail the row."""
    payload = _board(code, every_row=False)
    flight_id = gfid._rows_from_ds1(payload).rows[0][0][17]
    board = _parsed(payload)
    assert len(board) == 95
    (row,) = [r for r in board if r.flight_id == flight_id]
    assert [_flight_id_string(leg) for leg in row.flight.legs] == [f"{code}{_FIRST_FLIGHT}"]
    operator = row.operating[0]
    assert operator is not None
    assert operator[0] is row.flight.legs[0].airline


def test_a_board_sold_wholly_under_an_aliased_code_is_kept() -> None:
    """The base failed every row and read the page as re-shaped."""
    board = _parsed(_board("W9", every_row=True))
    assert len(board) == 95
    assert {leg.airline.name for row in board for leg in row.flight.legs} == {"W9"}


def test_a_leg_flown_by_an_aliased_carrier_is_pinned_as_it() -> None:
    """The base read no operating identity off such a leg and pinned the
    codeshare number it is sold under."""
    payload, flight_id = _first_row_flown_by("Z0")
    (row,) = [r for r in _parsed(payload) if r.flight_id == flight_id]
    assert row.flight.legs[0].airline.name == "BA"
    operator = row.operating[0]
    assert operator is not None
    assert (operator[0].name, operator[1]) == ("Z0", _FIRST_FLIGHT)
    pinned = gfid._pinned_flight(row).legs[0]
    assert (pinned.airline.name, pinned.flight_number) == ("Z0", _FIRST_FLIGHT)


def test_a_code_fli_has_no_entry_for_still_fails_its_row() -> None:
    """The row is skipped, a board of nothing else is a re-shaped page, and an
    operator under such a code leaves the leg pinned as it is sold."""
    assert len(_parsed(_board("XX", every_row=False))) == 94
    with pytest.raises(GfPageShapeError, match=r"AttributeError\('XX'\)"):
        _parsed(_board("XX", every_row=True))

    payload, flight_id = _first_row_flown_by("XX")
    (row,) = [r for r in _parsed(payload) if r.flight_id == flight_id]
    assert row.operating[0] is None
    pinned = gfid._pinned_flight(row).legs[0]
    assert (pinned.airline.name, pinned.flight_number) == ("BA", "1234")


# ─────────────────────────────── the CLI ────────────────────────────────


def test_an_aliased_include_runs_on_google_and_prints_its_rows(
    gf_session: Callable[..., Any],
) -> None:
    """A GET for the board and one for its Cheapest tab, each asking for W9,
    and every row Google sells under it printed. The base refused the include
    for Google and exited 2 before any request."""
    fake = gf_session(_page(json.dumps(_board("W9", every_row=True))))
    args = [*_SEARCH, "LTN", "TIA", "--dep", _DEP.isoformat(), "--ext", "AIRLINES W9"]
    result = CliRunner().invoke(cli.app, [*args, *_GOOGLE, "--format", "json", "-n", "100"])
    assert [_carriers(url) for url in fake.gets] == [[["W9"]], [["W9"]]]
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 95


def test_the_table_shows_an_aliased_carrier(gf_session: Callable[..., Any]) -> None:
    gf_session(_page(json.dumps(_board("W9", every_row=True))))
    args = [*_SEARCH, "LTN", "TIA", "--dep", _DEP.isoformat(), "--ext", "AIRLINES W9"]
    result = CliRunner().invoke(
        cli.app, [*args, *_GOOGLE, "--fast", "-n", "5"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "W9 " in result.stdout


def test_a_round_trip_pins_an_aliased_carrier(gf_session: Callable[..., Any]) -> None:
    """Every return board is asked for with the outbound pinned under W9; the
    Cheapest tab, read last, pins nothing."""
    fake = gf_session(
        _page(json.dumps(_board("W9", every_row=True))),
        _page(_answering(_ds1(_RETURN), origin="TIA", destination="LTN", date=_RET.isoformat())),
    )
    dates = ["--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
    args = [*_SEARCH, "LTN", "TIA", *dates, *_GOOGLE, "--fast", "--format", "json", "-n", "2"]
    result = CliRunner().invoke(cli.app, args)
    assert len(fake.gets) > 2, result.output
    assert _pinned_carriers(fake.gets[0]) == [[], []]
    assert _pinned_carriers(fake.gets[-1]) == [[], []]
    for url in fake.gets[1:-1]:
        outbound, ret = _pinned_carriers(url)
        assert outbound
        assert set(outbound) == {"W9"}
        assert ret == []
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_a_digit_leading_carrier_prints_as_its_code(gf_session: Callable[..., Any]) -> None:
    """fli keys 2K as `_2K`; the table prints the code."""
    gf_session(_page(json.dumps(_board("2K", every_row=False))))
    args = [*_SEARCH, "LTN", "TIA", "--dep", _DEP.isoformat(), *_GOOGLE, "--fast", "-n", "100"]
    result = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert f"2K {_FIRST_FLIGHT}" in result.stdout
    assert "_2K" not in result.stdout


# ──────────────────────────────── the trap ──────────────────────────────


def _enum_lookups(tree: ast.AST) -> list[int]:
    nodes = list(ast.walk(tree))
    names = {"Airline", "FliAirline"} | {
        alias.asname
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "Airline" and alias.asname
    }

    # An annotation and `ItineraryKey` name the type, not a code, and the body of
    # `fli_airlines` is where `fli_bridge` builds its own table.
    roots: list[ast.AST] = []
    for node in nodes:
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.returns is not None:
                roots.append(node.returns)
            if isinstance(node, ast.FunctionDef) and node.name == "fli_airlines":
                roots.extend(node.body)
        elif isinstance(node, ast.TypeAlias) and node.name.id == "ItineraryKey":
            roots.append(node.value)
    exempt = {id(sub) for root in roots for sub in ast.walk(root)}
    return sorted(
        node.lineno
        for node in nodes
        if id(node) not in exempt
        and (
            (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names)
            or (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr == "Airline"
            )
            or (isinstance(node, ast.Name) and node.id == "_parse_airline")
            or (isinstance(node, ast.Attribute) and node.attr == "_parse_airline")
            or (isinstance(node, ast.alias) and node.name == "_parse_airline")
        )
    )


def test_no_code_resolves_an_airline_through_flis_enum() -> None:
    """The enum hands an aliased code another carrier's member and fli's
    decoder has no entry for one, so every load of the enum outside an
    annotation, the `ItineraryKey` alias and the body of `fli_airlines` fails,
    as does any use of `_parse_airline`."""
    found = [
        f"{path.relative_to(_SRC).as_posix()}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        for line in _enum_lookups(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert not found, f"airline lookups through fli's enum or decoder: {found}"


def test_the_allowlist_names_exist_only_where_the_source_defines_them() -> None:
    """The guard exempts `fli_airlines` and `ItineraryKey` by name, so a second
    definition elsewhere would be a hole."""
    found: dict[str, list[str]] = {"fli_airlines": [], "ItineraryKey": []}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.FunctionDef) and node.name == "fli_airlines":
                found["fli_airlines"].append(path.name)
            elif isinstance(node, ast.TypeAlias) and node.name.id == "ItineraryKey":
                found["ItineraryKey"].append(path.name)
    assert found == {"fli_airlines": ["fli_bridge.py"], "ItineraryKey": ["_gflight_ids.py"]}


@pytest.mark.parametrize(
    "source",
    [
        "from fli.models.airline import Airline as Carrier\nCarrier['W9']",
        "from fli.models import airline as carriers\ncarriers.Airline['W9']",
        "import fli.models\nfli.models.Airline.W9",
        "from fli.models import Airline\nAirline.__members__.get('W9')",
        "from fli.models import Airline as Carrier\ngetattr(Carrier, 'W9')",
    ],
)
def test_the_guard_sees_the_enum_under_any_name(source: str) -> None:
    """Each of these hands W9 the W6 member."""
    assert _enum_lookups(ast.parse(source)) == [2]
