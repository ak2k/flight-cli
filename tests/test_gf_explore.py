# pyright: reportPrivateUsage=false
"""`flight explore`: where one origin flies, read off Google Flights' explore page.

The envelopes under `fixtures/gf_explore/` are `GetExploreDestinations`
responses the page received in a real Chrome, trimmed to the fields the parser
reads (images, coordinates, tokens and the filter panel are dropped). No test
here loads a page: the browser session is replaced wherever the path would reach
one.
"""

from __future__ import annotations

import json
import pathlib
import signal
import urllib.parse
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import _gf_browser as gfb
from flight_cli import _gf_explore as ge
from flight_cli import cli
from flight_cli._gf_browser import CapturedResponse
from flight_cli._gf_errors import GfThrottledError
from flight_cli._gf_rpc_shared import GfPageRpcError, refuse_a_wall
from flight_cli.links import google_flights_explore_url

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
NOV = date(2026, 11, 1)
_RPC_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetExploreDestinations?f.sid=1&rt=c"
)
_KEYS = [
    "code",
    "name",
    "country",
    "price",
    "currency",
    "departure",
    "return",
    "nights",
    "carrier",
    "stops",
    "duration_min",
]


def _fixture(name: str) -> str:
    return (FIXTURES / "gf_explore" / name).read_text()


# ───────────────────────────── the URL ───────────────────────────────────────


def _tfs(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]


@pytest.mark.parametrize(
    ("kwargs", "tfs"),
    [
        # The page's own URL for JFK with no filters: the next six months, one week.
        pytest.param(
            {"month": None, "trip_length": None, "max_price": None},
            "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBcAKCAQsI____________AZgBAQ",
            id="page-default",
        ),
        # The page's own URL for "Weekend trip in November", "up to $300".
        pytest.param(
            {"month": 11, "trip_length": 1, "max_price": 300},
            "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBYKwCcAKCAQQIChABmAEB",
            id="page-weekend-november-300",
        ),
        # Built and served: one week in November, two weeks in January.
        pytest.param(
            {"month": 11, "trip_length": None, "max_price": None},
            "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBcAKCAQIICpgBAQ",
            id="week-november",
        ),
        pytest.param(
            {"month": 1, "trip_length": 3, "max_price": None},
            "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBcAKCAQQIABADmAEB",
            id="two-weeks-january",
        ),
    ],
)
def test_the_explore_url_carries_the_tfs_the_page_writes(kwargs: dict[str, Any], tfs: str) -> None:
    url = google_flights_explore_url("JFK", **kwargs)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert parts.path == "/travel/explore"
    assert _tfs(url) == tfs
    assert (query["tfu"], query["gl"], query["curr"]) == (["GgA"], ["US"], ["USD"])


# ───────────────────────────── choices ───────────────────────────────────────


@pytest.mark.parametrize(
    ("lo", "hi", "names"),
    [
        pytest.param(5, 7, ["one week"], id="5-7"),
        pytest.param(2, 3, ["weekend"], id="2-3"),
        pytest.param(14, 14, ["two weeks"], id="14"),
        pytest.param(3, 7, ["weekend", "one week"], id="spans-two"),
        pytest.param(10, 12, [], id="between"),
        pytest.param(5, 5, [], id="gap"),
    ],
)
def test_a_nights_range_picks_the_trip_lengths_it_overlaps(
    lo: int, hi: int, names: list[str]
) -> None:
    assert [t.name for t in ge.trip_lengths_overlapping(lo, hi)] == names


def test_the_open_months_are_this_one_and_the_next_five_across_a_year() -> None:
    assert ge.months_open(date(2026, 9, 27)) == [
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2027, 1, 1),
        date(2027, 2, 1),
    ]


# ───────────────────────────── the parser ────────────────────────────────────


def test_one_week_in_november_lists_destinations_with_the_fares_airport() -> None:
    found = ge.parse_destinations(_fixture("jfk_nov_one_week.body"), origin="JFK", month=NOV)
    answer = ge.ExploreAnswer("USD", tuple(found))
    priced = answer.priced
    assert (len(found), len(priced)) == (85, 59)
    assert [p.price for p in priced] == sorted(p.price or 0 for p in priced)
    assert all(p.departure is not None and p.departure.month == 11 for p in priced)
    assert {p.nights for p in priced} == {6, 7, 8, 9}
    by_name = {p.name: p for p in priced}
    # Miami's fare flies to FLL, not the city's own airport.
    assert by_name["Miami"].code == "FLL"
    assert (by_name["Miami"].carrier, by_name["Miami"].stops) == ("Delta", 0)
    # A trip on two airlines is named by its carrier names, not "multi".
    assert by_name["Las Vegas"].carrier == "Alaska and JetBlue"


def test_a_price_cap_removes_prices_not_destinations() -> None:
    found = ge.parse_destinations(
        _fixture("jfk_nov_weekend_under_300.body"), origin="JFK", month=NOV
    )
    priced = ge.ExploreAnswer("USD", tuple(found)).priced
    assert (len(found), len(priced)) == (85, 16)
    assert max(p.price or 0 for p in priced) <= 300
    assert [(p.name, p.code) for p in priced[:2]] == [("Dallas", "DFW"), ("Fort Worth", "DFW")]
    assert {p.nights for p in priced} <= {1, 2, 3, 4}


def test_an_unpriced_destination_carries_no_flight_facts() -> None:
    found = ge.parse_destinations(
        _fixture("jfk_nov_weekend_under_300.body"), origin="JFK", month=NOV
    )
    unpriced = [d for d in found if d.price is None]
    assert unpriced
    assert all(d.stops is None and d.duration_min is None for d in unpriced)


def test_a_page_answering_for_another_origin_is_refused() -> None:
    """Without an origin the page geolocates; an answer for somewhere else must
    not be printed as this origin's."""
    with pytest.raises(GfPageRpcError, match="answered for JFK, not EWR"):
        ge.parse_destinations(_fixture("jfk_nov_one_week.body"), origin="EWR", month=NOV)


def test_a_departure_outside_the_month_asked_for_is_refused() -> None:
    with pytest.raises(GfPageRpcError, match="outside 2026-12"):
        ge.parse_destinations(
            _fixture("jfk_nov_one_week.body"), origin="JFK", month=date(2026, 12, 1)
        )


def _explore_body(info: list[list[Any]], prices: list[list[Any]], *, origin: str = "JFK") -> str:
    first = [None, None, None, [info], None, None, [["New York", None, None, origin]]]
    second = [None, None, None, None, [prices]]
    chunks = [json.dumps([["wrb.fr", None, json.dumps(p)]]) for p in (first, second)]
    return ")]}'\n" + "".join(f"\n{len(c) + 1}\n{c}" for c in chunks) + "\n"


def _info(mid: str, name: str, dep: str = "2026-11-06", ret: str = "2026-11-08") -> list[Any]:
    return [mid, None, name, None, "United States", *([None] * 6), dep, ret, None, None, "XXX"]


def _fare(mid: str, price: int, airport: str = "AUS") -> list[Any]:
    return [
        mid,
        [[None, price], None],
        None,
        None,
        None,
        None,
        ["AA", "American", 0, 200, None, airport],
    ]


def test_no_destinations_is_a_refusal_not_an_empty_list() -> None:
    with pytest.raises(GfPageRpcError, match="no destinations"):
        ge.parse_destinations(_explore_body([], []), origin="JFK", month=None)


def test_the_error_13_body_is_a_typed_refusal() -> None:
    with pytest.raises(GfPageRpcError) as caught:
        ge.parse_destinations(
            (FIXTURES / "gf_rpc" / "error13.body").read_text(), origin="JFK", month=None
        )
    assert caught.value.code == 13


def test_an_unreadable_date_is_a_refusal() -> None:
    body = _explore_body([_info("/m/1", "Austin", dep="11/06")], [])
    with pytest.raises(GfPageRpcError, match="date"):
        ge.parse_destinations(body, origin="JFK", month=None)


# ───────────────────────────── the command ───────────────────────────────────


class _FakeSession:
    def __init__(self, answer: str | BaseException) -> None:
        self._answer = answer
        self.urls: list[str] = []
        self.checks: list[object] = []
        self.held: list[tuple[int, object]] = []

    def capture(
        self,
        url: str,
        wanted: Callable[[str], bool],
        *,
        click: object = None,
        check_page: Callable[..., object] | None = None,
    ) -> CapturedResponse:
        assert click is None
        assert wanted(_RPC_URL)
        assert not wanted(_RPC_URL.replace("GetExploreDestinations", "GetBookingResults"))
        self.urls.append(url)
        self.checks.append(check_page)
        self.held.append((getattr(gfb._scope_depth, "n", 0), signal.getsignal(signal.SIGINT)))
        if isinstance(self._answer, BaseException):
            raise self._answer
        return CapturedResponse(url=_RPC_URL, status=200, body=self._answer)


@pytest.fixture(autouse=True)
def _fixed_today_and_wide_consoles(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """The captures are of November 2026, which `--month` may name from here.

    The consoles are wide and write to whatever `sys.stdout`/`sys.stderr` is
    when they print, so the runner captures a nine-column table unwrapped."""
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 27))
    monkeypatch.setattr(cli, "console", Console(width=300, no_color=True, highlight=False))
    monkeypatch.setattr(cli, "err", Console(stderr=True, width=300, no_color=True, highlight=False))


def _serve(monkeypatch: pytest.MonkeyPatch, answer: str | BaseException) -> _FakeSession:
    fake = _FakeSession(answer)

    def _session(*, headed: bool) -> _FakeSession:
        del headed
        return fake

    monkeypatch.setattr(gfb, "session", _session)
    return fake


def _no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*, headed: bool) -> object:
        raise AssertionError("this run must not open Chrome")

    monkeypatch.setattr(gfb, "session", _forbidden)


def _explore(*args: str) -> Any:
    return CliRunner().invoke(cli.app, ["explore", *args])


def test_json_is_a_stable_list_of_priced_destinations_cheapest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _serve(monkeypatch, _fixture("jfk_nov_one_week.body"))
    result = _explore("JFK", "--month", "2026-11", "--days", "5-7", "--format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert len(doc) == 59
    assert all(list(row) == _KEYS for row in doc)
    assert [row["price"] for row in doc] == sorted(row["price"] for row in doc)
    assert doc[0] == {
        "code": "CHS",
        "name": "Charleston",
        "country": "United States",
        "price": 199,
        "currency": "USD",
        "departure": "2026-11-07",
        "return": "2026-11-14",
        "nights": 7,
        "carrier": "Delta",
        "stops": 0,
        "duration_min": 148,
    }
    assert _tfs(fake.urls[0]) == "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBcAKCAQIICpgBAQ"
    assert fake.checks == [refuse_a_wall]
    assert "26 more destinations have no price" in result.stderr


def test_the_table_names_each_destination_and_its_airport(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _fixture("jfk_nov_weekend_under_300.body"))
    result = _explore("JFK", "--month", "2026-11", "--days", "2-3", "--max-price", "300")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "from JFK" in out
    assert "November 2026" in out
    assert "weekend (1-4 nights)" in out
    assert "up to USD300" in out
    assert out.index("Dallas") < out.index("Austin")
    assert "DFW" in out
    assert "USD217.00" in out
    assert "nonstop" in out


def test_a_cap_below_every_price_is_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _explore_body(
        [_info("/m/1", "Austin")],
        [["/m/1", None, None, None, None, None, ["multi", "", 0, 0, None, ""]]],
    )
    _serve(monkeypatch, body)
    table = _explore("JFK", "--max-price", "50")
    assert table.exit_code == 0, table.output
    assert "No destination from JFK is priced at or under USD50" in table.stdout
    _serve(monkeypatch, body)
    doc = _explore("JFK", "--max-price", "50", "--format", "json")
    assert doc.exit_code == 0, doc.output
    assert json.loads(doc.stdout) == []


def test_a_price_chunk_that_cannot_be_read_is_a_refusal_not_none_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destinations arrive a chunk ahead of their prices, so a body read
    only that far lists every destination unpriced, which under a cap reads as
    "none under P"."""
    lines = _explore_body([_info("/m/1", "Austin")], [_fare("/m/1", 40)]).split("\n")
    lines[-3:-1] = ["BROKEN PRICE CHUNK"]
    _serve(monkeypatch, "\n".join(lines))
    result = _explore("JFK", "--max-price", "300", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "could not be read" in result.stderr
    assert result.stdout == ""


def test_none_priced_without_a_cap_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _explore_body([_info("/m/1", "Austin")], []))
    result = _explore("JFK", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "priced none of its 1 destinations" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("args", "said"),
    [
        pytest.param(["NYC"], "city code", id="metro-code"),
        pytest.param(["JFK,EWR"], "one origin", id="two-airports"),
        pytest.param(["JFK", "--month", "2027-03"], "2026-09 to 2027-02", id="month-too-far"),
        pytest.param(["JFK", "--month", "2026-08"], "2026-09 to 2027-02", id="month-past"),
        pytest.param(["JFK", "--month", "2026-13"], "YYYY-MM", id="month-malformed"),
        pytest.param(["JFK", "--month", "0000-05"], "YYYY-MM", id="month-year-zero"),
        pytest.param(["JFK", "--days", "3-7"], "weekend (1-4 nights)", id="days-span-two"),
        pytest.param(["JFK", "--days", "10-12"], "two weeks (13-16 nights)", id="days-in-a-gap"),
        pytest.param(
            ["JFK", "--days", "3-7"],
            "overlaps weekend and one week; it must overlap exactly one",
            id="days-span-two-names-both",
        ),
        pytest.param(
            ["JFK", "--days", "10-12"],
            "overlaps no trip length; it must overlap exactly one",
            id="days-in-a-gap-says-so",
        ),
        pytest.param(["JFK", "--days", "7-5"], "below min", id="days-reversed"),
    ],
)
def test_a_question_the_page_cannot_ask_is_refused_before_chrome(
    monkeypatch: pytest.MonkeyPatch, args: list[str], said: str
) -> None:
    _no_chrome(monkeypatch)
    result = _explore(*args)
    assert result.exit_code == 2, result.output
    assert said in " ".join(result.stderr.split())
    assert result.stdout == ""


def test_the_month_is_written_without_a_year_and_the_default_is_six_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _serve(monkeypatch, _fixture("jfk_nov_one_week.body"))
    assert _explore("JFK", "--format", "json").exit_code == 0
    assert (
        _tfs(fake.urls[0]) == "CBwQAxoJagcIARIDSkZLGglyBwgBEgNKRktAAUgBcAKCAQsI____________AZgBAQ"
    )


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        pytest.param("error13", "error 13", id="error-13"),
        pytest.param(
            GfThrottledError("Google Flights rate-limited the browser; wait a while and retry"),
            "rate-limited",
            id="sorry-wall",
        ),
    ],
)
def test_a_refusal_fails_the_command_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, answer: str | BaseException, said: str
) -> None:
    body = (FIXTURES / "gf_rpc" / "error13.body").read_text() if answer == "error13" else answer
    _serve(monkeypatch, body)
    result = _explore("JFK", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "No explore results" in result.stderr
    assert said in result.stderr
    assert result.stdout == ""


def test_explore_runs_guarded_and_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _serve(monkeypatch, _fixture("jfk_nov_one_week.body"))
    closed: list[bool] = []
    monkeypatch.setattr(gfb, "close_thread_session", lambda: closed.append(True))
    before = signal.getsignal(signal.SIGINT)
    assert _explore("JFK", "--format", "json").exit_code == 0
    depth, handler = fake.held[0]
    assert depth == 1
    assert handler is not before
    assert closed == [True]
    assert signal.getsignal(signal.SIGINT) is before


def test_remote_destination_text_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _explore_body([_info("/m/1", "[bold]Aus\x1b[2Jtin[/x]")], [_fare("/m/1", 99, "[red]")])
    _serve(monkeypatch, body)
    result = _explore("JFK")
    assert result.exit_code == 0, result.output
    assert "[bold]Aus" in result.stdout
    assert "\x1b" not in result.stdout
    assert "[red]" in result.stdout
