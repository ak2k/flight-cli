# pyright: reportPrivateUsage=false
"""One calendar question, three formats: the table, `--format json` and
`--format envelope` carry the same answer.

`calendar_jfk_lax_7n_unpriced.json` is a live Matrix answer for JFK-LAX,
7-night round trips departing 2026-10-20 to 2026-11-16: it priced 23 of the 28
dates and left 2026-10-26 to 2026-10-30 without a fare. Matrix is a stub that
serves it, Google's graph is stubbed, and the conftest guard stays on.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from conftest import LITERAL_DATES_NOW
from flight_cli import _envelope, cli
from flight_cli import _gf_calgraph as cg
from flight_cli.models import CalendarResult
from test_calendar_split import _result

if TYPE_CHECKING:
    from click.testing import Result

    from flight_cli.domain import CalendarSearch

# The fixture's dates are literal; see `LITERAL_DATES_NOW`.
pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_FIXTURES = Path(__file__).parent / "fixtures"
_BODY: dict[str, Any] = json.loads((_FIXTURES / "calendar_jfk_lax_7n_unpriced.json").read_text())
_ROUTE = ["calendar", "JFK", "LAX", "--start", "2026-10-20", "--end", "2026-11-16", "-d", "7"]
_UNPRICED = (
    "Matrix priced no fare on 5 of 28 departure dates asked for 7-night trips: "
    "2026-10-26 to 2026-10-30."
)
_UNPRICED_OPENING = "Matrix priced no fare on "
_FORMATS = {"table": (), "json": ("--format", "json"), "envelope": ("--format", "envelope")}


def _serve(monkeypatch: pytest.MonkeyPatch, *bodies: dict[str, Any]) -> list[CalendarSearch]:
    """Matrix answers each query with the next body, the last one from then on."""
    asked: list[CalendarSearch] = []

    class _Matrix:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, search: CalendarSearch, *, cache: bool = True) -> CalendarResult:
            del cache
            asked.append(search)
            return CalendarResult.from_api(copy.deepcopy(bodies[min(len(asked), len(bodies)) - 1]))

    monkeypatch.setattr(cli, "MatrixClient", _Matrix)
    return asked


def _body_of(res: CalendarResult) -> dict[str, Any]:
    assert res.raw is not None
    return res.raw


def _graphs(
    monkeypatch: pytest.MonkeyPatch, answers: dict[int | None, cg.PriceGraph | BaseException]
) -> list[int | None]:
    """Google's graph for each trip length asked, recording the lengths."""
    seen: list[int | None] = []

    def _price_graph(
        search: CalendarSearch, *, headed: bool, pages: int = cg._MAX_PAGES
    ) -> cg.PriceGraph:
        del headed, pages
        nights = search.window.duration_min if len(search.legs) > 1 else None
        seen.append(nights)
        answer = answers[nights]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cg, "price_graph", _price_graph)
    return seen


def _graph(nights: int | None, prices: dict[str, float]) -> cg.PriceGraph:
    cells = tuple(
        cg.GraphCell(
            date.fromisoformat(d),
            None if nights is None else date.fromisoformat(d) + timedelta(days=nights),
            p,
        )
        for d, p in sorted(prices.items())
    )
    return cg.PriceGraph(nights, cells)


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli.app, [*args, "--no-cache"], env={"COLUMNS": "200"})


def _lines(text: str, opening: str = _UNPRICED_OPENING) -> list[str]:
    return [ln.rstrip() for ln in text.split("\n") if ln.startswith(opening)]


def _envelope_of(result: Result) -> dict[str, Any]:
    doc = json.loads(result.stdout)
    _envelope.ENVELOPE.validate_python(doc)
    return cast("dict[str, Any]", doc)


def _fully_priced() -> dict[str, Any]:
    """The fixture with the five dates Matrix left unpriced priced at USD500."""
    body = copy.deepcopy(_BODY)
    (october, _) = body["calendar"]["months"]
    for week in october["weeks"]:
        for day in week["days"]:
            if not day.get("disabled") and 26 <= day["date"] <= 30:
                day["minPrice"] = "USD500.00"
                day["solutionCount"] = 1
                day["tripDuration"] = {"options": [{"tripLength": 7, "minPrice": "USD500.00"}]}
    return body


# Google's graph for the fixture's window, as its table printed it beside that
# Matrix answer: every date priced, the low USD318 first on 2026-10-27.
_GOOGLE_7N = {
    "2026-10-20": 398,
    "2026-10-21": 398,
    "2026-10-22": 432,
    "2026-10-23": 398,
    "2026-10-24": 398,
    "2026-10-25": 432,
    "2026-10-26": 387,
    "2026-10-27": 318,
    "2026-10-28": 318,
    "2026-10-29": 366,
    "2026-10-30": 377,
    "2026-10-31": 324,
    "2026-11-01": 407,
    "2026-11-02": 377,
    "2026-11-03": 318,
    "2026-11-04": 318,
    "2026-11-05": 339,
    "2026-11-06": 356,
    "2026-11-07": 318,
    "2026-11-08": 407,
    "2026-11-09": 323,
    "2026-11-10": 318,
    "2026-11-11": 339,
    "2026-11-12": 408,
    "2026-11-13": 496,
    "2026-11-14": 453,
    "2026-11-15": 418,
    "2026-11-16": 408,
}


# ───────────────────────────── unpriced dates ─────────────────────────────────


@pytest.mark.parametrize("fmt", list(_FORMATS))
def test_each_format_names_the_dates_matrix_left_unpriced(
    fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _BODY)
    _graphs(monkeypatch, {7: _graph(7, {d: float(p) for d, p in _GOOGLE_7N.items()})})
    result = _run(*_ROUTE, *_FORMATS[fmt])
    assert result.exit_code == 0, result.output
    assert _lines(result.stderr) == [_UNPRICED]
    if fmt == "json":
        assert json.loads(result.stdout) == _BODY
    if fmt == "envelope":
        env = _envelope_of(result)
        assert env["complete"] is False
        assert _lines("\n".join(env["notes"])) == [_UNPRICED]


def test_a_range_names_a_date_only_for_the_length_matrix_left_unpriced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    every = {5: "USD300.00", 6: "USD310.00", 7: "USD320.00"}
    days = {d: ("USD300.00", 3, every) for d in (20, 22, 23)}
    days[21] = ("USD300.00", 3, {5: "USD300.00", 7: "USD320.00"})
    _serve(monkeypatch, _body_of(_result({10: days})))
    window = ["--start", "2026-10-20", "--end", "2026-10-23", "-d", "5-7"]
    table = _run("calendar", "JFK", "LAX", *window, "--gf-transport", "http")
    env = _run("calendar", "JFK", "LAX", *window, "--format", "envelope")
    assert table.exit_code == env.exit_code == 0, table.output + env.output
    line = "Matrix priced no fare on 1 of 4 departure dates asked for 6-night trips: 2026-10-21."
    assert _lines(table.stderr) == [line]
    assert _lines("\n".join(_envelope_of(env)["notes"])) == [line]
    assert _envelope_of(env)["complete"] is False


@pytest.mark.parametrize(
    "shape",
    [(), ("--gf-transport", "http"), ("--format", "json")],
    ids=["table", "http-table", "json"],
)
def test_a_one_way_names_its_unpriced_dates_with_no_trip_length(
    shape: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over `--gf-transport http` a one-way pair takes the weave, whose Matrix
    half delivers the grid itself."""
    priced = {d: ("USD200.00", 2, dict[int, str]()) for d in (20, 21, 24)}
    _serve(monkeypatch, _body_of(_result({10: priced})))
    _graphs(monkeypatch, {None: _graph(None, {"2026-10-20": 190.0})})
    window = ["--start", "2026-10-20", "--end", "2026-10-25", "--one-way"]
    result = _run("calendar", "JFK", "LAX", *window, *shape)
    assert result.exit_code == 0, result.output
    assert _lines(result.stderr) == [
        "Matrix priced no fare on 3 of 6 departure dates asked: "
        "2026-10-22 to 2026-10-23, 2026-10-25."
    ]


def test_a_grid_pricing_every_date_asked_reads_as_it_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over `--gf-transport http` the table and the JSON document are the bytes
    they were before any date was counted."""
    full = _fully_priced()
    _serve(monkeypatch, full)
    table = _run(*_ROUTE, "--gf-transport", "http")
    document = _run(*_ROUTE, "--gf-transport", "http", "--format", "json")
    env = _run(*_ROUTE, "--format", "envelope")
    assert table.exit_code == document.exit_code == env.exit_code == 0
    golden = (_FIXTURES / "calendar_jfk_lax_7n_priced.table.txt").read_text()
    assert (table.stdout, table.stderr) == (golden, "")
    assert (document.stdout, document.stderr) == (json.dumps(full, indent=2), "")
    assert _lines("\n".join(_envelope_of(env)["notes"])) == []
    assert _envelope_of(env)["complete"] is True
