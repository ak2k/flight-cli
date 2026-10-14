# pyright: reportPrivateUsage=false
"""`calendar --awards K` searches award space on the K cheapest priced dates.

Matrix serves the live JFK-LAX fixture of `test_calendar_formats`; the award
providers are a stub that has space on October 31 only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import LITERAL_DATES_NOW
from flight_cli import _envelope
from flight_cli.providers import registry
from flight_cli.providers.base import AwardFlight, CabinAward, LegQuery, record_failure
from test_calendar_formats import _BODY, _ROUTE, _envelope_of, _run, _serve

pytestmark = pytest.mark.time_machine(LITERAL_DATES_NOW)

_ENVELOPE = ("--format", "envelope")


def _providers(
    monkeypatch: pytest.MonkeyPatch, *, configured: bool = True, seats: int | None = None
) -> list[str]:
    """Economy award space on 2026-10-31 alone; the dates asked are recorded."""
    asked: list[str] = []

    async def _gather(
        legs: list[LegQuery], **_kw: Any
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        asked.extend(f"{q.origin}-{q.destination} {q.date}" for q in legs)
        found = AwardFlight(
            origin="JFK",
            destination="LAX",
            departure="2026-10-31T08:00:00",
            arrival="2026-10-31T11:00:00",
            flight_number="AA1",
            program="American Airlines",
            cabins=[CabinAward("Economy", 12500, 5.6, "USD", remaining_seats=seats)],
        )
        return ([[found] if q.date == "2026-10-31" else [] for q in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: configured)
    return asked


def _calendar_awards() -> dict[str, Any]:
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    return _envelope_of(result)


def test_the_cheapest_dates_are_searched_for_award_space(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch)
    env = _calendar_awards()
    # USD407 is the fixture's lowest fare, on four dates: the two earliest are asked.
    assert asked == ["JFK-LAX 2026-10-31", "JFK-LAX 2026-11-04"]
    assert env["awards"] == [
        {
            "departure": "2026-10-31",
            "return": "2026-11-07",
            "available": True,
            "programs": ["American Airlines"],
        },
        {"departure": "2026-11-04", "return": "2026-11-11", "available": False, "programs": []},
    ]
    assert not [n for n in env["notes"] if n.startswith("awards:")]


def test_awards_without_a_provider_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)
    _providers(monkeypatch, configured=False)
    env = _calendar_awards()
    assert env["awards"] is None
    assert "awards: no award provider is configured" in env["notes"]


@pytest.mark.parametrize(
    ("extra", "seats", "available"),
    [
        pytest.param((), 1, True, id="one-seat-for-one"),
        pytest.param(("--cabin", "business"), None, False, id="economy-only-for-business"),
        pytest.param(("--adults", "3"), 1, False, id="one-seat-for-three"),
        pytest.param(("--adults", "3"), None, True, id="unreported-seats-for-three"),
    ],
)
def test_a_date_has_award_space_only_for_an_offer_that_seats_the_party_in_the_cabin(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], seats: int | None, available: bool
) -> None:
    _serve(monkeypatch, _BODY)
    _providers(monkeypatch, seats=seats)
    result = _run(*_ROUTE, "--gf-transport", "http", *extra, "--awards", "1", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    [entry] = _envelope_of(result)["awards"]
    assert entry["departure"] == "2026-10-31"
    assert entry["available"] is available
    assert entry["programs"] == (["American Airlines"] if available else [])


@pytest.mark.parametrize(
    "spelling", ["premium", "premiumcoach", "premium_coach", "premium_economy", "Premium-Coach"]
)
def test_every_premium_spelling_asks_the_providers_for_premium_economy(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    _serve(monkeypatch, _BODY)
    asked: list[tuple[str, ...]] = []

    async def _gather(
        legs: list[LegQuery], *, cabins: tuple[str, ...], **_kw: Any
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        asked.append(cabins)
        return ([[] for _ in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    cabin = ("--cabin", spelling)
    result = _run(*_ROUTE, "--gf-transport", "http", *cabin, "--awards", "1", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    assert asked == [("Premium economy",)]


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param((), id="table"),
        pytest.param(("--format", "json"), id="json"),
        pytest.param(("--format", "envelope", "--fast"), id="fast"),
    ],
)
def test_awards_outside_an_envelope_run_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...]
) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2", *extra)
    assert result.exit_code == 2
    assert "needs --format envelope" in result.output
    assert asked == []


def test_a_failed_award_query_keeps_the_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)
    _providers(monkeypatch)

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("seats down")

    monkeypatch.setattr(registry, "gather_awards", _boom)
    result = _run(*_ROUTE, "--gf-transport", "http", "--awards", "2", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    env = _envelope_of(result)
    assert env["awards"] is None
    assert env["complete"] is False
    assert "awards: the award query failed" in env["notes"]
    assert env["results"]
    assert "--awards: award query failed: seats down" in result.output


def test_a_metro_calendar_asks_at_most_eight_airport_pairs_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    asked = _providers(monkeypatch)
    route = ["calendar", "NYC", "LON", "--start", "2026-10-20", "--end", "2026-11-16", "-d", "7"]
    result = _run(*route, "--gf-transport", "http", "--awards", "1", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    assert len(asked) == 8
    assert len({a.split()[0] for a in asked}) == 8
    assert "awards: asked the first 8 airport pairs" in _envelope_of(result)["notes"]


def test_the_award_search_asks_the_cabin_by_its_provider_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    asked: list[tuple[str, ...]] = []

    async def _gather(
        legs: list[LegQuery], *, cabins: tuple[str, ...], **_kw: Any
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        asked.append(cabins)
        return ([[] for _ in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    result = _run(
        *_ROUTE, "--gf-transport", "http", "--cabin", "business", "--awards", "1", *_ENVELOPE
    )
    assert result.exit_code == 0, result.output
    assert asked == [("Business",)]


def test_the_award_search_asks_seats_for_the_whole_party(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _BODY)
    seats: list[object] = []

    async def _gather(legs: list[LegQuery], **kw: Any) -> tuple[list[list[AwardFlight]], list[Any]]:
        seats.append(kw.get("num_passengers"))
        return ([[] for _ in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    party = ("--adults", "2", "--children", "1")
    result = _run(*_ROUTE, "--gf-transport", "http", *party, "--awards", "1", *_ENVELOPE)
    assert result.exit_code == 0, result.output
    assert seats == [3]


def test_the_docs_name_the_awards_option() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = " ".join((root / "README.md").read_text().split())
    assert "`calendar --awards K --format envelope`" in readme
    assert "deserves its own design" not in readme
    skill = (root / ".claude" / "skills" / "flight-search" / "SKILL.md").read_text()
    assert "`--awards K` (`--format envelope` only, not `--fast`)" in skill


def test_an_award_provider_that_failed_narrows_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _BODY)

    async def _gather(
        legs: list[LegQuery], **_kw: Any
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        record_failure("Seats.aero", "ReadTimeout")
        _envelope.narrow()
        return ([[] for _ in legs], [])

    monkeypatch.setattr(registry, "gather_awards", _gather)
    monkeypatch.setattr(registry, "has_any_configured", lambda: True)
    env = _calendar_awards()
    assert env["complete"] is False
    assert "Awards incomplete: Seats.aero failed (ReadTimeout)." in env["notes"]
