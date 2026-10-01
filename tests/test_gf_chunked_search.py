# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""A leg over one Google page's airports is asked as several pages and merged.

Google is faked at `_gflight_ids._one_call`, the rung-1 GET, keyed on the
airports the filter asks for, so the page plan, the throttle ladder, the pins
and the merge all run as shipped. An outbound page lists one flight per
(origin, destination) pair it asks for; a pinned outbound's return page lists
two flights back."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from fli.models.airport import Airport
from typer.testing import CliRunner

from conftest import capture_err
from flight_cli import _gf_browser as gfb
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_errors import (
    GfBackendError,
    GfBrowserUnavailableError,
    GfThrottledError,
    GfUpstreamStatusError,
)
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_DEP = dt.date.today() + dt.timedelta(days=45)
_RET = dt.date.today() + dt.timedelta(days=52)
_CAPTURE = pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
_SEED = gfid._parse_flight_with_id(gfid._rows_from_ds1(json.loads(_CAPTURE.read_text())).rows[1])

# The skill's Example 6: 8 + 13 = 21 airports, four pages of 4 x 7, 4 x 6.
_EX6_FROM = ("JFK", "LGA", "EWR", "BOS", "IAD", "DCA", "BWI", "PHL")
_EX6_TO = (
    "LHR",
    "CDG",
    "FRA",
    "AMS",
    "IST",
    "MAD",
    "BCN",
    "FCO",
    "MUC",
    "ZRH",
    "VIE",
    "CPH",
    "DUB",
)
_EX6_PAIRS = [(o, d) for o in _EX6_FROM for d in _EX6_TO]
# The skill's "US East Coast": 12 origins, two pages of 6 against one destination.
_EAST = ("JFK", "LGA", "EWR", "BOS", "IAD", "DCA", "BWI", "PHL", "ATL", "MIA", "FLL", "CLT")

type Page = tuple[tuple[str, ...], tuple[str, ...]]


def _row(number: int, day: dt.date, frm: str, to: str, price: float) -> gfid.GFlightWithId:
    """One nonstop, departing at an hour its number fixes, so one number on two
    pages is one itinerary to `row_key`."""
    leg = _SEED.flight.legs[0]
    departs = dt.datetime.combine(day, dt.time(6 + number % 12, number % 60))
    leg = leg.model_copy(
        update={
            "flight_number": str(number),
            "departure_airport": getattr(Airport, frm),
            "arrival_airport": getattr(Airport, to),
            "departure_datetime": departs,
            "arrival_datetime": departs + dt.timedelta(hours=7),
        }
    )
    return replace(
        _SEED,
        flight=_SEED.flight.model_copy(update={"legs": [leg], "price": price, "currency": "USD"}),
        flight_id=f"id-{number}-{day}",
        operating=(),
    )


def _codes(side: list[Any]) -> tuple[str, ...]:
    return tuple(a[0].name for a in side)


@dataclass
class _Google:
    """Rung 1's GET, answering the airports each filter asks for.

    `pairs` numbers every (origin, destination) the search can ask, which is
    the flight number its row carries; `fare` prices it. `refuse(page)` is
    raised for every GET on that page; `added(page)` adds rows to its outbound
    board. `calls` records each GET as (page, pinned flight number or None)."""

    pairs: list[tuple[str, str]]
    fare: Callable[[int], float] = lambda i: 100.0 + i
    refuse: Callable[[Page], Exception | None] = lambda _page: None
    added: Callable[[Page], list[gfid.GFlightWithId]] = lambda _page: []
    calls: list[tuple[Page, int | None]] = field(default_factory=list[tuple[Page, int | None]])
    urls: list[str] = field(default_factory=list[str])

    def __call__(self, filters: Any, *, currency: str = "USD") -> gfid.Board[gfid.GFlightWithId]:
        out = filters.flight_segments[0]
        page: Page = (_codes(out.departure_airport), _codes(out.arrival_airport))
        picked = out.selected_flight
        self.calls.append((page, None if picked is None else int(picked.legs[0].flight_number)))
        self.urls.append(gfid.search_page_url(filters, currency=currency))
        refusal = self.refuse(page)
        if refusal is not None:
            raise refusal
        if picked is None:
            rows = [
                _row(i, _DEP, o, d, self.fare(i))
                for i, (o, d) in enumerate(self.pairs)
                if o in page[0] and d in page[1]
            ]
            return gfid.Board([*rows, *self.added(page)])
        flown = picked.legs[0]
        number = int(flown.flight_number)
        back_from, back_to = flown.arrival_airport.name, flown.departure_airport.name
        return gfid.Board(
            [
                _row(5000 + 10 * number + j, _RET, back_from, back_to, self.fare(number) + 50 + j)
                for j in range(2)
            ]
        )

    def pages(self) -> list[Page]:
        return list(dict.fromkeys(p for p, _ in self.calls))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """The throttle ladder sleeps between its rungs; these tests count GETs."""

    def _noop(*_a: object) -> None:
        return None

    def _zero() -> float:
        return 0.0

    monkeypatch.setattr(gfid.time, "sleep", _noop)
    monkeypatch.setattr(gfid.random, "random", _zero)


def _search(
    monkeypatch: pytest.MonkeyPatch,
    google: _Google,
    origin: str,
    destination: str,
    *extra: str,
    ret: bool = False,
) -> Result:
    monkeypatch.setattr(gfid, "_one_call", google)
    args = [
        "search",
        "--cash-only",
        "--no-google-url",
        "--no-matrix-url",
        origin,
        destination,
        "--dep",
        _DEP.isoformat(),
        *(["--return", _RET.isoformat()] if ret else []),
        *extra,
    ]
    return CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})


def _flat(text: str) -> str:
    return " ".join(text.split())


def _numbers(doc: list[Any]) -> list[int]:
    """The flight number of each one-way row of a JSON document."""
    return [int(r["legs"][0]["flight_number"]) for r in doc]


# ──────────────────────────────── one-way ─────────────────────────────────


def test_example_six_one_way_asks_four_pages_and_shows_every_row_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: `--backend gflight` refused 21 airports on one leg."""
    google = _Google(_EX6_PAIRS)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "200",
    )
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 4
    pages = google.pages()
    assert all(len(os) + len(ds) <= 11 for os, ds in pages)
    asked = [(o, d) for os, ds in pages for o in os for d in ds]
    assert sorted(asked) == sorted(_EX6_PAIRS)
    numbers = _numbers(json.loads(result.stdout))
    assert sorted(numbers) == list(range(len(_EX6_PAIRS)))  # every row, none twice
    assert numbers == sorted(numbers)  # in price order across the pages


def test_an_itinerary_two_pages_list_is_one_row_at_the_cheaper_fare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: the search was refused before any page."""

    def extra(page: Page) -> list[gfid.GFlightWithId]:
        price = 300.0 if page == (_EX6_FROM[:4], _EX6_TO[:7]) else 250.0
        return [_row(999, _DEP, "JFK", "LHR", price)] if page[0] == _EX6_FROM[:4] else []

    google = _Google(_EX6_PAIRS, added=extra)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "200",
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    shared = [r for r in doc if r["legs"][0]["flight_number"] == "999"]
    assert [r["price"] for r in shared] == [250.0]
    assert len(doc) == len(_EX6_PAIRS) + 1


def test_a_refused_page_leaves_the_others_and_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base: the search was refused before any page."""
    second: Page = (_EX6_FROM[:4], _EX6_TO[7:])
    google = _Google(
        _EX6_PAIRS, refuse=lambda page: GfUpstreamStatusError(503) if page == second else None
    )
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "200",
    )
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 4
    numbers = set(_numbers(json.loads(result.stdout)))
    missing = {i for i, (o, d) in enumerate(_EX6_PAIRS) if o in second[0] and d in second[1]}
    assert numbers == set(range(len(_EX6_PAIRS))) - missing
    printed = _flat(buf.getvalue())
    assert (
        "Google Flights page 2 of 4 (JFK,LGA,EWR,BOS→FCO,MUC,ZRH,VIE,CPH,DUB) is missing: "
        "Google Flights returned HTTP 503." in printed
    ), printed
    assert "page 1 of 4" not in printed
    assert "page 3 of 4" not in printed


def test_a_throttled_page_stops_the_pages_after_it_and_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base: the search was refused before any page."""
    second: Page = (_EX6_FROM[:4], _EX6_TO[7:])
    google = _Google(
        _EX6_PAIRS,
        refuse=lambda page: GfThrottledError("rate-limited") if page == second else None,
    )
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "200",
    )
    assert result.exit_code == 0, result.output
    # Page 2 spent its whole ladder; pages 3 and 4 were never fetched.
    assert google.pages() == [(_EX6_FROM[:4], _EX6_TO[:7]), second]
    assert len(google.calls) > 2
    numbers = set(_numbers(json.loads(result.stdout)))
    assert numbers == {
        i for i, (o, d) in enumerate(_EX6_PAIRS) if o in _EX6_FROM[:4] and d in _EX6_TO[:7]
    }
    printed = _flat(buf.getvalue())
    assert (
        "page 2 of 4 (JFK,LGA,EWR,BOS→FCO,MUC,ZRH,VIE,CPH,DUB) is missing: "
        "Google Flights rate-limited." in printed
    ), printed
    for n, to in ((3, _EX6_TO[:7]), (4, _EX6_TO[7:])):
        assert (
            f"page {n} of 4 ({','.join(_EX6_FROM[4:])}→{','.join(to)}) is missing: "
            "not asked after page 2 stopped the search." in printed
        ), printed


def _single_refusal_lines(
    monkeypatch: pytest.MonkeyPatch, origin: str, destination: str
) -> tuple[int, str]:
    google = _Google(
        [(o, d) for o in origin.split(",") for d in destination.split(",")],
        refuse=lambda _page: GfUpstreamStatusError(503),
    )
    buf = capture_err(monkeypatch)
    result = _search(monkeypatch, google, origin, destination, "--backend", "gflight", "--fast")
    return result.exit_code, _flat(buf.getvalue())


def test_every_page_failing_exits_one_as_one_page_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base: the chunked search exited 2, refused before any page."""
    one_code, one_err = _single_refusal_lines(monkeypatch, "JFK", "LHR")
    all_code, all_err = _single_refusal_lines(monkeypatch, ",".join(_EX6_FROM), ",".join(_EX6_TO))
    assert one_code == all_code == 1
    refusal = "Google Flights returned HTTP 503. Use --backend matrix"
    assert refusal in one_err, one_err
    assert refusal in all_err, all_err
    assert all(f"page {n} of 4" in all_err for n in range(1, 5)), all_err


def test_a_page_note_is_printed_as_written_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note comes from `_gf_refusal`, which escaped the remote text in it:
    markup in a browser's failure prints literally, with no backslash."""
    second: Page = (_EX6_FROM[:4], _EX6_TO[7:])
    google = _Google(
        _EX6_PAIRS,
        refuse=lambda page: (
            GfBrowserUnavailableError("[bold]Chrome[/] died\x1b", remedy="Retry [now].")
            if page == second
            else None
        ),
    )
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    printed = buf.getvalue()
    assert "[bold]Chrome[/] died Retry [now]." in printed, printed
    assert "\\[" not in printed and "\x1b" not in printed


def test_a_browser_search_holds_one_session_across_its_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each page's query closes its session as it returns; across the pages
    one Chrome is held and closed once."""
    google = _Google(_EX6_PAIRS)
    sessions: list[gfb.GfBrowserSession] = []

    def browser(
        filters: Any, *, headed: bool, currency: str = "USD"
    ) -> gfid.Board[gfid.GFlightWithId]:
        sessions.append(gfb.session(headed=headed))
        return google(filters, currency=currency)

    monkeypatch.setattr(gfid, "_one_call_browser", browser)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "--gf-transport",
        "browser",
    )
    assert result.exit_code == 0, result.output
    assert len(sessions) == 4
    assert len({id(s) for s in sessions}) == 1


# ─────────────────────────────── round trip ───────────────────────────────


def test_a_chunked_round_trip_pins_the_cheapest_outbounds_of_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages + min(n, 10) GETs: every page's outbounds first, then the ten
    cheapest of all of them, each pinned on its own page. Red at the base: the
    search was refused before any page."""
    pairs = _EX6_PAIRS
    # A permutation of the fares, so the cheapest ten fall on every page.
    google = _Google(pairs, fare=lambda i: 100.0 + (i * 37) % len(pairs))
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 4 + 10
    assert [pin for _, pin in google.calls[:4]] == [None] * 4
    cheapest = sorted(range(len(pairs)), key=lambda i: (i * 37) % len(pairs))[:10]
    pinned = [pin for _, pin in google.calls[4:]]
    assert sorted(p for p in pinned if p is not None) == sorted(cheapest)
    assert {page for page, _ in google.calls[4:]} == set(google.pages())  # all four pin
    for page, pin in google.calls[4:]:
        assert pin is not None
        o, d = pairs[pin]
        assert o in page[0] and d in page[1]  # pinned on its own page
    doc = json.loads(result.stdout)
    assert len(doc) == 10
    assert len({tuple(m["legs"][0]["flight_number"] for m in pair) for pair in doc}) == 10
    printed = _flat(buf.getvalue())
    assert (
        "Google Flights asked this round trip as 4 pages of at most 11 airports; each "
        "return is priced within its own page's airports." in printed
    ), printed


def test_a_round_trip_with_few_pins_spends_them_where_the_cheapest_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12 origins to LAX is two pages; `-n 3` pins three outbounds over both:
    2 + 3 GETs, and a page holding none of the three costs no pin."""
    pairs = [(o, "LAX") for o in _EAST]
    # ATL, MIA (page 2) and JFK (page 1) are the three cheapest.
    fares = {"ATL": 100.0, "MIA": 101.0, "JFK": 102.0}
    google = _Google(pairs, fare=lambda i: fares.get(pairs[i][0], 200.0 + i))
    result = _search(
        monkeypatch,
        google,
        ",".join(_EAST),
        "LAX",
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "3",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 2 + 3
    first, second = (_EAST[:6], ("LAX",)), (_EAST[6:], ("LAX",))
    assert google.calls[2:] == [
        (first, _EAST.index("JFK")),
        (second, _EAST.index("ATL")),
        (second, _EAST.index("MIA")),
    ]
    doc = json.loads(result.stdout)
    assert [pair[0]["legs"][0]["flight_number"] for pair in doc] == [
        str(_EAST.index("ATL")),
        str(_EAST.index("ATL")),
        str(_EAST.index("MIA")),
    ]


def test_a_page_with_no_pin_costs_no_further_get(monkeypatch: pytest.MonkeyPatch) -> None:
    pairs = [(o, "LAX") for o in _EAST]
    # Page 2's six are the cheapest, so `-n 3` pins nothing on page 1.
    google = _Google(pairs, fare=lambda i: 100.0 + i if i >= 6 else 300.0 + i)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EAST),
        "LAX",
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        "-n",
        "3",
        ret=True,
    )
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 2 + 3
    assert {page for page, pin in google.calls if pin is not None} == {(_EAST[6:], ("LAX",))}


def _round_trip_throttled_on_page_two(
    monkeypatch: pytest.MonkeyPatch, *, from_get: int
) -> tuple[Result, _Google, str]:
    """The Example 6 round trip, page 2 throttled from its `from_get`-th GET on."""
    second: Page = (_EX6_FROM[:4], _EX6_TO[7:])
    gets: list[Page] = []

    def refuse(page: Page) -> Exception | None:
        gets.append(page)
        return GfThrottledError("rate-limited") if gets.count(second) >= from_get else None

    google = _Google(_EX6_PAIRS, fare=lambda i: 100.0 + (i * 37) % len(_EX6_PAIRS), refuse=refuse)
    buf = capture_err(monkeypatch)
    result = _search(
        monkeypatch,
        google,
        ",".join(_EX6_FROM),
        ",".join(_EX6_TO),
        "--backend",
        "gflight",
        "--fast",
        "--format",
        "json",
        ret=True,
    )
    return result, google, _flat(buf.getvalue())


def _missing(n: int, why: str) -> str:
    frm = _EX6_FROM[:4] if n <= 2 else _EX6_FROM[4:]
    to = _EX6_TO[:7] if n % 2 else _EX6_TO[7:]
    return f"page {n:d} of 4 ({','.join(frm)}→{','.join(to)}) is missing: {why}."


def test_a_throttle_on_an_outbound_names_the_page_before_it_for_its_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every outbound is asked before any return, so page 1's returns come
    after page 2's throttle and are not asked; page 1 was, and says so."""
    result, google, printed = _round_trip_throttled_on_page_two(monkeypatch, from_get=1)
    assert result.exit_code == 1, result.output
    assert google.pages() == [(_EX6_FROM[:4], _EX6_TO[:7]), (_EX6_FROM[:4], _EX6_TO[7:])]
    assert all(pin is None for _, pin in google.calls)
    stopped = "not asked after page 2 stopped the search"
    assert _missing(1, f"its returns were {stopped}") in printed, printed
    assert _missing(2, "Google Flights rate-limited") in printed, printed
    assert _missing(3, stopped) in printed, printed
    assert _missing(4, stopped) in printed, printed


def test_a_throttle_on_a_pin_names_the_later_pages_for_their_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages 3 and 4 answered their outbounds; only their pins were stopped.
    Page 1's pins came before the throttle and its round trips stand."""
    result, google, printed = _round_trip_throttled_on_page_two(monkeypatch, from_get=2)
    assert result.exit_code == 0, result.output
    assert [pin for _, pin in google.calls[:4]] == [None] * 4
    pinned_pages = list(dict.fromkeys(page for page, pin in google.calls if pin is not None))
    assert pinned_pages == [(_EX6_FROM[:4], _EX6_TO[:7]), (_EX6_FROM[:4], _EX6_TO[7:])]
    first = {i for i, (o, d) in enumerate(_EX6_PAIRS) if o in _EX6_FROM[:4] and d in _EX6_TO[:7]}
    doc = json.loads(result.stdout)
    assert doc
    assert {int(pair[0]["legs"][0]["flight_number"]) for pair in doc} <= first
    returns = "its returns were not asked after page 2 stopped the search"
    assert "page 1 of 4" not in printed, printed
    assert _missing(2, "Google Flights rate-limited") in printed, printed
    assert _missing(3, returns) in printed, printed
    assert _missing(4, returns) in printed, printed


def test_every_page_filtered_empty_hands_the_round_trip_to_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows the filter removed from every page's outbounds are why the
    board is empty, pinned or not, so auto hands the search to Matrix as one
    page does. Red at the base: auto sent 12 airports to Matrix unasked."""
    handed: list[tuple[Leg, ...]] = []

    def matrix(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        handed.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path", matrix)
    printed: list[str] = []
    for origin in ("JFK", ",".join(_EAST)):
        pairs = [(o, "LAX") for o in origin.split(",")]
        google = _Google(pairs)
        buf = capture_err(monkeypatch)
        result = _search(
            monkeypatch,
            google,
            origin,
            "LAX",
            "--fast",
            "--format",
            "json",
            "--max-price",
            "1",
            ret=True,
        )
        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert all(pin is None for _, pin in google.calls)
        assert len(google.calls) == (1 if origin == "JFK" else 2)
        printed.append(_flat(buf.getvalue()))
    assert len(handed) == 2
    assert "Using Matrix: no Google Flights itinerary matched" in printed[0], printed[0]
    assert "(1 rows filtered out)" in printed[0], printed[0]
    assert "Using Matrix: no Google Flights itinerary matched" in printed[1], printed[1]
    assert "(12 rows filtered out)" in printed[1], printed[1]


# ──────────────────────────────── one page ────────────────────────────────


@pytest.mark.parametrize("ret", [False, True], ids=["one-way", "round-trip"])
def test_a_search_that_fits_one_page_sends_the_request_it_always_sent(
    monkeypatch: pytest.MonkeyPatch, ret: bool
) -> None:
    """Green at the base: eleven airports are one page, asked once, by the
    URL the search's own filter builds."""
    origins = _EAST[:10]
    legs: tuple[Leg, ...] = (Leg.of(origins, "LAX", _DEP),)
    if ret:
        legs += (Leg.of("LAX", origins, _RET),)
    opts = SearchOptions(page_size=3)
    google = _Google([(o, "LAX") for o in origins])
    monkeypatch.setattr(gfid, "_one_call", google)
    board = cli._gflight_results(legs, opts, 3)
    expected = gfid.search_page_url(to_fli_filter(SpecificDateSearch(legs=legs, options=opts)))
    assert google.urls[0] == expected
    assert len(google.calls) == (1 + 3 if ret else 1)
    assert len(board) == (3 * 2 if ret else 10)


def test_a_one_page_refusal_is_raised_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the base: one page's failure is the search's own error."""
    google = _Google([("JFK", "LAX")], refuse=lambda _page: GfUpstreamStatusError(503))
    monkeypatch.setattr(gfid, "_one_call", google)
    with pytest.raises(GfBackendError) as caught:
        cli._gflight_results((Leg.of("JFK", "LAX", _DEP),), SearchOptions(), 10)
    assert isinstance(caught.value, GfUpstreamStatusError)
    assert len(google.calls) == 1
