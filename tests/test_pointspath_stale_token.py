# pyright: reportPrivateUsage=false
"""A stale PointsPath token is refreshed once per search, inside the award
deadline and beside seats.aero. Every other check of the stored tokens, the
pre-flight and the award gate included, reads them and sends no request."""

from __future__ import annotations

import functools
import json
import threading
from typing import TYPE_CHECKING

import pytest

from flight_cli import cli, log
from flight_cli.models import SearchResult
from flight_cli.pp import auth as pp_auth
from flight_cli.pp import cli as pp_cli
from flight_cli.pp.auth import PPAuthError, Tokens
from flight_cli.providers import registry
from flight_cli.providers.base import AwardFlight, LegQuery
from flight_cli.providers.seats_aero import auth as seats_auth

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_LABEL = "outbound NYC→MUC 2026-10-20"
_ORIGINS = ("JFK", "EWR", "LGA")


class _HealthySeats:
    """A seats.aero that answers at once, one flight per pair."""

    name = "Seats.aero"
    enabled = True

    async def search_leg(self, leg: LegQuery, **_kw: object) -> list[AwardFlight]:
        return [
            AwardFlight(
                origin=leg.origin,
                destination=leg.destination,
                departure="2026-10-20T18:00:00",
                arrival="2026-10-21T08:00:00",
                flight_number=f"LH{_ORIGINS.index(leg.origin) + 1}00",
            )
        ]

    async def aclose(self) -> None:
        return None


def _stale_stored_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A saved login whose access token ran out: the next check would refresh it."""
    store = tmp_path / "pp.json"
    _ = store.write_text(json.dumps(Tokens("access", "refresh", 0).to_json()))
    monkeypatch.setattr(pp_auth, "TOKENS_PATH", store)
    monkeypatch.delenv("PP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PP_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(seats_auth, "KEY_PATH", tmp_path / "seats.json")
    monkeypatch.delenv(seats_auth.API_KEY_ENV, raising=False)


def _real_pointspath_beside_healthy_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry's own PointsPath build, so its token check runs, beside a
    seats.aero that is already built."""

    async def seats() -> _HealthySeats:
        return _HealthySeats()

    def builders(**_kw: object) -> list[Callable[[], Awaitable[object]]]:
        return [functools.partial(registry._build_pointspath, None), seats]

    monkeypatch.setattr(registry, "_enabled_builders", builders)


def _search() -> None:
    legs = [LegQuery(o, "MUC", "2026-10-20", 0, _LABEL) for o in _ORIGINS]
    pp_cli.run_pp_for_search(
        SearchResult.from_api({}), legs=legs, cabins="Economy", pp_only=True, json_out=True
    )


def _flight_numbers(out: str) -> list[str]:
    return sorted(award["flight_number"] for leg in json.loads(out) for award in leg["awards"])


def test_the_refresh_of_a_stale_token_ends_at_the_award_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release = threading.Event()

    def stuck_refresh(_tokens: Tokens) -> Tokens:
        _ = release.wait()
        # Once released, the login is refused, so nothing reaches PointsPath.
        msg = "released"
        raise PPAuthError(msg)

    _stale_stored_token(monkeypatch, tmp_path)
    _real_pointspath_beside_healthy_seats(monkeypatch)
    monkeypatch.setattr(pp_auth, "refresh", stuck_refresh)
    monkeypatch.setattr(pp_cli, "AWARD_DEADLINE_SECS", 0.5)
    log.configure("warning")
    search = threading.Thread(target=_search, daemon=True)
    try:
        search.start()
        search.join(10)
        assert not search.is_alive(), "a stale token's refresh held the search past its deadline"
    finally:
        release.set()
        search.join(10)
    captured = capsys.readouterr()

    assert "Awards incomplete: PointsPath failed (not answered within 0.5 s)." in captured.err
    assert _flight_numbers(captured.out) == ["LH100", "LH200", "LH300"]


@pytest.mark.parametrize("mode", ["stored", "env"])
def test_a_refresh_the_login_refuses_is_an_awards_incomplete_line(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One refresh a search, so one refusal: an env token is never saved, so
    every check that refreshed it asked again."""
    refreshed: list[Tokens] = []

    def refused(tokens: Tokens) -> Tokens:
        refreshed.append(tokens)
        msg = "Supabase refresh failed: HTTP 400 refresh token expired"
        raise PPAuthError(msg)

    _stale_stored_token(monkeypatch, tmp_path)
    if mode == "env":
        monkeypatch.setenv("PP_ACCESS_TOKEN", "access")
        monkeypatch.setenv("PP_REFRESH_TOKEN", "refresh")
    _real_pointspath_beside_healthy_seats(monkeypatch)
    monkeypatch.setattr(pp_auth, "refresh", refused)
    log.configure("warning")
    _search()
    captured = capsys.readouterr()

    assert "PointsPath skipped" not in captured.err
    assert "Awards incomplete: PointsPath failed (Supabase refresh failed: HTTP 400" in captured.err
    assert _flight_numbers(captured.out) == ["LH100", "LH200", "LH300"]
    assert len(refreshed) == 1


@pytest.mark.parametrize("awards_only", [False, True])
def test_the_award_gate_reads_a_stale_token_without_refreshing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, awards_only: bool
) -> None:
    refreshed: list[Tokens] = []

    def recording_refresh(tokens: Tokens) -> Tokens:
        refreshed.append(tokens)
        return tokens

    _stale_stored_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pp_auth, "refresh", recording_refresh)
    sel = cli.ProviderSelection(
        provider_filter=None, cash_only=False, awards_only=awards_only, provider_opts={}
    )

    assert cli._should_run_awards(sel) is True
    assert refreshed == []
