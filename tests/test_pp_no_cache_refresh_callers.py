# pyright: reportPrivateUsage=false
"""Every path that searches award space hands `--no-cache` to PointsPath.

`test_pp_no_cache_refresh` follows the flag from one Matrix and one Google
search to the catalog request. Here each other path that asks the awards
providers stops at the call that reaches them and records the flag it was
given: the multi-cabin searches on each backend, the `--enrich` weave and
`calendar --awards`. No test leaves the machine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import LITERAL_DATES_NOW
from flight_cli import cli
from flight_cli.domain import Cabin
from flight_cli.providers import registry
from test_calendar_formats import _BODY, _ROUTE, _serve
from test_cross_check import _SEARCH as _ENRICH_SEARCH
from test_cross_check import _weave
from test_matrix_multi_sort_cabin_first import _DEP, _answer
from test_multi_cabin_marks import _Google, _search

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli.models import SearchResult


def _awards_asked(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Stub the award search at `run_pp_for_search`, recording each call's
    `no_cache`."""
    seen: list[bool] = []

    def _awards(_res: SearchResult, **kw: Any) -> None:
        seen.append(kw["no_cache"])

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    monkeypatch.delenv("FLIGHT_NO_CACHE", raising=False)
    return seen


_FLAGGED = [
    pytest.param(("--no-cache",), True, id="no-cache"),
    pytest.param((), False, id="default"),
]


@pytest.mark.parametrize(("extra", "expected"), _FLAGGED)
def test_matrix_multi_cabin_hands_the_flag_to_the_award_search(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], expected: bool
) -> None:
    seen = _awards_asked(monkeypatch)

    def _fan_out(**_kw: Any) -> dict[Cabin, SearchResult]:
        return {Cabin.COACH: _answer("USD103.00"), Cabin.BUSINESS: _answer("USD303.00")}

    monkeypatch.setattr(cli, "_run_matrix_multi", _fan_out)
    result = CliRunner().invoke(
        cli.app,
        [
            *("search", "JFK", "LAX", "--dep", _DEP.isoformat(), "--no-matrix-url"),
            *("--no-google-url", "--backend", "matrix", "--cabin", "economy,business", *extra),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [expected]


@pytest.mark.parametrize(("extra", "expected"), _FLAGGED)
def test_google_multi_cabin_hands_the_flag_to_the_award_search(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], expected: bool
) -> None:
    seen = _awards_asked(monkeypatch)
    result = _search(monkeypatch, _Google(), *extra, cash_only=False)
    assert result.exit_code == 0, result.output
    assert seen == [expected]


@pytest.mark.parametrize(("extra", "expected"), _FLAGGED)
def test_the_enrich_weave_hands_the_flag_to_the_award_search(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
    extra: tuple[str, ...],
    expected: bool,
) -> None:
    seen = _awards_asked(monkeypatch)
    _ = _weave(monkeypatch, gf_rows)
    args = [a for a in _ENRICH_SEARCH if a != "--cash-only"]
    result = CliRunner().invoke(cli.app, [*args, "-n", "5", "--enrich", *extra])
    assert result.exit_code == 0, result.output
    assert seen == [expected]


@pytest.mark.time_machine(LITERAL_DATES_NOW)
@pytest.mark.parametrize(("extra", "expected"), _FLAGGED)
def test_calendar_awards_hands_the_flag_to_the_registry(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], expected: bool
) -> None:
    refreshed: list[bool] = []

    async def _gather(*_a: Any, refresh: bool = False, **_kw: Any) -> tuple[list[Any], list[Any]]:
        refreshed.append(refresh)
        return ([[], []], [])

    _serve(monkeypatch, _BODY)
    monkeypatch.delenv("FLIGHT_NO_CACHE", raising=False)
    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    result = CliRunner().invoke(
        cli.app,
        [*_ROUTE, "--gf-transport", "http", "--awards", "2", "--format", "envelope", *extra],
    )
    assert result.exit_code == 0, result.output
    assert refreshed == [expected]
