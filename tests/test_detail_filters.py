# pyright: reportPrivateUsage=false
"""`detail` asks Matrix every filter the calendar asked.

A calendar's grid cell is priced under its `--depart-times`, `--return-times`
and `--include-unavailable`; phase 2 of the same question has to carry them, or
the itineraries it lists answer a wider one. The followup wire writes
`timeRanges` and `checkAvailability` per search, the Matrix link
`departureDatePreferredTimes` and `showOnlyAvailable`. Matrix's runner is
replaced by a recorder, so nothing reaches the network."""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli, links, wire

if TYPE_CHECKING:
    from click.testing import Result

    from flight_cli.domain import CalendarFollowup

_DETAIL = ["detail", "JFK", "LAX", "--dep", "2026-10-20", "--no-matrix-url", "--no-google-url"]
_ROUND_TRIP = [*_DETAIL, "--return", "2026-10-27"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[CalendarFollowup]:
    searches: list[CalendarFollowup] = []

    def _run(search: CalendarFollowup, *_a: Any) -> None:
        searches.append(search)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_run", _run)
    return searches


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def _link(search: CalendarFollowup) -> dict[str, Any]:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(links.matrix_deep_link(search)).query)
    return json.loads(base64.b64decode(query["search"][0]))


def test_depart_times_reach_the_outbound_slice(sent: list[CalendarFollowup]) -> None:
    """RED at base (no such option)."""
    result = _invoke(*_DETAIL, "--depart-times", "morning")
    assert result.exit_code == 0, result.output
    [search] = sent
    [only] = wire.to_wire(search).as_json()["inputs"]["slices"]
    assert only["timeRanges"] == [{"min": "8:00", "max": "11:00"}]


def test_return_times_reach_the_return_slice_only(sent: list[CalendarFollowup]) -> None:
    """RED at base (no such option)."""
    result = _invoke(*_ROUND_TRIP, "--return-times", "evening")
    assert result.exit_code == 0, result.output
    [search] = sent
    out, back = wire.to_wire(search).as_json()["inputs"]["slices"]
    assert "timeRanges" not in out
    assert back["timeRanges"] == [{"min": "17:00", "max": "21:00"}]


def test_include_unavailable_stops_asking_for_availability(
    sent: list[CalendarFollowup],
) -> None:
    """RED at base (no such option; the options pinned availability on)."""
    result = _invoke(*_DETAIL, "--include-unavailable")
    assert result.exit_code == 0, result.output
    [search] = sent
    assert wire.to_wire(search).as_json()["inputs"]["checkAvailability"] is False


def test_the_matrix_link_carries_the_times_and_availability(
    sent: list[CalendarFollowup],
) -> None:
    """RED at base (no such options)."""
    result = _invoke(
        *_ROUND_TRIP,
        "--depart-times",
        "morning",
        "--return-times",
        "evening",
        "--include-unavailable",
    )
    assert result.exit_code == 0, result.output
    [search] = sent
    state = _link(search)
    dates = state["slices"][0]["dates"]
    assert dates["departureDatePreferredTimes"] == ["morning"]
    assert dates["returnDatePreferredTimes"] == ["evening"]
    assert state["options"]["showOnlyAvailable"] == "false"


# The body a plain round-trip `detail` sent at the base, captured there.
_PLAIN_BODY = (
    '{"summarizers": ["carrierStopMatrix", "currencyNotice", "solutionList", '
    '"itineraryPriceSlider", "itineraryCarrierList", "itineraryDepartureTimeRanges", '
    '"itineraryArrivalTimeRanges", "durationSliderItinerary", "itineraryOrigins", '
    '"itineraryDestinations", "itineraryStopCountList", "warningsItinerary"], '
    '"summarizerSet": "wholeTrip", "name": "calendarFollowup", "inputs": {"pax": '
    '{"adults": 1}, "cabin": "COACH", "page": {"current": 1, "size": 25}, "sliceIndex": '
    '0, "sorts": "default", "firstDayOfWeek": "SUNDAY", "internalUser": false, '
    '"changeOfAirport": true, "checkAvailability": true, "maxLegsRelativeToMin": 1, '
    '"slices": [{"origins": ["JFK"], "destinations": ["LAX"], "date": "2026-10-20", '
    '"filter": {"warnings": {"values": []}}, "selected": false}, {"origins": ["LAX"], '
    '"destinations": ["JFK"], "date": "2026-10-27", "filter": {"warnings": {"values": '
    '[]}}, "selected": false}], "startDate": "2026-10-20", "endDate": "2026-11-19", '
    '"layover": {"min": 5, "max": 7}}}'
)


def test_a_plain_detail_sends_the_body_it_sent_before(sent: list[CalendarFollowup]) -> None:
    """Green at base and tip."""
    result = _invoke(*_ROUND_TRIP)
    assert result.exit_code == 0, result.output
    [search] = sent
    assert json.dumps(wire.to_wire(search).as_json()) == _PLAIN_BODY
