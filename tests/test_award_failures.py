# pyright: reportPrivateUsage=false
"""An award provider's failures reach the user as one typed line per search.

PointsPath answers per airline and seats.aero per pair, each through
`httpx.MockTransport`, so every swallow site between an HTTP response and
`run_pp_for_search` runs. Each test configures logging as the CLI does by
default, so a raw provider log line would land on the stderr it reads. The award
phase asks its pair queries at once and ends at a deadline, keeping what
answered. No test here reaches a provider."""

from __future__ import annotations

import json
import re
import threading
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest

from conftest import hand_out_providers
from flight_cli import _envelope, log
from flight_cli.models import SearchResult
from flight_cli.pp import cli as pp_cli
from flight_cli.pp.auth import PPAuthError, Tokens
from flight_cli.pp.client import API_BASE, PPClient
from flight_cli.pp.models import PricingInfoResponse
from flight_cli.providers import registry
from flight_cli.providers.base import LegQuery
from flight_cli.providers.pointspath import provider as pp_provider
from flight_cli.providers.pointspath.provider import PointsPathProvider
from flight_cli.providers.seats_aero import client as seats_client
from flight_cli.providers.seats_aero.provider import SeatsAeroProvider

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
    HTTP clients answer from `pointspath` and `seats`. Built before the award
    phase starts, so a deadline times the requests and not the setup of a
    client, whose TLS context alone takes tens of milliseconds."""
    pp = PPClient(
        Tokens(
            access_token="TOKEN",  # noqa: S106 — dummy test value
            refresh_token="REFRESH",  # noqa: S106 — dummy test value
            expires_at=9999999999,
        )
    )
    anyio.run(pp._client.aclose)
    pp._client = httpx.AsyncClient(base_url=API_BASE, transport=httpx.MockTransport(pointspath))
    sa = seats_client.SeatsAeroClient(api_key="KEY")
    anyio.run(sa._client.aclose)
    sa._client = httpx.AsyncClient(
        base_url=seats_client.API_BASE, transport=httpx.MockTransport(seats)
    )
    hand_out_providers(
        monkeypatch, PointsPathProvider(pp, PricingInfoResponse(), _AIRLINES), SeatsAeroProvider(sa)
    )
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)
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
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)
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


def test_pair_queries_are_asked_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pair queries ran one after another, each waiting for its slowest airline,
    so one stalled airline on the first pair held every pair behind it."""
    started: list[str] = []

    async def go() -> list[list[AwardFlight]]:
        second_pair_asked = anyio.Event()

        class _Waits:
            name = "Waits"
            enabled = True

            async def search_leg(self, leg: LegQuery, **_kw: object) -> list[AwardFlight]:
                started.append(leg.origin)
                if leg.origin == "JFK":
                    await second_pair_asked.wait()
                else:
                    second_pair_asked.set()
                return []

            async def aclose(self) -> None:
                return None

        hand_out_providers(monkeypatch, _Waits())
        with anyio.fail_after(5):
            per_query, _ = await registry.gather_awards(_legs()[:2], cabins=("Economy",))
        return per_query

    assert anyio.run(go) == [[], []]
    assert started == ["JFK", "EWR"]  # started in the order they were planned


def test_an_airline_turned_away_as_unsupported_is_asked_once_a_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One pair query after another, each knew what the one before it learned.
    Asked at once, every pair asked an airline PointsPath turns away as
    unsupported before the first refusal landed."""
    asked: list[str] = []

    def refusing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["airline"] == "Emirates":
            asked.append(f"{body['originAirport']} {body['cabinClass']}")
            return httpx.Response(400, text='{"error":"Unsupported airline"}')
        return _answer(request)

    _use_providers(monkeypatch, tmp_path, refusing, _empty_seats)
    log.configure("warning")
    pp_cli.run_pp_for_search(
        SearchResult.from_api({}),
        legs=_legs(),
        cabins="Economy,Business",
        pp_only=True,
        json_out=True,
    )
    captured = capsys.readouterr()

    assert asked == ["JFK Economy"]
    assert captured.err == ""
    assert sorted(set(_flight_numbers(captured.out))) == ["UA100", "UA200", "UA300"]


def test_the_award_phase_ends_at_its_deadline_and_keeps_what_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An airline that never answers held the whole search: the award phase
    waited on it however long it took. At the deadline its requests are cut and
    named, and every answer already in is joined and rendered."""
    release = threading.Event()

    async def stalling(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["airline"] == "TapAirPortugal":
            # Set from the test's own thread, where an anyio.Event cannot be.
            while not release.is_set():  # noqa: ASYNC110
                await anyio.sleep(0.05)
        return _answer(request)

    _use_providers(monkeypatch, tmp_path, stalling, _empty_seats)
    # Not raising: where the constant does not exist, the search hangs instead.
    monkeypatch.setattr(pp_cli, "AWARD_DEADLINE_SECS", 0.2, raising=False)
    log.configure("warning")
    # A thread the test can abandon, so a search that never ends fails the test
    # rather than hanging the suite.
    search = threading.Thread(target=_run, daemon=True)
    try:
        search.start()
        search.join(10)
        assert not search.is_alive(), "the award phase did not end at its deadline"
    finally:
        release.set()
        search.join(10)
    captured = capsys.readouterr()

    assert _summaries(captured.err) == [
        "Awards incomplete: PointsPath did not answer for TapAirPortugal "
        "(not answered within 0.2 s, 3 queries)."
    ], captured.err
    assert _flight_numbers(captured.out) == ["UA100", "UA200", "UA300"]


def test_a_token_refresh_ends_at_the_award_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 401 refreshed the token with a blocking call on the event loop, where no
    deadline could cut it: a refresh that did not return held the award phase."""
    release = threading.Event()

    def stuck_refresh(tokens: Tokens) -> Tokens:
        _ = release.wait()
        return tokens

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _use_providers(monkeypatch, tmp_path, unauthorized, _empty_seats)
    monkeypatch.setattr("flight_cli.pp.client.refresh_tokens", stuck_refresh)
    monkeypatch.setattr(pp_cli, "AWARD_DEADLINE_SECS", 0.2)
    log.configure("warning")
    search = threading.Thread(target=_run, daemon=True)
    try:
        search.start()
        search.join(10)
        assert not search.is_alive(), "a token refresh held the award phase past its deadline"
    finally:
        release.set()
        search.join(10)
    captured = capsys.readouterr()

    assert _summaries(captured.err) == [
        "Awards incomplete: PointsPath did not answer for "
        + ", ".join(f"{a} (not answered within 0.2 s, 3 queries)" for a in sorted(_AIRLINES))
        + "."
    ], captured.err


def test_a_token_check_ends_at_the_award_deadline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Building PointsPath's provider refreshes a stale token with a blocking
    request. In a thread the deadline cuts it: a refresh that did not return
    does not hold the award phase."""
    release = threading.Event()

    def stuck_tokens() -> Tokens:
        _ = release.wait()
        # Once released, PointsPath has no tokens, so nothing reaches it.
        msg = "released"
        raise PPAuthError(msg)

    monkeypatch.setattr(pp_provider, "get_valid_tokens", stuck_tokens)
    monkeypatch.setattr(registry, "pp_is_configured", lambda: True)
    monkeypatch.setattr(registry, "seats_is_configured", lambda: False)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)
    monkeypatch.setattr(pp_cli, "AWARD_DEADLINE_SECS", 0.5)
    log.configure("warning")
    search = threading.Thread(target=_run, daemon=True)
    try:
        search.start()
        search.join(10)
        assert not search.is_alive(), "a token check held the award phase past its deadline"
    finally:
        release.set()
        search.join(10)
    captured = capsys.readouterr()

    assert _summaries(captured.err) == [
        "Awards incomplete: PointsPath failed (not answered within 0.5 s)."
    ], captured.err
    assert _flight_numbers(captured.out) == []


# ─────────────────────────────── the envelope ───────────────────────────────

# A note naming a key of the envelope; every other note is a stderr line.
_KEY_NOTE = re.compile(r"^[a-z_]+: ")


async def _stalls(request: httpx.Request) -> httpx.Response:
    """Answers long after any deadline a test sets, so the deadline cuts it."""
    await anyio.sleep(10)
    return _answer(request)


def _pointspath_answering(
    pointspath: Any,
) -> Callable[[pytest.MonkeyPatch, Path], None]:
    def use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _use_providers(monkeypatch, tmp_path, pointspath, _empty_seats)

    return use


def _seats_answering(seats: Any) -> Callable[[pytest.MonkeyPatch, Path], None]:
    def use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _use_providers(monkeypatch, tmp_path, _answer, seats)

    return use


def _united(answer: Any) -> Any:
    """PointsPath answering United's searches with `answer`, and the rest as `_answer`."""

    def pointspath(request: httpx.Request) -> Any:
        return (answer if json.loads(request.content)["airline"] == "United" else _answer)(request)

    return pointspath


def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("", request=request)


def _error(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="upstream failed")


def _error_with_no_body(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _built_as(provider: str, create: Any) -> Callable[[pytest.MonkeyPatch, Path], None]:
    """Only `provider` is configured, and building it runs `create`."""

    def use(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
        class _Provider:
            @classmethod
            async def create(cls, **_kw: object) -> Any:
                return await create()

        pp = provider == "PointsPath"
        monkeypatch.setattr(registry, "pp_is_configured", lambda: pp)
        monkeypatch.setattr(registry, "seats_is_configured", lambda: not pp)
        monkeypatch.setattr(
            registry, "PointsPathProvider" if pp else "SeatsAeroProvider", _Provider
        )
        monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)

    return use


async def _raises() -> Any:
    raise httpx.ConnectTimeout("")


async def _never_built() -> Any:
    await anyio.sleep(10)


def _searching_raises(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    class _Raising:
        name = "Seats.aero"
        enabled = True

        async def search_leg(self, *_a: object, **_kw: object) -> list[AwardFlight]:
            msg = "boom"
            raise RuntimeError(msg)

        async def aclose(self) -> None:
            return None

    hand_out_providers(monkeypatch, _Raising())
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(_pointspath_answering(_united(_timed_out)), id="pp-raised"),
        pytest.param(_pointspath_answering(_united(_error)), id="pp-error"),
        pytest.param(_pointspath_answering(_united(_error_with_no_body)), id="pp-error-no-body"),
        pytest.param(_pointspath_answering(_united(_stalls)), id="pp-cut"),
        pytest.param(_seats_answering(_failing_seats), id="seats-error"),
        pytest.param(_seats_answering(_refused), id="seats-raised"),
        pytest.param(_seats_answering(_stalls), id="seats-cut"),
        pytest.param(_built_as("PointsPath", _raises), id="pp-not-built"),
        pytest.param(_built_as("PointsPath", _never_built), id="pp-build-cut"),
        pytest.param(_built_as("Seats.aero", _raises), id="seats-not-built"),
        pytest.param(_built_as("Seats.aero", _never_built), id="seats-build-cut"),
        pytest.param(_searching_raises, id="provider-raised"),
    ],
)
def test_the_envelope_says_what_the_awards_incomplete_line_says(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: Callable[[pytest.MonkeyPatch, Path], None],
) -> None:
    """Each site that records a failure for that line narrows the envelope, and
    the line is the one thing its notes say about it."""
    failing(monkeypatch, tmp_path)
    monkeypatch.setattr(pp_cli, "AWARD_DEADLINE_SECS", 0.2)
    log.configure("warning")
    _envelope.run("search", _run, consoles=(pp_cli.err,))
    env = json.loads(capsys.readouterr().out)
    said = [n for n in env["notes"] if not _KEY_NOTE.match(n)]
    assert len(_summaries("\n".join(said))) == 1, env["notes"]
    assert (env["complete"], len(said)) == (False, 1), env["notes"]
