# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The search-page `tfs=` encoder: one writer, an allowlist, no silent drops.

`build_search_tfs` shares `_encode_gflight_pinned_tfs` with the pinned booking
link, so the byte-exact pin fixture (test_links_gflight_pin.py) is the other
half of this file's coverage. What's asserted here is the search flavor: the
field-16 pin omitted, the zero-based stop ceiling, carrier codes taken from
fli's enum NAME, and a refusal for every filter the page has no field for.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fli.models import FlightLeg, FlightResult
from fli.models.airline import Airline
from fli.models.airport import Airport
from fli.models.google_flights.base import (
    Alliance,
    BagsFilter,
    EmissionsFilter,
    FlightSegment,
    LayoverRestrictions,
    MaxStops,
    PassengerInfo,
    PriceLimit,
    SeatType,
    SortBy,
    TimeRestrictions,
    TripType,
)
from fli.models.google_flights.flights import FlightSearchFilters

from flight_cli._gf_errors import GfTfsUnsupportedError
from flight_cli.links import build_search_tfs, google_flights_search_page_url

# fli's FlightSegment validator rejects a past travel date, so the fixture dates
# are derived from today rather than pinned — a literal rots the suite.
_OUT = date.today() + timedelta(days=45)
_BACK = _OUT + timedelta(days=7)


def _segment(origin: str, dest: str, day: date, **kw: Any) -> Any:
    return FlightSegment(
        departure_airport=[[Airport[origin], 0]],
        arrival_airport=[[Airport[dest], 0]],
        travel_date=day.isoformat(),
        **kw,
    )


def _filters(**kw: Any) -> Any:
    defaults: dict[str, Any] = {
        "passenger_info": PassengerInfo(adults=1),
        "flight_segments": [_segment("JFK", "LAX", _OUT)],
        "stops": MaxStops.ANY,
        "seat_type": SeatType.ECONOMY,
        "trip_type": TripType.ONE_WAY,
    }
    defaults.update(kw)
    return FlightSearchFilters(**defaults)


# ─────────────────────── a hand-rolled protobuf reader ─────────────────────
#
# Small enough to keep in the test: asserting on decoded fields rather than a
# base64 blob is what makes a failure say WHICH field moved.

_VARINT_CONT = 0x80
_VARINT_MASK = 0x7F


def _decode(buf: bytes) -> dict[int, list[Any]]:
    """Flat {field: [values]} — varints as ints, length-delimited as bytes."""
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 0x07
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i : i + length], i + length
        else:
            raise AssertionError(f"unexpected wire type {wire} on field {field}")
        out.setdefault(field, []).append(value)
    return out


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & _VARINT_MASK) << shift
        if not byte & _VARINT_CONT:
            return value, i
        shift += 7


def _slices(raw: bytes) -> list[dict[int, list[Any]]]:
    return [_decode(s) for s in _decode(raw).get(3, [])]


# ───────────────────────────── envelope ────────────────────────────────


def test_search_tfs_omits_the_max_u64_pin() -> None:
    """Field 16 is the pinned-booking-link marker. The search page doesn't
    need it, and one writer serves both — so its absence is the only thing
    separating the two flavors."""
    fields = _decode(build_search_tfs(_filters()))
    assert 16 not in fields
    assert fields[1] == [28]
    assert fields[2] == [2]
    assert fields[14] == [1]


def test_search_tfs_one_way_and_round_trip_trip_type() -> None:
    assert _decode(build_search_tfs(_filters()))[19] == [2]
    rt = _filters(
        flight_segments=[_segment("JFK", "LAX", _OUT), _segment("LAX", "JFK", _BACK)],
        trip_type=TripType.ROUND_TRIP,
    )
    assert _decode(build_search_tfs(rt))[19] == [1]


def test_search_tfs_carries_cabin_and_one_varint_per_adult() -> None:
    f = _filters(passenger_info=PassengerInfo(adults=3), seat_type=SeatType.BUSINESS)
    fields = _decode(build_search_tfs(f))
    assert fields[9] == [3]  # BUSINESS
    assert fields[8] == [1, 1, 1]


def test_search_tfs_encodes_origin_date_and_destination() -> None:
    sl = _slices(build_search_tfs(_filters()))[0]
    assert sl[2] == [_OUT.isoformat().encode()]
    assert _decode(sl[13][0])[2] == [b"JFK"]
    assert _decode(sl[14][0])[2] == [b"LAX"]


# ─────────────────────────── stop ceiling ──────────────────────────────


@pytest.mark.parametrize(
    "stops,expected",
    [
        (MaxStops.NON_STOP, [0]),
        (MaxStops.ONE_STOP_OR_FEWER, [1]),
        (MaxStops.TWO_OR_FEWER_STOPS, [2]),
    ],
)
def test_stop_ceiling_is_zero_based(stops: Any, expected: list[int]) -> None:
    """fli's MaxStops is one-based; tfs field 3.5 is zero-based."""
    assert _slices(build_search_tfs(_filters(stops=stops)))[0][5] == expected


def test_stop_ceiling_is_omitted_for_any() -> None:
    """Writing 0 for "any" would pin every search to nonstop — the field has to
    be absent, not zero."""
    assert 5 not in _slices(build_search_tfs(_filters(stops=MaxStops.ANY)))[0]


# ───────────────── selected leg (round-trip expansion) ─────────────────


def _picked(airline: Any, flight_number: str) -> Any:
    return FlightResult(
        price=357.0,
        currency="USD",
        duration=365,
        stops=0,
        legs=[
            FlightLeg(
                airline=airline,
                flight_number=flight_number,
                departure_airport=Airport["JFK"],
                arrival_airport=Airport["LAX"],
                departure_datetime=datetime(_OUT.year, _OUT.month, _OUT.day, 6, 0),
                arrival_datetime=datetime(_OUT.year, _OUT.month, _OUT.day, 9, 5),
                duration=365,
            )
        ],
    )


@pytest.mark.parametrize(
    "airline,code",
    [
        (Airline["B6"], "B6"),
        (Airline["_9W"], "9W"),  # digit-leading codes are underscore-prefixed by fli
        (Airline["_0B"], "0B"),
    ],
)
def test_selected_leg_uses_the_iata_code_not_the_airline_name(airline: Any, code: str) -> None:
    """fli maps codes to display NAMES (`Airline["_0B"].value == "Blue Air"`), so
    the enum name minus its underscore is the code Google wants."""
    rt = _filters(
        flight_segments=[
            _segment("JFK", "LAX", _OUT, selected_flight=_picked(airline, "123")),
            _segment("LAX", "JFK", _BACK),
        ],
        trip_type=TripType.ROUND_TRIP,
    )
    leg = _decode(_slices(build_search_tfs(rt))[0][4][0])
    assert leg[1] == [b"JFK"]
    assert leg[2] == [_OUT.isoformat().encode()]
    assert leg[3] == [b"LAX"]
    assert leg[5] == [code.encode()]
    assert leg[6] == [b"123"]


def test_unpinned_segment_carries_no_selected_leg() -> None:
    rt = _filters(
        flight_segments=[
            _segment("JFK", "LAX", _OUT, selected_flight=_picked(Airline["B6"], "123")),
            _segment("LAX", "JFK", _BACK),
        ],
        trip_type=TripType.ROUND_TRIP,
    )
    assert 4 not in _slices(build_search_tfs(rt))[1]


# ──────────────── the allowlist: refuse, never partially honour ────────────


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("airlines", {"airlines": [Airline["AA"]]}),
        ("airlines_exclude", {"airlines_exclude": [Airline["AA"]]}),
        ("alliances", {"alliances": [Alliance.ONEWORLD]}),
        ("alliances_exclude", {"alliances_exclude": [Alliance.SKYTEAM]}),
        ("layover_restrictions", {"layover_restrictions": LayoverRestrictions(max_duration=120)}),
        ("max_duration", {"max_duration": 600}),
        ("price_limit", {"price_limit": PriceLimit(max_price=500)}),
        ("bags", {"bags": BagsFilter(checked_bags=1)}),
        ("emissions", {"emissions": EmissionsFilter.LESS}),
        ("exclude_basic_economy", {"exclude_basic_economy": True}),
        ("sort_by", {"sort_by": SortBy.CHEAPEST}),
        ("children", {"passenger_info": PassengerInfo(adults=1, children=1)}),
        ("infants_in_seat", {"passenger_info": PassengerInfo(adults=1, infants_in_seat=1)}),
        ("infants_on_lap", {"passenger_info": PassengerInfo(adults=1, infants_on_lap=1)}),
    ],
)
def test_unencodable_filter_raises_naming_the_field(field: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(GfTfsUnsupportedError) as excinfo:
        build_search_tfs(_filters(**kwargs))
    assert excinfo.value.field == field


def test_time_restrictions_raise() -> None:
    f = _filters(
        flight_segments=[
            _segment("JFK", "LAX", _OUT, time_restrictions=TimeRestrictions(earliest_departure=6))
        ]
    )
    with pytest.raises(GfTfsUnsupportedError) as excinfo:
        build_search_tfs(f)
    assert excinfo.value.field == "time_restrictions"


def test_multi_city_raises() -> None:
    f = _filters(
        flight_segments=[
            _segment("JFK", "LAX", _OUT),
            _segment("LAX", "SEA", _BACK),
            _segment("SEA", "JFK", _BACK + timedelta(days=3)),
        ],
        trip_type=TripType.MULTI_CITY,
    )
    with pytest.raises(GfTfsUnsupportedError) as excinfo:
        build_search_tfs(f)
    assert excinfo.value.field == "trip_type"


def test_multi_airport_leg_raises() -> None:
    """The bridge flattens airport sets to the first code, so encoding one
    would answer a JFK,EWR search with JFK only."""
    f = _filters(
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport["JFK"], 0], [Airport["EWR"], 0]],
                arrival_airport=[[Airport["LAX"], 0]],
                travel_date=_OUT.isoformat(),
            )
        ]
    )
    with pytest.raises(GfTfsUnsupportedError) as excinfo:
        build_search_tfs(f)
    assert excinfo.value.field == "flight_segments"


def test_a_default_populated_filter_does_not_raise() -> None:
    """fli seeds sort_by, emissions, exclude_basic_economy and show_all_results
    on every filter — a truthiness scan would refuse every search."""
    f = _filters()
    assert f.sort_by is SortBy.BEST
    assert f.show_all_results is True
    assert build_search_tfs(f)


# ────────────────────────────── page URL ───────────────────────────────


def test_search_page_url_carries_locale_and_the_encoded_tfs() -> None:
    url = google_flights_search_page_url(build_search_tfs(_filters()))
    assert url.startswith("https://www.google.com/travel/flights?tfs=")
    assert "&hl=en&gl=US&curr=USD" in url
    assert "=" not in url.split("tfs=")[1].split("&")[0]  # base64 padding stripped


def test_every_filter_field_is_claimed_by_exactly_one_set() -> None:
    """The allowlist is only an allowlist if nothing escapes it. A future fli
    minor adding a filter must fail HERE, loudly, not encode as if unset."""
    from flight_cli.links import (
        _TFS_ENCODED_FIELDS,
        _TFS_IGNORED_FIELDS,
        _TFS_REFUSED_FIELDS,
        _TFS_REFUSED_PAX,
    )

    refused = {field for field, _ in _TFS_REFUSED_FIELDS}
    claimed = _TFS_ENCODED_FIELDS | refused | _TFS_IGNORED_FIELDS
    assert claimed == set(FlightSearchFilters.model_fields)
    assert not (_TFS_ENCODED_FIELDS & refused)

    # The nested models the encoder reaches into carry their own fields, and a
    # new one there is just as silent — `PassengerInfo` gaining a passenger kind
    # would price it as nothing at all.
    pax_refused = {field for field, _ in _TFS_REFUSED_PAX}
    assert pax_refused | {"adults"} == set(PassengerInfo.model_fields)
    segment_read = {"departure_airport", "arrival_airport", "travel_date", "selected_flight"}
    assert segment_read | {"time_restrictions"} == set(FlightSegment.model_fields)


def test_a_filter_field_fli_grows_later_is_refused() -> None:
    """Simulates the fli bump: a field none of the three sets claims."""

    class _Grown(FlightSearchFilters):
        surprise_filter: int = 0

    f = _Grown(**_filters().model_dump())
    with pytest.raises(GfTfsUnsupportedError, match="surprise_filter"):
        build_search_tfs(f)
