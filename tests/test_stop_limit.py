# pyright: reportPrivateUsage=false
"""`--stops N`: at most N stops per direction, on every backend.

Matrix's `maxLegsRelativeToMin` counts legs beyond the route's own minimum, so
every Matrix body a command sends carries `MAXSTOPS N` in each slice's
commandLine; the body itself is pinned in `test_wire_round_trip.py`, and here
each command that asks Matrix is run against a client that records what it was
sent. Google's page is asked for the ceiling, and each row it serves is held to
it as well, since Google has ignored a field it was sent."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli.domain import CalendarSearch
from flight_cli.models import CalendarResult, SearchResult
from flight_cli.wire import to_wire

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

    from flight_cli.domain import Search

# fli's validator rejects a past travel date, so the dates are derived.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_D, _R = _DEP.isoformat(), _RET.isoformat()
_QUIET = ("--no-matrix-url", "--no-google-url")
_MATRIX = ("--backend", "matrix", "--cash-only")
_LAX = "ds1_jfk_lax_tfu.json"

Json = dict[str, Any]


def _cli(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _flat(output: str) -> str:
    return " ".join(output.split())


def _calendar_answer() -> CalendarResult:
    day = {"date": 20, "solutionCount": 1, "minPrice": "USD100.00"}
    months = [{"month": _DEP.month, "weeks": [{"days": [day]}]}]
    return CalendarResult.from_api({"solutionCount": 1, "calendar": {"months": months}})


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Json]:
    """Matrix, answered in process: every body a run sends, as `to_wire`
    built it."""
    bodies: list[Json] = []

    class _Matrix:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(
            self, search: Search, *, cache: bool = True
        ) -> SearchResult | CalendarResult:
            _ = cache
            bodies.append(to_wire(search).as_json())
            if isinstance(search, CalendarSearch):
                return _calendar_answer()
            return SearchResult.from_api(
                {"solutionCount": 1, "solutions": [{"ext": {"price": "USD321.00"}}]}
            )

    monkeypatch.setattr(cli, "MatrixClient", _Matrix)
    return bodies


def _command_lines(bodies: list[Json]) -> list[list[str | None]]:
    return [[s.get("commandLine") for s in b["inputs"]["slices"]] for b in bodies]


# ─────────────────────────── every Matrix path ──────────────────────────────


@pytest.mark.parametrize(
    ("args", "slices"),
    [
        pytest.param(["search", "JFK", "BKK", "--dep", _D, *_MATRIX], 1, id="search"),
        pytest.param(
            ["search", "JFK", "BKK", "--dep", _D, "--return", _R, *_MATRIX], 2, id="round-trip"
        ),
        pytest.param(
            ["search", "--slice", f"JFK-LHR:{_D}", "--slice", f"LHR-CDG:{_R}", *_MATRIX],
            2,
            id="multi-city",
        ),
        pytest.param(["fare", "JFK", "BKK", "--dep", _D, "--no-pp"], 1, id="fare"),
        pytest.param(["detail", "JFK", "BKK", "--dep", _D], 1, id="detail"),
        pytest.param(
            ["calendar", "LGA", "LAX", "--start", _D, "--end", _R, "--one-way"],
            1,
            id="calendar",
        ),
    ],
)
def test_every_matrix_path_sends_the_ceiling_on_every_slice(
    sent: list[Json], args: list[str], slices: int
) -> None:
    result = _cli(*args, *_QUIET, "--format", "json", "--stops", "0")
    assert result.exit_code == 0, result.output
    assert _command_lines(sent) == [["MAXSTOPS 0"] * slices]
    assert [b["inputs"]["maxLegsRelativeToMin"] for b in sent] == [0]


def test_each_query_of_a_split_calendar_sends_the_ceiling(sent: list[Json]) -> None:
    result = _cli(
        "calendar", "JFK,EWR", "LAX", "--start", _D, "--end", _R, "--one-way",
        "--format", "json", *_QUIET, "--stops", "0",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert sorted(b["inputs"]["slices"][0]["origins"][0] for b in sent) == ["EWR", "JFK"]
    assert _command_lines(sent) == [["MAXSTOPS 0"], ["MAXSTOPS 0"]]


def test_the_enriched_search_sends_matrix_the_ceiling(
    sent: list[Json], monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]
) -> None:
    nonstops = [r for r in gf_rows(_LAX) if len(r.flight.legs) == 1][:2]

    def _gf(*_a: object, **_kw: object) -> gfid.Board[Any]:
        return gfid.Board(nonstops)

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    result = _cli("search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--stops", "0")
    assert result.exit_code == 0, result.output
    assert "Google Flights + Matrix" in result.stdout
    assert _command_lines(sent) == [["MAXSTOPS 0"]]


def test_a_ceiling_google_cannot_be_asked_for_is_sent_to_matrix(
    sent: list[Json], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_google(*_a: object, **_kw: object) -> None:
        raise AssertionError("Google Flights was asked")

    monkeypatch.setattr(cli, "_gflight_results", _no_google)
    result = _cli(
        "search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--format", "json",
        "--stops", "3",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "a stop ceiling above 2 (3)" in _flat(result.stderr)
    assert _command_lines(sent) == [["MAXSTOPS 3"]]


# ───────────────────────── Google rows under a ceiling ─────────────────────


class _Page:
    """Google's page, answered in process: `board` is what it serves whatever
    it was asked, held to the search's own row check."""

    def __init__(self) -> None:
        self.board: list[Any] = []

    def search_with_ids(self, _filters: Any, *, keep: Any = None, **_kw: Any) -> Any:
        kept = [r for r in self.board if keep is None or keep(0, r)]
        return gfid.Board(kept, dropped=len(self.board) - len(kept))


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, gf_rows: Callable[..., list[Any]]) -> _Page:
    """A page serving three one-stop rows to a nonstop search."""
    page = _Page()
    page.board.extend([r for r in gf_rows(_LAX) if len(r.flight.legs) == 2][:3])
    monkeypatch.setattr(gfid, "search_with_ids", page.search_with_ids)
    return page


def test_a_board_the_ceiling_empties_goes_to_matrix_naming_it(
    served: _Page, sent: list[Json]
) -> None:
    _ = served
    result = _cli(
        "search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--fast", "--stops", "0"
    )
    assert result.exit_code == 0, result.output
    assert (
        "Using Matrix: no Google Flights itinerary matched a stop ceiling of 0 "
        "(3 rows filtered out)."
    ) in _flat(result.stderr)
    assert _command_lines(sent) == [["MAXSTOPS 0"]]


def test_under_gflight_a_board_the_ceiling_empties_is_answered_empty(
    served: _Page, sent: list[Json]
) -> None:
    _ = served
    result = _cli(
        "search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--backend", "gflight",
        "--format", "json", "--stops", "0",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert (
        "Google Flights: no itinerary matched a stop ceiling of 0 (3 rows filtered out)."
    ) in _flat(result.stderr)
    assert sent == []


@pytest.mark.parametrize(
    ("args", "most"),
    [
        pytest.param(["--stops", "0"], 1, id="stops-0"),
        pytest.param(["--ext", "MAXSTOPS 0"], 1, id="ext-maxstops-0"),
        pytest.param([], 2, id="no-ceiling"),
    ],
)
def test_a_mixed_board_prints_only_the_rows_inside_the_ceiling(
    gf_session: Callable[..., Any],
    gf_rows: Callable[..., list[Any]],
    args: list[str],
    most: int,
) -> None:
    """The JFK-LAX capture as Google served it, nonstops and one-stops
    together, as a page that ignored its stop filter would."""
    gf_session(_page(_answering(_ds1(_LAX), origin=None, destination=None, date=_D)))
    result = _cli(
        "search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--fast",
        "--format", "json", "-n", "100", *args,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert max(len(r["legs"]) for r in rows) == most
    if most == 1:
        assert len(rows) == sum(len(r.flight.legs) == 1 for r in gf_rows(_LAX))


@pytest.mark.parametrize(
    ("backend", "said"),
    [
        pytest.param(
            [],
            "Using Matrix: no Google Flights itinerary matched a stop ceiling of 0 in "
            "COACH (4 rows filtered out), BUSINESS (4 rows filtered out).",
            id="auto",
        ),
        pytest.param(
            ["--backend", "gflight"],
            "Google Flights COACH: no itinerary matched a stop ceiling of 0.",
            id="gflight",
        ),
    ],
)
def test_a_multi_cabin_board_the_ceiling_empties_names_it(
    sent: list[Json], monkeypatch: pytest.MonkeyPatch, backend: list[str], said: str
) -> None:
    def _gf(*_a: object, **_kw: object) -> gfid.Board[Any]:
        return gfid.Board(dropped=4)

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    result = _cli(
        "search", "JFK", "LAX", "--dep", _D, "--cash-only", *_QUIET, "--cabin",
        "economy,business", "--fast", "--format", "json", "--stops", "0", *backend,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert said in _flat(result.stderr)
    assert _command_lines(sent) == ([["MAXSTOPS 0"]] * 2 if not backend else [])
