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
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
)

if TYPE_CHECKING:
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


def _spa_specific_leg(leg: Leg, *, return_leg: Leg | None = None) -> dict[str, Any]:
    """SPA URL-state slice for a specific-date search.

    For round-trip, pass the inbound leg as `return_leg` so the slice carries
    both directions in a single record (the SPA's current schema as of 2026-05).
    For one-way / multi-city, omit `return_leg`.
    """
    return_date = return_leg.date.isoformat() if return_leg and return_leg.date else ""
    return_modifier = str(return_leg.date_minus if return_leg else leg.date_plus)
    return_times = [t.value for t in return_leg.time_ranges] if return_leg else []
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
            "departureDatePreferredTimes": [t.value for t in leg.time_ranges],
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
        "departureDatePreferredTimes": [t.value for t in out.time_ranges],
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
    dates["returnDatePreferredTimes"] = [t.value for t in ret.time_ranges] if ret else []
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
# passenger TYPE. Values are Google's own `Passenger` enum, read out of
# fast_flights' generated protobuf (flights_pb2.Passenger) rather than guessed.
_GF_PAX_ADULT = 1
_GF_PAX_CHILD = 2
_GF_PAX_INFANT_IN_SEAT = 3
_GF_PAX_INFANT_ON_LAP = 4


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


def _encode_gflight_pinned_tfs(
    *,
    slices: list[dict[str, Any]],
    cabin: int,
    adults: int,
    children: int,
    infants_in_seat: int,
    infants_on_lap: int,
    pin_max_u64: bool = True,
) -> bytes:
    """Encode the tfs= protobuf for a Google Flights URL.

    One writer serves both flavors. A pinned booking link keeps `pin_max_u64`
    (field 16), which Google's own pinned URLs carry and the byte-exact fixture
    asserts; the search page doesn't need it and `build_search_tfs` omits it.

    `slices`: list of dicts shaped:
        {
            "date": "YYYY-MM-DD",
            "origin": "HNL",
            "destination": "MIA",
            "max_stops": 0,          # optional, zero-based ceiling; see below
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
        s = _PbWriter()
        s.string(2, sl["date"])
        # Stop ceiling is ZERO-based here (0 = nonstop) and absent means "any";
        # writing 0 for "any" would pin every search to nonstop. Emitted before
        # the selected legs to match the field order Google's own URLs carry.
        max_stops = sl.get("max_stops")
        if max_stops is not None:
            s.varint(5, max_stops)
        for seg in sl["segments"]:
            seg_w = _PbWriter()
            seg_w.string(1, seg["origin"])
            seg_w.string(2, seg["date"])
            seg_w.string(3, seg["destination"])
            seg_w.string(5, seg["carrier"])
            seg_w.string(6, seg["flight"])
            s.message(4, seg_w)
        # Pax info — slice origin/destination. Field 1 is observed to be
        # the constant `1` regardless of pax count (verified at adults=3:
        # encoded as `1` not `3`). The repeated field 8 at top level
        # encodes the per-pax-type count, not this varint.
        origin_w = _PbWriter()
        origin_w.varint(1, 1)
        origin_w.string(2, sl["origin"])
        s.message(13, origin_w)
        dest_w = _PbWriter()
        dest_w.varint(1, 1)
        dest_w.string(2, sl["destination"])
        s.message(14, dest_w)
        w.message(3, s)

    # Field 8: one repeated varint per occupant, carrying that occupant's TYPE.
    # Emitting a bare `1` for everyone encoded children and infants as ADULTS,
    # so a pinned link for 1 adult + 1 child priced and searched as 2 adults —
    # a different, more expensive itinerary than the row the user picked.
    for _type, _count in (
        (_GF_PAX_ADULT, adults),
        (_GF_PAX_CHILD, children),
        (_GF_PAX_INFANT_IN_SEAT, infants_in_seat),
        (_GF_PAX_INFANT_ON_LAP, infants_on_lap),
    ):
        for _ in range(_count):
            w.varint(8, _type)

    w.varint(9, cabin)
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
# would encode as if unset, dropping a constraint the user asked for. Honouring
# some of a user's constraints while dropping the rest is a silent wrong answer.
# `routing_predicates.page_can_encode` keeps those queries on Matrix; anything
# that reaches here anyway raises.

# Fields this encoder reads and writes into the tfs= payload.
_TFS_ENCODED_FIELDS = frozenset(
    {"trip_type", "passenger_info", "flight_segments", "stops", "seat_type"}
)

# Filters with no tfs= field, checked against fli's own model default rather
# than truthiness: fli populates sort_by, emissions, exclude_basic_economy and
# show_all_results on EVERY filter, so `if filters.sort_by` would refuse every
# search. Each entry is (field name, how to describe it to a user).
_TFS_REFUSED_FIELDS: tuple[tuple[str, str], ...] = (
    ("airlines", "a carrier include list"),
    ("airlines_exclude", "a carrier exclude list"),
    ("alliances", "an alliance filter"),
    ("alliances_exclude", "an alliance exclude filter"),
    ("layover_restrictions", "a layover airport or duration restriction"),
    ("max_duration", "a maximum itinerary duration"),
    ("price_limit", "a price cap"),
    ("bags", "a bag-count fare adjustment"),
    ("emissions", "an emissions filter"),
    ("exclude_basic_economy", "a basic-economy exclusion"),
    ("sort_by", "a server-side sort order"),
)
# Read and deliberately not acted on. `show_all_results` defaults to True and there is
# no tfs= field for it. The page serves Google's default board (~30 rows per
# leg, measured 2026-09-02) with no back-fill, so a top-N above that returns
# fewer rows than asked for. That's a board ceiling, not a dropped constraint —
# nothing the user asked for goes unhonoured.
_TFS_IGNORED_FIELDS = frozenset({"show_all_results"})

# Non-adult passengers ride tfs field 8 under distinct kind codes (2 child,
# 3 infant-in-seat, 4 infant-on-lap) that we have never verified against a
# live priced search. Emitting `1` for them — what the pinned writer does,
# where every occupant is already a chosen traveller — would price a child as
# an adult, so search refuses them and the backend picker sends them to Matrix.
_TFS_REFUSED_PAX: tuple[tuple[str, str], ...] = (
    ("children", "a child passenger"),
    ("infants_in_seat", "an infant-in-seat passenger"),
    ("infants_on_lap", "an infant-on-lap passenger"),
)

_TFS_MULTI_CITY = 3  # fli TripType.MULTI_CITY — the page inlines no rows for it


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


def _tfs_slice(segment: Any, *, max_stops: int | None) -> dict[str, Any]:
    """One tfs= slice from an fli FlightSegment, refusing what it can't carry."""
    if segment.time_restrictions is not None:
        raise GfTfsUnsupportedError("time_restrictions", "a departure/arrival time window")
    origins = [_tfs_iata(entry[0]) for entry in segment.departure_airport]
    destinations = [_tfs_iata(entry[0]) for entry in segment.arrival_airport]
    if len(origins) != 1 or len(destinations) != 1:
        raise GfTfsUnsupportedError("flight_segments", "more than one airport per leg")
    selected = segment.selected_flight
    return {
        "date": segment.travel_date,
        "origin": origins[0],
        "destination": destinations[0],
        "max_stops": max_stops,
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


def build_search_tfs(filters: Any) -> bytes:
    """Encode an fli `FlightSearchFilters` as the search page's tfs= protobuf.

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
    for field, description in _TFS_REFUSED_PAX:
        if getattr(filters.passenger_info, field, 0):
            raise GfTfsUnsupportedError(field, description)

    # fli's MaxStops is one-based (ANY=0, NON_STOP=1, …); tfs field 3.5 is
    # zero-based and omitted for "any".
    stops = filters.stops.value
    max_stops = stops - 1 if stops else None
    return _encode_gflight_pinned_tfs(
        slices=[_tfs_slice(seg, max_stops=max_stops) for seg in filters.flight_segments],
        cabin=filters.seat_type.value,
        adults=filters.passenger_info.adults,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
        pin_max_u64=False,
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
    leave it implicit."""
    b64 = base64.urlsafe_b64encode(tfs).rstrip(b"=").decode()
    return (
        f"https://www.google.com/travel/flights?tfs={urllib.parse.quote(b64)}"
        f"&hl={language}&gl={country}&curr={currency}"
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
    if not isinstance(s, SpecificDateSearch | CalendarFollowup):
        raise TypeError(
            "google_flights_pinned_url only meaningful for specific-date / "
            "calendar-followup searches (need per-leg dates).",
        )
    out = s.legs[0]
    if out.date is None:
        raise AssertionError("outbound leg.date must be set after validation")
    slices = [
        {
            "date": out.date.isoformat(),
            "origin": out.origins[0],
            "destination": out.destinations[0],
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
        slices.append(
            {
                "date": ret.date.isoformat(),
                "origin": ret.origins[0],
                "destination": ret.destinations[0],
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
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    curr = currency or s.options.currency or "USD"
    return (
        f"https://www.google.com/travel/flights/search?"
        f"tfs={urllib.parse.quote(b64)}&hl={language}&curr={curr}"
    )


_FLIGHT_NUMBER_RE = re.compile(r"^([A-Z][A-Z0-9])([0-9]+)$")


def extract_pin_segments_from_slice(s: Slice) -> list[dict[str, str]] | None:
    """Turn a parsed Matrix/gflight `Slice` into the segment-list shape
    that `google_flights_pinned_url` wants.

    Returns None if the slice doesn't carry enough data to deep-link
    (no origin/destination, missing stops for a multi-leg slice, or a
    flight identifier that doesn't parse as `<CARRIER><DIGITS>`).

    Per-segment dates:
    - If `s.segment_dates` is populated (gflight backend), use those —
      exact per-leg dates from fli's `departure_datetime`.
    - Otherwise (Matrix backend), fall back to a heuristic: all
      segments take the slice departure date, except the last segment
      of a slice whose arrival falls on a later calendar day — that
      one takes the arrival date. Exact for non-overnight 1-stop
      routings and 1-stop routings with a single overnight layover;
      3+ segment slices with multiple midnight crossings may be off
      by a day.
    """
    # Combined invariant check up front: required fields present,
    # stops/flights topology valid, segment_dates either absent or
    # matching length. Single bail-out → easier to reason about and
    # keeps the per-segment loop focused on flight-number parsing.
    n = len(s.flights)
    origin_code = s.origin.code if s.origin else None
    dest_code = s.destination.code if s.destination else None
    has_exact_dates = bool(s.segment_dates)
    if (
        n == 0
        or not s.departure
        or not origin_code
        or not dest_code
        or n - 1 != len(s.stops)
        or (has_exact_dates and len(s.segment_dates) != n)
    ):
        return None
    dep_date = s.departure[:10]
    arrival = s.arrival or s.departure
    arr_date = arrival[:10]
    out: list[dict[str, str]] = []
    for i, fl in enumerate(s.flights):
        m = _FLIGHT_NUMBER_RE.match(fl)
        seg_origin = origin_code if i == 0 else s.stops[i - 1].code
        seg_dest = dest_code if i == n - 1 else s.stops[i].code
        if not m or not seg_origin or not seg_dest:
            return None
        carrier, flight_no = m.group(1), m.group(2)
        if has_exact_dates:
            seg_date = s.segment_dates[i]
        else:
            # A segment is dated by when it DEPARTS. The last segment of a
            # multi-segment slice departs on the arrival date only when the
            # slice spans midnight — and a NONSTOP is never that case, even
            # though it satisfies `i == n - 1`: it departs on the departure
            # date by definition. Treating an overnight nonstop as arrival-
            # dated pinned BA178 JFK->LHR (dep 2026-12-31, arr 2027-01-01) to
            # 2027-01-01, sending the user to a search for the wrong day.
            spans_midnight = arr_date != dep_date
            is_last_of_many = n > 1 and i == n - 1
            seg_date = arr_date if (is_last_of_many and spans_midnight) else dep_date
        out.append(
            {
                "origin": seg_origin,
                "date": seg_date,
                "destination": seg_dest,
                "carrier": carrier,
                "flight": flight_no,
            }
        )
    return out


def google_flights_url(s: Search, *, currency: str | None = None, language: str = "en") -> str:
    """Build a Google Flights `tfs=` URL that opens directly into a populated
    search result. Multi-airport is flattened to first IATA per leg (Google
    Flights URL grammar doesn't support airport sets per slice). `currency`
    defaults to the search's own, then USD.

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
