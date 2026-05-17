# pyright: reportPrivateUsage=false
"""Tests for the provider registry's per-leg fan-out.

The registry's contract: concatenate `list[AwardFlight]` from all enabled
providers per leg, swallow per-provider exceptions so one failure doesn't
sink the whole run. These tests use a stub provider (no PointsPath HTTP)
to pin the behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from flight_cli.providers.base import AwardFlight, AwardProvider, LegQuery
from flight_cli.providers.registry import _gather_one_leg, _matches

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


def test_gather_one_leg_concatenates_across_providers() -> None:
    p1 = _StubProvider([_af("AA1"), _af("AA2")])
    p2 = _StubProvider([_af("DL1")])

    async def go() -> list[AwardFlight]:
        return await _gather_one_leg([p1, p2], _leg(), cabins=("Economy",), num_passengers=1)

    out: list[AwardFlight] = anyio.run(go)
    fn_numbers = sorted(a.flight_number for a in out)
    assert fn_numbers == ["AA1", "AA2", "DL1"]


def test_gather_one_leg_isolates_per_provider_failures() -> None:
    """One provider blowing up must not sink the others' results."""
    p_ok = _StubProvider([_af("AA1")])
    p_fail = _StubProvider([], raises=RuntimeError("simulated"))

    async def go() -> list[AwardFlight]:
        return await _gather_one_leg([p_ok, p_fail], _leg(), cabins=("Economy",))

    out: list[AwardFlight] = anyio.run(go)
    assert [a.flight_number for a in out] == ["AA1"]


def test_gather_one_leg_empty_provider_list_returns_empty() -> None:
    async def go() -> list[AwardFlight]:
        return await _gather_one_leg([], _leg(), cabins=("Economy",))

    assert anyio.run(go) == []


@pytest.mark.parametrize("count", [1, 3, 5])
def test_gather_one_leg_preserves_each_providers_full_output(count: int) -> None:
    """The fan-out shouldn't drop or dedupe — it's a concat."""
    providers: list[AwardProvider] = [_StubProvider([_af(f"X{i}")]) for i in range(count)]

    async def go() -> list[AwardFlight]:
        return await _gather_one_leg(providers, _leg(), cabins=("Economy",))

    out: list[AwardFlight] = anyio.run(go)
    assert len(out) == count


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
