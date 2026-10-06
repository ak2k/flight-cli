"""`search --verify` on a row whose carrier the probe page does not name.

The probe is one page of Matrix's unrouted answer, so a carrier missing from
it may still be on a trip the page left out. The verdict says how many trips
it read and that none named the carrier, never that Matrix has none."""

from __future__ import annotations

from typing import Any

from flight_cli import _verify as v
from flight_cli.models import SearchResult


def _flight(code: str, frm: str, to: str, dep: str, arr: str) -> v.Flight:
    return v.Flight(code[:2], code[2:], frm, to, dep, arr)


def _row(*slices: tuple[v.Flight, ...], price: str | None = "USD284.00") -> v.Row:
    return v.Row(tuple(slices), price)


def _solution(
    sid: str, price: str, dep: str, arr: str, flights: list[str], stops: list[str]
) -> dict[str, Any]:
    """One `solutionList` entry in the shape Matrix sends it."""
    return {
        "id": sid,
        "ext": {"price": price},
        "displayTotal": price,
        "itinerary": {
            "slices": [
                {
                    "origin": {"code": "JFK"},
                    "destination": {"code": "LAX"},
                    "departure": dep,
                    "arrival": arr,
                    "flights": flights,
                    "stops": [{"code": s} for s in stops],
                }
            ],
            "carriers": [{"code": flights[0][:2]}],
        },
    }


def _probe(*carriers: str, total: int) -> SearchResult:
    """An unrouted answer of `total` trips whose page holds one itinerary per
    carrier, in the L3 shape: carrier-stop matrix columns, the carrier filter,
    and the itineraries."""
    return SearchResult.from_api(
        {
            "solutionCount": total,
            "carrierStopMatrix": {"columns": [{"label": {"code": c}} for c in carriers]},
            "itineraryCarrierList": {"groups": [{"label": {"code": c}} for c in carriers]},
            "solutionList": {
                "solutions": [
                    _solution(
                        f"P{i}",
                        "USD99.00",
                        "2026-10-20T07:00-07:00",
                        "2026-10-20T08:10-07:00",
                        [f"{c}100"],
                        [],
                    )
                    for i, c in enumerate(carriers)
                ]
            },
        }
    )


_WN = _row(
    (_flight("WN1234", "LAS", "LAX", "2026-10-20T07:00", "2026-10-20T08:10"),), price="USD99.00"
)


def test_a_carrier_missing_from_one_probe_page_is_unseen_not_absent() -> None:
    verdict = v.unpriced(_WN, _probe("AA", "UA", total=88))
    assert verdict.outcome == "carrier-unseen"
    assert verdict.missing_carriers == ("WN",)
    assert verdict.reason is not None
    assert "none of the 2 of 88 trips Matrix returned" in verdict.reason
    assert "lists no itinerary" not in verdict.reason


def test_a_whole_answer_on_the_page_is_counted_once() -> None:
    verdict = v.unpriced(_WN, _probe("AA", "UA", total=2))
    assert verdict.outcome == "carrier-unseen"
    assert verdict.reason is not None
    assert "none of the 2 trips Matrix returned" in verdict.reason


def test_a_carrier_the_page_names_is_no_solution() -> None:
    verdict = v.unpriced(_WN, _probe("AA", "WN", total=88))
    assert verdict.outcome == "no-solution"
    assert verdict.missing_carriers == ()
