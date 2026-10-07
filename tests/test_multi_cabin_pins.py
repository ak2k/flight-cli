# pyright: reportPrivateUsage=false
"""A Google Flights round trip over several cabins pins every cabin on the sort
cabin's outbounds, so the joined table prices each cabin on the itineraries it
shows.

Google is faked below `search_with_ids`, at `_one_call_laddered`, so the pin
choice runs as shipped: each cabin's outbound board lists the flights a test
names, in the order it names them, and the return board for a pinned outbound
lists five returns."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import itertools
import json
import pathlib
import re
import sys
import threading
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import (  # pyright: ignore[reportMissingTypeStubs] — fli ships no stubs
    BagsFilter,
    PriceLimit,
)
from rich.console import Console
from typer.testing import CliRunner

from conftest import _NoSleepTime, capture_err
from flight_cli import _gf_browser as gfb
from flight_cli import _gf_common as gfc
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import (
    GfBrowserUnavailableError,
    GfThrottledError,
    GfUpstreamStatusError,
)
from flight_cli._multi_cabin import MultiCabinRow
from flight_cli.domain import Cabin, Leg, SearchOptions
from flight_cli.models import Itinerary, SearchResult
from test_envelope import _envelope_of
from test_gflight_page import _TODAY, _board_of, _return_board_of, _round_trip_filters

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from click.testing import Result
    from time_machine import TimeMachineFixture

_DEP = _TODAY + dt.timedelta(days=45)
_RET = _TODAY + dt.timedelta(days=52)
_ROUND_TRIP = (Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET))
_CAPTURE = pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
_SEED = gfid._parse_flight_with_id(gfid._rows_from_ds1(json.loads(_CAPTURE.read_text())).rows[1])
_JFK = _SEED.flight.legs[0].departure_airport
_LAX = _SEED.flight.legs[0].arrival_airport

# Economy's board lists flights 100-129 in number order, and its fares rise with
# the number. Business's board lists 120-129 first and its fares rise down its
# own board, so its ten cheapest are economy's dearest ten: on its own it pins
# none of economy's ten cheapest.
_ECONOMY = list(range(100, 130))
_BUSINESS = [*range(120, 130), *range(100, 120)]

# Per seat: the fare of its cheapest outbound's first return, what each step up
# the cabin's outbound fares adds, and what each later return adds.
_FARES = {"ECONOMY": (200, 7, 3), "PREMIUM_ECONOMY": (500, 9, 4), "BUSINESS": (800, 11, 5)}


def _fare(seat: str, outbound: int, back: int = 0) -> float:
    base, per_outbound, per_return = _FARES[seat]
    step = _BUSINESS.index(outbound) if seat == "BUSINESS" else outbound - 100
    return base + per_outbound * step + per_return * back


def _row(number: int, day: dt.date, frm: Any, to: Any, price: float | None) -> gfid.GFlightWithId:
    """One flight, departing at an hour its number fixes: the same flight on two
    cabins' boards is then the same itinerary to `_itinerary_key`."""
    leg = _SEED.flight.legs[0]
    departs = dt.datetime.combine(day, dt.time(6 + number % 12, 0))
    leg = leg.model_copy(
        update={
            "flight_number": str(number),
            "departure_airport": frm,
            "arrival_airport": to,
            "departure_datetime": departs,
            "arrival_datetime": departs + dt.timedelta(hours=5),
        }
    )
    return replace(
        _SEED,
        flight=_SEED.flight.model_copy(update={"legs": [leg], "price": price, "currency": "USD"}),
        flight_id=f"id-{number}-{day}",
        operating=(),
    )


class _Google:
    """Google Flights below the ladder, recording every GET and every pinned
    outbound by seat. `refuse` raises for a (seat, pinned flight) pair, None
    being the outbound page; `chrome` runs first on every rung-2 GET. Each
    cabin's Cheapest tab lists no row and is counted apart, in `cheapest`.
    `filters` holds every request's filters, the Cheapest tab's included."""

    def __init__(
        self,
        boards: dict[str, list[int]],
        *,
        refuse: dict[tuple[str, int | None], Exception] | None = None,
        chrome: Callable[[str, int | None, Any], None] | None = None,
    ) -> None:
        self.boards = boards
        self.refuse = refuse or {}
        self.chrome = chrome
        self.gets: Counter[str] = Counter()
        self.cheapest: Counter[str] = Counter()
        self.pins: dict[str, list[int]] = {}
        self.modes: list[tuple[str, str]] = []
        self.filters: list[Any] = []
        self._lock = threading.Lock()

    def __call__(
        self, filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        _ = currency
        seat: str = filters.seat_type.name
        with self._lock:
            self.filters.append(filters)
        if cheapest:
            with self._lock:
                self.cheapest[seat] += 1
            return gfid.Board()
        picked = filters.flight_segments[0].selected_flight
        flight = None if picked is None else int(picked.legs[0].flight_number)
        with self._lock:
            self.gets[seat] += 1
            self.modes.append((seat, transport.mode))
            if flight is not None:
                self.pins.setdefault(seat, []).append(flight)
        if transport.mode == gfc.TRANSPORT_BROWSER and self.chrome is not None:
            self.chrome(seat, flight, transport)
        refusal = self.refuse.get((seat, flight))
        if refusal is not None:
            raise refusal
        if flight is None:
            outbounds = self.boards[seat]
            return gfid.Board([_row(n, _DEP, _JFK, _LAX, _fare(seat, n)) for n in outbounds])
        return gfid.Board(
            [_row(900 + j, _RET, _LAX, _JFK, _fare(seat, flight, j)) for j in range(5)]
        )


_TABLE_ROW = re.compile(r"^\s*│\s*\d+\s*│")
_OUTBOUND = re.compile(r"[A-Z0-9]{2}(1\d\d)\b")


def _table(stdout: str) -> list[tuple[int, list[str]]]:
    """Each numbered row of the joined table: its outbound flight and its price
    cells, one per cabin in `--cabin` order."""
    rows: list[tuple[int, list[str]]] = []
    for line in stdout.splitlines():
        if not _TABLE_ROW.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("│").split("│")]
        outbound = _OUTBOUND.search(cells[2])
        assert outbound is not None, line
        rows.append((int(outbound.group(1)), cells[4:]))
    return rows


def _search(
    monkeypatch: pytest.MonkeyPatch,
    google: Callable[..., gfid.Board[gfid.GFlightWithId]],
    *extra: str,
    cabins: str = "economy,business",
    n: str = "10",
    backend: str = "gflight",
) -> Result:
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    args = [
        "search",
        "--cash-only",
        "--no-google-url",
        "--no-matrix-url",
        "JFK",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--return",
        _RET.isoformat(),
        "--cabin",
        cabins,
        "--backend",
        backend,
        "-n",
        n,
        *extra,
    ]
    return CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})


def _flat(text: str) -> str:
    return " ".join(text.split())


# ─────────────────────────── the `_gflight_ids` seams ──────────────────────────


def _outbound_board(*numbers: int) -> gfid.Board[gfid.GFlightWithId]:
    return gfid.Board([_row(n, _DEP, _JFK, _LAX, _fare("ECONOMY", n)) for n in numbers])


def _keys(board: list[gfid.GFlightWithId]) -> list[gfid.ItineraryKey]:
    return [gfid._itinerary_key(r) for r in board]


@pytest.mark.parametrize("top_n", [3, 10, 50])
@pytest.mark.parametrize(
    "listed",
    [list(range(100, 130)), [*range(115, 130), *range(100, 115)]],
    ids=["listed-cheapest-first", "cheapest-listed-lower-down"],
)
def test_with_no_preference_the_pins_are_the_cheapest_rows_of_the_board(
    top_n: int, listed: list[int]
) -> None:
    board = _outbound_board(*listed)
    by_number = dict(zip(listed, _keys(board), strict=True))
    cheapest = range(100, 100 + gfid.pinned_fanout(top_n))
    assert gfid.pin_keys(board, top_n=top_n) == [by_number[n] for n in cheapest]


def test_equal_fares_are_pinned_in_page_order_and_unpriced_rows_last() -> None:
    """The order the one-way trim uses: fare, then the page's order between
    equal fares, and a row Google did not price after every row it did."""
    fares: dict[int, float | None] = {105: 300, 101: None, 103: 200, 102: 300, 104: 200}
    board = gfid.Board([_row(n, _DEP, _JFK, _LAX, fare) for n, fare in fares.items()])
    by_number = dict(zip(fares, _keys(board), strict=True))
    assert gfid.pin_keys(board, top_n=5) == [by_number[n] for n in (103, 104, 105, 102, 101)]


def test_preferred_outbounds_are_pinned_first_in_their_order_then_the_board_fills() -> None:
    """An outbound the board does not list is skipped, and the slot it would
    have taken goes to the board's own next-cheapest row."""
    board = _outbound_board(*range(100, 115))
    by_number = dict(zip(range(100, 115), _keys(board), strict=True))
    absent = _keys(_outbound_board(199))[0]
    prefer = [by_number[112], absent, by_number[103], by_number[107]]
    assert gfid.pin_keys(board, top_n=10, prefer=prefer) == [
        by_number[n] for n in (112, 103, 107, 100, 101, 102, 104, 105, 106, 108)
    ]


def test_a_preferred_outbound_the_filter_removes_is_never_pinned() -> None:
    board = _outbound_board(*range(100, 115))
    by_number = dict(zip(range(100, 115), _keys(board), strict=True))

    def keep(_i: int, row: gfid.GFlightWithId) -> bool:
        return row.flight.legs[0].flight_number != "112"

    prefer = [by_number[112], by_number[103]]
    assert gfid.pin_keys(board, top_n=4, keep=keep, prefer=prefer) == [
        by_number[n] for n in (103, 100, 101, 102)
    ]


@pytest.mark.parametrize("days_late", [0, 1], ids=["same-day", "past-midnight"])
@pytest.mark.parametrize(("top_n", "pins"), [(3, 3), (10, 10), (50, 10)])
def test_a_longer_preference_never_buys_more_return_boards(
    monkeypatch: pytest.MonkeyPatch,
    time_machine: TimeMachineFixture,
    top_n: int,
    pins: int,
    days_late: int,
) -> None:
    """`prefer` reorders the pin budget and never grows it: every outbound on
    the board is preferred, and the GETs are still one board and the budget.

    The boards carry the dates read at import, and a suite that crosses
    midnight reaches this test on the next day."""
    # A timestamp, because time-machine reads a naive datetime as UTC.
    noon = dt.datetime.combine(_TODAY + dt.timedelta(days=days_late), dt.time(12))
    time_machine.move_to(noon.timestamp())
    google = _Google({"ECONOMY": _ECONOMY})
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    filters = _round_trip_filters()
    page = gfid.outbound_page(filters, transport=gfid.HTTP_TRANSPORT, currency="USD")
    everything = list(reversed(_keys(page)))
    out = gfid.search_with_ids(filters, top_n=top_n, first=page, prefer=everything)
    assert out is not None
    assert out.pinned == pins
    assert google.gets["ECONOMY"] == 1 + pins
    assert google.pins["ECONOMY"] == list(range(129, 129 - pins, -1))


def test_a_page_handed_back_as_first_is_the_search_that_fetches_it(
    gf_session: Callable[..., Any],
) -> None:
    """The same rows, insight, drop count and pin count from the same GETs, in
    the same order: handing the page back saves the second outbound GET and
    changes nothing else."""
    bodies = (_board_of(3), _return_board_of(2))
    filters = _round_trip_filters()
    alone = gf_session(*bodies)
    want = gfid.search_with_ids(filters, top_n=3)
    handed = gf_session(*bodies)
    page = gfid.outbound_page(filters, transport=gfid.HTTP_TRANSPORT, currency="USD")
    got = gfid.search_with_ids(filters, top_n=3, first=page)
    assert want is not None
    assert got is not None
    assert list(got) == list(want)
    assert (got.insight, got.dropped, got.pinned) == (want.insight, want.dropped, want.pinned)
    assert handed.gets == alone.gets
    assert len(alone.gets) == 1 + 3


# ─────────────────────────── the join, end to end ──────────────────────────────


def test_every_cabin_is_priced_on_the_sort_cabins_outbounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business's board lists every one of economy's ten cheapest outbounds,
    among its own dearest: pinning its own ten cheapest, it priced none of the
    rows the Y-sorted table shows."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    rows = _table(result.stdout)
    assert len(rows) == 10
    assert [prices for _, prices in rows if "—" in prices] == []
    assert google.pins["ECONOMY"] == google.pins["BUSINESS"] == list(range(100, 110))
    assert (
        "Google Flights prices every cabin on up to 10 of the Y cabin's cheapest "
        "outbounds; '—' means that cabin's search returned no fare for the itinerary."
    ) in _flat(result.stderr)


@pytest.mark.parametrize(
    ("extra", "asked"),
    [
        ((), {}),
        (("--bags", "1"), {"bags": BagsFilter(checked_bags=1, carry_on=False)}),
        (("--max-price", "1000"), {"price_limit": PriceLimit(max_price=1000, currency=None)}),
    ],
    ids=["plain", "bags", "cap"],
)
def test_the_page_loads_are_the_ones_each_cabin_spends_alone(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], asked: dict[str, Any]
) -> None:
    """Under `--bags` or `--max-price` too, with every request asking for
    them. Those two red at the base, which refused them (exit 2)."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    result = _search(monkeypatch, google, *extra)
    assert result.exit_code == 0, result.output
    assert google.gets == {"ECONOMY": 11, "BUSINESS": 11}
    assert google.cheapest == {"ECONOMY": 1, "BUSINESS": 1}
    assert len(google.filters) == 24
    for field, value in asked.items():
        assert [getattr(f, field) for f in google.filters] == [value] * 24, field


def test_a_follower_fills_the_pins_its_board_cannot_take_with_its_own_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business does not list economy's 103 or 107: it pins the eight it does,
    then its own two cheapest, and those two economy outbounds are the only
    rows with no business fare."""
    business = [*range(120, 130), *(n for n in range(100, 120) if n not in (103, 107))]
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": business})
    result = _search(monkeypatch, google, n="50")
    assert result.exit_code == 0, result.output
    assert google.pins["BUSINESS"] == [100, 101, 102, 104, 105, 106, 108, 109, 120, 121]
    assert google.gets == {"ECONOMY": 11, "BUSINESS": 11}
    rows = _table(result.stdout)
    assert len(rows) == 50  # economy's ten outbounds, five returns each
    unpriced = [outbound for outbound, (_, j) in rows if j == "—"]
    assert sorted(set(unpriced)) == [103, 107]
    assert len(unpriced) == 10


def test_the_sort_cabin_leads_whichever_cabin_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `--sort business` economy prices business's outbounds, and the
    business column is business's own answer: its ten cheapest returns over the
    ten outbounds it pins alone."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    result = _search(monkeypatch, google, "--sort", "business")
    assert result.exit_code == 0, result.output
    assert google.pins["ECONOMY"] == google.pins["BUSINESS"] == list(range(120, 130))
    rows = _table(result.stdout)
    alone = sorted(_fare("BUSINESS", n, j) for n in range(120, 130) for j in range(5))[:10]
    assert [float(j) for _, (_, j) in rows] == alone
    assert [prices for _, prices in rows if "—" in prices] == []
    assert "up to 10 of the J cabin's cheapest outbounds" in _flat(result.stderr)


def test_with_three_cabins_both_followers_pin_the_leaders_outbounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = _Google(
        {"ECONOMY": _ECONOMY, "PREMIUM_ECONOMY": _ECONOMY[::-1], "BUSINESS": _BUSINESS}
    )
    result = _search(monkeypatch, google, cabins="economy,premium,business")
    assert result.exit_code == 0, result.output
    leader = list(range(100, 110))
    assert google.pins == {"ECONOMY": leader, "PREMIUM_ECONOMY": leader, "BUSINESS": leader}
    assert google.gets == {"ECONOMY": 11, "PREMIUM_ECONOMY": 11, "BUSINESS": 11}
    rows = _table(result.stdout)
    assert len(rows) == 10
    assert [prices for _, prices in rows if "—" in prices] == []


def test_a_joined_row_shows_the_sort_cabins_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fan-out hands the cabins back in the order their searches finish, and
    a joined row is drawn from one cabin's itinerary: business finishing first
    must not put its seats on the rows of a Y-sorted table."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})

    def seated(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        seat: str = filters.seat_type.name
        economy = seat == "ECONOMY"
        amenities = gfid.LegAmenities(
            cabin=seat,
            pitch_inches=31 if economy else None,
            legroom_class="AVERAGE" if economy else "Suite",
        )
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        return gfid.Board([replace(r, amenities=[amenities]) for r in board])

    fan_out = cli._run_gflight_multi

    def business_first(**kw: Any) -> cli._CabinBoards:
        out = fan_out(**kw)
        return cli._CabinBoards(
            {cab: out[cab] for cab in reversed(kw["cabins"])}, leader=out.leader
        )

    monkeypatch.setattr(cli, "_run_gflight_multi", business_first)
    result = _search(monkeypatch, seated)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    details = [lines[i + 1] for i, line in enumerate(lines) if _TABLE_ROW.match(line)]
    assert len(details) == 10
    assert [d for d in details if 'Y 31"' not in d or "Suite" in d] == []


# ──────────────────────────── failures and budget ──────────────────────────────


def test_a_persistently_throttled_two_cabin_round_trip_costs_one_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-way bound holds for a round trip: its first round is two outbound
    pages under one shared ladder, and neither cabin is served, so no second
    round runs."""
    fetched: list[str] = []

    def _throttled(filters: Any, *, currency: str = "USD") -> PageFetch:
        _ = currency
        fetched.append(filters.seat_type.name)
        return PageFetch("", "https://www.google.com/sorry/index", 429)

    monkeypatch.setattr(gfid, "_fetch_page", _throttled)
    monkeypatch.setattr(gfid, "time", _NoSleepTime())
    capture_err(monkeypatch)
    cabins = (Cabin.COACH, Cabin.BUSINESS)
    out = cli._run_gflight_multi(
        legs=_ROUND_TRIP, opts=SearchOptions(cabin=Cabin.COACH), cabins=cabins, top_n=10
    )
    assert out == {}
    assert len(fetched) <= gfid._THROTTLE_RETRY_ATTEMPTS + 1 + (len(cabins) - 1)


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(GfThrottledError("Google Flights rate-limited the search"), id="throttled"),
        pytest.param(GfUpstreamStatusError(503), id="refused"),
    ],
)
def test_a_sort_cabin_whose_page_is_refused_leaves_every_cabin_its_own_pins(
    monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    google = _Google(
        {"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, refuse={("ECONOMY", None): refusal}
    )
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert "ECONOMY" not in google.pins
    assert google.pins["BUSINESS"] == list(range(120, 130))
    err = _flat(result.stderr)
    assert err.count("Google Flights COACH:") == 1, err
    # Nobody led, so the sentence is the one for cabins that each pinned their own.
    assert "joins cabins on up to 10 of each cabin's cheapest outbounds" in err
    assert "prices every cabin" not in err


def test_a_refused_return_board_of_the_sort_cabin_leaves_the_followers_pins_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = _Google(
        {"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS},
        refuse={("ECONOMY", 103): GfUpstreamStatusError(503)},
    )
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert google.pins["ECONOMY"] == google.pins["BUSINESS"] == list(range(100, 110))


def test_a_sort_cabin_whose_return_boards_are_all_refused_still_led(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its pins were handed on before its return boards were refused, so
    business priced economy's outbounds and the note says so, although the
    sort cabin's own column is empty."""
    refuse: dict[tuple[str, int | None], Exception] = {
        ("ECONOMY", n): GfUpstreamStatusError(503) for n in range(100, 110)
    }
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, refuse=refuse)
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert google.pins["BUSINESS"] == list(range(100, 110))
    err = _flat(result.stderr)
    assert err.count("Google Flights COACH:") == 1, err
    assert "prices every cabin on up to 10 of the Y cabin's cheapest outbounds" in err
    assert "each cabin's cheapest" not in err


def test_a_cabin_whose_page_is_empty_answers_as_it_does_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No GET beyond its page, and the empty board `_gflight_results` has always
    handed on for a cabin Google served nothing for."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": []})
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    capture_err(monkeypatch)
    out = cli._run_gflight_multi(
        legs=_ROUND_TRIP,
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        top_n=10,
    )
    assert out[Cabin.BUSINESS] == []
    assert len(out[Cabin.COACH]) == 50
    assert google.gets == {"ECONOMY": 11, "BUSINESS": 1}


# ───────────────────────────── a cap and bags ──────────────────────────────────


def _on_auto(monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[Result, list[SearchOptions]]:
    """A round trip on `--backend auto` where every business fare is USD800 or
    more and every economy fare under USD420, with the options of each search
    handed to Matrix."""
    handed: list[SearchOptions] = []

    def _matrix(*, opts: SearchOptions, **_kw: Any) -> None:
        handed.append(opts)

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _matrix)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    return _search(monkeypatch, google, *extra, backend="auto"), handed


def test_under_bags_a_cabin_the_cap_empties_stays_empty_on_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix prices no bags, so the search stays on Google: business's column
    is all '—' and its line names the cap. Red at the D1 commit, which handed
    the whole search to Matrix."""
    result, handed = _on_auto(monkeypatch, "--bags", "1", "--max-price", "700")
    assert result.exit_code == 0, result.output
    assert handed == []
    rows = _table(result.stdout)
    assert len(rows) == 10
    assert all(y != "—" and j == "—" for _, (y, j) in rows), rows
    assert "Google Flights BUSINESS: no itinerary matched a price cap of USD 700." in _flat(
        result.stderr
    )
    assert "Using Matrix" not in result.stderr


def test_without_bags_a_cabin_the_cap_empties_hands_the_search_to_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As a one-cabin search does, with the cap for Matrix to apply. Green at
    the D1 commit."""
    result, handed = _on_auto(monkeypatch, "--max-price", "700")
    assert result.exit_code == 0, result.output
    assert [opts.max_price for opts in handed] == [700]
    assert "Using Matrix: no Google Flights itinerary matched a price cap of USD 700" in _flat(
        result.stderr
    )


def test_a_handed_off_cabin_the_cap_empties_on_matrix_is_named_as_not_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handed the search, Matrix returns economy fares the cap then removes, so
    the line says Matrix shows none under the cap rather than that it returned
    none. Google's economy rows under the cap are left out, so the line still
    points at them and the envelope stays narrowed. Red where the line said
    Matrix returned no itinerary."""
    over = {
        Cabin.COACH: (("UA101", 750.0), ("UA102", 800.0), ("UA103", 850.0)),
        Cabin.BUSINESS: _MATRIX[Cabin.BUSINESS],
    }

    def _multi(*, opts: SearchOptions, **_kw: object) -> dict[Cabin, SearchResult]:
        return {cab: _matrix_answer(fares) for cab, fares in over.items()}

    monkeypatch.setattr(cli, "_run_matrix_multi", _multi)
    for fmt in ("table", "envelope"):
        google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
        result = _search(monkeypatch, google, "--max-price", "700", "--format", fmt, backend="auto")
        assert result.exit_code == 0, result.output
        stderr = _flat(result.stderr)
        assert "Matrix COACH: no fare at or under USD 700." in stderr
        assert (
            "Matrix shows no itinerary at or under USD 700 for COACH, where Google Flights "
            "had rows; --backend gflight shows them."
        ) in stderr, fmt
        assert "returned no itinerary" not in stderr
        if fmt == "envelope":
            assert _envelope_of(result)["complete"] is False


def test_a_cabin_whose_capped_page_served_nothing_says_no_fare_is_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business's USD page, asked for the cap, served no row. Red at the D1
    commit, which said nothing of business."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": []})
    result = _search(monkeypatch, google, "--max-price", "1000")
    assert result.exit_code == 0, result.output
    assert "Google Flights BUSINESS: no fare at or under USD 1000." in _flat(result.stderr)
    assert "Google Flights COACH" not in result.stderr
    uncapped = _search(monkeypatch, _Google({"ECONOMY": _ECONOMY, "BUSINESS": []}))
    assert "no fare at or under" not in uncapped.stderr


# What each seat's fares cover, as (checked, carry-on): its outbounds, then its
# returns. Under `--bags 1`, economy's include no checked bag and business's do.
_STATED: dict[str, tuple[tuple[int | None, int | None], tuple[int | None, int | None]]] = {
    "ECONOMY": ((0, 1), (0, 0)),
    "PREMIUM_ECONOMY": ((1, 1), (1, 1)),
    "BUSINESS": ((2, 1), (2, 1)),
}


def _with_bags(google: _Google, *, unstated: int | None = None) -> Callable[..., gfid.Board[Any]]:
    """`google` with each row stating the bags its fare covers (`_STATED`),
    and business's returns on the outbound `unstated` stating none."""

    def call(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        seat: str = filters.seat_type.name
        picked = filters.flight_segments[0].selected_flight
        flight = None if picked is None else int(picked.legs[0].flight_number)
        outbound, back = _STATED[seat]
        stated = (
            outbound
            if flight is None
            else (None, None)
            if seat == "BUSINESS" and flight == unstated
            else back
        )
        return gfid.Board([replace(r, bags_included=stated) for r in board])

    return call


def _members(result: Result, fmt: str) -> dict[str, list[Any]]:
    """Each cabin's row members in the document: both of a round trip's pair."""
    if fmt == "envelope":
        groups = {
            g["cabin"]: [r["row"] for r in g["rows"]] for g in _envelope_of(result)["results"]
        }
    else:
        assert result.exit_code == 0, result.output
        groups = json.loads(result.stdout)
    return {cab: [m for row in rows for m in row] for cab, rows in groups.items()}


@pytest.mark.parametrize("fmt", ["envelope", "json"])
def test_each_member_of_each_cabins_rows_says_what_its_price_covers(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Red at the D1 commit, where no member carried `bags_included`."""
    google = _with_bags(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}))
    members = _members(_search(monkeypatch, google, "--bags", "1", "--format", fmt), fmt)
    assert set(members) == {"COACH", "BUSINESS"}
    for cab, seat in (("COACH", "ECONOMY"), ("BUSINESS", "BUSINESS")):
        assert members[cab], cab
        out, back = _STATED[seat]
        assert [m["bags_included"] for m in members[cab]] == [
            {"checked": c, "carry_on": k} for _ in members[cab][::2] for c, k in (out, back)
        ], cab


@pytest.mark.parametrize("fmt", ["envelope", "json"])
def test_without_bags_no_member_says_what_its_price_covers(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Green at the D1 commit."""
    google = _with_bags(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}))
    members = _members(_search(monkeypatch, google, "--format", fmt), fmt)
    assert all(members.values())
    assert not any("bags_included" in m for ms in members.values() for m in ms)


def _award_legs(monkeypatch: pytest.MonkeyPatch, *extra: str) -> list[dict[str, Any]]:
    """The award document of a two-cabin round trip, with a provider that
    matches nothing, so every cash row reaches it on its own."""
    from flight_cli.pp import cli as pp_cli

    async def _gather(*, legs: list[Any], **_kw: Any) -> tuple[list[list[Any]], list[Any]]:
        return ([[] for _ in legs], [])

    def _configured(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _configured)
    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)
    google = _with_bags(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}))
    result = _search(monkeypatch, google, "--format", "json", *extra)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_each_cash_match_beside_the_awards_says_what_its_own_slice_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table's rows are economy's listings: one carry-on out, no bag back.
    Red at the D1 commit, whose matches carried no `bags_included`."""
    out, back = _award_legs(monkeypatch, "--bags", "1")
    assert len(out["matches"]) == len(back["matches"]) == 10
    assert {json.dumps(m["bags_included"]) for m in out["matches"]} == {
        json.dumps({"checked": 0, "carry_on": 1})
    }
    assert {json.dumps(m["bags_included"]) for m in back["matches"]} == {
        json.dumps({"checked": 0, "carry_on": 0})
    }


def test_without_bags_the_cash_matches_beside_the_awards_carry_no_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the D1 commit."""
    legs = _award_legs(monkeypatch)
    assert all(leg["matches"] for leg in legs)
    assert not any("bags_included" in m for leg in legs for m in leg["matches"])


_BAGS_KEY = "Bags: ✓ the fare includes the bags asked for, ✗ it does not, ? Google does not say."
_MARKED = re.compile(r"\d[\d,]*\.\d\d [✓✗?]")


def test_each_price_says_whether_its_own_listing_includes_the_bags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Economy's fares carry no checked bag and business's carry two, except
    its returns on 101, which state nothing: each cell is marked by its own
    cabin's listing, whichever cabin's seats the row shows. The key follows
    the table once. Red at the D1 commit, whose cells carried no mark."""
    google = _with_bags(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}), unstated=101)
    result = _search(monkeypatch, google, "--bags", "1")
    assert result.exit_code == 0, result.output
    rows = _table(result.stdout)
    assert len(rows) == 10
    assert 101 in {outbound for outbound, _ in rows}
    for outbound, (y, j) in rows:
        assert _MARKED.fullmatch(y), y
        assert _MARKED.fullmatch(j), j
        assert y.endswith(" ✗"), y
        assert j.endswith(" ?" if outbound == 101 else " ✓"), (outbound, j)
    lines = result.stdout.splitlines()
    assert [i for i, line in enumerate(lines) if line == _BAGS_KEY] == [
        max(i for i, line in enumerate(lines) if line.startswith("└")) + 1
    ]


def test_a_cabins_own_cheapest_is_marked_as_its_cells_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the D1 commit."""
    result = _search(monkeypatch, _with_bags(_reversed(monkeypatch)), "--bags", "1")
    assert result.exit_code == 0, result.output
    assert _own_lines(result.stdout) == [
        "J's own cheapest: USD1020.00 ✓ (B6109 / B6900), on no row above; "
        "--sort business lists J's cheapest first."
    ]
    lines = result.stdout.splitlines()
    assert lines.index(_BAGS_KEY) == lines.index(_own_lines(result.stdout)[0]) + 1


def test_a_three_cabin_round_trip_wraps_no_marked_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 200 columns each price stays whole on its row's first line. Red at the
    D1 commit, whose cells carried no mark."""
    google = _with_bags(
        _Google({"ECONOMY": _ECONOMY, "PREMIUM_ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    )
    result = _search(monkeypatch, google, "--bags", "1", cabins="economy,premium,business")
    assert result.exit_code == 0, result.output
    body = [
        [c.strip() for c in line.strip().strip("│").split("│")]
        for line in result.stdout.splitlines()
        if line.startswith("│") and "│" in line[1:]
    ]
    numbered = [cells for cells in body if cells[0].isdigit()]
    assert len(numbered) == 10
    assert all(_MARKED.fullmatch(c) for cells in numbered for c in cells[4:]), numbered
    assert all(not any(cells[4:]) for cells in body if not cells[0].isdigit() and cells[0] != "#")


def test_without_bags_no_price_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the D1 commit."""
    google = _with_bags(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}), unstated=101)
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert all(re.fullmatch(r"\d+\.\d\d", c) for _, cells in _table(result.stdout) for c in cells)
    assert "Bags:" not in result.stdout


def _insight(cheapest: float, low: float, high: float) -> gfid.PriceInsight:
    return gfid.PriceInsight(cheapest=cheapest, typical_low=low, typical_high=high, currency="USD")


_Y_INSIGHT = "Price insight for Y: prices are typical for this trip (usually USD150.00-USD300.00)."
_J_INSIGHT = "Price insight for J: prices are high for this trip (usually USD2000.00-USD3500.00)."


def _with_insights(
    google: _Google, insights: dict[str, gfid.PriceInsight]
) -> Callable[..., gfid.Board[Any]]:
    """`google` with each seat's outbound page in `insights` carrying its insight."""

    def call(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        if cheapest or filters.flight_segments[0].selected_flight is not None:
            return board
        return gfid.Board(list(board), insight=insights.get(filters.seat_type.name))

    return call


_INSIGHTS = {"ECONOMY": _insight(200, 150, 300), "BUSINESS": _insight(4000, 2000, 3500)}


def test_each_google_cabins_insight_follows_every_other_line_in_cabin_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In `--cabin` order, whichever cabin the table is sorted by, and after
    the line naming a cabin's own cheapest. Red at the D1 commit."""
    google = _with_insights(_reversed(monkeypatch), _INSIGHTS)
    result = _search(monkeypatch, google, "--sort", "business")
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[-2:] == [_Y_INSIGHT, _J_INSIGHT]
    assert _own_lines(result.stdout)
    assert sum("Price insight" in line for line in lines) == 2


def test_a_cabin_with_no_insight_prints_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the D1 commit, which printed no insight at all."""
    google = _with_insights(
        _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}), {"BUSINESS": _INSIGHTS["BUSINESS"]}
    )
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    assert [line for line in result.stdout.splitlines() if "Price insight" in line] == [_J_INSIGHT]


def test_the_documents_carry_no_insight_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON stays the boards' rows, and the envelope carries each insight
    under `insight`, as it did. Green at the D1 commit."""
    google = _with_insights(_Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}), _INSIGHTS)
    document = _search(monkeypatch, google, "--format", "json")
    assert document.exit_code == 0, document.output
    assert set(json.loads(document.stdout)) == {"COACH", "BUSINESS"}
    envelope = _envelope_of(_search(monkeypatch, google, "--format", "envelope"))
    assert [(i["cabin"], i["level"]) for i in envelope["insight"]] == [
        ("COACH", "typical"),
        ("BUSINESS", "high"),
    ]


def test_a_marked_row_reads_amount_bags_then_ticketing_and_the_keys_follow_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bags key comes before the ticketing key, and the insight line after
    both. Red at the D1 commit, whose renderer took no bag mark."""
    [it] = _matrix_answer((("UA101", 300.0),)).solutions
    it = it.model_copy(update={"ticketing": "separate_tickets"})
    row = MultiCabinRow(itinerary=it, prices={Cabin.COACH: "USD300.00"}, listings={Cabin.COACH: it})
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=200, no_color=True))

    def mark(_it: Itinerary) -> str:
        return "✓"

    cli._render_multi_cabin_search(
        [row],
        cabins=(Cabin.COACH,),
        sort_by=Cabin.COACH,
        bag_mark=mark,
        insights={Cabin.COACH: _insight(200, 150, 300)},
    )
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert "300.00 ✓ †" in _table_cells(lines)
    after = lines[[i for i, line in enumerate(lines) if line.startswith("└")][-1] + 1 :]
    assert after[0] == _BAGS_KEY
    assert after[1].startswith("† separate tickets")
    assert after[-1] == _Y_INSIGHT


def _table_cells(lines: list[str]) -> list[str]:
    return [c.strip() for line in lines if _TABLE_ROW.match(line) for c in line.split("│")]


# ─────────────────────────────────── rung 2 ────────────────────────────────────


def _rung_two(google: _Google, monkeypatch: pytest.MonkeyPatch, *cabins: Cabin) -> Any:
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    return cli._run_gflight_multi(
        legs=_ROUND_TRIP,
        opts=SearchOptions(cabin=cabins[0]),
        cabins=cabins,
        top_n=10,
        gf_mode=gfc.TRANSPORT_BROWSER,
    )


def _no_chrome(_seat: str, _flight: int | None, _transport: Any) -> None:
    raise GfBrowserUnavailableError(
        "Chrome failed to launch for Google Flights: no browser on this machine.",
        remedy=gfb._INSTALL_HINT,
    )


def test_a_round_trip_at_rung_two_enters_the_interrupt_guard_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cabin inside one guard: a second guard would clear the interrupt
    the first cabin recorded."""
    entries: list[bool] = []
    real = gfb.interrupt_guard

    @contextlib.contextmanager
    def _counting(*, armed: bool = True) -> Generator[None]:
        entries.append(armed)
        with real(armed=armed):
            yield

    monkeypatch.setattr(gfb, "interrupt_guard", _counting)
    google = _Google(
        {"ECONOMY": _ECONOMY, "PREMIUM_ECONOMY": _ECONOMY[::-1], "BUSINESS": _BUSINESS}
    )
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS)
    assert entries == [True]
    assert list(out) == [Cabin.COACH, Cabin.PREMIUM_COACH, Cabin.BUSINESS]
    assert {mode for _, mode in google.modes} == {gfc.TRANSPORT_BROWSER}
    leader = list(range(100, 110))
    assert google.pins == {"ECONOMY": leader, "PREMIUM_ECONOMY": leader, "BUSINESS": leader}


def test_a_round_trip_whose_chrome_never_opens_runs_both_rounds_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = capture_err(monkeypatch)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_no_chrome)
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.BUSINESS)
    assert sorted(out) == [Cabin.BUSINESS, Cabin.COACH]
    assert buf.getvalue().count("multi-cabin is using http") == 1
    assert google.modes[0] == ("ECONOMY", gfc.TRANSPORT_BROWSER)
    assert {mode for _, mode in google.modes[1:]} == {gfc.TRANSPORT_HTTP}
    assert google.pins["ECONOMY"] == google.pins["BUSINESS"] == list(range(100, 110))


def test_a_chrome_that_dies_on_the_first_cabins_pins_reruns_the_search_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is served yet, so the whole search moves to rung 1, as a single
    cabin whose pins die does."""

    def _dies_on_pins(seat: str, flight: int | None, transport: Any) -> None:
        if flight is not None:
            _no_chrome(seat, flight, transport)

    buf = capture_err(monkeypatch)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_dies_on_pins)
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.BUSINESS)
    assert sorted(out) == [Cabin.BUSINESS, Cabin.COACH]
    assert buf.getvalue().count("multi-cabin is using http") == 1
    # The sort cabin's page and its first pin, as the cabin searched alone
    # spends them: no follower's page is loaded for a search rung 1 reruns.
    browser = [seat for seat, mode in google.modes if mode == gfc.TRANSPORT_BROWSER]
    assert browser == ["ECONOMY", "ECONOMY"]
    http = [seat for seat, mode in google.modes if mode == gfc.TRANSPORT_HTTP]
    assert Counter(http) == {"ECONOMY": 11, "BUSINESS": 11}


def _fails_from(navigation: int) -> Callable[[str, int | None, Any], None]:
    """A Chrome that loads pages until its `navigation`-th, and none after."""
    seen: list[tuple[str, int | None]] = []

    def chrome(seat: str, flight: int | None, _transport: Any) -> None:
        seen.append((seat, flight))
        if len(seen) >= navigation:
            raise GfBrowserUnavailableError(
                "Chrome could not load Google Flights' search page: Timeout 30000ms exceeded"
            )

    return chrome


def test_a_chrome_that_dies_before_any_cabin_is_served_says_only_that_it_moved_to_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome loads the sort cabin's page and dies on its first pin. Rung 1 then
    serves both columns, so a note that business's column is missing is false."""
    buf = capture_err(monkeypatch)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_fails_from(2))
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.BUSINESS)
    assert sorted(out) == [Cabin.BUSINESS, Cabin.COACH]
    err = _flat(buf.getvalue())
    assert err.count("multi-cabin is using http") == 1, err
    assert "Google Flights BUSINESS:" not in err, err


def test_a_chrome_that_dies_after_the_sort_cabin_is_served_keeps_what_it_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome serves the sort cabin's page and first pin, then dies. The sort
    cabin keeps what it was served and business's column is missing, which is
    what searching the cabins one after the other costs; rerunning over http
    would load every page Chrome served a second time."""
    buf = capture_err(monkeypatch)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_fails_from(3))
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.BUSINESS)
    assert list(out) == [Cabin.COACH]
    assert len(out[Cabin.COACH]) == 5
    assert Counter(google.modes) == {
        ("ECONOMY", gfc.TRANSPORT_BROWSER): 3,
        ("BUSINESS", gfc.TRANSPORT_BROWSER): 1,
    }
    err = _flat(buf.getvalue())
    assert "multi-cabin is using http" not in err, err
    assert err.count("Google Flights BUSINESS:") == 1, err


def test_a_later_cabins_chrome_failure_is_that_cabins_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _business_has_no_chrome(seat: str, flight: int | None, transport: Any) -> None:
        if seat == "BUSINESS":
            _no_chrome(seat, flight, transport)

    buf = capture_err(monkeypatch)
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_business_has_no_chrome)
    out = _rung_two(google, monkeypatch, Cabin.COACH, Cabin.BUSINESS)
    assert list(out) == [Cabin.COACH]
    assert "multi-cabin is using http" not in buf.getvalue()
    assert _flat(buf.getvalue()).count("Google Flights BUSINESS:") == 1
    assert {mode for _, mode in google.modes} == {gfc.TRANSPORT_BROWSER}


def test_a_two_cabin_round_trip_at_rung_two_launches_one_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Each cabin's outbound page and its return boards navigate on the one
    session the scope keeps open. Unmarked, because the fake playwright
    replaces the launcher seam the guard watches."""
    from test_gf_browser import _PAGE_URL, _install, _page_of

    pw = _install(monkeypatch, tmp_path)

    def _navigate(_seat: str, _flight: int | None, transport: Any) -> None:
        gfb.session(headed=transport.headed).get_html(_PAGE_URL)

    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}, chrome=_navigate)
    monkeypatch.setattr(gfid, "_one_call_laddered", google)
    out = cli._run_gflight_multi(
        legs=_ROUND_TRIP,
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        top_n=1,
        gf_mode=gfc.TRANSPORT_BROWSER,
    )
    assert sorted(out) == [Cabin.BUSINESS, Cabin.COACH]
    assert pw.chromium.launches == 1
    assert len(_page_of(pw).gotos) == 2 + 2  # two outbound pages, one pin per cabin


# ────────────────── what the table shows, and what the documents carry ──────────────────

# Business's fares reversed: each step up economy's outbounds is a step down
# business's, so business's cheapest fare on the ten outbounds economy leads
# with is on 109, which no row of the Y-sorted table flies. Its own board starts
# at USD1020 (109 out, 900 back), its column at USD1097 (102 out).
_REVERSED = list(range(129, 99, -1))
_OWN_LINE = (
    "J's own cheapest: USD1020.00 (B6109 / B6900), on no row above; "
    "--sort business lists J's cheapest first."
)


def _reversed(monkeypatch: pytest.MonkeyPatch) -> _Google:
    monkeypatch.setattr(sys.modules[__name__], "_BUSINESS", _REVERSED)
    return _Google({"ECONOMY": _ECONOMY, "BUSINESS": _REVERSED})


def _cells(stdout: str) -> dict[str, set[float]]:
    """Each cabin's priced cells in the joined table, by the envelope's cabin name."""
    rows = _table(stdout)
    return {
        cab: {float(prices[i]) for _, prices in rows if prices[i] != "—"}
        for i, cab in enumerate(("COACH", "BUSINESS"))
    }


def _document_prices(result: Result, fmt: str) -> dict[str, list[float]]:
    """Each cabin's row prices in the document, in its order; a round trip's
    row is priced by its return, as every surface prices it."""
    if fmt == "envelope":
        env = _envelope_of(result)
        return {g["cabin"]: [r["price"] for r in g["rows"]] for g in env["results"]}
    assert result.exit_code == 0, result.output
    doc: dict[str, list[list[dict[str, Any]]]] = json.loads(result.stdout)
    return {cab: [row[-1]["price"] for row in rows] for cab, rows in doc.items()}


def test_a_cabins_own_cheapest_off_the_table_is_named_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which named business's own cheapest nowhere."""
    result = _search(monkeypatch, _reversed(monkeypatch))
    assert result.exit_code == 0, result.output
    assert min(_cells(result.stdout)["BUSINESS"]) == 1097.0
    lines = result.stdout.splitlines()
    named = [i for i, line in enumerate(lines) if "own cheapest" in line]
    assert [lines[i] for i in named] == [_OWN_LINE]
    assert named[0] > max(i for i, line in enumerate(lines) if line.startswith("└"))


def test_sorted_by_business_the_line_names_economys_own_cheapest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Economy prices business's outbounds, so economy's own cheapest (USD340,
    on 120) is the line, under the name `--sort` takes, and business, the sort
    cabin, has none. Red at the base."""
    result = _search(monkeypatch, _reversed(monkeypatch), "--sort", "business")
    assert result.exit_code == 0, result.output
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "Y's own cheapest: USD340.00 (B6120 / B6900), on no row above; "
        "--sort economy lists Y's cheapest first."
    ]


def test_a_cabin_whose_cheapest_is_a_row_gets_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Business's cheapest, USD910 on 100, is row 1's J cell. Green at the base."""
    result = _search(monkeypatch, _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS}))
    assert result.exit_code == 0, result.output
    assert min(_cells(result.stdout)["BUSINESS"]) == 910.0
    assert "own cheapest" not in result.stdout


def test_an_awards_only_search_names_no_cheapest(monkeypatch: pytest.MonkeyPatch) -> None:
    """No table, so no line under it."""
    awarded: list[object] = []

    def _awards(*a: object, **_kw: object) -> None:
        awarded.append(a)

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    monkeypatch.setattr(gfid, "_one_call_laddered", _reversed(monkeypatch))
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "--awards-only", "--no-google-url", "--no-matrix-url", "JFK", "LAX"),
            *("--dep", _DEP.isoformat(), "--return", _RET.isoformat()),
            *("--cabin", "economy,business", "--backend", "gflight", "-n", "10"),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert len(awarded) == 1
    assert "own cheapest" not in result.output


@pytest.mark.parametrize("fmt", ["envelope", "json"])
def test_every_fare_the_table_prints_is_a_row_of_its_cabin_in_the_document(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Each cabin's -n cheapest come first, then the rows the table prices in
    that cabin, all in price order. Red at the base, where none of the ten J
    cells is among business's ten cheapest."""
    table = _search(monkeypatch, _reversed(monkeypatch))
    assert table.exit_code == 0, table.output
    shown = _cells(table.stdout)
    listed = _document_prices(_search(monkeypatch, _reversed(monkeypatch), "--format", fmt), fmt)
    pins = range(100, 110)
    for cab, seat in (("COACH", "ECONOMY"), ("BUSINESS", "BUSINESS")):
        assert shown[cab] - set(listed[cab]) == set(), cab
        assert listed[cab] == sorted(listed[cab]), cab
        own = sorted(_fare(seat, n, j) for n in pins for j in range(5))
        assert listed[cab][:10] == own[:10], cab
    assert len(listed["BUSINESS"]) == 20
    assert len(listed["COACH"]) == 10


def _in_euros(google: _Google, pins: dict[int, float]) -> Callable[..., gfid.Board[Any]]:
    """`google` with business's return board on each outbound in `pins` priced
    in EUR, from that fare up by one a return."""

    def call(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        picked = filters.flight_segments[0].selected_flight
        flight = None if picked is None else int(picked.legs[0].flight_number)
        if filters.seat_type.name != "BUSINESS" or flight is None or flight not in pins:
            return board
        return gfid.Board(
            [
                replace(r, flight=r.flight.model_copy(update={"currency": "EUR", "price": fare}))
                for r, fare in zip(board, itertools.count(pins[flight]), strict=False)
            ]
        )

    return call


@pytest.mark.parametrize("fmt", ["envelope", "json"])
def test_the_cheapest_fare_the_table_names_is_a_row_of_its_cabin_in_the_document(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Business's returns on 105 and 106 are priced in EUR, below its USD1020 by
    number, so they are its ten cheapest rows by amount. The line under the
    table still names USD1020, and the document carries it. Red at the base."""
    euros = {105: 900.0, 106: 910.0}
    table = _search(monkeypatch, _in_euros(_reversed(monkeypatch), euros))
    assert table.exit_code == 0, table.output
    assert _OWN_LINE in table.stdout.splitlines()
    document = _search(monkeypatch, _in_euros(_reversed(monkeypatch), euros), "--format", fmt)
    if fmt == "envelope":
        business = next(g for g in _envelope_of(document)["results"] if g["cabin"] == "BUSINESS")
        listed = [(r["currency"], r["price"]) for r in business["rows"]]
    else:
        assert document.exit_code == 0, document.output
        doc: dict[str, list[list[dict[str, Any]]]] = json.loads(document.stdout)
        listed = [(row[-1]["currency"], row[-1]["price"]) for row in doc["BUSINESS"]]
    assert listed[:10] == sorted(("EUR", base + j) for base in euros.values() for j in range(5))
    assert listed[10] == ("USD", 1020.0)
    assert [price for _, price in listed] == sorted(price for _, price in listed)
    assert {("USD", p) for p in _cells(table.stdout)["BUSINESS"]} <= set(listed)
    assert len(listed) == 21


def _business_in_euros(google: _Google) -> Callable[..., gfid.Board[Any]]:
    """`google` with every business row, outbound and return, priced in EUR at
    the same number."""

    def call(
        filters: Any, transport: Any, *, currency: str = "USD", cheapest: bool = False
    ) -> gfid.Board[gfid.GFlightWithId]:
        board = google(filters, transport, currency=currency, cheapest=cheapest)
        if filters.seat_type.name != "BUSINESS":
            return board
        return gfid.Board(
            [replace(r, flight=r.flight.model_copy(update={"currency": "EUR"})) for r in board]
        )

    return call


def test_a_cabin_google_prices_only_in_another_currency_names_its_own_cheapest_in_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google prices every business row in EUR though USD was asked. Its column
    starts at EUR1097 and its envelope list at EUR1020, so the line names
    EUR1020 against the EUR fares the column shows. Red at the base."""
    table = _search(monkeypatch, _business_in_euros(_reversed(monkeypatch)))
    assert table.exit_code == 0, table.output
    j_cells = {prices[1] for _, prices in _table(table.stdout)}
    assert all(cell.startswith("EUR") for cell in j_cells), j_cells
    assert min(float(cell.removeprefix("EUR")) for cell in j_cells) == 1097.0
    assert [line for line in table.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest: EUR1020.00 (B6109 / B6900), on no row above; "
        "--sort business lists J's cheapest first."
    ]
    document = _search(
        monkeypatch, _business_in_euros(_reversed(monkeypatch)), "--format", "envelope"
    )
    business = next(g for g in _envelope_of(document)["results"] if g["cabin"] == "BUSINESS")
    assert (business["rows"][0]["currency"], business["rows"][0]["price"]) == ("EUR", 1020.0)


def test_the_table_and_both_documents_load_the_same_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rows a document adds are read off the boards the join already holds.
    Green at the base."""
    for extra in ((), ("--format", "json"), ("--format", "envelope")):
        google = _reversed(monkeypatch)
        assert _search(monkeypatch, google, *extra).exit_code == 0, extra
        assert google.gets == {"ECONOMY": 11, "BUSINESS": 11}, extra
        assert google.cheapest == {"ECONOMY": 1, "BUSINESS": 1}, extra


def test_each_row_names_its_carriers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base, which printed `?` on every Google row."""
    result = _search(monkeypatch, _reversed(monkeypatch))
    assert result.exit_code == 0, result.output
    carriers = [
        [c.strip() for c in line.strip().strip("│").split("│")][1]
        for line in result.stdout.splitlines()
        if _TABLE_ROW.match(line)
    ]
    assert carriers == ["B6"] * 10


# A one-way Matrix answer per cabin: economy's cheapest two are UA101 and UA102,
# business's cheapest is UA103, which a two-row economy-sorted table leaves out.
_MATRIX = {
    Cabin.COACH: (("UA101", 300.0), ("UA102", 350.0), ("UA103", 400.0)),
    Cabin.BUSINESS: (("UA101", 1500.0), ("UA102", 1400.0), ("UA103", 900.0)),
}


def _matrix_answer(
    fares: tuple[tuple[str, float], ...],
    currency: str = "USD",
    *,
    party: int = 1,
    totaled: bool = True,
) -> SearchResult:
    """`fares` as Matrix answers them for one traveler; for a `party`, each is
    one traveler's price beside the party's total, which a cabin not `totaled`
    states none of."""

    def slice_of(flight: str) -> dict[str, Any]:
        hour = int(flight[2:]) - 94
        return {
            "flights": [flight],
            "departure": f"{_DEP}T{hour:02d}:00",
            "arrival": f"{_DEP}T{hour + 5:02d}:00",
            "origin": {"code": "JFK"},
            "destination": {"code": "LAX"},
        }

    def priced(fare: float) -> dict[str, Any]:
        if party == 1:
            return {"displayTotal": f"{currency}{fare:.2f}"}
        total = {"displayTotal": f"{currency}{fare * party:.2f}"} if totaled else {}
        return {"ext": {"price": f"{currency}{fare:.2f}"}, **total}

    solutions = [
        {**priced(fare), "itinerary": {"slices": [slice_of(flight)]}} for flight, fare in fares
    ]
    return SearchResult.from_api(
        {"solutionList": {"solutions": solutions}, "solutionCount": len(solutions)}
    )


def _matrix_search(
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    currency: str = "USD",
    party: int = 1,
    untotaled: frozenset[Cabin] = frozenset(),
    asked: list[SearchOptions] | None = None,
) -> Result:
    def _multi(*, opts: SearchOptions, **_kw: object) -> dict[Cabin, SearchResult]:
        if asked is not None:
            asked.append(opts)
        return {
            cab: _matrix_answer(fares, currency, party=party, totaled=cab not in untotaled)
            for cab, fares in _MATRIX.items()
        }

    monkeypatch.setattr(cli, "_run_matrix_multi", _multi)
    return CliRunner().invoke(
        cli.app,
        [
            *("search", "--cash-only", "--no-google-url", "--no-matrix-url", "JFK", "LAX"),
            *("--dep", _DEP.isoformat(), "--cabin", "economy,business"),
            *("--backend", "matrix", "-n", "2", *extra),
        ],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )


def test_a_matrix_table_names_a_cabins_own_cheapest_off_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base."""
    result = _matrix_search(monkeypatch)
    assert result.exit_code == 0, result.output
    assert _cells(result.stdout)["BUSINESS"] == {1500.0, 1400.0}
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest: USD900.00 (UA103), on no row above; "
        "--sort business lists J's cheapest first."
    ]


def test_a_matrix_table_in_its_own_currency_names_a_cabins_own_cheapest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no --currency, Matrix prices in its own default (GBP from LHR): the
    line names the fare in the currency the table prints. Red at the base."""
    result = _matrix_search(monkeypatch, currency="GBP")
    assert result.exit_code == 0, result.output
    assert _cells(result.stdout)["BUSINESS"] == {1500.0, 1400.0}
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest: GBP900.00 (UA103), on no row above; "
        "--sort business lists J's cheapest first."
    ]


@pytest.mark.parametrize("party", [1, 2])
def test_every_fare_a_matrix_table_prints_is_a_row_of_its_cabin_in_the_envelope(
    monkeypatch: pytest.MonkeyPatch, party: int
) -> None:
    """Matrix's envelope carries each cabin's whole answer, priced as the table
    and the line under it print it: for two, at the party's total. Green at the
    base, and for two at the merge."""
    adults = ("--adults", str(party))
    shown = _cells(_matrix_search(monkeypatch, *adults, party=party).stdout)
    envelope = _matrix_search(monkeypatch, *adults, "--format", "envelope", party=party)
    listed = _document_prices(envelope, "envelope")
    for cab, cells in shown.items():
        assert cells, cab
        assert cells - set(listed[cab]) == set(), cab
    assert 900.0 * party in listed["BUSINESS"]


def test_a_partys_matrix_line_names_a_cabins_own_cheapest_at_the_partys_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two adults: the table prints each cabin's party total, so the line names
    business's own cheapest at the total Matrix states for the two, and says so.
    Red at the merge, which named one traveler's price beside the totals."""
    result = _matrix_search(monkeypatch, "--adults", "2", party=2)
    assert result.exit_code == 0, result.output
    assert _cells(result.stdout)["BUSINESS"] == {3000.0, 2800.0}
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest, total for 2 travelers: USD1800.00 (UA103), on no row above; "
        "--sort business lists J's cheapest first."
    ]


def test_a_partys_matrix_line_says_per_traveler_where_matrix_states_no_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix states no total for business, so its column is starred per
    traveler, and the line names one traveler's price as such. Red at the
    merge."""
    result = _matrix_search(
        monkeypatch, "--adults", "2", party=2, untotaled=frozenset({Cabin.BUSINESS})
    )
    assert result.exit_code == 0, result.output
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest, per traveler: USD900.00 (UA103), on no row above; "
        "--sort business lists J's cheapest first."
    ]


def test_a_partys_google_line_names_the_total_google_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google prices the whole party, so its listed fare is the total the column
    prints, and the line says it is. Red at the merge."""
    result = _search(monkeypatch, _reversed(monkeypatch), "--adults", "2")
    assert result.exit_code == 0, result.output
    assert [line for line in result.stdout.splitlines() if "own cheapest" in line] == [
        "J's own cheapest, total for 2 travelers: USD1020.00 (B6109 / B6900), on no row above; "
        "--sort business lists J's cheapest first."
    ]


def _own_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if "own cheapest" in line]


def test_a_capped_matrix_cabin_keeps_only_its_fares_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business keeps UA103 at USD900 alone, which no row of the economy-sorted
    table flies, so its line names it. Red at the D1 commit, whose J column
    printed USD1500 and USD1400."""
    result = _matrix_search(monkeypatch, "--max-price", "1000")
    assert result.exit_code == 0, result.output
    assert _cells(result.stdout) == {"COACH": {300.0, 350.0}, "BUSINESS": set()}
    assert _own_lines(result.stdout) == [
        "J's own cheapest: USD900.00 (UA103), on no row above; "
        "--sort business lists J's cheapest first."
    ]
    assert "no fare at or under" not in result.stderr


def test_a_matrix_cabin_the_cap_leaves_no_fare_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the D1 commit."""
    result = _matrix_search(monkeypatch, "--max-price", "800")
    assert result.exit_code == 0, result.output
    assert _cells(result.stdout) == {"COACH": {300.0, 350.0}, "BUSINESS": set()}
    assert _own_lines(result.stdout) == []
    assert "Matrix BUSINESS: no fare at or under USD 800." in _flat(result.stderr)
    assert "Matrix COACH" not in result.stderr


def _envelope_prices(result: Result) -> tuple[dict[str, list[float]], bool]:
    env = _envelope_of(result)
    return {g["cabin"]: [r["price"] for r in g["rows"]] for g in env["results"]}, env["complete"]


def _raw_counts(result: Result) -> dict[str, tuple[list[str], int]]:
    """Each cabin's `{cabin: raw}` document: its fares and its `solutionCount`."""
    assert result.exit_code == 0, result.output
    doc: dict[str, dict[str, Any]] = json.loads(result.stdout)
    return {
        cab: (
            [s.get("displayTotal") for s in raw["solutionList"]["solutions"]],
            raw["solutionCount"],
        )
        for cab, raw in doc.items()
    }


@pytest.mark.parametrize(
    ("cap", "business", "count"), [("1000", [900.0], 1), ("800", [], 0)], ids=["1000", "800"]
)
def test_a_capped_matrix_document_holds_no_fare_over_the_cap(
    monkeypatch: pytest.MonkeyPatch, cap: str, business: list[float], count: int
) -> None:
    """Business's dropped fares are all over the cap, so its count is the kept
    rows; economy drops none, so its count stays Matrix's. The cap narrows
    nothing. Red at the D1 commit."""
    listed, complete = _envelope_prices(
        _matrix_search(monkeypatch, "--max-price", cap, "--format", "envelope")
    )
    assert listed == {"COACH": [300.0, 350.0, 400.0], "BUSINESS": business}
    assert complete == _envelope_prices(_matrix_search(monkeypatch, "--format", "envelope"))[1]
    raw = _raw_counts(_matrix_search(monkeypatch, "--max-price", cap, "--format", "json"))
    assert raw == {
        "COACH": (["USD300.00", "USD350.00", "USD400.00"], 3),
        "BUSINESS": ([f"USD{p:.2f}" for p in business], count),
    }


@pytest.mark.parametrize(
    ("untotaled", "business", "count", "line"),
    [
        (frozenset[Cabin](), [1800.0], 1, None),
        (
            frozenset({Cabin.BUSINESS}),
            list[float](),
            3,
            "Matrix BUSINESS: no fare that states a USD total is at or under USD 2000; "
            "those that state none are not shown.",
        ),
    ],
    ids=["totaled", "untotaled"],
)
def test_a_partys_capped_matrix_cabin_is_held_to_its_total(
    monkeypatch: pytest.MonkeyPatch,
    untotaled: frozenset[Cabin],
    business: list[float],
    count: int,
    line: str | None,
) -> None:
    """Two adults under USD2000: UA103's total is USD1800, and the other
    business totals are over the cap. Where Matrix states no total, no fare
    can be held to the cap, so none is kept and Matrix's count stays, and the
    line says those fares are not shown: none was read as over the cap. Red at
    the D1 commit; the untotaled line red where it said no fare was under."""
    args = ("--adults", "2", "--max-price", "2000")
    envelope = _matrix_search(
        monkeypatch, *args, "--format", "envelope", party=2, untotaled=untotaled
    )
    listed, _ = _envelope_prices(envelope)
    assert listed == {"COACH": [600.0, 700.0, 800.0], "BUSINESS": business}
    raw = _raw_counts(
        _matrix_search(monkeypatch, *args, "--format", "json", party=2, untotaled=untotaled)
    )
    assert raw["BUSINESS"] == ([f"USD{p:.2f}" for p in business], count)
    stderr = _flat(envelope.stderr)
    assert "no fare at or under" not in stderr
    if line is None:
        assert "Matrix BUSINESS" not in stderr
    else:
        assert line in stderr


def test_a_capped_matrix_compare_asks_matrix_in_the_cap_s_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the D1 commit, which left the currency to Matrix."""
    asked: list[SearchOptions] = []
    assert _matrix_search(monkeypatch, "--max-price", "1000", asked=asked).exit_code == 0
    assert _matrix_search(monkeypatch, asked=asked).exit_code == 0
    assert [opts.currency for opts in asked] == ["USD", None]
