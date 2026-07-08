"""Search-path progressive weave (`_run_enriched_path`) failure isolation.

The GF-serveable search dispatches Google Flights and the Matrix search
concurrently under one event loop, paints the GF table first, then repaints a
reconciled GF+Matrix table once Matrix lands. This asserts the search weave
isolates an UNEXPECTED (non-`MatrixApiError`) failure from `execute()` the same
way the calendar weave does (work-ai0jn.1): a raw transport/status error must
not tear down the task group / cancel the GF paint — the GF table still shows
and the command does not raise. Mirrors
`test_calendar_enriched_unexpected_matrix_error_still_shows_grid` in
tests/test_calendar_split.py. No network — both backends are faked.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from flight_cli import cli
from flight_cli.domain import Cabin, Leg, SearchOptions, SpecificDateSearch


def _legs() -> tuple[Leg, ...]:
    return (Leg.of("JFK", "LHR", date(2026, 8, 15)),)


def _opts() -> SearchOptions:
    return SearchOptions(cabin=Cabin.COACH)


class _RaisingClient:
    """A Matrix client whose `execute` raises the given exception type."""

    _exc: type[Exception] = RuntimeError

    def __init__(self, **_kwargs: object) -> None: ...

    async def __aenter__(self) -> _RaisingClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, search: SpecificDateSearch, *, cache: bool = True) -> Any:
        _ = (search, cache)
        raise self._exc("raw transport blip")


def _spy_renderers(monkeypatch: Any) -> dict[str, int]:
    """Replace the search renderers + URL emitter with call-counting spies."""
    calls: dict[str, int] = {"gflight": 0, "merged": 0}

    def _gf_table(*_a: object, **_k: object) -> None:
        calls["gflight"] += 1

    def _merged(*_a: object, **_k: object) -> None:
        calls["merged"] += 1

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(cli, "_render_gflight_table", _gf_table)
    monkeypatch.setattr(cli, "_render_merged", _merged)
    monkeypatch.setattr(cli, "_emit_urls", _noop)
    return calls


def _run_enriched() -> None:
    cli._run_enriched_path(  # pyright: ignore[reportPrivateUsage]
        legs=_legs(),
        opts=_opts(),
        top_n=5,
        run_pp=False,
        sel=None,
        matrix_url=False,
        google_url=False,
        pick=None,
        rps=10.0,
        impersonate="chrome",
        no_cache=True,
    )


def _fake_gf(*_a: object, **_k: object) -> list[Any]:
    return [object()]  # truthy → GF table paints


def test_enriched_search_unexpected_matrix_error_still_shows_gflight(monkeypatch: Any) -> None:
    # A NON-MatrixApiError from execute() (raw transport/status error) must not
    # tear down the task group / cancel the GF paint — the GF table still shows
    # and the command does not raise (per-backend isolation for every failure class).
    class _RawErrClient(_RaisingClient):
        _exc = RuntimeError

    monkeypatch.setattr(cli, "MatrixClient", _RawErrClient)
    monkeypatch.setattr(cli, "_gflight_results", _fake_gf)
    calls = _spy_renderers(monkeypatch)
    _run_enriched()  # must NOT raise / traceback — GF table was shown
    assert calls["gflight"] == 1  # GF table still painted despite the unexpected Matrix error
    assert calls["merged"] == 0  # Matrix errored → no reconciled repaint
