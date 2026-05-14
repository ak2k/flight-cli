"""URL generators for paste-back / handoff workflows.

- `matrix_deep_link(search)` → matrix.itasoftware.com/{flights,calendar} URL
  with base64-encoded JSON state. Reproduces the search in the web UI.
- `google_flights_url(search)` → google.com/travel/flights URL with tfs=
  base64-protobuf payload. Click-through to actual booking.

Both dispatch on the Search variant via `match` (with `assert_never` for
exhaustiveness checking)."""
from __future__ import annotations
import base64, json, urllib.parse
from datetime import date, timedelta
from typing import Any, Literal, assert_never

from .domain import (
    Cabin, Leg, Pax, Search, SearchOptions,
    SpecificDateSearch, CalendarSearch, CalendarFollowup,
)


# ───────────────────────── Matrix deep-link URL ────────────────────────────

def _spa_options_block(opts: SearchOptions, *, extra_stops_override: int | None = None) -> dict[str, str]:
    """SPA URL-state `options` dict. All values are strings."""
    if extra_stops_override is not None:
        es = extra_stops_override
    elif opts.extra_stops is not None:
        es = opts.extra_stops
    else:
        # Mirror Matrix UI default: -1 when stops constrained, 1 otherwise.
        es = -1 if opts.max_stops is not None else 1
    return {
        "cabin": opts.cabin.value,
        "stops": ("-1" if opts.max_stops is None or opts.max_stops < 0
                  else str(opts.max_stops)),
        "extraStops": str(es),
        "allowAirportChanges": "true" if opts.allow_airport_changes else "false",
        "showOnlyAvailable": "true" if opts.show_only_available else "false",
    }


def _pax_strs(pax: Pax) -> dict[str, str]:
    d = {"adults": str(pax.adults)}
    for k, v in (("children", pax.children), ("seniors", pax.seniors),
                  ("youth", pax.youth),
                  ("infantsInSeat", pax.infants_in_seat),
                  ("infantsInLap", pax.infants_in_lap)):
        if v:
            d[k] = str(v)
    return d


def _spa_specific_leg(leg: Leg) -> dict[str, Any]:
    """SPA URL-state slice for a specific-date search."""
    return {
        "origin": list(leg.origins),
        "dest": list(leg.destinations),
        "dates": {
            "searchDateType": "specific",
            "departureDate": leg.date.isoformat() if leg.date else "",
            "departureDateType": "depart",
            "departureDateModifier": str(leg.date_minus),
            "departureDatePreferredTimes": [t.value for t in leg.time_ranges],
            "returnDate": "",
            "returnDateType": "depart",
            "returnDateModifier": str(leg.date_plus),
            "returnDatePreferredTimes": [],
        },
    }


def _spa_calendar_leg(out: Leg, ret: Leg | None,
                       start: date, end: date,
                       duration_min: int, duration_max: int) -> dict[str, Any]:
    """SPA URL-state slice for calendar mode. Round-trip is folded into ONE
    slice with `routingRet`/`extRet` carrying return-direction routing."""
    d: dict[str, Any] = {
        "origin": list(out.origins),
        "dest": list(out.destinations),
    }
    if out.route_language or out.extension:
        d["routing"] = out.route_language or ""
        d["ext"] = out.extension or ""
        if ret is None or (ret.route_language == out.route_language and
                            ret.extension == out.extension):
            d["routingRet"] = ""
            d["extRet"] = ""
        else:
            d["routingRet"] = ret.route_language or ""
            d["extRet"] = ret.extension or ""
    d["dates"] = {
        "searchDateType": "calendar",
        "departureDate": start.isoformat(),
        "departureDateType": "depart",
        "departureDateModifier": "0",
        "departureDatePreferredTimes": [t.value for t in out.time_ranges],
        "duration": (f"{duration_min}-{duration_max}"
                     if duration_min != duration_max
                     else str(duration_min)),
        "returnDateType": "depart",
        "returnDateModifier": "0",
        "returnDatePreferredTimes": ([t.value for t in ret.time_ranges]
                                       if ret else []),
    }
    return d


def _encode_payload(payload: dict[str, Any], path: str) -> str:
    b = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return f"https://matrix.itasoftware.com/{path}?search={urllib.parse.quote(b)}"


def matrix_deep_link(s: Search) -> str:
    """Build the matrix.itasoftware.com deep-link URL for any search variant."""
    match s:
        case SpecificDateSearch():
            trip = ("round-trip" if len(s.legs) == 2
                    else ("one-way" if len(s.legs) == 1 else "multi-city"))
            payload = {
                "type": trip,
                "slices": [_spa_specific_leg(leg) for leg in s.legs],
                "options": _spa_options_block(s.options),
                "pax": _pax_strs(s.options.pax),
            }
            return _encode_payload(payload, "flights")

        case CalendarSearch():
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == 2 else None
            payload = {
                "type": "round-trip" if ret else "one-way",
                "slices": [_spa_calendar_leg(out, ret, s.window.start, s.window.end,
                                              s.window.duration_min,
                                              s.window.duration_max)],
                "options": _spa_options_block(s.options),
                "pax": _pax_strs(s.options.pax),
            }
            return _encode_payload(payload, "calendar")

        case CalendarFollowup():
            # The SPA URL for a followup is essentially a specific-date URL
            # for the picked dates — that's how you'd share "the itineraries
            # I'm looking at" with someone else.
            trip = "round-trip" if len(s.legs) == 2 else "one-way"
            payload = {
                "type": trip,
                "slices": [_spa_specific_leg(leg) for leg in s.legs],
                "options": _spa_options_block(s.options),
                "pax": _pax_strs(s.options.pax),
            }
            return _encode_payload(payload, "flights")

        case _:
            assert_never(s)


# ───────────────────────── Google Flights URL ──────────────────────────────

_CABIN_TFS: dict[Cabin, Literal["economy", "premium-economy", "business", "first"]] = {
    Cabin.COACH: "economy",
    Cabin.PREMIUM_COACH: "premium-economy",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}


def google_flights_url(s: Search, *, currency: str = "USD",
                        language: str = "en") -> str:
    """Build a Google Flights `tfs=` URL that opens directly into a populated
    search result. Multi-airport is flattened to first IATA per leg (Google
    Flights URL grammar doesn't support airport sets per slice).

    For CalendarSearch (no per-leg dates), uses window start as departure
    and start + mean(duration) as return — gives the user a representative
    URL to land on Google Flights with, even though Google doesn't have a
    calendar-grid concept."""
    from fast_flights import TFSData, FlightData, Passengers

    match s:
        case SpecificDateSearch() | CalendarFollowup():
            flight_data: list[Any] = []
            for leg in s.legs:
                # SpecificDate/Followup validators guarantee leg.date is set
                assert leg.date is not None
                flight_data.append(FlightData(
                    date=leg.date.isoformat(),
                    from_airport=leg.origins[0],
                    to_airport=leg.destinations[0]))
        case CalendarSearch():
            mean_dur = (s.window.duration_min + s.window.duration_max) // 2
            ret_date = s.window.start + timedelta(days=mean_dur)
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == 2 else None
            flight_data = [FlightData(
                date=s.window.start.isoformat(),
                from_airport=out.origins[0],
                to_airport=out.destinations[0])]
            if ret:
                flight_data.append(FlightData(
                    date=ret_date.isoformat(),
                    from_airport=ret.origins[0],
                    to_airport=ret.destinations[0]))
        case _:
            assert_never(s)

    if len(flight_data) == 1:
        trip = "one-way"
    elif len(flight_data) == 2:
        trip = "round-trip"
    else:
        trip = "multi-city"

    p = s.options.pax
    td = TFSData.from_interface(
        flight_data=flight_data,
        seat=_CABIN_TFS[s.options.cabin],
        trip=trip,
        passengers=Passengers(
            adults=(p.adults + p.seniors + p.youth) or 1,
            children=p.children,
            infants_in_seat=p.infants_in_seat,
            infants_on_lap=p.infants_in_lap,
        ),
    )
    b64 = td.as_b64().decode()
    return (f"https://www.google.com/travel/flights/search?"
            f"tfs={urllib.parse.quote(b64)}&hl={language}&curr={currency}")
