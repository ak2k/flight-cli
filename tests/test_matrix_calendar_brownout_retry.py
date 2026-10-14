"""A Matrix calendar answer with no priced day is a brownout, and the client asks
again; any other search is asked for once."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import _http, cli, client
from flight_cli.client import MatrixClient, MatrixShapeError
from flight_cli.domain import CalendarSearch, CalendarWindow, Leg, SearchOptions, SpecificDateSearch
from flight_cli.models import CalendarResult, SearchResult

if TYPE_CHECKING:
    import pathlib

_KEY = "AIzaSy" + "k" * 33
_START = dt.date.today() + dt.timedelta(days=30)
_END = _START + dt.timedelta(days=7)
_EMPTY: dict[str, Any] = {"solutionCount": 0, "calendar": {"months": []}}
_PRICED: dict[str, Any] = {
    "solutionCount": 3,
    "currencyNotice": {"ext": {"price": "USD500.00"}},
    "calendar": {
        "months": [
            {
                "month": _START.month,
                "year": _START.year,
                "weeks": [
                    {
                        "days": [
                            {"date": _START.day, "solutionCount": 3, "minPrice": "USD500.00"},
                        ]
                    }
                ],
            }
        ]
    },
}


def _matrix_answers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    bodies: list[dict[str, Any]],
    *,
    delay: float = 0.0,
) -> list[httpx.Request]:
    """Matrix behind the real client: each request is answered with the next body
    (the last one again once they run out), and every request is kept."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FLIGHT_API_KEY", _KEY)
    monkeypatch.setattr(client, "_CALENDAR_BROWNOUT_DELAY_S", delay, raising=False)
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=bodies[min(len(seen), len(bodies)) - 1])

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(answer)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    return seen


def _calendar() -> CalendarSearch:
    window = CalendarWindow(start=_START, end=_END, duration_min=0, duration_max=0)
    return CalendarSearch(legs=(Leg.of("JFK", "LAX"),), window=window)


def _execute(search: CalendarSearch | SpecificDateSearch, *, cache: bool) -> object:
    async def go() -> object:
        async with MatrixClient(cache_read=cache, cache_write=cache) as c:
            return await c.execute(search, cache=cache)

    return anyio.run(go)


def _asks_and_priced_days(res: object, seen: list[httpx.Request]) -> tuple[int, int]:
    assert isinstance(res, CalendarResult)
    return len(seen), len(res.priced_days)


@pytest.mark.parametrize("cache", [False, True], ids=["uncached", "cached"])
def test_a_brownout_answer_is_asked_for_again_and_the_priced_one_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, cache: bool
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [_EMPTY, _PRICED])

    res = _execute(_calendar(), cache=cache)

    assert _asks_and_priced_days(res, seen) == (2, 1)


def test_a_grid_that_stays_empty_is_returned_after_two_more_asks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [_EMPTY])

    res = _execute(_calendar(), cache=True)

    assert _asks_and_priced_days(res, seen) == (3, 0)


def test_the_asks_are_five_seconds_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [_EMPTY], delay=5.0)
    slept: list[float] = []
    real_sleep = anyio.sleep

    async def sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(anyio, "sleep", sleep)

    res = _execute(_calendar(), cache=False)

    assert _asks_and_priced_days(res, seen) == (3, 0)
    assert slept == [5.0, 5.0]


def test_a_priced_grid_is_asked_for_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [_PRICED])

    res = _execute(_calendar(), cache=True)

    assert _asks_and_priced_days(res, seen) == (1, 1)


def test_an_answer_the_models_refuse_is_not_kept_in_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    drifted = {"solutionCount": 1, "session": "s-1", "solutionList": [{"id": "x"}]}
    seen = _matrix_answers(monkeypatch, tmp_path, [drifted])
    search = SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _START),), options=SearchOptions())

    for _ in range(2):
        with pytest.raises(MatrixShapeError):
            _execute(search, cache=True)

    assert len(seen) == 2


def test_a_specific_date_search_with_no_solution_is_asked_for_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [{"solutionCount": 0, "solutionList": {}}])
    search = SpecificDateSearch(legs=(Leg.of("JFK", "LAX", _START),), options=SearchOptions())

    res = _execute(search, cache=True)

    assert isinstance(res, SearchResult)
    assert (len(seen), res.solutions) == (1, [])


def test_the_command_prints_the_priced_grid_after_a_brownout_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen = _matrix_answers(monkeypatch, tmp_path, [_EMPTY, _PRICED])
    argv = ["calendar", "JFK", "LAX", "--start", _START.isoformat(), "--end", _END.isoformat()]

    result = CliRunner().invoke(cli.app, [*argv, "--no-cache", "--gf-transport", "http"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    assert "Calendar empty" not in result.stdout
    assert "500" in result.stdout
