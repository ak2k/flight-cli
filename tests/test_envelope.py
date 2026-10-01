# pyright: reportPrivateUsage=false
"""`--format envelope`: one document of ten keys for `search` and `calendar`.

Every state below reads stdout as ONE JSON object with exactly the ten keys, each
of its declared type, the schema's own validation over it, and stderr's lines as
the head of `notes`. What changes between states is `complete`, which is false
exactly when the answer is narrower than what was asked, and the keys' values.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import _envelope, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_calgraph import GraphCell, PriceGraph
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfThrottledError
from flight_cli.client import MatrixApiError
from flight_cli.domain import Cabin, CalendarSearch, SpecificDateSearch
from flight_cli.models import SearchResult
from flight_cli.pp import cli as pp_cli
from flight_cli.pp import client as pp_client
from flight_cli.pp.auth import PPAuthError, Tokens
from flight_cli.providers.base import AwardFlight, LegQuery
from test_calendar_split import _pair_client, _result
from test_gf_full_board import _DEP, _LAX, _LHR, _RET, _URL, _return_board, _served
from test_json_document import _one_document

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_KEYS = [
    "version",
    "command",
    "backend",
    "currency",
    "complete",
    "notes",
    "results",
    "awards",
    "insight",
    "price_history",
]
_SEARCH = ["search", "--no-google-url", "--no-matrix-url"]
_ENVELOPE = ["--format", "envelope"]
_MATRIX_BODY: dict[str, Any] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
    ).read_text()
)
_SCHEMA = Path(__file__).parent.parent / "docs" / "envelope.schema.json"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _Matrix:
    """`MatrixClient` answering every search with `body`, and refusing the cabins in
    `refused` as Matrix refuses a query, with an error naming its kind."""

    body: ClassVar[dict[str, Any]] = _MATRIX_BODY
    refused: ClassVar[frozenset[Cabin]] = frozenset()
    calls: ClassVar[int] = 0

    def __init__(self, **_kw: object) -> None:
        pass

    async def __aenter__(self) -> _Matrix:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, search: SpecificDateSearch, **_kw: object) -> SearchResult:
        type(self).calls += 1
        if search.options.cabin in type(self).refused:
            raise MatrixApiError("no fares for that cabin", kind="input")
        return SearchResult.from_api(type(self).body)


def _first_row_award(leg: LegQuery) -> AwardFlight:
    """An award on the flight of the LAX board's cheapest row, so the matcher
    attaches it to that cash row."""
    board = gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))
    first = cli._price_ordered(board)[0].flight.legs[0]
    return AwardFlight(
        origin=leg.origin,
        destination=leg.destination,
        departure=first.departure_datetime.isoformat(),
        arrival=first.arrival_datetime.isoformat(),
        flight_number=f"{first.airline.name}{first.flight_number}",
        provider="PointsPath",
        program="American Airlines",
    )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """Matrix, the award providers, the token check and the award decision in
    process. The decision is the real one under `--cash-only`, which is where it
    says why `awards` is null, and on otherwise: providers are a property of the
    machine, and the states below choose them."""
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(_Matrix, "body", _MATRIX_BODY)
    monkeypatch.setattr(_Matrix, "refused", frozenset[Cabin]())
    monkeypatch.setattr(_Matrix, "calls", 0)
    monkeypatch.setattr(cli, "MatrixClient", _Matrix)

    async def _gather(
        *, legs: list[LegQuery], **_kw: object
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        return ([[_first_row_award(lg)] for lg in legs], [])

    real = cli._should_run_awards

    def _decide(sel: cli.ProviderSelection) -> bool:
        return real(sel) if sel.cash_only else True

    monkeypatch.setattr(cli, "_should_run_awards", _decide)
    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr(pp_cli, "load_tokens", lambda: None)


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _search(*args: str) -> Result:
    return _run(*_SEARCH, *args, *_ENVELOPE)


def _envelope_of(r: Result, *, command: str = "search", code: int = 0) -> dict[str, Any]:
    """The one envelope on stdout, checked for its keys, their types and the
    schema, with stderr's lines at the head of its notes."""
    assert r.exit_code == code, r.output
    doc = _one_document(r.stdout)
    assert isinstance(doc, dict)
    env = cast("dict[str, Any]", doc)
    assert list(env) == _KEYS
    _envelope.ENVELOPE.validate_python(env)
    assert type(env["version"]) is int and env["version"] == 1
    assert env["command"] == command
    assert env["backend"] in ("gflight", "matrix", None)
    assert env["currency"] is None or isinstance(env["currency"], str)
    assert isinstance(env["complete"], bool)
    assert isinstance(env["notes"], list)
    assert all(isinstance(n, str) for n in cast("list[Any]", env["notes"]))
    assert isinstance(env["results"], list)
    assert env["awards"] is None or isinstance(env["awards"], list)
    assert isinstance(env["insight"], list)
    assert isinstance(env["price_history"], list)
    said = [_ANSI.sub("", ln).rstrip() for ln in r.stderr.split("\n") if ln.strip()]
    assert env["notes"][: len(said)] == said
    assert not any(n.startswith("stdout:") for n in cast("list[str]", env["notes"]))
    return env


def _rows(env: dict[str, Any], cabin: str = "COACH") -> list[dict[str, Any]]:
    groups = cast("list[dict[str, Any]]", env["results"])
    (group,) = [g for g in groups if g["cabin"] == cabin]
    return cast("list[dict[str, Any]]", group["rows"])


def _notes(env: dict[str, Any], key: str) -> list[str]:
    return [n for n in cast("list[str]", env["notes"]) if n.startswith(f"{key}: ")]


# ─────────────────────────────── Google search ──────────────────────────────


def test_a_google_one_way_carries_its_rows_insight_and_history(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "--cash-only",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "-n",
            "5",
        )
    )
    assert (env["backend"], env["currency"], env["complete"]) == ("gflight", "USD", True)
    rows = _rows(env)
    assert len(rows) == 5
    assert all(r["row"]["flight_id"] and r["price"] == r["row"]["price"] for r in rows)
    assert all(r["currency"] == "USD" for r in rows)
    (insight,) = env["insight"]
    assert insight["cabin"] == "COACH" and insight["currency"] == "USD"
    assert insight["level"] in ("low", "typical", "high")
    (history,) = env["price_history"]
    points = history["points"]
    assert (history["cabin"], history["currency"], len(points)) == ("COACH", "USD", 61)
    assert points[0] == {"date": "2026-07-29", "price": 169.0}
    assert points[-1] == {"date": "2026-09-27", "price": 204.0}
    assert env["awards"] is None
    assert _notes(env, "awards") == ["awards: --cash-only skips the award search"]


def test_the_rows_are_the_json_documents_rows(gf_session: Callable[..., Any]) -> None:
    args = [
        "--cash-only",
        "JFK",
        "LAX",
        "--dep",
        _DEP.isoformat(),
        "--backend",
        "gflight",
        "--fast",
    ]
    gf_session(_served(_LAX))
    document = _run(*_SEARCH, *args, "--format", "json")
    gf_session(_served(_LAX))
    env = _envelope_of(_search(*args))
    assert [r["row"] for r in _rows(env)] == json.loads(document.stdout)


def test_a_google_round_trip_is_priced_by_its_return(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LHR), _return_board())
    trip = ["JFK", "LHR", "--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
    env = _envelope_of(_search("--cash-only", *trip, "--backend", "gflight", "--fast", "-n", "1"))
    assert (env["backend"], env["complete"]) == ("gflight", True)
    rows = _rows(env)
    assert rows
    for r in rows:
        outbound, inbound = r["row"]
        assert (r["price"], r["currency"]) == (inbound["price"], inbound["currency"])
        assert outbound["legs"][0]["departure_airport"] != inbound["legs"][0]["departure_airport"]


def test_an_empty_google_board_is_a_complete_answer(
    gf_capture: Callable[[str], str], gf_session: Callable[..., Any]
) -> None:
    gf_session(gf_capture("ds1_flightless_board.json"))
    env = _envelope_of(
        _search(
            "--cash-only", "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast"
        )
    )
    assert (env["backend"], env["complete"], _rows(env)) == ("gflight", True, [])
    assert _notes(env, "results") == ["results: no itinerary in any cabin asked"]
    assert _notes(env, "insight") == ["insight: no Google Flights page answered with one"]
    assert _notes(env, "price_history") == [
        "price_history: no Google Flights page answered with one"
    ]


def test_a_board_a_filter_emptied_is_a_note_not_a_narrowing(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "--cash-only",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "--max-price",
            "1",
        )
    )
    assert (env["backend"], env["complete"], _rows(env)) == ("gflight", True, [])
    assert any("rows filtered out" in n for n in env["notes"]), env["notes"]
    # The history is the route's: the filter that drops the insight leaves it.
    assert env["insight"] == []
    assert len(env["price_history"][0]["points"]) == 61


def test_a_google_failure_handed_to_matrix_is_matrixs_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _wall(*_a: object, **_kw: object) -> Any:
        raise GfThrottledError("Google Flights rate-limited the request (/sorry/)")

    monkeypatch.setattr(cli, "_gflight_results", _wall)
    env = _envelope_of(_search("--cash-only", "JFK", "LHR", "--dep", _DEP.isoformat()))
    assert (env["backend"], env["currency"], env["complete"]) == ("matrix", "GBP", True)
    assert "Using Matrix: Google Flights rate-limited." in env["notes"]
    assert _notes(env, "insight") == [
        "insight: Matrix answered, and only a Google Flights page carries one"
    ]


# ─────────────────────────────── Matrix search ──────────────────────────────


def test_a_matrix_search_carries_each_solution_of_the_body() -> None:
    env = _envelope_of(
        _search("--cash-only", "JFK", "LHR", "--dep", _DEP.isoformat(), "--backend", "matrix")
    )
    assert (env["backend"], env["currency"], env["complete"]) == ("matrix", "GBP", True)
    solutions = _MATRIX_BODY["solutionList"]["solutions"]
    rows = _rows(env)
    assert [r["row"] for r in rows] == solutions
    assert rows[0]["price"] == float(solutions[0]["ext"]["price"].removeprefix("GBP"))
    assert env["insight"] == env["price_history"] == []
    assert _notes(env, "price_history") == [
        "price_history: Matrix answered, and only a Google Flights page carries one"
    ]


def test_a_matrix_cabin_that_failed_narrows_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_Matrix, "refused", frozenset({Cabin.BUSINESS}))
    env = _envelope_of(
        _search(
            "--cash-only",
            "JFK",
            "LHR",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "matrix",
            "--cabin",
            "economy,business",
        )
    )
    assert env["complete"] is False
    assert [g["cabin"] for g in env["results"]] == ["COACH", "BUSINESS"]
    assert _rows(env, "COACH") and _rows(env, "BUSINESS") == []
    assert any("Matrix BUSINESS query failed" in n for n in env["notes"]), env["notes"]


def test_a_google_cabin_refused_narrows_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    board = gfid._rows_from_page_html(PageFetch(_served(_LAX), _URL, 200))

    def _results(_legs: object, opts: Any, *_a: object, **_kw: object) -> Any:
        if opts.cabin == Cabin.BUSINESS:
            raise GfThrottledError("Google Flights rate-limited the request (/sorry/)")
        return board

    monkeypatch.setattr(cli, "_gflight_results", _results)
    env = _envelope_of(
        _search(
            "--cash-only",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "--cabin",
            "economy,business",
            "-n",
            "3",
        )
    )
    assert (env["backend"], env["complete"]) == ("gflight", False)
    assert len(_rows(env, "COACH")) == 3 and _rows(env, "BUSINESS") == []
    assert [i["cabin"] for i in env["insight"]] == ["COACH"]
    assert any("Google Flights BUSINESS" in n for n in env["notes"]), env["notes"]


# ─────────────────────────────────── awards ─────────────────────────────────


def test_awards_ride_beside_the_cash_rows(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast", "-n", "5"
        )
    )
    assert env["complete"] is True
    assert len(_rows(env)) == 5
    (leg,) = env["awards"]
    assert leg["slice_index"] == 0 and leg["leg"].startswith("one-way JFK")
    assert leg["matches"], leg
    for match in leg["matches"]:
        assert match["flights"] and match["flights"][0] == match["flight"]
    assert any(m["awards"] for m in leg["matches"])


def test_pointspath_skipped_with_tokens_that_failed_narrows_the_answer(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refused() -> Tokens:
        raise PPAuthError("Supabase refresh failed: HTTP 400")

    monkeypatch.setattr(pp_cli, "load_tokens", lambda: Tokens("access", "refresh", 0))
    monkeypatch.setattr(pp_cli, "get_valid_tokens", _refused)
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast", "-n", "5"
        )
    )
    assert env["complete"] is False
    assert any(n.startswith("PointsPath skipped: Supabase refresh failed") for n in env["notes"])
    assert env["awards"] is not None


def test_pointspath_skipped_with_no_tokens_is_a_note(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _none() -> Tokens:
        raise PPAuthError("No PointsPath tokens. Run `flight-cli auth pp login`.")

    monkeypatch.setattr(pp_cli, "get_valid_tokens", _none)
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search(
            "JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast", "-n", "5"
        )
    )
    assert env["complete"] is True
    assert any(n.startswith("PointsPath skipped: No PointsPath tokens") for n in env["notes"])
    assert env["awards"] is not None


def test_a_failed_award_query_leaves_awards_null_and_narrows(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(**_kw: object) -> Any:
        raise RuntimeError("providers unreachable")

    monkeypatch.setattr(pp_cli, "gather_awards", _down)
    gf_session(_served(_LAX))
    env = _envelope_of(
        _search("JFK", "LAX", "--dep", _DEP.isoformat(), "--backend", "gflight", "--fast")
    )
    assert (env["complete"], env["awards"]) == (False, None)
    assert _rows(env)
    assert _notes(env, "awards") == ["awards: the award query failed"]


def test_a_pointspath_airline_that_answered_500_narrows_the_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The airline search swallows the failure and answers empty, so the narrowing
    is said where it is swallowed. One airline fails; the other has no awards."""
    monkeypatch.setattr(pp_client, "UNSUPPORTED_CACHE", tmp_path / "unsupported.json")

    def _answer(request: httpx.Request) -> httpx.Response:
        airline = json.loads(request.content)["airline"]
        return httpx.Response(500 if airline == "Delta" else 204, text="upstream failed")

    def _asked(airlines: tuple[str, ...]) -> None:
        client = pp_client.PPClient(Tokens("access", "refresh", 0))
        client._client = httpx.AsyncClient(
            base_url=pp_client.API_BASE, transport=httpx.MockTransport(_answer)
        )
        spec = pp_client.SearchSpec(origin="JFK", destination="LAX", date=_DEP.isoformat())

        async def go() -> None:
            async with client:
                await client.airline_search_many(spec, airlines)

        anyio.run(go)

    for airlines, complete in ((("United",), True), (("Delta", "United"), False)):
        _envelope.run("search", partial(_asked, airlines), consoles=())
        assert json.loads(capsys.readouterr().out)["complete"] is complete


# ─────────────────────────────────── calendar ───────────────────────────────

_START = date.today() + timedelta(days=30)
_CALENDAR = [
    "calendar",
    "--start",
    _START.isoformat(),
    "--end",
    (_START + timedelta(days=13)).isoformat(),
    "--one-way",
    "--no-cache",
    "--no-matrix-url",
    "--no-google-url",
    *_ENVELOPE,
]


def _priced_grid(price: str) -> Any:
    return _result({9: {7: (price, 3, {}), 8: ("", 0, {})}}, cheapest=price)


def test_a_matrix_calendar_carries_each_priced_day(monkeypatch: pytest.MonkeyPatch) -> None:
    _pair_client(monkeypatch, {("JFK", "LAX"): _priced_grid("USD204.00")})
    env = _envelope_of(_run(*_CALENDAR[:1], "JFK", "LAX", *_CALENDAR[1:]), command="calendar")
    assert (env["backend"], env["currency"], env["complete"]) == ("matrix", "USD", True)
    (day,) = env["results"]
    assert (day["price"], day["currency"], day["row"]["date"]) == (204.0, "USD", 7)
    assert env["awards"] is None
    assert _notes(env, "awards") == ["awards: calendar runs no award search"]
    assert _notes(env, "insight") == ["insight: a calendar carries none"]


def test_a_split_calendar_missing_a_pair_narrows_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pair_client(
        monkeypatch,
        {
            ("JFK", "LAX"): _priced_grid("USD204.00"),
            ("EWR", "LAX"): MatrixApiError("too busy", kind="brownout"),
        },
    )
    env = _envelope_of(_run(*_CALENDAR[:1], "JFK,EWR", "LAX", *_CALENDAR[1:]), command="calendar")
    assert (env["backend"], env["complete"]) == ("matrix", False)
    assert len(env["results"]) == 1
    assert any("1 of 2 sub-queries failed" in n for n in env["notes"]), env["notes"]
    assert any(n.startswith("Queried 2 airport pairs") for n in env["notes"]), env["notes"]


def test_a_split_calendar_with_every_pair_lost_exits_1_with_its_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pair_client(
        monkeypatch,
        {
            ("JFK", "LAX"): MatrixApiError("too busy", kind="brownout"),
            ("EWR", "LAX"): MatrixApiError("too busy", kind="brownout"),
        },
    )
    r = _run(*_CALENDAR[:1], "JFK,EWR", "LAX", *_CALENDAR[1:])
    env = _envelope_of(r, command="calendar", code=1)
    assert (env["backend"], env["complete"], env["results"]) == (None, False, [])
    assert _notes(env, "backend") == ["backend: the run failed before an answer"]


def test_the_fast_graph_is_a_google_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    from flight_cli import _gf_calgraph as cg

    graph = PriceGraph(
        None,
        (GraphCell(_START, None, 204.0), GraphCell(_START + timedelta(days=1), None, 214.5)),
    )

    def _graph(_search: CalendarSearch, *, headed: bool, pages: int = 0) -> PriceGraph:
        del headed, pages
        return graph

    monkeypatch.setattr(cg, "price_graph", _graph)
    r = _run(*_CALENDAR[:1], "JFK", "LAX", *_CALENDAR[1:], "--fast", "--gf-transport", "browser")
    env = _envelope_of(r, command="calendar")
    assert (env["backend"], env["currency"], env["complete"]) == ("gflight", "USD", True)
    assert [(c["price"], c["row"]) for c in env["results"]] == [
        (204.0, {"departure": _START.isoformat(), "price": 204}),
        (214.5, {"departure": (_START + timedelta(days=1)).isoformat(), "price": 214.5}),
    ]
    assert _Matrix.calls == 0


# ─────────────────────────────────── refusals ───────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["detail", "JFK", "LAX", "--dep", _DEP.isoformat()], id="detail"),
        pytest.param(["explore", "JFK"], id="explore"),
        pytest.param(["fare", "JFK", "LAX", "--dep", _DEP.isoformat()], id="fare"),
        pytest.param(["gflight", "JFK", "LAX", "--dep", _DEP.isoformat()], id="gflight"),
        pytest.param(["doctor"], id="doctor"),
    ],
)
def test_other_commands_refuse_the_envelope_naming_the_two_that_write_it(
    args: list[str],
) -> None:
    r = _run(*args, *_ENVELOPE)
    assert (r.exit_code, r.stdout) == (2, ""), r.output
    assert "--format envelope is written by search and calendar only" in r.stderr
    assert _Matrix.calls == 0


@pytest.mark.parametrize("flag", ["--sellers", "--fare-rules"])
def test_a_document_of_its_own_is_refused_before_any_request(
    flag: str, gf_session: Callable[..., Any]
) -> None:
    fake = gf_session(_served(_LAX))
    r = _search("--cash-only", "JFK", "LAX", "--dep", _DEP.isoformat(), flag)
    assert (r.exit_code, r.stdout) == (2, ""), r.output
    assert "use --format json" in r.stderr and flag in r.stderr
    assert (_Matrix.calls, len(fake.gets)) == (0, 0)


def test_a_usage_error_writes_no_envelope() -> None:
    """Exit 2 is a command that never ran: its stderr, and no document."""
    r = _search("--cash-only", "JFK", "LAX", "--dep", _DEP.isoformat(), "--cabin", "steerage")
    assert (r.exit_code, r.stdout) == (2, "")
    assert "steerage" in r.stderr


# ─────────────────────────────── schema and history ─────────────────────────


def test_the_committed_schema_is_the_generated_one() -> None:
    assert _SCHEMA.read_text() == _envelope.schema_text(), (
        "regenerate: uv run python -c 'from flight_cli._envelope import schema_text; "
        'print(schema_text(), end="")\' > docs/envelope.schema.json'
    )


@pytest.mark.parametrize(
    ("capture", "points", "first", "last"),
    [
        ("ds1_jfk_lax_tfu.json", 61, (date(2026, 7, 29), 169.0), (date(2026, 9, 27), 204.0)),
        ("ds1_jfk_lhr_tfu.json", 62, (date(2026, 7, 28), 289.0), (date(2026, 9, 27), 293.0)),
    ],
)
def test_the_page_carries_a_daily_history(
    capture: str, points: int, first: tuple[date, float], last: tuple[date, float]
) -> None:
    board = gfid._rows_from_page_html(PageFetch(_served(capture), _URL, 200))
    assert board.history is not None
    assert (len(board.history.points), board.history.points[0], board.history.points[-1]) == (
        points,
        first,
        last,
    )
    assert board.history.currency == "USD"
    days = [d for d, _ in board.history.points]
    assert all(b - a == timedelta(days=1) for a, b in itertools.pairwise(days))


def test_a_page_without_the_block_has_no_history(gf_capture: Callable[[str], str]) -> None:
    board = gfid._rows_from_page_html(
        PageFetch(gf_capture("ds1_metadata_blocks_kept.json"), _URL, 200)
    )
    assert board and board.history is None
