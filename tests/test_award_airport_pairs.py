# pyright: reportPrivateUsage=false
"""An award search over an airport set asks the providers about every airport in it.

`search JFK,EWR LAX` asks each award provider about JFK-LAX and EWR-LAX, and a
metro code is asked as its member airports. A search asks at most
`MAX_AWARD_PAIR_QUERIES` pairs, those its cash rows fly first; a pair the cap
leaves unasked is named on stderr and in the JSON document. Each query carries
only the cash hints of rows on its own pair, and one leg's answers render as
one entry. A one-airport search asks exactly what it always asked.

No test here reaches a provider: the registry builds a recording provider in
place of the real ones, so `gather_awards` and the fan-out under it run."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from flight_cli._gflight_ids import Board
from flight_cli.models import SearchResult
from flight_cli.pp import cli as pp_cli
from flight_cli.providers import registry
from flight_cli.providers.base import AwardFlight, CabinAward, LegQuery
from test_gf_airport_sets import _board_from_jfk_and_ewr

if TYPE_CHECKING:
    from click.testing import Result

    from flight_cli.pp.client import CashFlightHint

# The searches built here carry literal travel dates; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_DEP = date(2026, 11, 4)
_RET = date(2026, 11, 11)
_MATRIX_BODY: dict[str, Any] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
    ).read_text()
)


@dataclass(frozen=True)
class _Call:
    """One `search_leg` call, as the provider received it."""

    pair: str
    date: str
    slice_index: int
    label: str
    hints: tuple[dict[str, Any], ...]
    cabins: tuple[str, ...]
    num_passengers: int


@dataclass
class _Arms:
    calls: list[_Call] = field(default_factory=list[_Call])
    answers: dict[str, list[AwardFlight]] = field(default_factory=dict[str, list[AwardFlight]])
    matrix_body: dict[str, Any] = field(default_factory=lambda: _MATRIX_BODY)


class _Recorder:
    """An award provider that writes down every query and answers from `answers`."""

    name = "Recorder"
    enabled = True

    def __init__(self, arms: _Arms) -> None:
        self._arms = arms

    async def search_leg(
        self,
        leg: LegQuery,
        *,
        cabins: tuple[str, ...],
        num_passengers: int = 1,
        cash_hints: tuple[CashFlightHint, ...] = (),
    ) -> list[AwardFlight]:
        pair = f"{leg.origin}-{leg.destination}"
        self._arms.calls.append(
            _Call(
                pair=pair,
                date=leg.date,
                slice_index=leg.slice_index,
                label=leg.label,
                hints=tuple(h.to_payload() for h in cash_hints),
                cabins=cabins,
                num_passengers=num_passengers,
            )
        )
        return list(self._arms.answers.get(pair, []))

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def arms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Arms:
    state = _Arms()

    async def _construct(**_kw: object) -> list[Any]:
        return [_Recorder(state)]

    class _Matrix:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, _search: object, **_kw: object) -> SearchResult:
            return SearchResult.from_api(state.matrix_body)

    def _awards_run(sel: Any) -> bool:
        return not cast("bool", sel.cash_only)

    monkeypatch.setattr(registry, "_construct_enabled", _construct)
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_run)
    monkeypatch.setattr(cli, "MatrixClient", _Matrix)
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    return state


def _google(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> None:
    """Google answers `rows`: B6 1523 and B6 123 from JFK, then B6 523 from EWR."""

    def _results(*_a: object, **_kw: object) -> Board[Any]:
        return Board[Any](rows)

    monkeypatch.setattr(cli, "_gflight_results", _results)


def _gflight(origin: str, *extra: str) -> Result:
    return CliRunner().invoke(
        cli.app,
        ["search", origin, "LAX", "--dep", _DEP.isoformat(), "--fast", "--format", "json", *extra],
    )


def _matrix(origin: str, destination: str, *extra: str) -> Result:
    return CliRunner().invoke(
        cli.app,
        [
            "search",
            origin,
            destination,
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "--backend",
            "matrix",
            *extra,
        ],
    )


def _asked(arms: _Arms) -> list[tuple[str, int, str]]:
    return [(c.pair, c.slice_index, c.label) for c in arms.calls]


def _hint_ids(call: _Call) -> list[str]:
    return [cast("str", h["flightId"]) for h in call.hints]


def _not_asked_lines(r: Result) -> list[str]:
    return [ln for ln in r.stderr.splitlines() if "not asked" in ln]


# ───────────────────────── every pair of the set ────────────────────────────


@pytest.mark.parametrize(
    ("origin", "want"),
    [
        (
            "JFK,EWR",
            [
                ("JFK-LAX", 0, "one-way JFK,EWR→LAX 2026-11-04"),
                ("EWR-LAX", 0, "one-way JFK,EWR→LAX 2026-11-04"),
            ],
        ),
        # LGA last: the board flies JFK and EWR, and LGA nothing.
        (
            "NYC",
            [
                ("JFK-LAX", 0, "one-way NYC→LAX 2026-11-04"),
                ("EWR-LAX", 0, "one-way NYC→LAX 2026-11-04"),
                ("LGA-LAX", 0, "one-way NYC→LAX 2026-11-04"),
            ],
        ),
    ],
)
@pytest.mark.parametrize("awards_only", [False, True])
def test_a_set_asks_every_airport_pair_and_answers_as_one_leg(
    monkeypatch: pytest.MonkeyPatch,
    arms: _Arms,
    origin: str,
    want: list[tuple[str, int, str]],
    awards_only: bool,
) -> None:
    """Red at the base, which asked JFK-LAX alone for `JFK,EWR`, and `NYC-LAX`."""
    _google(monkeypatch, _board_from_jfk_and_ewr())

    r = _gflight(origin, *(["--awards-only"] if awards_only else []))

    assert r.exit_code == 0, r.output
    assert _asked(arms) == want
    (leg,) = cast("list[dict[str, Any]]", json.loads(r.stdout))
    assert set(leg) == {"leg", "slice_index", "awards" if awards_only else "matches"}, leg
    assert leg["leg"] == want[0][2], leg
    assert _not_asked_lines(r) == [], r.stderr


def test_a_matrix_round_trip_asks_each_pair_on_its_own_slice(arms: _Arms) -> None:
    """Red at the base, which asked JFK-LHR and LHR-JFK alone."""
    r = _matrix("JFK,EWR", "LHR", "--format", "json")

    assert r.exit_code == 0, r.output
    assert _asked(arms) == [
        ("JFK-LHR", 0, "outbound JFK,EWR→LHR 2026-11-04"),
        ("EWR-LHR", 0, "outbound JFK,EWR→LHR 2026-11-04"),
        ("LHR-JFK", 1, "return LHR→JFK,EWR 2026-11-11"),
        ("LHR-EWR", 1, "return LHR→JFK,EWR 2026-11-11"),
    ]
    document = cast("list[dict[str, Any]]", json.loads(r.stdout))
    assert [(d["leg"], d["slice_index"]) for d in document] == [
        ("outbound JFK,EWR→LHR 2026-11-04", 0),
        ("return LHR→JFK,EWR 2026-11-11", 1),
    ]


# ───────────────────────────── hints per pair ───────────────────────────────


def test_each_pair_query_carries_only_its_own_rows_hints(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    """Red at the base, whose one JFK-LAX query carried the EWR row's hint too."""
    _google(monkeypatch, _board_from_jfk_and_ewr())

    r = _gflight("JFK,EWR")

    assert r.exit_code == 0, r.output
    assert {c.pair: _hint_ids(c) for c in arms.calls} == {
        "JFK-LAX": ["fuqYmc", "eVt0r"],
        "EWR-LAX": ["VV7jhf"],
    }


def _priced_row(origin: str, flight_id: str, minute: int) -> dict[str, Any]:
    return {
        "ext": {"price": "USD179.00"},
        "itinerary": {
            "slices": [
                {
                    "flights": [f"B6{100 + minute}"],
                    "departure": f"2026-11-04T06:{minute:02d}",
                    "arrival": f"2026-11-04T09:{minute:02d}",
                    "origin": {"code": origin},
                    "destination": {"code": "LAX"},
                    "flight_id": flight_id,
                }
            ]
        },
    }


def test_a_pair_gets_its_own_rows_hints_when_another_pair_has_more_than_the_hint_cap(
    arms: _Arms,
) -> None:
    """51 priced JFK rows rank ahead of one EWR row. Each pair query is capped
    at 50 hints of its own rows, so the EWR query carries the EWR row's id and
    the provider matches on it; without a hint it would not."""
    rows = [_priced_row("JFK", f"jfk{i:02d}", i) for i in range(51)]
    res = SearchResult.from_api(
        {"solutionList": {"solutions": [*rows, _priced_row("EWR", "ewr", 59)]}}
    )
    label = "one-way JFK,EWR→LAX 2026-11-04"

    pp_cli.run_pp_for_search(
        res,
        legs=[
            LegQuery("JFK", "LAX", "2026-11-04", 0, label),
            LegQuery("EWR", "LAX", "2026-11-04", 0, label),
        ],
        json_out=True,
    )

    assert {c.pair: _hint_ids(c) for c in arms.calls} == {
        "JFK-LAX": [f"jfk{i:02d}" for i in range(50)],
        "EWR-LAX": ["ewr"],
    }


def _award(fn: str, origin: str, departure: str, matched_id: str) -> AwardFlight:
    return AwardFlight(
        origin=origin,
        destination="LAX",
        departure=departure,
        arrival="",
        flight_number=fn,
        provider="PointsPath",
        program="JetBlue",
        cabins=[CabinAward(cabin="Economy", miles=12000, tax_usd=5.6, tax_currency="USD")],
        matched_google_flight_id=matched_id,
    )


def test_an_award_from_the_second_airport_shows_on_that_airports_row(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    """Red at the base: EWR was never asked, so the EWR row had no award."""
    _google(monkeypatch, _board_from_jfk_and_ewr())
    arms.answers = {
        "JFK-LAX": [_award("B6123", "JFK", "2026-10-14T06:00:00", "eVt0r")],
        "EWR-LAX": [_award("B6523", "EWR", "2026-10-14T07:00:00", "VV7jhf")],
    }

    r = _gflight("JFK,EWR")

    assert r.exit_code == 0, r.output
    (leg,) = cast("list[dict[str, Any]]", json.loads(r.stdout))
    attached = {
        (m["flight"], m["origin"]): [(a["flight_number"], a["matched_origin"]) for a in m["awards"]]
        for m in leg["matches"]
    }
    assert attached == {
        ("B61523", "JFK"): [],
        ("B6123", "JFK"): [("B6123", "JFK")],
        ("B6523", "EWR"): [("B6523", "EWR")],
    }


def _via_ord(destination: str, second: str, price: str) -> dict[str, Any]:
    """A Matrix one-way solution DFW-ORD-`destination` on AA100 then `second`."""
    return {
        "ext": {"price": price},
        "itinerary": {
            "slices": [
                {
                    "flights": ["AA100", second],
                    "departure": "2026-11-04T09:00",
                    "arrival": "2026-11-04T17:00",
                    "origin": {"code": "DFW"},
                    "destination": {"code": destination},
                    "stops": [{"code": "ORD"}],
                }
            ],
            "carriers": [{"code": "AA"}],
        },
    }


def _aa100_to(destination: str, miles: int) -> AwardFlight:
    return AwardFlight(
        origin="DFW",
        destination=destination,
        departure="2026-11-04T09:00",
        arrival="",
        flight_number="AA100",
        num_connections=1,
        stop_airports=["ORD"],
        provider="PointsPath",
        program="American",
        cabins=[CabinAward(cabin="Economy", miles=miles, tax_usd=5.6, tax_currency="USD")],
    )


def test_the_table_keeps_each_airports_row_when_two_share_a_first_flight(arms: _Arms) -> None:
    """Two connections share their first flight, AA100 DFW-ORD, and end at JFK
    and EWR. Each airport's award shows on its own row of the matched table;
    a row is one first flight and date on one pair of airports."""
    arms.matrix_body = {
        "solutionList": {
            "solutions": [
                _via_ord("JFK", "AA101", "USD300.00"),
                _via_ord("EWR", "AA102", "USD330.00"),
            ]
        }
    }
    arms.answers = {"DFW-JFK": [_aa100_to("JFK", 11000)], "DFW-EWR": [_aa100_to("EWR", 22000)]}

    r = CliRunner().invoke(
        cli.app, ["search", "DFW", "JFK,EWR", "--dep", _DEP.isoformat(), "--backend", "matrix"]
    )

    assert r.exit_code == 0, r.output
    assert _asked(arms) == [
        ("DFW-JFK", 0, "one-way DFW→JFK,EWR 2026-11-04"),
        ("DFW-EWR", 0, "one-way DFW→JFK,EWR 2026-11-04"),
    ]
    matched = r.stdout.split("Cash + award", 1)[1].splitlines()
    rows = {
        flight: miles
        for ln in matched
        for flight in ("AA100/AA101", "AA100/AA102")
        for miles in ("11.0k", "22.0k")
        if flight in ln and miles in ln
    }
    assert rows == {"AA100/AA101": "11.0k", "AA100/AA102": "22.0k"}, r.stdout


# ─────────────────── a one-airport search: the base's calls ──────────────────

_JFK_ROW_HINTS = (
    {
        "origin": "JFK",
        "dest": "LAX",
        "startDateTime": "2026-10-14 15:10",
        "endDateTime": "2026-10-14 18:22",
        "flightId": "fuqYmc",
        "airline": "JetBlue",
        "googleAirlines": ["JetBlue"],
        "numConnections": 0,
        "hasCarryOnBaggage": False,
        "firstFlightNumber": "B61523",
        "cashPrice": 179,
        "rawCashPriceString": "USD179.00",
    },
    {
        "origin": "JFK",
        "dest": "LAX",
        "startDateTime": "2026-10-14 06:00",
        "endDateTime": "2026-10-14 09:00",
        "flightId": "eVt0r",
        "airline": "JetBlue",
        "googleAirlines": ["JetBlue"],
        "numConnections": 0,
        "hasCarryOnBaggage": False,
        "firstFlightNumber": "B6123",
        "cashPrice": 179,
        "rawCashPriceString": "USD179.00",
    },
)


def test_a_one_airport_google_search_makes_the_calls_it_always_made(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    """Green at the base and after: the literals are the base's calls."""
    _google(monkeypatch, _board_from_jfk_and_ewr()[:2])

    r = _gflight("JFK")

    assert r.exit_code == 0, r.output
    assert arms.calls == [
        _Call(
            pair="JFK-LAX",
            date="2026-11-04",
            slice_index=0,
            label="one-way JFK→LAX 2026-11-04",
            hints=_JFK_ROW_HINTS,
            cabins=("Economy", "Business"),
            num_passengers=1,
        )
    ]
    assert [sorted(d) for d in json.loads(r.stdout)] == [["leg", "matches", "slice_index"]]
    assert _not_asked_lines(r) == [], r.stderr


def test_a_one_airport_matrix_round_trip_makes_the_calls_it_always_made(arms: _Arms) -> None:
    """Green at the base and after: the literals are the base's calls."""
    r = _matrix("JFK", "LHR", "--format", "json")

    assert r.exit_code == 0, r.output
    assert arms.calls == [
        _Call(
            pair="JFK-LHR",
            date="2026-11-04",
            slice_index=0,
            label="outbound JFK→LHR 2026-11-04",
            hints=(),
            cabins=("Economy", "Business"),
            num_passengers=1,
        ),
        _Call(
            pair="LHR-JFK",
            date="2026-11-11",
            slice_index=1,
            label="return LHR→JFK 2026-11-11",
            hints=(),
            cabins=("Economy", "Business"),
            num_passengers=1,
        ),
    ]
    assert [sorted(d) for d in json.loads(r.stdout)] == [["leg", "matches", "slice_index"]] * 2
    assert _not_asked_lines(r) == [], r.stderr


# ─────────────────────────────── the cap ────────────────────────────────────


def _first_row_on_ewr_and_lcy() -> dict[str, Any]:
    """The captured JFK-LHR round trip with its first row moved to EWR-LCY and
    back: a pair the cash rows fly that is not first in typed order."""
    body = copy.deepcopy(_MATRIX_BODY)
    out, back = body["solutionList"]["solutions"][0]["itinerary"]["slices"]
    out["origin"]["code"], out["destination"]["code"] = "EWR", "LCY"
    back["origin"]["code"], back["destination"]["code"] = "LCY", "EWR"
    return body


_OUT_NOT_ASKED = (
    "JFK→LTN, JFK→LCY, JFK→SEN, LGA→LHR, LGA→LGW, LGA→STN, LGA→LTN, LGA→LCY, LGA→SEN, "
    "EWR→LHR, EWR→LGW, EWR→STN, EWR→LTN, EWR→SEN"
)
_BACK_NOT_ASKED = (
    "LGW→JFK, LGW→LGA, LGW→EWR, STN→JFK, STN→LGA, STN→EWR, LTN→JFK, LTN→LGA, LTN→EWR, "
    "LCY→JFK, LCY→LGA, SEN→JFK, SEN→LGA, SEN→EWR"
)


def _pairs(text: str) -> list[dict[str, str]]:
    return [{"origin": o, "destination": d} for o, d in (p.split("→") for p in text.split(", "))]


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_a_metro_round_trip_past_the_cap_asks_the_flown_pairs_first_and_names_the_rest(
    arms: _Arms, fmt: str
) -> None:
    """`NYC LON` is 18 pairs a leg. Eight are asked, four a leg, the pairs the
    cash rows fly first, and each leg's other 14 are named once on stderr and in
    the JSON leg object. Red at the base, which asked NYC-LON and LON-NYC."""
    arms.matrix_body = _first_row_on_ewr_and_lcy()

    r = _matrix("NYC", "LON", "--format", fmt)

    assert r.exit_code == 0, r.output
    assert _asked(arms) == [
        ("EWR-LCY", 0, "outbound NYC→LON 2026-11-04"),
        ("JFK-LHR", 0, "outbound NYC→LON 2026-11-04"),
        ("JFK-LGW", 0, "outbound NYC→LON 2026-11-04"),
        ("JFK-STN", 0, "outbound NYC→LON 2026-11-04"),
        ("LCY-EWR", 1, "return LON→NYC 2026-11-11"),
        ("LHR-JFK", 1, "return LON→NYC 2026-11-11"),
        ("LHR-LGA", 1, "return LON→NYC 2026-11-11"),
        ("LHR-EWR", 1, "return LON→NYC 2026-11-11"),
    ]
    assert _not_asked_lines(r) == [
        "Awards for outbound NYC→LON 2026-11-04: asked 4 of 18 airport pairs "
        f"(at most 8 a search); not asked: {_OUT_NOT_ASKED}",
        "Awards for return LON→NYC 2026-11-11: asked 4 of 18 airport pairs "
        f"(at most 8 a search); not asked: {_BACK_NOT_ASKED}",
    ], r.stderr
    if fmt == "json":
        document = cast("list[dict[str, Any]]", json.loads(r.stdout))
        assert [d["pairs_not_asked"] for d in document] == [
            _pairs(_OUT_NOT_ASKED),
            _pairs(_BACK_NOT_ASKED),
        ]
        assert [list(d) for d in document] == [
            ["leg", "slice_index", "matches", "pairs_not_asked"]
        ] * 2


def _query(i: int, origin: str, destination: str) -> LegQuery:
    return LegQuery(origin, destination, "2026-11-04", i, f"leg {i + 1}")


def test_a_search_with_more_legs_than_the_cap_asks_every_leg_its_first_pair() -> None:
    """Red at the base, which had no planner."""
    n = pp_cli.MAX_AWARD_PAIR_QUERIES + 2
    one_pair = [_query(i, "JFK", "LAX") for i in range(n)]
    plan = pp_cli._plan_pair_queries(SearchResult.from_api({}), one_pair)
    assert [(leg.asked, leg.not_asked) for leg in plan] == [((q,), ()) for q in one_pair]

    two_pairs = [q for i in range(n) for q in (_query(i, "JFK", "LAX"), _query(i, "EWR", "LAX"))]
    plan = pp_cli._plan_pair_queries(SearchResult.from_api({}), two_pairs)
    assert [(leg.asked, leg.not_asked) for leg in plan] == [
        ((two_pairs[2 * i],), (two_pairs[2 * i + 1],)) for i in range(n)
    ]
