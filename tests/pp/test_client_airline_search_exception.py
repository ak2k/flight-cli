# pyright: reportPrivateUsage=false
"""A per-airline failure drops that airline's answer; its log line must still
say why, including for exceptions whose message is empty."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import httpx
from structlog.testing import capture_logs

from flight_cli.pp.auth import Tokens
from flight_cli.pp.client import API_BASE, PPClient, SearchSpec

if TYPE_CHECKING:
    import pathlib

    from structlog.typing import EventDict

    from flight_cli.pp.models import AirlineSearchResponse


def _client(transport: httpx.MockTransport) -> PPClient:
    tokens = Tokens(
        access_token="TOKEN",  # noqa: S106 — dummy test value
        refresh_token="REFRESH",  # noqa: S106 — dummy test value
        expires_at=9999999999,
    )
    pp = PPClient(tokens)
    pp._client = httpx.AsyncClient(
        base_url=API_BASE, transport=transport, headers=pp._client.headers
    )
    return pp


def _search(
    handler: Any, monkeypatch: Any, tmp_path: pathlib.Path
) -> tuple[dict[str, AirlineSearchResponse], list[EventDict]]:
    monkeypatch.setattr("flight_cli.pp.client.UNSUPPORTED_CACHE", tmp_path / "unsupported.json")
    out: dict[str, AirlineSearchResponse] = {}

    async def go() -> None:
        pp = _client(httpx.MockTransport(handler))
        try:
            out.update(
                await pp.airline_search_many(
                    SearchSpec(origin="NYC", destination="LAX", date="2026-10-20"),
                    ("AirFrance",),
                ),
            )
        finally:
            await pp.aclose()

    with capture_logs() as logs:
        anyio.run(go)
    return out, [e for e in logs if e["event"] == "pp_airline_search_exception"]


def test_timeout_with_empty_message_logs_its_type(tmp_path: pathlib.Path, monkeypatch: Any):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=req)

    out, events = _search(handler, monkeypatch, tmp_path)
    assert out == {}
    assert [(e["airline"], e["error"], e["error_type"]) for e in events] == [
        ("AirFrance", "", "ReadTimeout"),
    ]


def test_unreadable_stop_is_a_logged_validation_failure(tmp_path: pathlib.Path, monkeypatch: Any):
    """A stop in neither known shape must drop the airline with a typed
    warning, never parse as a flight with no connection airports."""
    flight = {
        "origin": "JFK",
        "destination": "LAX",
        "localDepartureDateTime": "2026-10-20T07:22:00",
        "localArrivalDateTime": "2026-10-20T14:34:00",
        "firstFlightNumber": "AF6789",
        "numConnections": 1,
        "stops": [{"code": "SEA"}],
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outboundFlights": [flight], "inboundFlights": None})

    out, events = _search(handler, monkeypatch, tmp_path)
    assert out == {}
    assert [(e["airline"], e["error_type"]) for e in events] == [("AirFrance", "ValidationError")]
    assert "outboundFlights.0.stops.0" in events[0]["error"]
