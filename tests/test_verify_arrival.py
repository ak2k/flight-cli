# pyright: reportPrivateUsage=false
"""`search --verify` on booking details that leave out a leg's arrival.

Details that do not state every leg cannot show the row is that itinerary, so
the check gives no verdict rather than "Verified" beside an unknown arrival."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from flight_cli import _verify as v
from flight_cli import cli
from flight_cli.client import MatrixClient
from flight_cli.models import BookingDetailsResult
from test_verify import (
    _DETAILS_SHORT_OF_A_FLIGHT,
    _as_row,
    _chain,
    _details_of,
    _gives_no_verdict,
    _Matrix,
    _row_solution,
    _run,
    _served,
)

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()

    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: val for k, val in kw.items() if k == "impersonate"},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)
    return fake


def _without_last_arrival(row: Any) -> dict[str, Any]:
    """Booking details for `row` in which the last flight and its leg state no
    arrival."""
    details = _details_of(row)
    last = details["bookingDetails"]["itinerary"]["slices"][0]["segments"][-1]
    del last["arrival"], last["legs"][0]["arrival"]
    return details


def test_details_silent_on_a_legs_arrival_do_not_match_the_row() -> None:
    _n, row = _as_row()
    details = BookingDetailsResult.from_api(_details_of(row)).booking_details
    short = BookingDetailsResult.from_api(_without_last_arrival(row)).booking_details
    assert details is not None and details.itinerary is not None
    assert short is not None and short.itinerary is not None
    google = v.google_row(row)
    assert v.same_flights(google, details.itinerary)
    assert not v.same_flights(google, short.itinerary)


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_details_silent_on_a_legs_arrival_give_no_verdict(
    gf_session: Callable[..., Any], matrix: _Matrix, fmt: str
) -> None:
    n, row = _as_row()
    matrix.chain = _chain(_row_solution("AS-1", f"USD{row.flight.price:.2f}", row))
    matrix.details = {"AS-1": _without_last_arrival(row)}
    gf_session(_served())
    result = _run("-n", "40", "--fast", "--verify", "--pick", str(n), "--format", fmt)
    _gives_no_verdict(result, fmt, _DETAILS_SHORT_OF_A_FLIGHT)
    assert "Verified on Matrix" not in result.stdout
    assert matrix.summarized() == [("viewDetails", "AS-1")]
