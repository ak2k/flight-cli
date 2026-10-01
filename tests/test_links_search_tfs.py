# pyright: reportMissingTypeStubs=false
#   fli ships no stubs and this module imports eleven names from five of its
#   packages; a per-import suppression on each is five places to forget when a
#   twelfth arrives. Same file-level form as tests/test_gf_native_filters.py.
"""The search-page `tfs=` encoder: one writer, an allowlist, no silent drops.

`build_search_tfs` shares `_encode_gflight_pinned_tfs` with the pinned booking
link, so the byte-exact pin fixture (test_links_gflight_pin.py) is the other
half of this file's coverage. What's asserted here is the search flavor: the
field-16 pin omitted, the zero-based stop ceiling, carrier codes taken from
fli's enum NAME, the filters Google honored on a live page (checked field by
field against the pages' own URLs), and a refusal for every filter the page
has no field for.
"""

from __future__ import annotations

import base64
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
from fli.models.google_flights.flights import (
    FlightSearchFilters,
)

from flight_cli._gf_errors import GfTfsUnsupportedError
from flight_cli.links import (
    _encode_gflight_pinned_tfs,  # pyright: ignore[reportPrivateUsage]  # byte-exact against Google's own pages
    build_search_tfs,
    google_flights_search_page_url,
)

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


@pytest.mark.parametrize(
    ("pax", "kinds"),
    [
        (PassengerInfo(adults=1, infants_on_lap=1), [1, 3]),
        (PassengerInfo(adults=1, infants_in_seat=1), [1, 4]),
        (PassengerInfo(adults=2, children=1, infants_in_seat=1, infants_on_lap=1), [1, 1, 2, 4, 3]),
    ],
)
def test_an_infant_is_written_as_its_own_kind(pax: Any, kinds: list[int]) -> None:
    """Field 8 is one entry per occupant: 3 a lap infant, priced at a tenth of
    the adult fare, and 4 an infant in a seat, priced at the full fare."""
    assert _decode(build_search_tfs(_filters(passenger_info=pax)))[8] == kinds


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


def test_a_pinned_leg_over_an_airport_set_keeps_the_set_on_both_slices() -> None:
    """A round trip over a set pins the outbound and re-fetches the returns: the
    pinned request carries the chosen legs AND every airport of both slices."""
    nyc = [[Airport["JFK"], 0], [Airport["LGA"], 0], [Airport["EWR"], 0]]
    rt = _filters(
        flight_segments=[
            FlightSegment(
                departure_airport=nyc,
                arrival_airport=[[Airport["LAX"], 0]],
                travel_date=_OUT.isoformat(),
                selected_flight=_picked(Airline["B6"], "123"),
            ),
            FlightSegment(
                departure_airport=[[Airport["LAX"], 0]],
                arrival_airport=nyc,
                travel_date=_BACK.isoformat(),
            ),
        ],
        trip_type=TripType.ROUND_TRIP,
    )
    out, back = _slices(build_search_tfs(rt))
    assert _decode(out[4][0])[6] == [b"123"]
    assert (_endpoints(out, 13), _endpoints(out, 14)) == ([b"JFK", b"LGA", b"EWR"], [b"LAX"])
    assert (_endpoints(back, 13), _endpoints(back, 14)) == ([b"LAX"], [b"JFK", b"LGA", b"EWR"])


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
        ("airlines_exclude", {"airlines_exclude": [Airline["AA"]]}),
        ("alliances", {"alliances": [Alliance.ONEWORLD]}),
        ("alliances_exclude", {"alliances_exclude": [Alliance.SKYTEAM]}),
        ("emissions", {"emissions": EmissionsFilter.LESS}),
        ("exclude_basic_economy", {"exclude_basic_economy": True}),
        ("sort_by", {"sort_by": SortBy.CHEAPEST}),
        # 3.15 exists, but a connection airport means one position to Matrix.
        (
            "layover_restrictions",
            {"layover_restrictions": LayoverRestrictions(airports=[Airport["ORD"]])},
        ),
        (
            "layover_restrictions",
            {
                "layover_restrictions": LayoverRestrictions(
                    airports=[Airport["ORD"]], max_duration=120
                )
            },
        ),
    ],
)
def test_unencodable_filter_raises_naming_the_field(field: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(GfTfsUnsupportedError) as excinfo:
        build_search_tfs(_filters(**kwargs))
    assert excinfo.value.field == field


# ───────────── the filters Google honored, against its own URLs ─────────────
#
# Each is the tfs= of a JFK page Google served with the filter applied: the
# UI's own URL for the time window and the duration, the ones the live check
# fetched for the others. Only the travel date differs from what the encoder
# writes today.

_PAGE_CARRIER_AA = "CBwQAhoiEgoyMDI2LTExLTA0MgJBQWoHCAESA0pGS3IHCAESA0xBWEABSAFwAZgBAg"
_PAGE_ONEWORLD_LHR = "CBwQAhooEgoyMDI2LTExLTA0MghPTkVXT1JMRGoHCAESA0pGS3IHCAESA0xIUkABSAFwAZgBAg"
_PAGE_MAX_380_MIN = "CBwQAhohEgoyMDI2LTExLTA0YPwCagcIARIDSkZLcgcIARIDTEFYQAFIAXABmAEC"
_PAGE_DEPART_6_TO_11 = "CBwQAhomEgoyMDI2LTExLTA0QAZIC1AAWBdqBwgBEgNKRktyBwgBEgNMQVhAAUgBcAGYAQI"
_PAGE_LAYOVER_120_TO_240 = "CBwQAholEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEFYiAF4kAHwAUABSAFwAZgBAg"
_PAGE_ADULT_AND_CHILD = "CBwQAhoeEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEFYQAFAAkgBcAGYAQI"


def _undated(raw: bytes) -> tuple[dict[int, list[Any]], list[dict[int, list[Any]]]]:
    """(envelope without its slices, each slice without its date)."""
    envelope = _decode(raw)
    slices = [_decode(s) for s in envelope.pop(3)]
    for sl in slices:
        sl.pop(2)
    return envelope, slices


def _page_tfs(b64: str) -> bytes:
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))


@pytest.mark.parametrize(
    "page,kwargs",
    [
        (_PAGE_CARRIER_AA, {"airlines": [Airline["AA"]]}),
        (
            _PAGE_ONEWORLD_LHR,
            {
                "airlines": [Airline["ONEWORLD"]],
                "flight_segments": [_segment("JFK", "LHR", _OUT)],
            },
        ),
        (_PAGE_MAX_380_MIN, {"max_duration": 380}),
        (
            _PAGE_DEPART_6_TO_11,
            {
                "flight_segments": [
                    _segment(
                        "JFK",
                        "LAX",
                        _OUT,
                        time_restrictions=TimeRestrictions(
                            earliest_departure=6, latest_departure=11
                        ),
                    )
                ]
            },
        ),
        (
            _PAGE_LAYOVER_120_TO_240,
            {"layover_restrictions": LayoverRestrictions(min_duration=120, max_duration=240)},
        ),
        (_PAGE_ADULT_AND_CHILD, {"passenger_info": PassengerInfo(adults=1, children=1)}),
    ],
    ids=["carrier", "alliance", "duration", "departure-window", "layover", "child"],
)
def test_a_filter_encodes_as_the_page_google_served_it(page: str, kwargs: dict[str, Any]) -> None:
    assert _undated(build_search_tfs(_filters(**kwargs))) == _undated(_page_tfs(page))


# ─────────── a price cap (12) and bags (13), as Google served them ───────────
#
# The pages a live check fetched with each field set, JFK on 2026-11-04. Both
# fields are top level, after the cabin (9) and before 14; a zero bag count is
# left out of 13, the form those pages carried.

_PAGE_LAX_CAP_250 = "CBwQAhoeEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEFYQAFIAWD6AXABmAEC"
_PAGE_LHR_CAP_300 = "CBwQAhoeEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEhSQAFIAWCsAnABmAEC"
_PAGE_LAX_CHECKED_1 = "CBwQAhoeEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEFYQAFIAWoCGAFwAZgBAg"
_PAGE_LAX_CARRY_1 = "CBwQAhoeEgoyMDI2LTExLTA0agcIARIDSkZLcgcIARIDTEFYQAFIAWoCEAFwAZgBAg"


def _dated_page_tfs(dest: str, **kw: Any) -> bytes:
    """The search-page tfs= for JFK to `dest` on the pages' own date, which
    `build_search_tfs` cannot write: fli refuses a past travel date."""
    return _encode_gflight_pinned_tfs(
        slices=[{"date": "2026-11-04", "origin": "JFK", "destination": dest, "segments": []}],
        cabin=1,
        adults=1,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
        pin_max_u64=False,
        **kw,
    )


@pytest.mark.parametrize(
    "page,dest,kwargs",
    [
        (_PAGE_LAX_CAP_250, "LAX", {"max_price": 250}),
        (_PAGE_LHR_CAP_300, "LHR", {"max_price": 300}),
        (_PAGE_LAX_CHECKED_1, "LAX", {"bags": (1, 0)}),
        (_PAGE_LAX_CARRY_1, "LAX", {"bags": (0, 1)}),
    ],
    ids=["lax-cap-250", "lhr-cap-300", "checked-1", "carry-on-1"],
)
def test_a_cap_or_bags_encodes_byte_for_byte_as_google_served_it(
    page: str, dest: str, kwargs: dict[str, Any]
) -> None:
    assert base64.urlsafe_b64encode(_dated_page_tfs(dest, **kwargs)).rstrip(b"=").decode() == page


def test_both_bag_kinds_write_the_ui_s_own_field() -> None:
    """The UI's bags dialog wrote `agQQARgB`, `{2: 1, 3: 1}`, for one of each."""
    assert _page_tfs("agQQARgB") in _dated_page_tfs("LAX", bags=(1, 1))


def test_no_bag_asked_for_writes_no_field() -> None:
    assert _dated_page_tfs("LAX", bags=(0, 0)) == _dated_page_tfs("LAX")


def test_the_search_page_carries_the_cap_and_the_bags() -> None:
    fields = _decode(
        build_search_tfs(
            _filters(
                price_limit=PriceLimit(max_price=250),
                bags=BagsFilter(checked_bags=2, carry_on=True),
            )
        )
    )
    assert fields[12] == [250]
    assert _decode(fields[13][0]) == {2: [1], 3: [2]}


def test_a_search_with_neither_writes_neither_field() -> None:
    fields = _decode(build_search_tfs(_filters()))
    assert 12 not in fields
    assert 13 not in fields


def test_the_ui_writes_all_four_hours_once_one_is_set() -> None:
    """The UI's URL for "6:00 AM to end of day, arriving by 9:00 PM": a latest
    hour is the last hour included, so 9 PM is 20 and the end of the day 23."""
    ui = _slices(
        _page_tfs(
            "CBwQAhopEgoyMDI2LTExLTA0QAZIF1AAWBRg4ANqBwgBEgNKRktyBwgBEgNMQVhAAUgBcAGCAQsI____________AZgBAg"
        )
    )[0]
    assert (ui[8], ui[9], ui[10], ui[11]) == ([6], [23], [0], [20])
    window = TimeRestrictions(earliest_departure=6, latest_arrival=20)
    ours = _slices(
        build_search_tfs(
            _filters(flight_segments=[_segment("JFK", "LAX", _OUT, time_restrictions=window)])
        )
    )[0]
    assert (ours[8], ours[9], ours[10], ours[11]) == ([6], [23], [0], [20])


def test_a_minimum_layover_is_written_alone() -> None:
    sl = _slices(
        build_search_tfs(_filters(layover_restrictions=LayoverRestrictions(min_duration=120)))
    )[0]
    assert sl[17] == [120]
    assert 18 not in sl


def test_carriers_repeat_and_take_the_iata_code_not_the_name() -> None:
    sl = _slices(build_search_tfs(_filters(airlines=[Airline["AA"], Airline["_9W"]])))[0]
    assert sl[6] == [b"AA", b"9W"]


def test_a_round_trip_carries_the_trip_filters_on_both_slices_and_each_its_own_window() -> None:
    rt = _filters(
        flight_segments=[
            _segment(
                "JFK",
                "LAX",
                _OUT,
                time_restrictions=TimeRestrictions(earliest_departure=8, latest_departure=11),
            ),
            _segment("LAX", "JFK", _BACK),
        ],
        trip_type=TripType.ROUND_TRIP,
        airlines=[Airline["AA"]],
        max_duration=380,
        layover_restrictions=LayoverRestrictions(max_duration=90),
    )
    out, back = _slices(build_search_tfs(rt))
    for sl in (out, back):
        assert (sl[6], sl[12], sl[18]) == ([b"AA"], [380], [90])
    assert (out[8], out[9], out[10], out[11]) == ([8], [11], [0], [23])
    assert not {8, 9, 10, 11} & set(back)


def test_an_unset_filter_writes_nothing() -> None:
    sl = _slices(build_search_tfs(_filters()))[0]
    assert not {6, 7, 8, 9, 10, 11, 12, 15, 17, 18} & set(sl)


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


def _endpoints(sl: dict[int, list[Any]], field: int) -> list[bytes]:
    return [_decode(entry)[2][0] for entry in sl[field]]


def test_a_multi_airport_leg_repeats_every_airport_in_order() -> None:
    """The page takes a repeated 3.13/3.14 entry per airport; carrying only the
    first would answer a JFK,EWR search with JFK only."""
    f = _filters(
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport["JFK"], 0], [Airport["EWR"], 0]],
                arrival_airport=[[Airport["LAX"], 0]],
                travel_date=_OUT.isoformat(),
            )
        ]
    )
    sl = _slices(build_search_tfs(f))[0]
    assert _endpoints(sl, 13) == [b"JFK", b"EWR"]
    assert _endpoints(sl, 14) == [b"LAX"]
    # Kind 1 (an airport) on every entry.
    assert [_decode(entry)[1] for entry in sl[13]] == [[1], [1]]


def test_a_round_trip_over_sets_runs_the_return_from_the_destination_set() -> None:
    nyc = [[Airport["JFK"], 0], [Airport["LGA"], 0], [Airport["EWR"], 0]]
    lon = [[Airport["LHR"], 0], [Airport["LGW"], 0]]
    f = _filters(
        flight_segments=[
            FlightSegment(departure_airport=nyc, arrival_airport=lon, travel_date=_OUT.isoformat()),
            FlightSegment(
                departure_airport=lon, arrival_airport=nyc, travel_date=_BACK.isoformat()
            ),
        ],
        trip_type=TripType.ROUND_TRIP,
    )
    out, back = _slices(build_search_tfs(f))
    assert (_endpoints(out, 13), _endpoints(out, 14)) == (
        [b"JFK", b"LGA", b"EWR"],
        [b"LHR", b"LGW"],
    )
    assert (_endpoints(back, 13), _endpoints(back, 14)) == (
        [b"LHR", b"LGW"],
        [b"JFK", b"LGA", b"EWR"],
    )


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
    # The three sets ARE the unit under test, so reaching for them is the point.
    from flight_cli.links import (
        _TFS_ENCODED_FIELDS,  # pyright: ignore[reportPrivateUsage]
        _TFS_ENCODED_PAX,  # pyright: ignore[reportPrivateUsage]
        _TFS_IGNORED_FIELDS,  # pyright: ignore[reportPrivateUsage]
        _TFS_REFUSED_FIELDS,  # pyright: ignore[reportPrivateUsage]
    )

    refused = {field for field, _ in _TFS_REFUSED_FIELDS}
    claimed = _TFS_ENCODED_FIELDS | refused | _TFS_IGNORED_FIELDS
    assert claimed == set(FlightSearchFilters.model_fields)
    assert not (_TFS_ENCODED_FIELDS & refused)

    # The nested models the encoder reaches into carry their own fields, and a
    # new one there is just as silent — `PassengerInfo` gaining a passenger kind
    # would price it as nothing at all.
    assert set(PassengerInfo.model_fields) == _TFS_ENCODED_PAX
    segment_read = {
        "departure_airport",
        "arrival_airport",
        "travel_date",
        "selected_flight",
        "time_restrictions",
    }
    assert segment_read == set(FlightSegment.model_fields)
    # `airports` is read to refuse it.
    assert {"airports", "min_duration", "max_duration"} == set(LayoverRestrictions.model_fields)
    # The cap's currency is the page's `curr=`, so only the amount is written.
    assert {"max_price", "currency"} == set(PriceLimit.model_fields)
    assert {"checked_bags", "carry_on"} == set(BagsFilter.model_fields)
    assert {
        "earliest_departure",
        "latest_departure",
        "earliest_arrival",
        "latest_arrival",
    } == set(TimeRestrictions.model_fields)


def test_a_filter_field_fli_grows_later_is_refused() -> None:
    """Simulates the fli bump: a field none of the three sets claims."""

    class _Grown(FlightSearchFilters):
        surprise_filter: int = 0

    f = _Grown(**_filters().model_dump())
    with pytest.raises(GfTfsUnsupportedError, match="surprise_filter"):
        build_search_tfs(f)
