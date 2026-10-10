# pyright: reportCallIssue=false
# DIVERGE: pydantic Field(alias=...) on _Loose models trips basedpyright into
# treating alias names as required kwargs. Same posture as tests/test_enrich.py.
"""A Matrix row with no Google trip of its own takes a Google row's price only
if the two land within minutes of each other (`_enrich.merge_results`)."""

from __future__ import annotations

import pytest

from flight_cli._enrich import MergedRow, merge_results
from flight_cli.models import (
    Itinerary,
    ItineraryDetails,
    ItineraryExt,
    SearchResult,
    Slice,
)

_DATES = ["2026-10-20", "2026-10-23"]


def _row(price: str, arrival: str | None, segment_dates: list[str] | None = None) -> Itinerary:
    """FI614 then FI450 out of JFK on 2026-10-20, landing at `arrival`."""
    slc = Slice(
        flights=["FI614", "FI450"],
        departure="2026-10-20T20:30",
        arrival=arrival,
        segment_dates=segment_dates or [],
    )
    return Itinerary(ext=ItineraryExt(price=price), itinerary=ItineraryDetails(slices=[slc]))


def _sr(*its: Itinerary) -> SearchResult:
    return SearchResult(solutionCount=len(its), solutions=list(its))


def _google(arrival: str | None = "2026-10-23T11:55:00") -> SearchResult:
    return _sr(_row("USD900.00", arrival, _DATES))


def _matrix(*landings: str) -> SearchResult:
    """Matrix rows priced USD884, USD885, ... in the order given, each landing
    as Matrix writes it, with a UTC offset."""
    return _sr(*(_row(f"USD{884 + n}.00", at) for n, at in enumerate(landings)))


def _view(rows: list[MergedRow]) -> list[tuple[str, str | None, str | None]]:
    """(source, Google price, Matrix price) per row, in row order."""
    return [(r.source, r.gf_price, r.matrix_price) for r in rows]


def test_a_matrix_trip_landing_days_from_the_only_google_row_takes_no_google_price() -> None:
    """Matrix lands the 21st, Google's only row the 23rd: one key, two trips."""
    rows = merge_results(_google(), _matrix("2026-10-21T11:55+00:00"), currency="USD")
    assert _view(rows) == [
        ("matrix", None, "USD884.00"),
        ("gf", "USD900.00", None),
    ]


def test_a_matrix_trip_landing_a_day_off_at_the_same_clock_time_takes_no_google_price() -> None:
    rows = merge_results(_google(), _matrix("2026-10-24T11:55+00:00"), currency="USD")
    assert _view(rows) == [
        ("matrix", None, "USD884.00"),
        ("gf", "USD900.00", None),
    ]


def test_every_matrix_row_of_a_key_may_take_the_google_row_landing_near_it() -> None:
    """The key's first Matrix row lands the 21st; the second lands within three
    minutes of Google's row and is priced by it, undated."""
    rows = merge_results(
        _google(), _matrix("2026-10-21T11:55+00:00", "2026-10-23T11:58+00:00"), currency="USD"
    )
    assert _view(rows) == [
        ("matrix", None, "USD884.00"),
        ("both", "USD900.00", "USD885.00"),
    ]
    assert rows[1].itinerary.itinerary is not None
    assert rows[1].itinerary.itinerary.slices[0].segment_dates == []
    assert rows[1].same_trip is False


@pytest.mark.parametrize(
    ("landing", "source"),
    [
        ("2026-10-23T11:50+00:00", "both"),
        ("2026-10-23T12:00+00:00", "both"),
        ("2026-10-23T11:49+00:00", "matrix"),
        ("2026-10-23T12:01+00:00", "matrix"),
    ],
)
def test_a_landing_five_minutes_from_googles_pairs_and_six_does_not(
    landing: str, source: str
) -> None:
    (row, *_) = merge_results(_google(), _matrix(landing), currency="USD")
    assert (row.source, row.gf_price) == (source, "USD900.00" if source == "both" else None)


def test_a_skew_across_midnight_counts_in_minutes_not_days() -> None:
    rows = merge_results(
        _google("2026-10-23T23:59:00"), _matrix("2026-10-24T00:02+00:00"), currency="USD"
    )
    assert _view(rows) == [("both", "USD900.00", "USD884.00")]


def test_a_google_row_with_no_landing_time_may_price_a_matrix_row() -> None:
    rows = merge_results(_google(None), _matrix("2026-10-21T11:55+00:00"), currency="USD")
    assert _view(rows) == [("both", "USD900.00", "USD884.00")]


def test_a_matrix_row_with_no_landing_time_may_take_a_google_row() -> None:
    matrix = _sr(_row("USD884.00", None))
    assert _view(merge_results(_google(), matrix, currency="USD")) == [
        ("both", "USD900.00", "USD884.00")
    ]
