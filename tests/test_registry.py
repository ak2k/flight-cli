# pyright: reportPrivateUsage=false
"""Tests for the provider registry's per-leg fan-out.

The registry's contract: concatenate `list[AwardFlight]` from all enabled
providers per leg, swallow per-provider exceptions so one failure doesn't
sink the whole run. These tests use a stub provider (no PointsPath HTTP)
to pin the behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import anyio
import httpx
import pytest

from conftest import hand_out_providers
from flight_cli.providers import registry
from flight_cli.providers.base import (
    AwardFlight,
    AwardProvider,
    LegQuery,
    ProviderFailure,
    award_run,
)
from flight_cli.providers.registry import _matches

if TYPE_CHECKING:
    from flight_cli.pp.client import CashFlightHint


class _StubProvider:
    name: str = "Stub"
    enabled: bool = True

    def __init__(self, flights: list[AwardFlight], *, raises: Exception | None = None) -> None:
        self._flights = flights
        self._raises = raises

    async def search_leg(
        self,
        leg: LegQuery,
        *,
        cabins: tuple[str, ...],
        num_passengers: int = 1,
        cash_hints: tuple[CashFlightHint, ...] = (),
    ) -> list[AwardFlight]:
        _ = leg, cabins, num_passengers, cash_hints
        if self._raises:
            raise self._raises
        return list(self._flights)


def _af(fn: str) -> AwardFlight:
    return AwardFlight(
        origin="JFK",
        destination="LHR",
        departure="2026-08-15T19:00:00",
        arrival="2026-08-16T07:00:00",
        flight_number=fn,
        provider="Stub",
        program="Test",
    )


def _leg() -> LegQuery:
    return LegQuery(
        origin="JFK",
        destination="LHR",
        date="2026-08-15",
        slice_index=0,
        label="outbound JFK→LHR",
    )


def _asked(monkeypatch: pytest.MonkeyPatch, *providers: AwardProvider) -> list[AwardFlight]:
    """The awards `gather_awards` collects for one leg from `providers`."""
    hand_out_providers(monkeypatch, *providers)

    async def go() -> list[AwardFlight]:
        per_leg, _ = await registry.gather_awards([_leg()], cabins=("Economy",))
        return per_leg[0]

    return anyio.run(go)


def test_gather_awards_concatenates_across_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    p1 = _StubProvider([_af("AA1"), _af("AA2")])
    p2 = _StubProvider([_af("DL1")])

    fn_numbers = sorted(a.flight_number for a in _asked(monkeypatch, p1, p2))
    assert fn_numbers == ["AA1", "AA2", "DL1"]


def test_gather_awards_isolates_per_provider_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """One provider blowing up must not sink the others' results."""
    p_ok = _StubProvider([_af("AA1")])
    p_fail = _StubProvider([], raises=RuntimeError("simulated"))

    assert [a.flight_number for a in _asked(monkeypatch, p_ok, p_fail)] == ["AA1"]


def test_gather_awards_with_no_provider_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _asked(monkeypatch) == []


@pytest.mark.parametrize("count", [1, 3, 5])
def test_gather_awards_preserves_each_providers_full_output(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """The fan-out shouldn't drop or dedupe — it's a concat."""
    providers = [_StubProvider([_af(f"X{i}")]) for i in range(count)]

    assert len(_asked(monkeypatch, *providers)) == count


def test_a_provider_is_asked_while_another_is_still_being_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every provider was built before any was asked, so a PointsPath catalog that
    stalled to the award deadline left seats.aero, healthy, no time to answer, and
    named it as failing too."""

    async def go() -> tuple[list[list[AwardFlight]], list[str], list[ProviderFailure]]:
        seats_answered = anyio.Event()

        class _CatalogOutlastsSeats:
            """PointsPath, whose catalog request ends only after seats.aero answered."""

            @classmethod
            async def create(cls, **_kw: object) -> AwardProvider:
                await seats_answered.wait()
                raise httpx.ReadTimeout("")

        class _Seats(_StubProvider):
            name = "Seats.aero"

            @classmethod
            async def create(cls, **_kw: object) -> _Seats:
                return cls([_af("BA1")])

            @override
            async def search_leg(
                self,
                leg: LegQuery,
                *,
                cabins: tuple[str, ...],
                num_passengers: int = 1,
                cash_hints: tuple[CashFlightHint, ...] = (),
            ) -> list[AwardFlight]:
                seats_answered.set()
                return await super().search_leg(
                    leg, cabins=cabins, num_passengers=num_passengers, cash_hints=cash_hints
                )

        monkeypatch.setattr(registry, "pp_is_configured", lambda: True)
        monkeypatch.setattr(registry, "seats_is_configured", lambda: True)
        monkeypatch.setattr(registry, "PointsPathProvider", _CatalogOutlastsSeats)
        monkeypatch.setattr(registry, "SeatsAeroProvider", _Seats)
        with award_run() as run, anyio.fail_after(5):
            per_leg, providers = await registry.gather_awards([_leg()], cabins=("Economy",))
        return per_leg, [p.name for p in providers], run.failures

    per_leg, names, failures = anyio.run(go)
    assert [[a.flight_number for a in awards] for awards in per_leg] == [["BA1"]]
    assert names == ["Seats.aero"]
    assert failures == [ProviderFailure("PointsPath", "ReadTimeout")]


# ─────────────────────────── provider filter (work-4byx + work-2eoa) ─────


def test_matches_filter_case_insensitive() -> None:
    """Filter entries are case-normalized through the alias map."""
    assert _matches(("PP",), "pp") is True
    assert _matches(("Pp",), "pp") is True


def test_matches_filter_trims_whitespace() -> None:
    assert _matches((" pp ",), "pp") is True


def test_matches_returns_false_for_unknown_provider() -> None:
    """An unknown filter name doesn't match any canonical."""
    assert _matches(("pp",), "seats-aero") is False


def test_matches_empty_filter_returns_false() -> None:
    assert _matches((), "pp") is False


def test_matches_resolves_aliases_to_canonical() -> None:
    """The point of the alias map: filter spelled in any user-friendly form
    matches the canonical name the caller passes."""
    assert _matches(("sa",), "seats-aero") is True
    assert _matches(("seats.aero",), "seats-aero") is True
    assert _matches(("seatsaero",), "seats-aero") is True
    assert _matches(("pointspath",), "pp") is True
    assert _matches(("Points-Path",), "pp") is True


def test_matches_multi_filter_aliases() -> None:
    """A CSV of aliases collapses correctly."""
    f = ("sa", "pointspath")
    assert _matches(f, "seats-aero") is True
    assert _matches(f, "pp") is True
