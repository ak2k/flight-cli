# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search --split`: the cheapest one-way each way, beside a round trip.

Google is faked at `cli._gflight_results`, which answers a round trip's two legs
with (outbound, return) combinations and a one-way's leg with one-way rows, keyed
on the leg's origin. Matrix is faked at `_matrix_into` on the enriched path and
at `_run_matrix_path` where Matrix answers the search instead. No test reaches
Google, Matrix or Chrome."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from fli.models import Airline, Airport
from typer.testing import CliRunner

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_errors import GfThrottledError
from flight_cli.domain import Leg, SearchOptions
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_DEP = dt.date.today() + dt.timedelta(days=45)
_RET = dt.date.today() + dt.timedelta(days=52)
_CAPTURE = pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
_SEED = gfid._parse_flight_with_id(gfid._rows_from_ds1(json.loads(_CAPTURE.read_text())).rows[1])


def _row(
    flights: str,
    frm: str,
    to: str,
    day: dt.date,
    price: float | None,
    *,
    currency: str = "USD",
    at: dt.time = dt.time(7, 0),
    hours: int = 2,
) -> gfid.GFlightWithId:
    """A row flying `flights` ('B6188+B6917'), connecting at ORD between legs:
    the first leaves `day` at `at`, each flies `hours`, three hours apart."""
    hops = flights.split("+")
    airports = [frm, *["ORD"] * (len(hops) - 1), to]
    legs: list[Any] = []
    for i, hop in enumerate(hops):
        departs = dt.datetime.combine(day, at) + dt.timedelta(hours=3 * i)
        legs.append(
            _SEED.flight.legs[0].model_copy(
                update={
                    "airline": getattr(Airline, hop[:2]),
                    "flight_number": hop[2:],
                    "departure_airport": getattr(Airport, airports[i]),
                    "arrival_airport": getattr(Airport, airports[i + 1]),
                    "departure_datetime": departs,
                    "arrival_datetime": departs + dt.timedelta(hours=hours),
                }
            )
        )
    return replace(
        _SEED,
        flight=_SEED.flight.model_copy(
            update={"legs": legs, "price": price, "currency": currency, "stops": len(legs) - 1}
        ),
        flight_id=f"id-{flights}-{day}",
        operating=(),
    )


def _round_trip() -> list[Any]:
    """Two combinations; the second is cheaper, so price order swaps them."""
    return [
        (_row("AA10", "JFK", "LAX", _DEP, 500.0), _row("AA11", "LAX", "JFK", _RET, 520.0)),
        (_row("AA20", "JFK", "LAX", _DEP, 450.0), _row("AA21", "LAX", "JFK", _RET, 470.0)),
    ]


def _outbound(price: float = 229.0) -> list[Any]:
    """Unpriced first and a dearer fare beside it: the pick is the cheapest priced."""
    return [
        _row("DL5", "JFK", "LAX", _DEP, None),
        _row("DL100", "JFK", "LAX", _DEP, 300.0),
        _row("DL747", "JFK", "LAX", _DEP, price),
    ]


def _back(price: float = 184.0) -> list[Any]:
    return [
        _row("B6300", "LAX", "JFK", _RET, 250.0),
        _row("B6188+B6917", "LAX", "JFK", _RET, price),
    ]


@dataclass
class _Google:
    """`_gflight_results`, answering each leg set and recording every call."""

    one_ways: dict[str, list[Any] | Exception]
    round_trip: list[Any] | Exception = field(default_factory=_round_trip)
    dropped: int = 0
    calls: list[tuple[tuple[Leg, ...], SearchOptions, str, bool]] = field(
        default_factory=list[tuple[tuple[Leg, ...], SearchOptions, str, bool]]
    )

    def __call__(
        self,
        legs: tuple[Leg, ...],
        opts: SearchOptions,
        _top_n: int,
        gf_mode: str = "http",
        gf_headed: bool = False,
        **_kw: object,
    ) -> gfid.Board[Any]:
        self.calls.append((legs, opts, gf_mode, gf_headed))
        answer = self.round_trip if len(legs) == 2 else self.one_ways[legs[0].origins[0]]
        if isinstance(answer, Exception):
            raise answer
        return gfid.Board(answer, dropped=self.dropped if len(legs) == 2 else 0)


def _google(
    monkeypatch: pytest.MonkeyPatch,
    out: list[Any] | Exception | None = None,
    back: list[Any] | Exception | None = None,
    **kw: Any,
) -> _Google:
    google = _Google(
        {"JFK": _outbound() if out is None else out, "LAX": _back() if back is None else back},
        **kw,
    )
    monkeypatch.setattr(cli, "_gflight_results", google)
    return google


def _matrix_answers() -> Callable[..., Any]:
    async def _answer(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        slices = [
            {
                "flights": [flight],
                "departure": f"{day}T08:00",
                "arrival": f"{day}T11:00",
                "origin": {"code": frm},
                "destination": {"code": to},
            }
            for flight, day, frm, to in (("UA1", _DEP, "JFK", "LAX"), ("UA2", _RET, "LAX", "JFK"))
        ]
        solution = {"displayTotal": "USD610.00", "itinerary": {"slices": slices}}
        state["matrix"] = SearchResult.model_validate({"solutions": [solution], "solutionCount": 1})

    return _answer


async def _no_matrix(*_a: object, **_kw: object) -> None:
    return None


def _search(*extra: str, ret: bool = True, back_day: dt.date = _RET, to: str = "LAX") -> Result:
    args = [
        "search",
        "JFK",
        to,
        "--dep",
        _DEP.isoformat(),
        *(["--return", back_day.isoformat()] if ret else []),
        "--no-matrix-url",
        "--no-google-url",
        *extra,
    ]
    return CliRunner().invoke(cli.app, args, env={"COLUMNS": "200", "NO_COLOR": "1"})


def _split_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("Two one-way tickets:")]


_LINE = (
    "Two one-way tickets: USD229.00 (DL747) out + USD184.00 (B6188+B6917) back = USD413.00, "
    "booked as two separate tickets."
)


# ──────────────────────────────── the table ───────────────────────────────


def test_fast_table_adds_one_line_after_the_unchanged_round_trip_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _google(monkeypatch)
    plain = _search("--cash-only", "--fast")
    split = _search("--cash-only", "--fast", "--split")
    assert plain.exit_code == 0, plain.output
    assert split.exit_code == 0, split.output
    assert _split_lines(split.stdout) == [_LINE]
    assert split.stdout.index("Google Flights · JFK→LAX") < split.stdout.index(_LINE)
    # The round-trip rows are neither replaced nor reordered: the line is all
    # that --split adds to stdout.
    assert split.stdout.replace(_LINE + "\n", "") == plain.stdout
    assert _split_lines(plain.stdout) == []


@pytest.mark.parametrize(
    ("out", "back", "line"),
    [
        pytest.param(
            229.5,
            184.25,
            "Two one-way tickets: USD229.50 (DL747) out + USD184.25 (B6188+B6917) back = "
            "USD413.75, booked as two separate tickets.",
            id="cents",
        ),
        pytest.param(229.0, 184.0, _LINE, id="whole"),
    ],
)
def test_the_line_states_each_fare_and_their_sum(
    monkeypatch: pytest.MonkeyPatch, out: float, back: float, line: str
) -> None:
    _google(monkeypatch, _outbound(out), _back(back))
    result = _search("--cash-only", "--fast", "--split")
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == [line]


def test_enriched_line_follows_the_merged_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _google(monkeypatch)
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers())
    result = _search("--cash-only", "--split")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert _split_lines(out) == [_LINE]
    assert out.index("Google Flights · JFK→LAX") < out.index("Google Flights + Matrix")
    assert out.index("Google Flights + Matrix") < out.index(_LINE)


def test_enriched_line_follows_the_google_table_when_matrix_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _google(monkeypatch)
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    result = _search("--cash-only", "--split")
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == [_LINE]
    assert result.stdout.index("Google Flights · JFK→LAX") < result.stdout.index(_LINE)
    assert "Google Flights + Matrix" not in result.stdout


def test_enriched_round_trip_failure_asks_no_one_way(monkeypatch: pytest.MonkeyPatch) -> None:
    google = _google(monkeypatch, round_trip=GfThrottledError("rate-limited"))
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers())
    result = _search("--cash-only", "--split")
    assert result.exit_code == 0, result.output
    assert len(google.calls) == 1
    assert _split_lines(result.stdout) == []
    assert (
        result.stderr.count(
            "No split tickets: the Google Flights round trip failed, so no one-way was asked."
        )
        == 1
    )


def test_remote_flight_text_is_escaped_in_the_line(monkeypatch: pytest.MonkeyPatch) -> None:
    out = [_row("DL747[/x]", "JFK", "LAX", _DEP, 229.0, currency="U[/]S")]
    back = [_row("B6188", "LAX", "JFK", _RET, 184.0, currency="U[/]S")]
    _google(monkeypatch, out, back)
    result = _search("--cash-only", "--fast", "--split")
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == [
        "Two one-way tickets: U[/]S229.00 (DL747[/x]) out + U[/]S184.00 (B6188) back = "
        "U[/]S413.00, booked as two separate tickets."
    ]


# ──────────────────────────────── the queries ─────────────────────────────


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(["--fast"], id="fast"),
        pytest.param(["--fast", "--format", "json"], id="json"),
        pytest.param([], id="enriched"),
    ],
)
def test_split_asks_exactly_each_leg_alone_without_the_cap(
    monkeypatch: pytest.MonkeyPatch, mode: list[str]
) -> None:
    monkeypatch.setattr(cli, "_matrix_into", _matrix_answers())
    argv = ("--cash-only", "--max-price", "900", "--gf-headed", *mode)
    plain = _google(monkeypatch)
    assert _search(*argv).exit_code == 0
    split = _google(monkeypatch)
    result = _search(*argv, "--split")
    assert result.exit_code == 0, result.output
    assert len(split.calls) == len(plain.calls) + 2
    (rt_legs, rt_opts, rt_mode, rt_headed), *_ = split.calls
    assert rt_opts.max_price == 900
    out, back = split.calls[-2:]
    assert out[0] == (rt_legs[0],)
    assert back[0] == (rt_legs[1],)
    for _legs, opts, gf_mode, gf_headed in (out, back):
        assert opts == rt_opts.model_copy(update={"max_price": None})
        assert (gf_mode, gf_headed) == (rt_mode, rt_headed) == ("http", True)


def test_the_help_counts_two_loads_a_page_on_a_leg_asked_as_several() -> None:
    """Each one-way leg is asked as its own pages, so twelve origins cost four
    more loads, not the two a one-page leg costs."""
    import click
    import typer

    group = typer.main.get_command(cli.app)
    assert isinstance(group, click.Group)
    (split,) = [
        p
        for p in group.commands["search"].params
        if isinstance(p, click.Option) and "--split" in p.opts
    ]
    help_text = " ".join((split.help or "").split())
    assert "(two more page loads, two per page on a leg asked as several pages)" in help_text, (
        help_text
    )


def test_without_the_flag_nothing_more_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    google = _google(monkeypatch)
    result = _search("--cash-only", "--fast")
    assert result.exit_code == 0, result.output
    assert [len(legs) for legs, *_ in google.calls] == [2]


# ──────────────────────────────── the document ────────────────────────────


def _json_row(row: Any) -> Any:
    return json.loads(json.dumps(cli._gflight_json_row(row), default=str))


@pytest.mark.parametrize(
    ("out", "back", "total"),
    [
        pytest.param(229.0, 184.0, 413, id="whole-is-an-int"),
        pytest.param(229.5, 184.25, 413.75, id="cents"),
    ],
)
def test_json_wraps_the_unchanged_search_document(
    monkeypatch: pytest.MonkeyPatch, out: float, back: float, total: float
) -> None:
    outbound, returning = _outbound(out), _back(back)
    _google(monkeypatch, outbound, returning)
    plain = _search("--cash-only", "--fast", "--format", "json")
    wrapped = _search("--cash-only", "--fast", "--format", "json", "--split")
    assert plain.exit_code == 0, plain.output
    assert wrapped.exit_code == 0, wrapped.output
    doc = json.loads(wrapped.stdout)
    assert set(doc) == {"search", "split_ticket"}
    assert doc["search"] == json.loads(plain.stdout)
    assert doc["split_ticket"] == {
        "outbound": _json_row(outbound[2]),
        "return": _json_row(returning[1]),
        "total": total,
        "currency": "USD",
    }
    assert type(doc["split_ticket"]["total"]) is type(total)


def test_an_empty_round_trip_board_still_carries_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    _google(monkeypatch, round_trip=[])
    argv = ("--cash-only", "--fast", "--backend", "gflight")
    result = _search(*argv, "--format", "json", "--split")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["search"] == []
    assert doc["split_ticket"]["total"] == 413
    table = _search(*argv, "--split")
    assert table.exit_code == 0, table.output
    assert table.stdout.index("Google Flights: no results.") < table.stdout.index(_LINE)


# ──────────────────────────── a pair one traveler flies ───────────────────


def _same_day_out() -> list[Any]:
    """The cheapest lands after midnight, after every return of the day leaves.
    The next lands at 10:00 off its second flight, its first having landed at
    07:00."""
    return [
        _row("DL1", "JFK", "LAX", _DEP, 100.0, at=dt.time(23, 0), hours=4),
        _row("DL2+DL5", "JFK", "LAX", _DEP, 150.0, at=dt.time(6, 0), hours=1),
    ]


def _same_day_back() -> list[Any]:
    """In price order: one whose first flight leaves at 08:00 and second at
    11:00, one leaving at 09:30, and one at 15:00, the only one after 10:00."""
    return [
        _row("DL3+DL6", "LAX", "JFK", _DEP, 100.0, at=dt.time(8, 0)),
        _row("DL7", "LAX", "JFK", _DEP, 120.0, at=dt.time(9, 30)),
        _row("DL4", "LAX", "JFK", _DEP, 150.0, at=dt.time(15, 0)),
    ]


_FLOWN = (
    "Two one-way tickets: USD150.00 (DL2+DL5) out + USD150.00 (DL4) back = USD300.00, "
    "booked as two separate tickets."
)


@pytest.mark.parametrize(
    ("mode", "matrix"),
    [
        pytest.param(["--fast"], None, id="fast"),
        pytest.param([], _matrix_answers(), id="enriched"),
    ],
)
def test_the_pair_is_the_cheapest_whose_return_leaves_after_the_outbound_lands(
    monkeypatch: pytest.MonkeyPatch, mode: list[str], matrix: Callable[..., Any] | None
) -> None:
    """A same-day round trip: the cheapest one-way each way is a pair no one
    can fly, the return leaving before the outbound lands."""
    if matrix is not None:
        monkeypatch.setattr(cli, "_matrix_into", matrix)
    _google(monkeypatch, _same_day_out(), _same_day_back())
    result = _search("--cash-only", "--split", *mode, back_day=_DEP)
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == [_FLOWN]


def test_json_carries_the_pair_one_traveler_flies(monkeypatch: pytest.MonkeyPatch) -> None:
    out, back = _same_day_out(), _same_day_back()
    _google(monkeypatch, out, back)
    result = _search("--cash-only", "--fast", "--format", "json", "--split", back_day=_DEP)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["split_ticket"] == {
        "outbound": _json_row(out[1]),
        "return": _json_row(back[2]),
        "total": 300,
        "currency": "USD",
    }


def _into_lax() -> list[Any]:
    return [_row("DL747", "JFK", "LAX", _DEP, 100.0, at=dt.time(6, 0))]


def _two_airports_back() -> list[Any]:
    """In price order: one leaving NRT at 09:00, an hour after the outbound
    lands by LAX's clock and sixteen hours before it by one clock, and one
    leaving LAX at 12:00."""
    return [
        _row("AA10", "NRT", "JFK", _DEP, 100.0, at=dt.time(9, 0)),
        _row("DL4", "LAX", "JFK", _DEP, 300.0, at=dt.time(12, 0)),
    ]


_FROM_WHERE_IT_LANDS = (
    "Two one-way tickets: USD100.00 (DL747) out + USD300.00 (DL4) back = USD400.00, "
    "booked as two separate tickets."
)


@pytest.mark.parametrize(
    ("mode", "matrix"),
    [
        pytest.param(["--fast"], None, id="fast"),
        pytest.param([], _matrix_answers(), id="enriched"),
    ],
)
def test_the_return_leaves_the_airport_the_outbound_lands_at(
    monkeypatch: pytest.MonkeyPatch, mode: list[str], matrix: Callable[..., Any] | None
) -> None:
    """JFK to LAX or NRT: the cheapest return leaves NRT, not LAX where the
    outbound lands, so its clock says nothing about whether it leaves after."""
    if matrix is not None:
        monkeypatch.setattr(cli, "_matrix_into", matrix)
    _google(monkeypatch, _into_lax(), _two_airports_back())
    result = _search("--cash-only", "--split", *mode, back_day=_DEP, to="LAX,NRT")
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == [_FROM_WHERE_IT_LANDS]


_NO_FLOWN_RETURN = (
    "Google Flights priced no return one-way that leaves the airport an outbound one-way "
    "lands at, after it lands"
)


@pytest.mark.parametrize(
    ("out", "back", "to"),
    [
        pytest.param(_same_day_out()[:1], _same_day_back(), "LAX", id="leaves-before-it-lands"),
        pytest.param(_into_lax(), _two_airports_back()[:1], "LAX,NRT", id="leaves-elsewhere"),
    ],
)
@pytest.mark.parametrize(
    "json_doc", [pytest.param(True, id="json"), pytest.param(False, id="table")]
)
def test_no_return_one_traveler_can_fly_is_named(
    monkeypatch: pytest.MonkeyPatch, out: list[Any], back: list[Any], to: str, json_doc: bool
) -> None:
    _google(monkeypatch, out, back)
    fmt = ["--format", "json"] if json_doc else []
    result = _search("--cash-only", "--fast", "--split", *fmt, back_day=_DEP, to=to)
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == []
    assert result.stderr.count(f"No split tickets: {_NO_FLOWN_RETURN}.") == 1
    if json_doc:
        assert json.loads(result.stdout)["split_ticket"] == {"error": _NO_FLOWN_RETURN}


# ──────────────────────────────── no pair ─────────────────────────────────

_NO_PAIR = [
    pytest.param(
        {"out": GfThrottledError("Google Flights rate-limited the request")},
        "the outbound one-way failed (Google Flights rate-limited the request)",
        id="outbound-refused",
    ),
    pytest.param(
        {"back": RuntimeError("boom")}, "the return one-way failed (boom)", id="return-raised"
    ),
    pytest.param(
        {"back": RuntimeError()}, "the return one-way failed (RuntimeError)", id="blank-error"
    ),
    pytest.param({"out": []}, "Google Flights priced no outbound one-way", id="empty-board"),
    pytest.param(
        {"back": [_row("B6300", "LAX", "JFK", _RET, None)]},
        "Google Flights priced no return one-way",
        id="no-priced-row",
    ),
    pytest.param(
        {"back": [_row("B6300", "LAX", "JFK", _RET, 150.0, currency="EUR")]},
        "Google Flights priced the outbound one-way in USD and the return in EUR",
        id="two-currencies",
    ),
]


@pytest.mark.parametrize(("legs", "reason"), _NO_PAIR)
def test_json_names_why_there_is_no_pair_and_still_exits_0(
    monkeypatch: pytest.MonkeyPatch, legs: dict[str, Any], reason: str
) -> None:
    _google(monkeypatch, **legs)
    plain = _search("--cash-only", "--fast", "--format", "json")
    result = _search("--cash-only", "--fast", "--format", "json", "--split")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc == {"search": json.loads(plain.stdout), "split_ticket": {"error": reason}}
    assert result.stderr.count(f"No split tickets: {reason}.") == 1


@pytest.mark.parametrize(("legs", "reason"), _NO_PAIR)
@pytest.mark.parametrize(
    ("mode", "matrix"),
    [
        pytest.param(["--fast"], None, id="fast"),
        pytest.param([], _matrix_answers(), id="enriched"),
    ],
)
def test_a_table_names_why_there_is_no_pair_on_stderr_only(
    monkeypatch: pytest.MonkeyPatch,
    legs: dict[str, Any],
    reason: str,
    mode: list[str],
    matrix: Callable[..., Any] | None,
) -> None:
    if matrix is not None:
        monkeypatch.setattr(cli, "_matrix_into", matrix)
    _google(monkeypatch, **legs)
    result = _search("--cash-only", "--split", *mode)
    assert result.exit_code == 0, result.output
    assert _split_lines(result.stdout) == []
    assert "No split tickets" not in result.stdout
    assert result.stderr.count(f"No split tickets: {reason}.") == 1


# ──────────────────────────────── refusals ────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "ret", "awards", "said"),
    [
        pytest.param(
            ["--cash-only"],
            False,
            False,
            "prices a round trip as two one-ways; add --return",
            id="one-way",
        ),
        pytest.param(
            ["--cash-only", "--slice", f"JFK-LAX:{_DEP}"],
            True,
            False,
            "prices a round trip as two one-ways, and --slice is a multi-city search",
            id="slice",
        ),
        pytest.param(
            ["--cash-only", "--cabin", "economy,business"],
            True,
            False,
            "prices one cabin; drop the extra --cabin values",
            id="multi-cabin",
        ),
        pytest.param(
            ["--cash-only", "--backend", "matrix"],
            True,
            False,
            "needs Google Flights, and --backend matrix searches Matrix",
            id="backend-matrix",
        ),
        pytest.param(
            ["--cash-only", "--sellers"],
            True,
            False,
            "cannot run beside --sellers; drop one of them",
            id="sellers",
        ),
        pytest.param(
            ["--awards-only"],
            True,
            True,
            "prints beside the results table, and --awards-only prints none",
            id="awards-only",
        ),
        pytest.param(
            ["--format", "json"],
            True,
            True,
            "cannot join the award document --format json writes; add --cash-only",
            id="awards-json",
        ),
    ],
)
def test_a_search_split_cannot_join_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], ret: bool, awards: bool, said: str
) -> None:
    google = _google(monkeypatch)

    def _no_request(*_a: object, **_kw: object) -> object:
        raise AssertionError("refused before the backend is picked")

    for name in ("_pick_backend", "_run", "_run_matrix_path", "_matrix_into"):
        monkeypatch.setattr(cli, name, _no_request)

    def _awards(_sel: cli.ProviderSelection) -> bool:
        return awards

    monkeypatch.setattr(cli, "_should_run_awards", _awards)
    result = _search(*argv, "--split", ret=ret)
    assert result.exit_code == 2, result.output
    assert f"--split {said}." in result.stderr
    assert result.stdout == ""
    assert google.calls == []


# ──────────────────────────────── Matrix answers ──────────────────────────

_ON_MATRIX = (
    "No split tickets: --split prices Google Flights one-ways, and this search runs on Matrix."
)


@pytest.mark.parametrize(
    ("argv", "board"),
    [
        pytest.param(["--no-airport-changes"], None, id="picked-matrix"),
        pytest.param(["--fast"], [], id="handed-on-empty"),
        pytest.param(["--format", "json"], GfThrottledError("rate-limited"), id="handed-on-wall"),
    ],
)
def test_auto_answered_by_matrix_says_so_once_and_asks_no_one_way(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], board: list[Any] | Exception | None
) -> None:
    google = _google(monkeypatch, round_trip=_round_trip() if board is None else board, dropped=3)
    matrix: list[bool] = []

    def _matrix(**_kw: object) -> None:
        matrix.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = _search("--cash-only", *argv, "--split")
    assert result.exit_code == 0, result.output
    assert matrix == [True]
    assert result.stderr.count(_ON_MATRIX) == 1
    assert [len(legs) for legs, *_ in google.calls] == ([] if board is None else [2])
