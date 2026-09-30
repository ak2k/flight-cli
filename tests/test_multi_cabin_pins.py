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
import json
import pathlib
import re
import threading
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
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
from flight_cli.domain import Cabin, Leg, SearchOptions
from test_gflight_page import _board_of, _return_board_of, _round_trip_filters

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from click.testing import Result

_DEP = dt.date.today() + dt.timedelta(days=45)
_RET = dt.date.today() + dt.timedelta(days=52)
_ROUND_TRIP = (Leg.of("JFK", "LAX", _DEP), Leg.of("LAX", "JFK", _RET))
_CAPTURE = pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
_SEED = gfid._parse_flight_with_id(gfid._rows_from_ds1(json.loads(_CAPTURE.read_text())).rows[1])
_JFK = _SEED.flight.legs[0].departure_airport
_LAX = _SEED.flight.legs[0].arrival_airport

# Economy's board ranks flights 100-129 in number order. Business's first ten are
# flights economy ranks 21-30, so on its own it pins none of economy's first ten.
_ECONOMY = list(range(100, 130))
_BUSINESS = [*range(120, 130), *range(100, 120)]

# Per seat: the fare of flight 100's first return, what each later outbound adds,
# and what each later return adds.
_FARES = {"ECONOMY": (200, 7, 3), "PREMIUM_ECONOMY": (500, 9, 4), "BUSINESS": (800, 11, 5)}


def _fare(seat: str, outbound: int, back: int = 0) -> float:
    base, per_outbound, per_return = _FARES[seat]
    return base + per_outbound * (outbound - 100) + per_return * back


def _row(number: int, day: dt.date, frm: Any, to: Any, price: float) -> gfid.GFlightWithId:
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
    being the outbound page; `chrome` runs first on every rung-2 GET."""

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
        self.pins: dict[str, list[int]] = {}
        self.modes: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(
        self, filters: Any, transport: Any, *, currency: str = "USD"
    ) -> gfid.Board[gfid.GFlightWithId]:
        _ = currency
        seat: str = filters.seat_type.name
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
        "gflight",
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
def test_with_no_preference_the_pins_are_the_first_rows_of_the_board(top_n: int) -> None:
    board = _outbound_board(*range(100, 130))
    assert gfid.pin_keys(board, top_n=top_n) == _keys(board[: gfid.pinned_fanout(top_n)])


def test_preferred_outbounds_are_pinned_first_in_their_order_then_the_board_fills() -> None:
    """An outbound the board does not list is skipped, and the slot it would
    have taken goes to the board's own next row."""
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


@pytest.mark.parametrize(("top_n", "pins"), [(3, 3), (10, 10), (50, 10)])
def test_a_longer_preference_never_buys_more_return_boards(
    monkeypatch: pytest.MonkeyPatch, top_n: int, pins: int
) -> None:
    """`prefer` reorders the pin budget and never grows it: every outbound on
    the board is preferred, and the GETs are still one board and the budget."""
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
    """Business's board lists every one of economy's first ten outbounds, only
    lower down: pinning its own first ten, it priced none of the rows the
    Y-sorted table shows."""
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    result = _search(monkeypatch, google)
    assert result.exit_code == 0, result.output
    rows = _table(result.stdout)
    assert len(rows) == 10
    assert [prices for _, prices in rows if "—" in prices] == []
    assert google.pins["ECONOMY"] == google.pins["BUSINESS"] == list(range(100, 110))
    assert (
        "Google Flights prices every cabin on up to 10 of the Y cabin's first-ranked "
        "outbounds; '—' means that cabin's search returned no fare for the itinerary."
    ) in _flat(result.stderr)


def test_the_page_loads_are_the_ones_each_cabin_spends_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = _Google({"ECONOMY": _ECONOMY, "BUSINESS": _BUSINESS})
    assert _search(monkeypatch, google).exit_code == 0
    assert google.gets == {"ECONOMY": 11, "BUSINESS": 11}


def test_a_follower_fills_the_pins_its_board_cannot_take_with_its_own_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business does not list economy's 103 or 107: it pins the eight it does,
    then its own first two, and those two economy outbounds are the only rows
    with no business fare."""
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
    assert "up to 10 of the J cabin's first-ranked outbounds" in _flat(result.stderr)


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
        filters: Any, transport: Any, *, currency: str = "USD"
    ) -> gfid.Board[gfid.GFlightWithId]:
        seat: str = filters.seat_type.name
        economy = seat == "ECONOMY"
        amenities = gfid.LegAmenities(
            cabin=seat,
            pitch_inches=31 if economy else None,
            legroom_class="AVERAGE" if economy else "Suite",
        )
        board = google(filters, transport, currency=currency)
        return gfid.Board([replace(r, amenities=[amenities]) for r in board])

    fan_out = cli._run_gflight_multi

    def business_first(**kw: Any) -> dict[Cabin, list[Any]]:
        out = fan_out(**kw)
        return {cab: out[cab] for cab in reversed(kw["cabins"])}

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
    assert "joins cabins on up to 10 of each cabin's first-ranked outbounds" in err
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
