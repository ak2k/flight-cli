"""URL generators for paste-back / handoff workflows.

- `matrix_deep_link(search)` → matrix.itasoftware.com/{flights,calendar} URL
  with base64-encoded JSON state. Reproduces the search in the web UI.
- `google_flights_url(search)` → google.com/travel/flights URL with tfs=
  base64-protobuf payload. Click-through to actual booking.

Both dispatch on the Search variant via `match` (with `assert_never` for
exhaustiveness checking).

The same tfs= writer also builds the gflight backend's *transport* URL —
`build_search_tfs` + `google_flights_search_page_url`, fetched by
`_gflight_ids` — so the pin and the search encode through one code path."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal, assert_never, cast

from ._gf_errors import GfTfsUnsupportedError
from ._metro import expand_airports, gf_leg_refusal
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .domain import TimeWindow
    from .models import Slice

# ───────────────────────── Matrix deep-link URL ────────────────────────────


def _spa_options_block(
    opts: SearchOptions,
    *,
    extra_stops_override: int | None = None,
) -> dict[str, str]:
    """SPA URL-state `options` dict. All values are strings."""
    if extra_stops_override is not None:
        es = extra_stops_override
    elif opts.extra_stops is not None:
        es = opts.extra_stops
    else:
        # Mirror Matrix UI default: -1 when stops constrained, 1 otherwise.
        es = -1 if opts.max_extra_stops is not None else 1
    return {
        "cabin": opts.cabin.value,
        "stops": (
            "-1"
            if opts.max_extra_stops is None or opts.max_extra_stops < 0
            else str(opts.max_extra_stops)
        ),
        "extraStops": str(es),
        "allowAirportChanges": "true" if opts.allow_airport_changes else "false",
        "showOnlyAvailable": "true" if opts.show_only_available else "false",
    }


def _pax_strs(pax: Pax) -> dict[str, str]:
    d = {"adults": str(pax.adults)}
    for k, v in (
        ("children", pax.children),
        ("seniors", pax.seniors),
        ("youth", pax.youth),
        ("infantsInSeat", pax.infants_in_seat),
        ("infantsInLap", pax.infants_in_lap),
    ):
        if v:
            d[k] = str(v)
    return d


def _preferred_times(windows: Sequence[TimeWindow]) -> list[str]:
    """The SPA's preferred-times list: it names the six buckets and nothing
    finer, so a window to the minute is left out of the link."""
    return [w.value for w in windows if isinstance(w, TimeOfDay)]


def _spa_specific_leg(leg: Leg, *, return_leg: Leg | None = None) -> dict[str, Any]:
    """SPA URL-state slice for a specific-date search.

    For round-trip, pass the inbound leg as `return_leg` so the slice carries
    both directions in a single record (the SPA's current schema as of 2026-05).
    For one-way / multi-city, omit `return_leg`.
    """
    return_date = return_leg.date.isoformat() if return_leg and return_leg.date else ""
    return_modifier = str(return_leg.date_minus if return_leg else leg.date_plus)
    return_times = _preferred_times(return_leg.time_ranges) if return_leg else []
    return {
        "origin": list(leg.origins),
        "dest": list(leg.destinations),
        "dates": {
            "searchDateType": "specific",
            "departureDate": leg.date.isoformat() if leg.date else "",
            # "depart" | "arrive" — the SPA's encoding of arrival-date intent,
            # the URL-state counterpart of the API's `isArrivalDate` bool.
            "departureDateType": "arrive" if leg.is_arrival_date else "depart",
            "departureDateModifier": str(leg.date_minus),
            "departureDatePreferredTimes": _preferred_times(leg.time_ranges),
            "returnDate": return_date,
            "returnDateType": "arrive" if (return_leg and return_leg.is_arrival_date) else "depart",
            "returnDateModifier": return_modifier,
            "returnDatePreferredTimes": return_times,
        },
        **_spa_routing_fields(leg, return_leg),
    }


def _spa_routing_fields(leg: Leg, return_leg: Leg | None) -> dict[str, str]:
    """Routing-language / extension-code keys for a SPA URL-state slice.

    The SPA names these `routing` / `ext` — NOT the `routeLanguage` /
    `commandLine` the /batch API uses for the same values. Both shapes were
    captured from the real UI: with codes set the slice carries all four keys
    (`routingRet` / `extRet` hold the inbound leg's own codes); with none set
    it omits them entirely, which is what the tracked fixtures show. We mirror
    that, so a link is byte-identical to what the app itself would produce.

    Dropping these meant a link built from `--routing BA+ --ext "MAXSTOPS 0"`
    opened an UNCONSTRAINED search — offering the user itineraries the CLI had
    deliberately excluded.
    """
    routing = leg.route_language or ""
    ext = leg.extension or ""
    routing_ret = (return_leg.route_language or "") if return_leg else ""
    ext_ret = (return_leg.extension or "") if return_leg else ""
    if not any((routing, ext, routing_ret, ext_ret)):
        return {}
    return {
        "routing": routing,
        "ext": ext,
        "routingRet": routing_ret,
        "extRet": ext_ret,
    }


def _spa_specific_slices(legs: tuple[Leg, ...]) -> tuple[str, list[dict[str, Any]]]:
    """Build (type, slices) for a specific-date SpecificDateSearch / followup.

    Round-trip is encoded as a single slice carrying both dates — the SPA
    drifted to this schema after the legacy two-slice form was deprecated.
    """
    n = len(legs)
    if n == 1:
        return "one-way", [_spa_specific_leg(legs[0])]
    if n == _ROUND_TRIP_LEGS and _is_inverse_pair(legs[0], legs[1]):
        return "round-trip", [_spa_specific_leg(legs[0], return_leg=legs[1])]
    return "multi-city", [_spa_specific_leg(leg) for leg in legs]


def _is_inverse_pair(out: Leg, ret: Leg) -> bool:
    """Whether two legs form a true round trip — the return departs where the
    outbound landed AND lands where it started.

    Round-trip's SPA encoding folds both legs into ONE slice carrying two
    dates, which structurally cannot express a second route. Treating any
    2-leg search as a round trip therefore DELETED the second leg: SFO->JFK
    plus LAX->HNL encoded as SFO->JFK with a return date, and LAX/HNL simply
    vanished from the emitted link. Multi-city keeps a slice per leg, so
    anything that isn't a genuine inverse belongs there.

    Multi-airport legs count as inverse only when the sets match exactly; a
    partial overlap is an itinerary we cannot faithfully fold.
    """
    return set(out.destinations) == set(ret.origins) and set(out.origins) == set(ret.destinations)


def _spa_calendar_leg(
    out: Leg, ret: Leg | None, start: date, end: date, duration_min: int, duration_max: int
) -> dict[str, Any]:
    """SPA URL-state slice for calendar mode. Round-trip is folded into ONE
    slice with `routingRet`/`extRet` carrying return-direction routing."""
    d: dict[str, Any] = {
        "origin": list(out.origins),
        "dest": list(out.destinations),
    }
    if out.route_language or out.extension:
        d["routing"] = out.route_language or ""
        d["ext"] = out.extension or ""
        if ret is None or (
            ret.route_language == out.route_language and ret.extension == out.extension
        ):
            d["routingRet"] = ""
            d["extRet"] = ""
        else:
            d["routingRet"] = ret.route_language or ""
            d["extRet"] = ret.extension or ""
    dates: dict[str, Any] = {
        "searchDateType": "calendar",
        "departureDate": start.isoformat(),
        "departureDateType": "depart",
        "departureDateModifier": "0",
        "departureDatePreferredTimes": _preferred_times(out.time_ranges),
    }
    if ret is not None:
        # `duration` is the trip LENGTH — nights between the outbound and the return —
        # so it belongs only on a round-trip URL. On a one-way it makes two otherwise
        # identical searches produce different links, and opening one hands the SPA the
        # trip-length state that makes Matrix answer 200 + "Internal server error"
        # (work-h70kv.7). Inserted here so a round-trip URL keeps its captured key order.
        dates["duration"] = (
            f"{duration_min}-{duration_max}" if duration_min != duration_max else str(duration_min)
        )
    dates["returnDateType"] = "depart"
    dates["returnDateModifier"] = "0"
    dates["returnDatePreferredTimes"] = _preferred_times(ret.time_ranges) if ret else []
    d["dates"] = dates
    return d


_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way; >2 = multi-city


def _encode_payload(payload: dict[str, Any], path: str) -> str:
    b = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return f"https://matrix.itasoftware.com/{path}?search={urllib.parse.quote(b)}"


def matrix_itinerary_url(
    s: Search,
    *,
    solution_id: str,
    session: str,
    solution_set: str,
) -> str:
    """Matrix `/itinerary` URL pre-selecting a specific solution.

    Requires server-generated identifiers from the `/v1/search` response:

    - `solution_id` → maps to the URL's `solution.Si`; comes from
      `Itinerary.id` on the chosen row.
    - `session` → maps to `solution.sessionId`; from `SearchResult.session`.
    - `solution_set` → maps to `solution.rh`; from `SearchResult.solution_set`.

    Only meaningful for specific-date searches; calendar / followup don't
    produce itinerary rows and won't have valid identifiers. The URL is the
    same shape as `matrix_deep_link()` plus the `solution` block, and it
    routes to the SPA's `/itinerary` view (the page reached by clicking a
    flight in the results table).

    Session-scoped: Matrix's session/solutionSet/Si IDs expire on the server
    side (~10-30 min observed); a stale URL fails with `Input error for
    "bookingDetails" (SolutionSummarizer), "x.solution" is required` and the
    UI shows no booking details. Re-run the search to get a fresh URL.
    """
    if not isinstance(s, SpecificDateSearch):
        raise TypeError(f"matrix_itinerary_url requires SpecificDateSearch, got {type(s).__name__}")
    trip, slices = _spa_specific_slices(s.legs)
    payload = {
        "type": trip,
        "slices": slices,
        "options": _spa_options_block(s.options),
        "pax": _pax_strs(s.options.pax),
        # Sub-key order matches the SPA's emission (sessionId, xd, rh, Si).
        "solution": {
            "sessionId": session,
            "xd": True,
            "rh": solution_set,
            "Si": solution_id,
        },
    }
    return _encode_payload(payload, "itinerary")


def matrix_deep_link(s: Search) -> str:
    """Build the matrix.itasoftware.com deep-link URL for any search variant."""
    match s:
        case SpecificDateSearch():
            trip, slices = _spa_specific_slices(s.legs)
            payload = {
                "type": trip,
                "slices": slices,
                "options": _spa_options_block(s.options),
                "pax": _pax_strs(s.options.pax),
            }
            return _encode_payload(payload, "flights")

        case CalendarSearch():
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == _ROUND_TRIP_LEGS else None
            payload = {
                "type": "round-trip" if ret else "one-way",
                "slices": [
                    _spa_calendar_leg(
                        out,
                        ret,
                        s.window.start,
                        s.window.end,
                        s.window.duration_min,
                        s.window.duration_max,
                    )
                ],
                "options": _spa_options_block(s.options),
                "pax": _pax_strs(s.options.pax),
            }
            return _encode_payload(payload, "calendar")

        case CalendarFollowup():
            # The SPA URL for a followup is essentially a specific-date URL
            # for the picked dates — that's how you'd share "the itineraries
            # I'm looking at" with someone else.
            trip, slices = _spa_specific_slices(s.legs)
            payload = {
                "type": trip,
                "slices": slices,
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

# Numeric tfs= cabin codes (field 9 in the pinned-itinerary protobuf).
# Observed value for BUSINESS is 3 in the captured payload.
_CABIN_TFS_INT: dict[Cabin, int] = {
    Cabin.COACH: 1,
    Cabin.PREMIUM_COACH: 2,
    Cabin.BUSINESS: 3,
    Cabin.FIRST: 4,
}

# tfs= trip-type enum (field 19).
_GF_TRIP_ROUND_TRIP = 1
_GF_TRIP_ONE_WAY = 2
_GF_TRIP_MULTI_CITY = 3

# tfs= field 8 is a repeated varint, one entry per occupant, carrying the
# passenger TYPE. The infant codes are the reverse of what fast_flights' enum
# (flights_pb2.Passenger) names them: Google prices 3 at a tenth of the adult
# fare, a lap, and 4 at the full fare, a seat.
_GF_PAX_ADULT = 1
_GF_PAX_CHILD = 2
_GF_PAX_INFANT_ON_LAP = 3
_GF_PAX_INFANT_IN_SEAT = 4


def _gf_pax_kinds(
    *, adults: int, children: int, infants_in_seat: int, infants_on_lap: int
) -> list[int]:
    """Field 8's entries: one passenger-kind code per occupant."""
    return (
        [_GF_PAX_ADULT] * adults
        + [_GF_PAX_CHILD] * children
        + [_GF_PAX_INFANT_IN_SEAT] * infants_in_seat
        + [_GF_PAX_INFANT_ON_LAP] * infants_on_lap
    )


# ───────────────── Google Flights tfs= protobuf (RE'd) ──────────────────────
#
# The `tfs=` query param is a base64-encoded protobuf. Two flavors:
#
#   SEARCH-PRELOAD (the one fast_flights.TFSData generates): lands on a results
#   list. Slices have date + origin/dest only.
#
#   PIN-ITINERARY (RE'd from headed-browser navigation capture in
#   research/capture/manual-1779193717/): lands on a specific selected
#   itinerary. Adds these fields vs. the search-preload form:
#     - Top-level field 1 = 28 (some "mode" varint; constant in observed cases)
#     - Top-level field 2 = 2  (constant)
#     - Each slice (top-level field 3) gains repeated field 4 = segments:
#         {1: origin_iata, 2: yyyy-mm-dd, 3: dest_iata, 5: carrier, 6: flt_no}
#     - Slice pax-info fields 13/14 gain prefix `1: <pax_count>` varint
#     - Top-level field 8 becomes packed/repeated `1` (one per adult/etc)
#       rather than a single bytes blob.
#     - Top-level field 16 = {1: 0xFFFFFFFFFFFFFFFF} (sentinel/marker)
#
# We hand-encode the pin payload via _PbWriter rather than introducing a
# generated proto schema — the schema is private to Google Flights and the
# field tags could shift; keeping the encoder tiny and inline makes any
# future RE round cheap. Verification: byte-exact reproduction of captured
# pinned `tfs=` payloads is asserted in tests/test_links.py.


class _PbWriter:
    """Minimal protobuf writer covering varint, length-delimited, and message
    composition. Enough for the tfs= schema; no float/fixed/sint support."""

    def __init__(self) -> None:
        self.buf = bytearray()

    _VARINT_CONT = 0x80
    _VARINT_MASK = 0x7F

    @staticmethod
    def _varint(n: int) -> bytes:
        out = bytearray()
        while n > _PbWriter._VARINT_MASK:
            out.append((n & _PbWriter._VARINT_MASK) | _PbWriter._VARINT_CONT)
            n >>= 7
        out.append(n & _PbWriter._VARINT_MASK)
        return bytes(out)

    def _tag(self, field: int, wire: int) -> None:
        self.buf.extend(self._varint((field << 3) | wire))

    def varint(self, field: int, value: int) -> None:
        self._tag(field, 0)
        self.buf.extend(self._varint(value))

    def string(self, field: int, value: str) -> None:
        data = value.encode("utf-8")
        self._tag(field, 2)
        self.buf.extend(self._varint(len(data)))
        self.buf.extend(data)

    def message(self, field: int, inner: _PbWriter) -> None:
        data = bytes(inner.buf)
        self._tag(field, 2)
        self.buf.extend(self._varint(len(data)))
        self.buf.extend(data)


def _endpoint_codes(value: str | Sequence[str]) -> tuple[str, ...]:
    # A str is itself a Sequence[str]: iterated, "HNL" would write H, N and L.
    return (value,) if isinstance(value, str) else tuple(value)


def _tfs_slice_message(sl: dict[str, Any]) -> _PbWriter:
    """One slice (top-level field 3) of `_encode_gflight_pinned_tfs`."""
    s = _PbWriter()
    s.string(2, sl["date"])
    # Stop ceiling is ZERO-based here (0 = nonstop) and absent means "any";
    # writing 0 for "any" would pin every search to nonstop. Emitted before
    # the selected legs to match the field order Google's own URLs carry.
    max_stops = sl.get("max_stops")
    if max_stops is not None:
        s.varint(5, max_stops)
    for code in sl.get("carriers") or ():
        s.string(6, code)
    for field, hour in zip((8, 9, 10, 11), sl.get("hours") or (), strict=False):
        s.varint(field, hour)
    if (max_duration := sl.get("max_duration")) is not None:
        s.varint(12, max_duration)
    for seg in sl["segments"]:
        seg_w = _PbWriter()
        seg_w.string(1, seg["origin"])
        seg_w.string(2, seg["date"])
        seg_w.string(3, seg["destination"])
        seg_w.string(5, seg["carrier"])
        seg_w.string(6, seg["flight"])
        s.message(4, seg_w)
    # Slice origins (13) and destinations (14), one repeated entry per
    # airport. Field 1 is the endpoint kind: 1 an airport (Google's UI
    # writes 3 for a city, with a Knowledge Graph id in place of the code).
    for field, key in ((13, "origin"), (14, "destination")):
        for code in _endpoint_codes(sl[key]):
            end_w = _PbWriter()
            end_w.varint(1, 1)
            end_w.string(2, code)
            s.message(field, end_w)
    for field, key in ((17, "layover_min"), (18, "layover_max")):
        if (minutes := sl.get(key)) is not None:
            s.varint(field, minutes)
    return s


def _encode_gflight_pinned_tfs(
    *,
    slices: list[dict[str, Any]],
    cabin: int,
    adults: int,
    children: int,
    infants_in_seat: int,
    infants_on_lap: int,
    pin_max_u64: bool = True,
    max_price: int | None = None,
    bags: tuple[int, int] | None = None,
) -> bytes:
    """Encode the tfs= protobuf for a Google Flights URL.

    One writer serves both flavors. A pinned booking link keeps `pin_max_u64`
    (field 16), which Google's own pinned URLs carry and the byte-exact fixture
    asserts; the search page doesn't need it and `build_search_tfs` omits it.

    `max_price` is top-level field 12, whole units of the page's `curr=`.
    `bags` is (checked, carry-on), top-level field 13 as `{2: carry-on,
    3: checked}`; a zero count is left out, the form Google honored live.

    `slices`: list of dicts shaped:
        {
            "date": "YYYY-MM-DD",
            "origin": "HNL",         # or a sequence of airports, one entry each
            "destination": "MIA",    # likewise
            "max_stops": 0,          # optional, zero-based ceiling; see below
            # Optional search filters; each absent key writes nothing.
            "carriers": ["AA", "ONEWORLD"],  # 3.6: IATA codes and alliance names
            "hours": (6, 11, 0, 23),  # 3.8-3.11: dep from/to, arr from/to
            "max_duration": 380,     # 3.12, minutes
            "layover_min": 120,      # 3.17, minutes
            "layover_max": 240,      # 3.18, minutes
            "segments": [
                {"origin": "HNL", "date": "2026-10-14",
                 "destination": "LAX", "carrier": "AA", "flight": "162"},
                ...
            ],
        }
    cabin: 1=ECONOMY, 2=PREMIUM_ECONOMY, 3=BUSINESS, 4=FIRST (TFS values).
    pax counts: adults+children+inf_seat+inf_lap broken out per Google's wire
    layout (field 8 is repeated varint, one per occupant).
    """
    w = _PbWriter()
    # Mode markers — observed to be 28, 2 on every pinned URL we captured.
    w.varint(1, 28)
    w.varint(2, 2)

    for sl in slices:
        w.message(3, _tfs_slice_message(sl))

    # Field 8 carries each occupant's TYPE: a bare `1` for everyone would price
    # a child or an infant as an adult.
    for kind in _gf_pax_kinds(
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
    ):
        w.varint(8, kind)

    w.varint(9, cabin)
    if max_price is not None:
        w.varint(12, max_price)
    if bags is not None and any(bags):
        checked, carry_on = bags
        bag_w = _PbWriter()
        if carry_on:
            bag_w.varint(2, carry_on)
        if checked:
            bag_w.varint(3, checked)
        w.message(13, bag_w)
    w.varint(14, 1)

    if pin_max_u64:
        # Field 16: marker sub-message {1: 0xFFFFFFFFFFFFFFFF}
        marker = _PbWriter()
        marker.varint(1, (1 << 64) - 1)
        w.message(16, marker)

    # Field 19: trip type. TFS enum: 1 = round-trip, 2 = one-way, 3 = multi-city.
    # `>= 2` meant round-trip, so a three-leg itinerary was labelled a round
    # trip; Google then read only the first two slices and the third leg was
    # silently dropped from a link we still described as "pinned".
    if len(slices) == 1:
        trip_type = _GF_TRIP_ONE_WAY
    elif len(slices) == _ROUND_TRIP_LEGS:
        trip_type = _GF_TRIP_ROUND_TRIP
    else:
        trip_type = _GF_TRIP_MULTI_CITY
    w.varint(19, trip_type)

    return bytes(w.buf)


# ───────────── Google Flights search-page tfs= (transport) ─────────────────
#
# The gflight backend addresses its search through this parameter rather than
# the `GetShoppingResults` RPC's f.req JSON; `_gflight_ids` fetches the page and
# reads `ds:1`, and its module docstring holds the why.
#
# The encoder below is deliberately an ALLOWLIST, and enforced as one: every
# field on fli's model must be named in exactly one of the three sets below, and
# `build_search_tfs` raises on anything left over. A deny-list would be quietly
# wrong the day `flights>=0.9` (an open floor) adds a filter — the new field
# would encode as if unset, dropping a constraint the user asked for. Honoring
# some of a user's constraints while dropping the rest is a silent wrong answer.
# The backend picker (`_gf_postfilter.search_page_reasons`) keeps those queries
# on Matrix; anything that reaches here anyway raises.

# Fields this encoder reads and writes into the tfs= payload. `airlines` carries
# alliance names as well as carrier codes: the bridge writes an alliance there,
# and 3.6 takes both in one list. `price_limit` is read for its amount only: the
# page prices in its `curr=`, so fli's currency on the cap is never written.
_TFS_ENCODED_FIELDS = frozenset(
    {
        "trip_type",
        "passenger_info",
        "flight_segments",
        "stops",
        "seat_type",
        "airlines",
        "max_duration",
        "layover_restrictions",
        "price_limit",
        "bags",
    }
)

# Filters this encoder refuses, checked against fli's own model default rather
# than truthiness: fli populates sort_by, emissions, exclude_basic_economy and
# show_all_results on EVERY filter, so `if filters.sort_by` would refuse every
# search. Each entry is (field name, how to describe it to a user).
# The exclude lists do have a field (3.7), but Google ignored it on JFK-LHR.
_TFS_REFUSED_FIELDS: tuple[tuple[str, str], ...] = (
    ("airlines_exclude", "a carrier exclude list"),
    ("alliances", "an alliance filter"),
    ("alliances_exclude", "an alliance exclude filter"),
    ("emissions", "an emissions filter"),
    ("exclude_basic_economy", "a basic-economy exclusion"),
    ("sort_by", "a server-side sort order"),
)
# Read and deliberately not acted on. `show_all_results` defaults to True and there is
# no tfs= field for it: the page URL asks for the full board on every search
# through its own `tfu=` parameter instead (`google_flights_search_page_url`).
_TFS_IGNORED_FIELDS = frozenset({"show_all_results"})

# Passenger kinds field 8 carries for a search. Google prices both infant
# kinds, but has answered a route with flights (JFK-LAX) with an empty board
# for any infant, so the search hands such an empty board to Matrix
# (`cli._run_gflight_path`).
_TFS_ENCODED_PAX = frozenset({"adults", "children", "infants_in_seat", "infants_on_lap"})

_TFS_MULTI_CITY = 3  # fli TripType.MULTI_CITY — the page inlines no rows for it

# `{2: {1: 0, 2: 1}, 4: {}}`: field 2.2 is the page's "show all flights" bit.
_GF_SHOW_ALL_TFU = "EgQIABABIgA"


def _tfs_iata(value: Any) -> str:
    """Bare IATA code for an fli `Airport`/`Airline` enum (or a plain string).

    fli maps codes to display NAMES (`Airline._0B.value == "Blue Air"`) and
    underscore-prefixes the digit-leading ones, so the enum *name* minus that
    prefix is the code — the same rule fli's own request serializer uses."""
    return str(getattr(value, "name", value)).removeprefix("_")


def _tfs_field_is_default(filters: Any, field: str) -> bool:
    """True when `field` still holds the value pydantic seeded it with.

    Read off the class, not the instance: pydantic v2 deprecates
    `instance.model_fields`, and `filterwarnings = ["error"]` turns that into a
    test failure."""
    spec = cast("dict[str, Any]", type(filters).model_fields).get(field)
    if spec is None:
        return True  # fli dropped the field; there is nothing to refuse
    return bool(getattr(filters, field, None) == spec.default)


# The hours 3.8-3.11 take when a window leaves its side open. A "latest" hour
# includes its every minute, so the day ends at 23.
_TFS_FIRST_HOUR = 0
_TFS_LAST_HOUR = 23


def _tfs_hours(window: Any) -> tuple[int, int, int, int] | None:
    """3.8-3.11 for an fli `TimeRestrictions`: all four once any is set."""
    if window is None:
        return None
    return (
        _TFS_FIRST_HOUR if window.earliest_departure is None else window.earliest_departure,
        _TFS_LAST_HOUR if window.latest_departure is None else window.latest_departure,
        _TFS_FIRST_HOUR if window.earliest_arrival is None else window.earliest_arrival,
        _TFS_LAST_HOUR if window.latest_arrival is None else window.latest_arrival,
    )


def _tfs_slice(
    segment: Any, *, max_stops: int | None, trip_filters: dict[str, Any]
) -> dict[str, Any]:
    """One tfs= slice from an fli FlightSegment. `trip_filters` are the
    search-wide filters, which the page takes on every slice."""
    selected = segment.selected_flight
    return {
        **trip_filters,
        "date": segment.travel_date,
        "origin": [_tfs_iata(entry[0]) for entry in segment.departure_airport],
        "destination": [_tfs_iata(entry[0]) for entry in segment.arrival_airport],
        "max_stops": max_stops,
        "hours": _tfs_hours(segment.time_restrictions),
        # A pinned leg (round-trip expansion sets `selected_flight` on the
        # outbound and re-fetches) becomes repeated field 3.4, which is how the
        # page is asked for returns against a chosen outbound.
        "segments": [
            {
                "origin": _tfs_iata(leg.departure_airport),
                "date": leg.departure_datetime.strftime("%Y-%m-%d"),
                "destination": _tfs_iata(leg.arrival_airport),
                "carrier": _tfs_iata(leg.airline),
                "flight": str(leg.flight_number),
            }
            for leg in selected.legs
        ]
        if selected is not None
        else [],
    }


# The widest cap every protobuf integer type reads back unchanged; how the page
# reads a wider one was never measured. No USD fare comes near it, so a wider
# cap is left to the row check.
_TFS_MAX_PRICE = 2**31 - 1


def search_page_cap(max_price: int | None, currency: str) -> int | None:
    """The price cap a search page priced in `currency` is asked for (field 12),
    or None where the row check alone applies it.

    Only a USD page is asked. A EUR page asked for a cap served fewer of the
    fares under it than its uncapped board (JFK-LAX at EUR 240: 34 of 45)."""
    if currency != "USD" or max_price is None or max_price > _TFS_MAX_PRICE:
        return None
    return max_price


def build_search_tfs(filters: Any, *, currency: str = "USD") -> bytes:
    """Encode an fli `FlightSearchFilters` as the search page's tfs= protobuf,
    for a page priced in `currency`.

    Raises `GfTfsUnsupportedError` for any filter this transport has no field
    for — see `_TFS_REFUSED_FIELDS` for why that's an allowlist and not a
    truthiness scan."""
    unclaimed = (
        set(cast("dict[str, Any]", type(filters).model_fields))
        - _TFS_ENCODED_FIELDS
        - {field for field, _ in _TFS_REFUSED_FIELDS}
        - _TFS_IGNORED_FIELDS
    )
    if unclaimed:
        # fli grew a filter since this encoder was written. Refusing is the only
        # safe default: we cannot know whether it is set, let alone encode it.
        raise GfTfsUnsupportedError(
            ", ".join(sorted(unclaimed)),
            "a filter this encoder has never seen (fli's model gained a field)",
        )
    if filters.trip_type.value == _TFS_MULTI_CITY:
        raise GfTfsUnsupportedError(
            "trip_type",
            "a multi-city trip (Google loads those rows through the gated RPC)",
        )
    for field, description in _TFS_REFUSED_FIELDS:
        if not _tfs_field_is_default(filters, field):
            raise GfTfsUnsupportedError(field, description)
    layover = filters.layover_restrictions
    if layover is not None and layover.airports:
        raise GfTfsUnsupportedError("layover_restrictions", "a connecting-airport restriction")

    # fli's MaxStops is one-based (ANY=0, NON_STOP=1, …); tfs field 3.5 is
    # zero-based and omitted for "any".
    stops = filters.stops.value
    max_stops = stops - 1 if stops else None
    trip_filters: dict[str, Any] = {
        "carriers": [_tfs_iata(a) for a in filters.airlines or ()],
        "max_duration": filters.max_duration,
        "layover_min": layover.min_duration if layover else None,
        "layover_max": layover.max_duration if layover else None,
    }
    price_limit = filters.price_limit
    max_price = search_page_cap(
        price_limit.max_price if price_limit is not None else None, currency
    )
    bags = filters.bags
    return _encode_gflight_pinned_tfs(
        slices=[
            _tfs_slice(seg, max_stops=max_stops, trip_filters=trip_filters)
            for seg in filters.flight_segments
        ],
        cabin=filters.seat_type.value,
        adults=filters.passenger_info.adults,
        children=filters.passenger_info.children,
        infants_in_seat=filters.passenger_info.infants_in_seat,
        infants_on_lap=filters.passenger_info.infants_on_lap,
        pin_max_u64=False,
        max_price=max_price,
        bags=(bags.checked_bags, int(bags.carry_on)) if bags is not None else None,
    )


def google_flights_search_page_url(
    tfs: bytes,
    *,
    currency: str = "USD",
    language: str = "en",
    country: str = "US",
) -> str:
    """The public search-page URL `_gflight_ids` GETs for a tfs= payload.

    `gl=` is explicit because the page's row set and its consent behaviour both
    key off the resolved country, and IP geolocation is not stable enough to
    leave it implicit.

    `tfu=` sets the "show all" bit. Without it the page inlines only Google's
    top ~30 rows (JFK-LAX 30 of 95, JFK-LHR 22 of 101), so a larger `-n` comes
    back short and a post-filter answers from a partial board. The cost: the
    page roughly doubles (JFK-LAX 3.6 MB to 7.5 MB, about 0.7 s more)."""
    b64 = base64.urlsafe_b64encode(tfs).rstrip(b"=").decode()
    return (
        f"https://www.google.com/travel/flights?tfs={urllib.parse.quote(b64)}"
        f"&hl={language}&gl={country}&curr={currency}&tfu={_GF_SHOW_ALL_TFU}"
    )


def google_flights_pinned_url(
    s: Search,
    *,
    outbound_segments: list[dict[str, str]],
    return_segments: list[dict[str, str]] | None = None,
    currency: str | None = None,
    language: str = "en",
) -> str:
    """Build a Google Flights URL that pre-selects a specific itinerary
    (not just pre-filled search criteria). `currency` defaults to the
    search's own, then USD.

    `outbound_segments` / `return_segments` shape per segment:
        {"origin": "HNL", "date": "2026-10-14",
         "destination": "LAX", "carrier": "AA", "flight": "162"}

    Verified against captured headed-browser navigation in
    research/capture/manual-1779193717/. See `_encode_gflight_pinned_tfs`
    docstring for the protobuf schema notes."""
    b64 = _pinned_tfs_b64(s, outbound_segments, return_segments)
    curr = currency or s.options.currency or "USD"
    return (
        f"https://www.google.com/travel/flights/search?"
        f"tfs={urllib.parse.quote(b64)}&hl={language}&curr={curr}"
    )


def google_flights_booking_url(
    s: Search,
    *,
    outbound_segments: list[dict[str, str]],
    return_segments: list[dict[str, str]] | None = None,
    currency: str | None = None,
    language: str = "en",
    country: str = "US",
) -> str:
    """The booking page for one itinerary: the pinned link's `tfs=` on
    `/travel/flights/booking`, which lists every seller of that itinerary.

    The page needs no booking token; the legs in `tfs=` are enough. `gl=` is
    explicit for the reason the search page's is: sellers and their prices key
    off the resolved country, and they are compared against a table priced
    under `gl=US`. `currency` is the currency of the picked row's Google price:
    Google can price a row in another currency than the search asked for, and
    the sellers are compared with that row. A row with no Google price is
    asked in the search's currency, then USD."""
    b64 = _pinned_tfs_b64(s, outbound_segments, return_segments)
    curr = currency or s.options.currency or "USD"
    return (
        f"https://www.google.com/travel/flights/booking?"
        f"tfs={urllib.parse.quote(b64)}&hl={language}&gl={country}&curr={curr}"
    )


def _pinned_tfs_b64(
    s: Search,
    outbound_segments: list[dict[str, str]],
    return_segments: list[dict[str, str]] | None,
) -> str:
    """The pinned-itinerary `tfs=` value, shared by the search and booking pages."""
    if not isinstance(s, SpecificDateSearch | CalendarFollowup):
        raise TypeError(
            "google_flights_pinned_url only meaningful for specific-date / "
            "calendar-followup searches (need per-leg dates).",
        )
    out = s.legs[0]
    if out.date is None:
        raise AssertionError("outbound leg.date must be set after validation")
    out_origins, out_dests = _pinned_slice_airports(out, outbound_segments)
    slices: list[dict[str, Any]] = [
        {
            "date": out.date.isoformat(),
            "origin": out_origins,
            "destination": out_dests,
            "segments": outbound_segments,
        }
    ]
    if return_segments is not None:
        if len(s.legs) < _ROUND_TRIP_LEGS:
            raise AssertionError(
                "return_segments given but search has no return leg",
            )
        ret = s.legs[1]
        if ret.date is None:
            raise AssertionError(
                "return_segments given but return leg has no date set",
            )
        ret_origins, ret_dests = _pinned_slice_airports(ret, return_segments)
        slices.append(
            {
                "date": ret.date.isoformat(),
                "origin": ret_origins,
                "destination": ret_dests,
                "segments": return_segments,
            }
        )

    p = s.options.pax
    raw = _encode_gflight_pinned_tfs(
        slices=slices,
        cabin=_CABIN_TFS_INT[s.options.cabin],
        adults=p.adults + p.seniors + p.youth,
        children=p.children,
        infants_in_seat=p.infants_in_seat,
        infants_on_lap=p.infants_in_lap,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# The explore page's `tfs=`, decoded from the URLs its own controls write. Field
# 16 holds the month and the trip length: 16.1 is the month minus one with NO
# year (Google picks the next such month; the value 0 answered the next January),
# or the all-ones sentinel for "the next six months"; 16.2 is 1 for a weekend,
# 3 for two weeks, and absent (or 2) for one week.
_EXPLORE_ANY_MONTH = (1 << 64) - 1
_EXPLORE_MODE = 3
_EXPLORE_TRIP_ROUND_TRIP = 1


def google_flights_explore_url(
    origin: str,
    *,
    month: int | None,
    trip_length: int | None,
    max_price: int | None,
    currency: str = "USD",
    language: str = "en",
    country: str = "US",
) -> str:
    """The explore page from `origin`: round trips to everywhere Google prices.

    `month` is 1-12, None for the next six months; `trip_length` is 16.2's code,
    None for one week. The origin is always written: without one the page
    geolocates and answers for wherever it thinks the user is.

    `tfu=GgA` is what the page's own URLs carry, an empty message."""
    w = _PbWriter()
    w.varint(1, 28)
    w.varint(2, _EXPLORE_MODE)
    for side in (13, 14):
        airport = _PbWriter()
        airport.varint(1, 1)
        airport.string(2, origin)
        slice_w = _PbWriter()
        slice_w.message(side, airport)
        w.message(3, slice_w)
    w.varint(8, 1)
    w.varint(9, 1)
    if max_price is not None:
        w.varint(12, max_price)
    w.varint(14, 2)
    when = _PbWriter()
    when.varint(1, _EXPLORE_ANY_MONTH if month is None else month - 1)
    if trip_length is not None:
        when.varint(2, trip_length)
    w.message(16, when)
    w.varint(19, _EXPLORE_TRIP_ROUND_TRIP)
    b64 = base64.urlsafe_b64encode(bytes(w.buf)).rstrip(b"=").decode()
    return (
        f"https://www.google.com/travel/explore?tfs={urllib.parse.quote(b64)}"
        f"&tfu=GgA&hl={language}&gl={country}&curr={currency}"
    )


def _pinned_slice_airports(
    leg: Leg, segments: list[dict[str, str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A pinned slice's origin and destination airports.

    The leg's own sets, metro codes expanded, while Google's page can take them
    (`gf_leg_refusal`). Past that, the pinned itinerary's first departure and last
    arrival: one airport per end is a shape the page serves, and the caller's
    caveat says the link shows the itinerary's airports rather than the set."""
    if gf_leg_refusal(leg.origins, leg.destinations) is None:
        return expand_airports(leg.origins), expand_airports(leg.destinations)
    if not segments:
        raise AssertionError("a pinned slice past the page's airport limit needs its segments")
    return (segments[0]["origin"],), (segments[-1]["destination"],)


_FLIGHT_NUMBER_RE = re.compile(r"^([A-Z][A-Z0-9])([0-9]+)$")


def extract_pin_segments_from_slice(s: Slice) -> list[dict[str, str]] | None:
    """Turn a parsed Matrix/gflight `Slice` into the segment-list shape
    that `google_flights_pinned_url` wants.

    Returns None if the slice doesn't carry enough data to deep-link
    (no origin/destination, missing stops for a multi-leg slice, a
    flight identifier that doesn't parse as `<CARRIER><DIGITS>`, or a
    flight whose date its source does not state — `pin_dates_are_stated`).

    Each segment takes its date from `s.segment_dates`, or, for a single
    flight with none, from the slice's departure day.
    """
    # Combined invariant check up front: every flight dated, required
    # fields present, stops/flights topology valid, segment_dates either
    # absent or matching length. Single bail-out → easier to reason about and
    # keeps the per-segment loop focused on flight-number parsing.
    n = len(s.flights)
    origin_code = s.origin.code if s.origin else None
    dest_code = s.destination.code if s.destination else None
    if (
        not pin_dates_are_stated(s)
        or not s.departure
        or not origin_code
        or not dest_code
        or n - 1 != len(s.stops)
        or (s.segment_dates and len(s.segment_dates) != n)
    ):
        return None
    dates = s.segment_dates or [s.departure[:10]]
    out: list[dict[str, str]] = []
    for i, fl in enumerate(s.flights):
        m = _FLIGHT_NUMBER_RE.match(fl)
        seg_origin = origin_code if i == 0 else s.stops[i - 1].code
        seg_dest = dest_code if i == n - 1 else s.stops[i].code
        if not m or not seg_origin or not seg_dest:
            return None
        carrier, flight_no = m.group(1), m.group(2)
        out.append(
            {
                "origin": seg_origin,
                "date": dates[i],
                "destination": seg_dest,
                "carrier": carrier,
                "flight": flight_no,
            }
        )
    return out


def pin_dates_are_stated(s: Slice) -> bool:
    """Whether the slice's source dates every one of its flights: one date per
    flight in `segment_dates`, or a single flight, which leaves on the slice's
    departure day. A connection's two end dates do not date the flights
    between them: one that crosses the date line lands on the day it left
    while its second flight leaves the next day."""
    n = len(s.flights)
    return n == 1 or (n > 0 and len(s.segment_dates) == n)


def google_flights_url(s: Search, *, currency: str | None = None, language: str = "en") -> str:
    """Build a Google Flights `tfs=` URL that opens directly into a populated
    search result. Multi-airport is flattened to first IATA per leg: fast_flights'
    proto takes one airport per slice end, where Google's own tfs= repeats them.
    `currency` defaults to the search's own, then USD.

    For CalendarSearch (no per-leg dates), uses window start as departure
    and start + mean(duration) as return — gives the user a representative
    URL to land on Google Flights with, even though Google doesn't have a
    calendar-grid concept."""

    # we only need on this code path.
    from fast_flights import FlightData, Passengers, TFSData  # noqa: PLC0415

    match s:
        case SpecificDateSearch() | CalendarFollowup():
            flight_data: list[Any] = []
            for leg in s.legs:
                # SpecificDate/Followup validators guarantee leg.date is set;
                # surface a clear error if invariants were bypassed.
                if leg.date is None:
                    raise AssertionError(
                        f"{type(s).__name__}.leg.date should be set after validation",
                    )
                flight_data.append(
                    FlightData(
                        date=leg.date.isoformat(),
                        from_airport=leg.origins[0],
                        to_airport=leg.destinations[0],
                    )
                )
        case CalendarSearch():
            mean_dur = (s.window.duration_min + s.window.duration_max) // 2
            ret_date = s.window.start + timedelta(days=mean_dur)
            out = s.legs[0]
            ret = s.legs[1] if len(s.legs) == _ROUND_TRIP_LEGS else None
            flight_data = [
                FlightData(
                    date=s.window.start.isoformat(),
                    from_airport=out.origins[0],
                    to_airport=out.destinations[0],
                )
            ]
            if ret:
                flight_data.append(
                    FlightData(
                        date=ret_date.isoformat(),
                        from_airport=ret.origins[0],
                        to_airport=ret.destinations[0],
                    )
                )
        case _:
            assert_never(s)

    if len(flight_data) == 1:
        trip = "one-way"
    elif len(flight_data) == _ROUND_TRIP_LEGS:
        trip = "round-trip"
    else:
        trip = "multi-city"

    p = s.options.pax
    adults = (p.adults + p.seniors + p.youth) or 1
    passengers = Passengers(
        adults=adults,
        children=p.children,
        infants_in_seat=p.infants_in_seat,
        infants_on_lap=p.infants_in_lap,
    )
    # Built for its checks (at most nine, a lap per infant), then given Google's
    # codes: fast_flights writes an infant in a seat as a lap and the reverse.
    passengers.pb = _gf_pax_kinds(
        adults=adults,
        children=p.children,
        infants_in_seat=p.infants_in_seat,
        infants_on_lap=p.infants_in_lap,
    )
    td = TFSData.from_interface(
        flight_data=flight_data,
        seat=_CABIN_TFS[s.options.cabin],
        trip=trip,
        passengers=passengers,
        # The stop limit is a TFSData-level field, not per-FlightData. Omitting
        # it made a `--stops 0` link byte-identical to an unconstrained one, so
        # a nonstop-only result table handed the user a page that also offered
        # connections.
        max_stops=s.options.max_extra_stops,
    )
    b64 = td.as_b64().decode()
    curr = currency or s.options.currency or "USD"
    return (
        f"https://www.google.com/travel/flights/search?"
        f"tfs={urllib.parse.quote(b64)}&hl={language}&curr={curr}"
    )
