# pyright: reportPrivateUsage=false
"""The full Google Flights board and what is served from it.

The page URL asks for every row (`tfu=`), so `-n` above Google's top ~30 is
honored, the Tier-2 carrier predicates are filtered on the complete board, and
the page's own price insight prints under the table. The two `*_tfu.json`
captures are the `ds:1` of those full pages: JFK-LAX (95 rows) and JFK-LHR
(101 rows, 4 unpriced, carrying the codeshare rows the carrier filters rest on).
"""

from __future__ import annotations

import base64
import itertools
import json
import urllib.parse
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page, _unpriced
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin, Leg, SearchOptions

if TYPE_CHECKING:
    from collections.abc import Callable

# fli's own validator rejects a past travel date, so these are derived rather
# than pinned: a literal rots the suite on the day it passes.
_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_LAX = "ds1_jfk_lax_tfu.json"
_LHR = "ds1_jfk_lhr_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]


def _served(name: str, *, day: date = _DEP) -> str:
    return _page(_answering(_ds1(name), origin=None, destination=None, date=day.isoformat()))


def _board(html: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


def _booked(row: Any) -> str:
    return "+".join(f"{leg.airline.name}{leg.flight_number}" for leg in row.flight.legs)


def _json_booked(member: dict[str, Any]) -> str:
    """A dumped row's legs as `<airline>|<number>`, fli dumping the airline's name."""
    return "+".join(f"{leg['airline']}|{leg['flight_number']}" for leg in member["legs"])


def _tfs(url: str) -> bytes:
    raw = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


# ─────────────────────────────── full board ────────────────────────────────


def test_every_search_page_asks_for_the_full_board_exactly_once() -> None:
    """Without the show-all bit the page inlines Google's top ~30 rows, so a
    bigger `-n` comes back short and a post-filter answers from part of the
    board. Pinned pages go through the same builder."""
    board = _board(_page(_ds1("ds1_jfk_lax_3rows.json")))
    filters = _round_trip_filters()
    url = gfid.search_page_url(filters)
    assert url.count("tfu=EgQIABABIgA") == 1
    filters.flight_segments[0].selected_flight = board[0].flight
    assert gfid.search_page_url(filters).count("tfu=") == 1


@pytest.mark.parametrize(("name", "rows", "unpriced"), [(_LAX, 95, 0), (_LHR, 101, 4)])
def test_a_full_board_parses_every_row(name: str, rows: int, unpriced: int) -> None:
    board = _board(_page(_ds1(name)))
    assert len(board) == rows
    assert sum(r.flight.price is None for r in board) == unpriced
    keys = {gfid._itinerary_key(r) for r in board}
    assert len(keys) == rows  # no two rows on either capture are one itinerary


# ───────────────────────────────── dedupe ─────────────────────────────────


def _with_copy_of_first_row(*, price: int | None, first: bool) -> str:
    """The LAX capture with its first row listed a second time: at `price`, or
    unpriced for None, placed ahead of the original when `first`."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    rows: list[Any] = payload[2][0]
    copy: Any = json.loads(json.dumps(rows[0]))
    if price is None:
        copy[1][0] = []  # Google's "no shopping-list price" marker; see conftest._unpriced
    else:
        copy[1][0][1] = price
    rows.insert(0 if first else len(rows), copy)
    return _page(json.dumps(payload))


def test_an_itinerary_listed_twice_keeps_the_cheaper_fare() -> None:
    """Google can list one itinerary twice at two prices; only the cheaper is
    on offer, and the first listing keeps its place in board order."""
    original = _board(_page(_ds1(_LAX)))[0]
    for first in (False, True):
        board = _board(_with_copy_of_first_row(price=999, first=first))
        assert len(board) == 95
        assert _booked(board[0]) == _booked(original)
        assert board[0].flight.price == original.flight.price == 204


def test_a_priced_listing_beats_an_unpriced_one() -> None:
    board = _board(_with_copy_of_first_row(price=None, first=True))
    assert len(board) == 95
    assert board[0].flight.price == 204


def test_the_same_flight_numbers_a_day_apart_are_two_itineraries() -> None:
    """Dates are part of the key: FI614+FI450 flies twice on the LHR capture,
    its connection a day apart, and both are real trips."""
    board = _board(_page(_ds1(_LHR)))
    fi = [r for r in board if _booked(r) == "FI614+FI450"]
    assert len(fi) == 2
    assert fi[0].flight.legs[1].departure_datetime != fi[1].flight.legs[1].departure_datetime


# ──────────────────────────────── -n above 30 ─────────────────────────────


def test_n_above_thirty_returns_the_rows_the_full_board_holds(
    gf_session: Callable[..., Any],
) -> None:
    fake = gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "-n",
            "60",
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    rows: list[Any] = json.loads(result.stdout)
    assert len(rows) == 60
    assert len({_json_booked(r) + str(r["legs"][0]["departure_datetime"]) for r in rows}) == 60
    assert len(fake.gets) == 1
    assert "tfu=EgQIABABIgA" in fake.gets[0]


# ─────────────────────────── carrier-exclude meaning ──────────────────────


def test_a_carrier_exclude_reads_the_carrier_each_leg_is_booked_under(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Matrix answers `~BA+` on this route with AA142/AA106 at $295 and AA6939
    (BA178's metal, sold by AA): a fare booked under another carrier stays even
    when BA also sells the flight. AA100 is AA-sold and AA-operated, with BA among
    its other sellers; AA142 on this capture is booked as BA1594 and goes."""
    gf_session(_served(_LHR))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP, route_language="~BA+"),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=200,
        json_out=True,
    )
    rows: list[Any] = json.loads(capsys.readouterr().out)
    kept = {_json_booked(r) for r in rows}
    assert "American Airlines|100" in kept
    assert "American Airlines|6939" in kept
    assert "British Airways|1594" not in kept
    assert not any("British Airways" in k for k in kept)
    assert len(rows) == 84  # of 101


def _lhr_with_first_leg_edited(booked: str, edit: Callable[[list[Any]], None]) -> str:
    """The LHR capture with `edit` applied to the first leg of the row booked
    as `booked`."""
    payload: list[Any] = json.loads(_ds1(_LHR))
    for raw in gfid._rows_from_ds1(payload).rows:
        if _booked(gfid._parse_flight_with_id(raw)) == booked:
            edit(raw[0][2][0])
            break
    else:
        pytest.fail(f"{booked} is not on the capture")
    return _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )


def _lhr_without_operating_identity(booked: str) -> str:
    """The LHR capture with the first leg of the row booked as `booked`
    carrying no operating tuple (`fl[22]`)."""

    def _drop(leg: list[Any]) -> None:
        leg[22] = None

    return _lhr_with_first_leg_edited(booked, _drop)


@pytest.mark.parametrize("extension", ["-CODESHARE", "-OPAIRLINES VS"])
def test_a_leg_with_no_operating_identity_fails_an_operating_filter(
    extension: str, gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """AF9656 is VS26 sold by Air France. Without its operating tuple nothing
    says whether it is a codeshare or flown by VS, and a filter that cannot
    tell must not answer with a row Matrix drops."""
    gf_session(_lhr_without_operating_identity("AF9656"))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP, extension=extension),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=200,
        json_out=True,
    )
    kept = {_json_booked(r) for r in json.loads(capsys.readouterr().out)}
    assert kept, "the filter answered with nothing"
    assert "Air France|9656" not in kept


@pytest.mark.parametrize(("routing", "extension"), [("~AA+", None), (None, "-AIRLINES AA")])
def test_a_leg_with_no_booking_flight_number_fails_a_marketing_exclude(
    routing: str | None,
    extension: str | None,
    gf_session: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AA100 is booked and flown by AA. Without its flight number the booking
    carrier cannot be read off the leg, and an exclude that cannot tell must
    not answer with a row Matrix drops."""

    def _no_number(leg: list[Any]) -> None:
        leg[22][1] = None

    gf_session(_lhr_with_first_leg_edited("AA100", _no_number))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP, route_language=routing, extension=extension),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=200,
        json_out=True,
    )
    kept = {_json_booked(r) for r in json.loads(capsys.readouterr().out)}
    assert kept, "the filter answered with nothing"
    assert not any("American Airlines" in k for k in kept)


# ────────────────────────── round trip: filter, then pin ──────────────────


def _round_trip_filters() -> Any:
    from flight_cli.fli_bridge import to_fli_filter

    return to_fli_filter(
        cli.SpecificDateSearch(legs=_round_trip(), options=SearchOptions(cabin=Cabin.COACH))
    )


def _round_trip(routing: str | None = None) -> tuple[Leg, ...]:
    return (
        Leg.of("JFK", "LHR", _DEP, route_language=routing),
        Leg.of("LHR", "JFK", _RET, route_language=routing),
    )


def _lhr_board_with_ba_first() -> str:
    """The LHR capture with every BA-booked row moved to the top of the board,
    the order in which the round trip takes its pins."""
    payload: list[Any] = json.loads(_ds1(_LHR))
    rows = gfid._rows_from_ds1(payload).rows
    parsed = [gfid._parse_flight_with_id(r) for r in rows]
    ba = [raw for raw, p in zip(rows, parsed, strict=True) if p.flight.legs[0].airline.name == "BA"]
    rest = [
        raw for raw, p in zip(rows, parsed, strict=True) if p.flight.legs[0].airline.name != "BA"
    ]
    assert len(ba) >= 5
    payload[2] = [ba + rest]
    payload[3] = None
    return _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )


def _return_board() -> str:
    return _page(
        _answering(
            _ds1("ds1_return_leg_pinned.json"),
            origin="LHR",
            destination="JFK",
            date=_RET.isoformat(),
        )
    )


def test_the_outbound_is_filtered_before_the_pins_are_taken(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The pins are the first rows in board order. Filtering after pinning
    spends every pin on a BA outbound the filter then drops, and a satisfiable
    `~BA+` round trip answers "no results" with non-BA outbounds right below."""
    fake = gf_session(_lhr_board_with_ba_first(), _return_board())
    cli._run_gflight_path(
        legs=_round_trip("~BA+"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1,
        json_out=True,
    )
    rows: list[Any] = json.loads(capsys.readouterr().out)
    assert rows, "a satisfiable round trip answered with nothing"
    assert all(len(r) == 2 for r in rows)
    assert not any("British Airways" in _json_booked(m) for r in rows for m in r)
    assert len(fake.gets) == 2  # the outbound, then one pin


def test_a_codeshare_booked_pin_names_the_operating_flight(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Pinned under the codeshare number it is booked as, a leg comes back with
    no return board; pinned as the flight that operates it, the board is served.
    The row itself keeps the booking identity the table and document show."""
    fake = gf_session(_served(_LHR), _return_board())
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1,
        json_out=True,
    )
    rows: list[Any] = json.loads(capsys.readouterr().out)
    assert _json_booked(rows[0][0]) == "Air France|9656"  # the capture's first row, VS26 metal
    pinned = _tfs(fake.gets[1])
    assert b"VS" in pinned and b"26" in pinned
    assert b"9656" not in pinned


def test_a_return_board_the_routing_empties_is_counted_and_routed_to_matrix(
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every return on the served board is AA, so `~AA+` leaves the pinned
    outbound with no return: counted where the pins are accounted for, and the
    empty answer handed on rather than printed as Google's."""
    gf_session(_served(_LHR), _return_board())
    unmatched = cli._run_gflight_path(
        legs=_round_trip("~AA+"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1,
        json_out=True,
        matrix_fallback=True,
    )
    assert unmatched  # the rows the filter removed, outbound and return
    assert "1 of 1 pinned outbounds have no return flight matching the routing" in caplog.text


# ─────────────────────────── the empty filtered answer ────────────────────


def test_under_auto_an_answer_the_routing_emptied_goes_to_matrix_with_the_reason(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served(_LAX))
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--routing",
            "O:LH+",
            "--fast",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]
    assert result.stdout == ""  # the Matrix path, stubbed here, writes the document
    assert (
        "Using Matrix: no Google Flights itinerary matched an operating carrier filter (LH) "
        "(95 rows"
    ) in " ".join(result.stderr.split())


def test_under_explicit_gflight_it_says_why_and_the_document_is_empty(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served(_LAX))

    def _matrix(**_kw: object) -> None:
        pytest.fail("the search went to Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--routing",
            "O:LH+",
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "no itinerary matched an operating carrier filter (LH) (95 rows filtered out)" in (
        " ".join(result.stderr.split())
    )


# ───────────── constraints the page encodes, held on the rows too ─────────────


def _search_json(*extra: str) -> list[str]:
    return [
        *_SEARCH,
        "JFK",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "100",
        *extra,
    ]


def _no_matrix(**_kw: object) -> None:
    pytest.fail("the search went to Matrix")


def _legs(member: dict[str, Any]) -> list[dict[str, Any]]:
    return member["legs"]


def _clock(stamp: str) -> str:
    return stamp[11:16]


def _gaps(member: dict[str, Any]) -> list[float]:
    legs = _legs(member)
    return [
        (
            datetime.fromisoformat(b["departure_datetime"])
            - datetime.fromisoformat(a["arrival_datetime"])
        ).total_seconds()
        / 60
        for a, b in itertools.pairwise(legs)
    ]


def _within_380_min(member: dict[str, Any]) -> bool:
    return member["duration"] <= 380


def _layovers_of_two_hours(member: dict[str, Any]) -> bool:
    return all(gap >= 120 for gap in _gaps(member))


def _departs_in_the_morning(member: dict[str, Any]) -> bool:
    return "08:00" <= _clock(_legs(member)[0]["departure_datetime"]) <= "11:00"


def _sold_as_aa(member: dict[str, Any]) -> bool:
    return all(
        leg["airline"] == "American Airlines" or "AA" in leg["amenities"]["marketing_carriers"]
        for leg in _legs(member)
    )


@pytest.mark.parametrize(
    ("extra", "fields", "holds"),
    [
        (("--routing", "AA+"), {6: [b"AA"]}, _sold_as_aa),
        (("--ext", "MAXDUR 6:20"), {12: [380]}, _within_380_min),
        (("--ext", "MINCONNECT 2:00"), {17: [120]}, _layovers_of_two_hours),
        (
            ("--depart-times", "morning"),
            {8: [8], 9: [11], 10: [0], 11: [23]},
            _departs_in_the_morning,
        ),
    ],
    ids=["carrier", "duration", "layover", "departure-window"],
)
def test_an_encoded_constraint_is_asked_of_the_page_and_held_on_its_rows(
    extra: tuple[str, ...],
    fields: dict[int, list[Any]],
    holds: Callable[[dict[str, Any]], bool],
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google has ignored a field it was sent, so the captured board (served
    whatever the tfs= said) stands in for a page that ignored this one: every
    row it prints still holds, and the page was asked."""
    fake = gf_session(_served(_LAX))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search_json(*extra))
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    assert 0 < len(rows) < 95
    assert all(holds(m) for m in rows)
    sl = _decode_slice(_tfs(fake.gets[0]))
    assert {f: sl.get(f) for f in fields} == fields


@pytest.mark.parametrize(
    ("stops", "extension"),
    [("0", "MAXSTOPS 2"), ("3", "MAXSTOPS 0"), ("0", "MAXSTOPS 3")],
    ids=["looser-ext", "looser-flag-past-the-ceiling", "looser-ext-past-the-ceiling"],
)
def test_the_strictest_stop_limit_is_the_one_asked(
    stops: str,
    extension: str,
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonstop limit asks the page for nonstops beside any looser one, even
    one past the stop ceiling the page can encode."""
    fake = gf_session(_served(_LAX))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search_json("--stops", stops, "--ext", extension))
    assert result.exit_code == 0, result.output
    assert _decode_slice(_tfs(fake.gets[0]))[5] == [0]


def test_a_child_is_priced_as_a_child(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = gf_session(_served(_LAX))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search_json("--children", "1"))
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 95
    assert _decode_fields(_tfs(fake.gets[0]))[8] == [1, 2]


def _lax_departing_after(hour: int) -> str:
    """The JFK-LAX capture cut to the rows that leave at `hour` or later."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    rows = gfid._rows_from_ds1(payload).rows
    parsed = [gfid._parse_flight_with_id(r) for r in rows]
    late = [
        raw
        for raw, p in zip(rows, parsed, strict=True)
        if p.flight.legs[0].departure_datetime.hour >= hour
    ]
    payload[2] = [late]
    payload[3] = None
    return _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )


def test_a_departure_window_alone_that_empties_the_board_names_itself(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_lax_departing_after(17))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = CliRunner().invoke(cli.app, _search_json("--depart-times", "morning"))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    printed = " ".join(result.stderr.split())
    assert "no itinerary matched a departure-time window (morning) (" in printed, printed


def test_under_auto_every_check_that_emptied_the_board_is_named(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served(_LAX))
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    args = [a for a in _search_json("--routing", "AA+", "--ext", "MAXDUR 1:00") if a != "gflight"]
    args.remove("--backend")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert ran == [True]
    printed = " ".join(result.stderr.split())
    assert (
        "Using Matrix: no Google Flights itinerary matched a carrier filter (AA) and a maximum "
        "trip duration (60 min) (95 rows filtered out)"
    ) in printed, printed


def _decode_fields(buf: bytes) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        field, wire = tag >> 3, tag & 0x07
        if wire == 0:
            value, i = _read_varint(buf, i)
        else:
            length, i = _read_varint(buf, i)
            value, i = buf[i : i + length], i + length
        out.setdefault(field, []).append(value)
    return out


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def _decode_slice(buf: bytes) -> dict[int, list[Any]]:
    return _decode_fields(_decode_fields(buf)[3][0])


def _flightless() -> str:
    return _page(_ds1("ds1_flightless_board.json"))


def _round_trip_search(*extra: str) -> list[str]:
    return [
        *_SEARCH,
        "JFK",
        "LHR",
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        "--fast",
        *extra,
    ]


def test_under_auto_a_round_trip_whose_returns_google_left_empty_goes_to_matrix(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~AA+` removed 17 outbound rows before the pins were taken, and Google
    served every pinned return board empty. Those rows are why the answer is
    empty, so it goes to Matrix rather than being printed as Google's."""
    gf_session(_served(_LHR), _flightless())
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app, _round_trip_search("--routing", "~AA+", "-n", "3", "--format", "json")
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]
    assert result.stdout == ""
    assert (
        "Using Matrix: no Google Flights itinerary matched a carrier exclusion (AA) (17 rows"
        in (" ".join(result.stderr.split()))
    )


def test_under_explicit_gflight_a_round_trip_whose_returns_google_left_empty_says_why(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served(_LHR), _flightless())

    def _matrix(**_kw: object) -> None:
        pytest.fail("the search went to Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        _round_trip_search(
            "--routing", "~AA+", "-n", "3", "--backend", "gflight", "--format", "json"
        ),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert (
        "no round trip matched a carrier exclusion (AA) (17 rows filtered out; "
        "returns were searched for the first 3 outbound options)"
    ) in " ".join(result.stderr.split())


def test_a_round_trip_the_routing_emptied_names_the_outbounds_it_tried(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every return on the served board is AA, so under `~AA+` both pinned
    outbounds come back with no return. The outbounds below the pins were never
    tried, and the line says how many were rather than reading as the answer
    for the whole board."""
    fake = gf_session(_served(_LHR), _return_board())

    def _matrix(**_kw: object) -> None:
        pytest.fail("the search went to Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        _round_trip_search(
            "--routing", "~AA+", "-n", "2", "--backend", "gflight", "--format", "json"
        ),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert (
        "no round trip matched a carrier exclusion (AA) (23 rows filtered out; "
        "returns were searched for the first 2 outbound options)"
    ) in " ".join(result.stderr.split())
    assert len(fake.gets) == 3  # the outbound, then two pins


@pytest.mark.parametrize("outbound_served", [False, True])
def test_with_no_filter_a_round_trip_google_served_empty_is_no_results(
    outbound_served: bool, gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google's empty is its answer, whether the outbound board or every pinned
    return board was the one served empty: no routing reason, and no Matrix."""
    gf_session(*((_served(_LHR), _flightless()) if outbound_served else (_flightless(),)))

    def _matrix(**_kw: object) -> None:
        pytest.fail("the search went to Matrix")

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(cli.app, _round_trip_search("-n", "2"))
    assert result.exit_code == 0, result.output
    assert "Google Flights: no results." in result.stdout
    assert "routing" not in result.stderr


# ──────────────────────────────── price insight ───────────────────────────


@pytest.mark.parametrize(
    ("name", "cheapest", "low", "high"), [(_LAX, 204, 85, 225), (_LHR, 293, 170, 295)]
)
def test_the_price_insight_is_read_off_the_page(
    name: str, cheapest: float, low: float, high: float
) -> None:
    """`ds:1[5][1]` is the board's cheapest priced row on both captures."""
    board = _board(_page(_ds1(name)))
    assert board.insight == gfid.PriceInsight(
        cheapest=cheapest, typical_low=low, typical_high=high, currency="USD"
    )
    assert board.insight is not None
    assert board.insight.level == "typical"
    assert min(r.flight.price for r in board if r.flight.price is not None) == cheapest


@pytest.mark.parametrize(
    ("block", "level"),
    [
        # A live JFK-LHR round-trip outbound: cheapest 780 above the typical 485-680.
        ([5, [None, 780], [None, 613], [None, -168], [None, 485], [None, 680]], "high"),
        ([4, [None, 150], [None, 0], [None, 0], [None, 170], [None, 295]], "low"),
        ([4, [None, 170], [None, 0], [None, 0], [None, 170], [None, 295]], "typical"),
    ],
)
def test_the_level_is_derived_from_the_numbers(block: list[Any], level: str) -> None:
    payload: list[Any] = json.loads(_ds1(_LAX))
    payload[5] = block
    board = _board(_page(json.dumps(payload)))
    assert board.insight is not None
    assert board.insight.level == level


def test_the_insight_currency_comes_from_a_priced_row() -> None:
    """Unpriced rows carry no currency, and the board's first row may be one."""
    board = _board(_page(_unpriced(_ds1(_LHR), index=0)))
    assert board[0].flight.price is None
    assert board.insight is not None
    assert board.insight.currency == "USD"


@pytest.mark.parametrize(
    "block", [None, [], [4, [None, "204"]], [4, [None, 204], None, None, [None, 300], [None, 200]]]
)
def test_a_page_without_a_readable_insight_has_none(block: Any) -> None:
    payload: list[Any] = json.loads(_ds1(_LAX))
    payload[5] = block
    assert _board(_page(json.dumps(payload))).insight is None


def test_the_insight_prints_one_line_under_the_table_in_the_page_currency(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [*_SEARCH, "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast"],
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("Price insight:")]
    assert lines == [
        "Price insight: prices are typical for this trip (usually USD85.00-USD225.00)."
    ]
    assert "$" not in lines[0]


def _insight_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith("Price insight:")]


def test_a_filtered_board_states_the_level_of_the_fares_it_kept(
    gf_session: Callable[..., Any],
) -> None:
    """Google's cheapest is the unfiltered board's USD293, typical. `O:LH+`
    keeps three fares from USD319, above the range Google calls usual."""
    gf_session(_served(_LHR))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--routing",
            "O:LH+",
            "--backend",
            "gflight",
            "--fast",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "USD319.00" in result.stdout
    assert _insight_lines(result.stdout) == [
        "Price insight: prices are high for this trip (usually USD170.00-USD295.00)."
    ]


def test_a_filter_that_kept_no_priced_fare_prints_no_insight(
    gf_session: Callable[..., Any],
) -> None:
    """`O:SK+` keeps only the four SK rows Google did not price, so no fare is
    left to state a level for."""
    gf_session(_served(_LHR))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--routing",
            "O:SK+",
            "--backend",
            "gflight",
            "--fast",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SK916" in result.stdout
    assert _insight_lines(result.stdout) == []


def test_a_filtered_round_trip_states_the_level_of_its_combination_fares(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A combination is priced by its return member, USD6616 on the served
    return board, not by the outbound board's unfiltered cheapest."""
    gf_session(_lhr_board_with_ba_first(), _return_board())
    cli._run_gflight_path(
        legs=_round_trip("~BA+"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1,
        json_out=False,
    )
    assert _insight_lines(capsys.readouterr().out) == [
        "Price insight: prices are high for this trip (usually USD170.00-USD295.00)."
    ]


def test_the_json_document_does_not_carry_the_insight(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "insight" not in result.stdout.lower()
    assert "Price insight" not in result.stderr


def test_the_first_paint_prints_the_insight_too(capsys: pytest.CaptureFixture[str]) -> None:
    board = _board(_page(_ds1(_LAX)))
    cli._paint_first_gf_table(
        {}, board, legs=(Leg.of("JFK", "LAX", _DEP),), top_n=3, awards_only=False
    )
    assert "Price insight: prices are typical for this trip" in capsys.readouterr().out


def test_a_cabin_the_routing_emptied_says_so(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The multi-cabin table names a cabin with no matching row rather than
    leaving its column silently empty."""
    gf_session(_served(_LAX))
    cli._run_gflight_path_multi(
        legs=(Leg.of("JFK", "LAX", _DEP, route_language="O:LH+"),),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH,),
        sort_by=Cabin.COACH,
        top_n=5,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None,
            cash_only=True,
            awards_only=False,
            provider_opt=(),
            legacy_no_pp=False,
            legacy_pp_only=False,
            legacy_pp_airlines=None,
            legacy_pp_cabin=None,
        ),
    )
    err = " ".join(capsys.readouterr().err.split())
    assert "Google Flights COACH: no itinerary matched an operating carrier filter (LH)" in err
