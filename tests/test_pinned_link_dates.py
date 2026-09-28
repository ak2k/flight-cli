# pyright: reportPrivateUsage=false
"""The Google Flights link under a row pins it only on the days its source gives
each flight. Any other row gets the search link, whose label claims no pin."""

from __future__ import annotations

import base64
import urllib.parse
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from flight_cli import cli
from flight_cli._enrich import merge_results
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.links import google_flights_pinned_url, google_flights_url
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    import pytest

_DAY = date.today() + timedelta(days=45)
_D, _D1 = _DAY.isoformat(), (_DAY + timedelta(days=1)).isoformat()
_SEARCH = SpecificDateSearch(legs=(Leg.of(("SYD",), ("HNL",), _DAY),), options=SearchOptions())


def _nz(**extra: Any) -> dict[str, Any]:
    """NZ104 SYD-AKL, then NZ10 AKL-HNL, which leaves Auckland after midnight
    and lands in Honolulu on the day the trip began."""
    return {
        "flights": ["NZ104", "NZ10"],
        "departure": f"{_D}T18:00+11:00",
        "arrival": f"{_D}T10:00-10:00",
        "origin": {"code": "SYD"},
        "destination": {"code": "HNL"},
        "stops": [{"code": "AKL"}],
        **extra,
    }


def _result(slice_: dict[str, Any], price: str, sid: str | None = None) -> SearchResult:
    solution: dict[str, Any] = {"displayTotal": price, "itinerary": {"slices": [slice_]}}
    if sid is not None:
        solution["id"] = sid
    return SearchResult.model_validate({"solutions": [solution]})


def _google_row() -> SearchResult:
    return _result(_nz(segment_dates=[_D, _D1]), "USD900.00")


def _matrix_row() -> SearchResult:
    return _result(_nz(), "USD880.00", sid="sol-1")


def _printed_google_link(result: SearchResult, capsys: pytest.CaptureFixture[str]) -> str:
    cli._emit_urls(_SEARCH, matrix_url=False, google_url=True, result=result)
    # rich hard-wraps a URL at the console width.
    return "".join(capsys.readouterr().out.split())


def _tfs(url: str) -> bytes:
    blob = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["tfs"][0]
    return base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))


def test_a_matrix_connection_with_no_flight_dates_gets_the_unpinned_link(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printed = _printed_google_link(_matrix_row(), capsys)
    assert "pinned" not in printed
    assert "GoogleFlights(tfs=structured):" in printed
    assert google_flights_url(_SEARCH) in printed


def test_a_google_row_pins_each_flight_on_the_day_google_gives_it() -> None:
    url = cli._try_pinned_gflight_url(_SEARCH, _google_row(), 0)
    assert url == google_flights_pinned_url(
        _SEARCH,
        outbound_segments=[
            {"origin": "SYD", "date": _D, "destination": "AKL", "carrier": "NZ", "flight": "104"},
            {"origin": "AKL", "date": _D1, "destination": "HNL", "carrier": "NZ", "flight": "10"},
        ],
        return_segments=None,
    )


def test_a_matrix_connection_matched_to_a_google_row_is_pinned_on_googles_days(
    capsys: pytest.CaptureFixture[str],
) -> None:
    google = _google_row()
    (row,) = merge_results(google, _matrix_row())
    printed = _printed_google_link(google.model_copy(update={"solutions": [row.itinerary]}), capsys)
    assert "GoogleFlights(cheapestitinerarypinned):" in printed
    googles_own = cli._try_pinned_gflight_url(_SEARCH, google, 0)
    assert googles_own is not None
    assert googles_own in printed
    assert _D1.encode() in _tfs(googles_own).split(b"104", 1)[1]
