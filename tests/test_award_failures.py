# pyright: reportPrivateUsage=false
"""An award provider's failures reach the user as one typed line per search.

PointsPath answers per airline and seats.aero per pair, each through
`httpx.MockTransport`, so every swallow site between an HTTP response and
`run_pp_for_search` runs. Each test configures logging as the CLI does by
default, so a raw provider log line would land on the stderr it reads. No test
here reaches a provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from flight_cli import log
from flight_cli.models import SearchResult
from flight_cli.pp import cli as pp_cli
from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPClient
from flight_cli.pp.models import PricingInfoResponse
from flight_cli.providers import registry
from flight_cli.providers.base import LegQuery
from flight_cli.providers.pointspath.provider import PointsPathProvider
from flight_cli.providers.seats_aero import client as seats_client
from flight_cli.providers.seats_aero.provider import SeatsAeroProvider

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from flight_cli.providers.base import AwardFlight

_LABEL = "outbound NYC→MUC 2026-10-20"
_ORIGINS = ("JFK", "EWR", "LGA")
_AIRLINES = ("United", "TapAirPortugal", "AirFrance", "Emirates")
_RAW_EVENTS = ("pp_airline_", "provider_", "seats_aero_")


def _legs() -> list[LegQuery]:
    """One leg over an airport set: three pair queries sharing a slice."""
    return [LegQuery(o, "MUC", "2026-10-20", 0, _LABEL) for o in _ORIGINS]


def _flight(origin: str) -> dict[str, Any]:
    return {
        "origin": origin,
        "destination": "MUC",
        "localDepartureDateTime": "2026-10-20T18:00:00",
        "localArrivalDateTime": "2026-10-21T08:00:00",
        "firstFlightNumber": f"UA{_ORIGINS.index(origin) + 1}00",
        "perCabinMilesPricing": [
            {
                "cabinClass": "Economy",
                "perPassengerPricing": {
                    "perPassengerMilesAmount": 30000,
                    "perPassengerTaxAmountUsd": 5.6,
                },
            }
        ],
    }


def _answer(request: httpx.Request) -> httpx.Response:
    """United has a flight on every pair; anyone else has nothing on the route."""
    body = json.loads(request.content)
    if body["airline"] != "United":
        return httpx.Response(204)
    return httpx.Response(
        200, json={"outboundFlights": [_flight(body["originAirport"])], "inboundFlights": None}
    )


def _failing_pointspath(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    airline, origin = body["airline"], body["originAirport"]
    if airline == "TapAirPortugal":
        raise httpx.ReadTimeout("", request=request)
    if airline == "AirFrance" and origin != "LGA":
        return httpx.Response(500, text="upstream\n  exploded")
    if airline == "Emirates" and origin == "JFK":
        return httpx.Response(500)  # an outage with no body at all
    return _answer(request)


def _failing_seats(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="[/x]\x1b[2J")


def _empty_seats(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [], "count": 0, "hasMore": False})


def _use_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pointspath: Any, seats: Any
) -> None:
    """The registry hands out a real PointsPath and seats.aero provider whose
    HTTP clients answer from `pointspath` and `seats`."""

    async def construct(**_kw: object) -> list[Any]:
        pp = PPClient(
            Tokens(
                access_token="TOKEN",  # noqa: S106 — dummy test value
                refresh_token="REFRESH",  # noqa: S106 — dummy test value
                expires_at=9999999999,
            )
        )
        await pp._client.aclose()
        pp._client = httpx.AsyncClient(base_url=API_BASE, transport=httpx.MockTransport(pointspath))
        sa = seats_client.SeatsAeroClient(api_key="KEY")
        await sa._client.aclose()
        sa._client = httpx.AsyncClient(
            base_url=seats_client.API_BASE, transport=httpx.MockTransport(seats)
        )
        return [PointsPathProvider(pp, PricingInfoResponse(), _AIRLINES), SeatsAeroProvider(sa)]

    monkeypatch.setattr(registry, "_construct_enabled", construct)
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", tmp_path / "unsupported.json")


def _run(**kw: Any) -> None:
    pp_cli.run_pp_for_search(
        SearchResult.from_api({}), legs=_legs(), cabins="Economy", pp_only=True, json_out=True, **kw
    )


def _summaries(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith("Awards incomplete:")]


def _flight_numbers(out: str) -> list[str]:
    return [award["flight_number"] for leg in json.loads(out) for award in leg["awards"]]


def test_every_failure_of_a_search_is_one_typed_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timed-out airline used to be a raw warning per request with an empty
    `error=`, a 500 another, a 500 with no body nothing at all, and a seats.aero
    outage a fourth kind: the reader saw log lines and could not tell what the
    award columns were missing."""
    _use_providers(monkeypatch, tmp_path, _failing_pointspath, _failing_seats)
    log.configure("warning")
    _run()
    captured = capsys.readouterr()

    assert _summaries(captured.err) == [
        "Awards incomplete: PointsPath did not answer for TapAirPortugal (ReadTimeout, 3 queries), "
        "AirFrance (HTTP 500: upstream exploded, 2 queries), Emirates (HTTP 500); "
        "Seats.aero failed (HTTP 503: [/x][2J, 3 queries)."
    ], captured.err
    assert [event for event in _RAW_EVENTS if event in captured.err] == [], captured.err
    assert "\x1b" not in captured.err
    # What did answer is kept.
    assert _flight_numbers(captured.out) == ["UA100", "UA200", "UA300"]


def test_a_provider_that_fails_whole_is_named_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registry's own swallow sites: a provider that cannot be built, and one
    whose whole search raises."""

    class _Unbuildable:
        @classmethod
        async def create(cls, **_kw: object) -> Any:
            raise httpx.ConnectTimeout("")

    class _Raising:
        name = "Seats.aero"
        enabled = True

        @classmethod
        async def create(cls, **_kw: object) -> _Raising:
            return cls()

        async def search_leg(self, *_a: object, **_kw: object) -> list[AwardFlight]:
            msg = "[/x]\x1b[2Jboom"
            raise RuntimeError(msg)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(registry, "pp_is_configured", lambda: True)
    monkeypatch.setattr(registry, "seats_is_configured", lambda: True)
    monkeypatch.setattr(registry, "PointsPathProvider", _Unbuildable)
    monkeypatch.setattr(registry, "SeatsAeroProvider", _Raising)
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    log.configure("warning")
    _run()
    captured = capsys.readouterr()

    assert _summaries(captured.err) == [
        "Awards incomplete: PointsPath failed (ConnectTimeout); "
        "Seats.aero failed ([/x][2Jboom, 3 queries)."
    ], captured.err
    assert [event for event in _RAW_EVENTS if event in captured.err] == [], captured.err


def test_a_search_with_no_failure_prints_no_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_providers(monkeypatch, tmp_path, _answer, _empty_seats)
    log.configure("warning")
    _run()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _flight_numbers(captured.out) == ["UA100", "UA200", "UA300"]


def test_dash_vv_still_shows_each_failure_as_its_own_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_providers(monkeypatch, tmp_path, _failing_pointspath, _failing_seats)
    log.configure("debug")
    _run()
    err = capsys.readouterr().err
    for event in (
        "pp_airline_search_exception",
        "pp_airline_search_failed",
        "seats_aero_search_failed",
    ):
        assert event in err, err
