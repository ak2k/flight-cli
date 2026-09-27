# pyright: reportPrivateUsage=false
# DIVERGE: the pinned-tfs encoder and the caveat helpers are module-internal by
# design; exporting them for a test would widen the API.
"""Airport sets and metro codes at the Google boundary.

`flight search NYC LAX` and `JFK,EWR LHR` are answered by Google over every
airport of the set. The expansion happens at the two Google sites only — the
fli bridge and the pinned link — so the Matrix request keeps the user's tokens.
Past the page's per-leg bound the search goes to Matrix, and the pinned link
under it falls back to the itinerary's own airports and says so."""

from __future__ import annotations

import base64
import io
import json
import urllib.parse
from datetime import date, timedelta
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter
from flight_cli.links import (
    _encode_gflight_pinned_tfs,
    build_search_tfs,
    google_flights_pinned_url,
)
from flight_cli.models import SearchResult
from flight_cli.wire import to_wire

# fli's FlightSegment validator rejects a past travel date, so the dates are
# derived from today rather than pinned.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)

# The skill's "to Europe" list: with one origin, 14 airports on the leg.
_EUROPE = "LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB"


def _search(origin: str, destination: str, *, round_trip: bool = False) -> SpecificDateSearch:
    o, d = cli._parse_iata_list(origin), cli._parse_iata_list(destination)
    legs = (Leg.of(o, d, _DEP),)
    if round_trip:
        legs += (Leg.of(d, o, _RET),)
    return SpecificDateSearch(legs=legs, options=SearchOptions())


def _names(entries: list[list[Any]]) -> list[str]:
    return [entry[0].name for entry in entries]


def _seg(origin: str, destination: str, carrier: str, flight: str, day: date) -> dict[str, str]:
    return {
        "origin": origin,
        "date": day.isoformat(),
        "destination": destination,
        "carrier": carrier,
        "flight": flight,
    }


def _tfs(url: str) -> bytes:
    b64 = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))


def _pinned_tfs(slices: list[dict[str, Any]]) -> bytes:
    return _encode_gflight_pinned_tfs(
        slices=slices, cabin=1, adults=1, children=0, infants_in_seat=0, infants_on_lap=0
    )


# ─────────────────────────────── the fli bridge ─────────────────────────────


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("NYC", ["JFK", "LGA", "EWR"]),
        ("JFK,EWR", ["JFK", "EWR"]),
        # fli's table has QSF as Ain Arnat, Algeria; the member table wins.
        ("QSF", ["SFO", "OAK", "SJC"]),
        ("NYC,JFK,BOS", ["JFK", "LGA", "EWR", "BOS"]),
    ],
)
def test_the_bridge_asks_google_for_every_airport_of_the_set(
    origin: str, expected: list[str]
) -> None:
    seg = to_fli_filter(_search(origin, "LAX")).flight_segments[0]
    assert _names(seg.departure_airport) == expected
    assert _names(seg.arrival_airport) == ["LAX"]


def test_a_round_trip_returns_from_the_destination_set_to_the_origin_set() -> None:
    out, back = to_fli_filter(_search("NYC", "LON", round_trip=True)).flight_segments
    london = ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"]
    assert (_names(out.departure_airport), _names(out.arrival_airport)) == (
        ["JFK", "LGA", "EWR"],
        london,
    )
    assert (_names(back.departure_airport), _names(back.arrival_airport)) == (
        london,
        ["JFK", "LGA", "EWR"],
    )


def test_the_matrix_request_keeps_the_users_metro_code() -> None:
    """Matrix takes metro codes natively; the expansion is Google's alone."""
    body = json.dumps(to_wire(_search("NYC", "LAX")).as_json())
    assert "NYC" in body
    assert "LGA" not in body


def test_a_round_trip_at_the_bound_on_each_leg_is_served_by_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is per leg: a round trip carries 11 airports in each slice (22
    in the URL), which Google served live."""
    ten = "JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA"
    seen: list[tuple[Leg, ...]] = []

    def _gf(**kw: Any) -> None:
        seen.append(kw["legs"])

    def _unreached(**_kw: object) -> None:
        raise AssertionError("an 11-airport round trip left Google Flights")

    monkeypatch.setattr(cli, "_run_gflight_path", _gf)
    for path in ("_run_matrix_path", "_run_enriched_path"):
        monkeypatch.setattr(cli, path, _unreached)
    argv = ["search", ten, "LAX", "--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
    result = CliRunner().invoke(cli.app, [*argv, "--fast", "--cash-only"])
    assert result.exit_code == 0, result.output

    (legs,) = seen
    filters = to_fli_filter(SpecificDateSearch(legs=legs, options=SearchOptions()))
    for seg in filters.flight_segments:
        assert len(seg.departure_airport) + len(seg.arrival_airport) == 11
    assert build_search_tfs(filters)


# ─────────────────────────────── the pinned link ────────────────────────────


def test_a_one_airport_search_pins_exactly_as_before() -> None:
    segs = [_seg("JFK", "LHR", "BA", "112", _DEP)]
    url = google_flights_pinned_url(_search("JFK", "LHR"), outbound_segments=segs)
    expected = [{"date": _DEP.isoformat(), "origin": "JFK", "destination": "LHR", "segments": segs}]
    assert _tfs(url) == _pinned_tfs(expected)


def test_the_pinned_link_carries_every_airport_of_both_slices() -> None:
    out = [_seg("LGA", "LAX", "F9", "3287", _DEP)]
    back = [_seg("LAX", "LGA", "F9", "3215", _RET)]
    url = google_flights_pinned_url(
        _search("NYC", "LAX", round_trip=True), outbound_segments=out, return_segments=back
    )
    nyc = ("JFK", "LGA", "EWR")
    assert _tfs(url) == _pinned_tfs(
        [
            {"date": _DEP.isoformat(), "origin": nyc, "destination": ("LAX",), "segments": out},
            {"date": _RET.isoformat(), "origin": ("LAX",), "destination": nyc, "segments": back},
        ]
    )


def test_past_the_bound_the_pinned_link_uses_the_itinerarys_own_airports() -> None:
    """A Matrix-served region list keeps a pinned link: the slice is written
    from the pinned segments' first departure and last arrival, one airport per
    end, which is a shape the page serves — not the list's first code (LHR)
    over an itinerary that lands at CDG."""
    segs = [_seg("JFK", "DUB", "EI", "106", _DEP), _seg("DUB", "CDG", "EI", "520", _DEP)]
    url = google_flights_pinned_url(_search("JFK", _EUROPE), outbound_segments=segs)
    expected = [{"date": _DEP.isoformat(), "origin": "JFK", "destination": "CDG", "segments": segs}]
    assert _tfs(url) == _pinned_tfs(expected)


def _emitted(search: SpecificDateSearch, origin: str, destination: str) -> str:
    """What `_emit_urls` prints for a one-slice result flying origin -> destination."""
    buf = io.StringIO()
    console = Console(file=buf, width=400, no_color=True, highlight=False)
    result = SearchResult.model_validate(
        {
            "solutions": [
                {
                    "itinerary": {
                        "slices": [
                            {
                                "flights": ["BA112"],
                                "departure": f"{_DEP.isoformat()}T09:00:00",
                                "arrival": f"{_DEP.isoformat()}T21:00:00",
                                "origin": {"code": origin},
                                "destination": {"code": destination},
                                "stops": [],
                            }
                        ]
                    }
                }
            ]
        }
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "console", console)
        cli._emit_urls(search, matrix_url=False, google_url=True, result=result, pick=None)
    return " ".join(buf.getvalue().split())


def test_past_the_bound_the_pinned_link_says_it_shows_the_itinerarys_airports() -> None:
    printed = _emitted(_search("JFK", _EUROPE), "JFK", "LHR")
    assert "Google Flights (cheapest itinerary pinned):" in printed, printed
    assert (
        "note: the link shows the pinned itinerary's own airports, not every airport "
        "searched: Google's page can't take 14 airports on one leg (its limit is 11)"
    ) in printed, printed


def test_within_the_bound_the_pinned_link_needs_no_note() -> None:
    printed = _emitted(_search("NYC", "LHR"), "EWR", "LHR")
    assert "pinned" in printed, printed
    assert "note:" not in printed, printed


# ─────────────────── the fast_flights fallback link and titles ──────────────


def test_the_fallback_link_is_caveated_for_a_single_metro_code() -> None:
    """fast_flights writes the metro code itself as the airport; the note says
    which airports the search covered, so the link is not read as serving them."""
    notes = cli._gflight_url_caveats(_search("NYC", "LAX"))
    assert notes == [
        "multi-airport search narrowed to NYC→LAX (Google's link format takes one airport "
        "code per end; the search covered JFK,LGA,EWR→LAX)"
    ]


def test_a_code_that_is_its_own_airport_gets_no_fallback_caveat() -> None:
    assert cli._gflight_url_caveats(_search("LAX", "JFK")) == []


def test_the_google_table_titles_name_the_whole_set(capsys: pytest.CaptureFixture[str]) -> None:
    legs = _search("JFK,EWR", "LHR").legs
    cli._render_gflight_table([], legs=legs, top_n=1)
    cli._render_merged([], legs=legs, top_n=1)
    out = capsys.readouterr().out
    assert "Google Flights · JFK,EWR→LHR" in out, out
    assert "Google Flights + Matrix · JFK,EWR→LHR" in out, out
