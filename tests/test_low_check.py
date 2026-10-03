# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_verify.py follows; the constructor has no transport
# injection point.
"""The default merged search asks Matrix for Google's low row when Google
undercuts every fare in Matrix's own answer.

Google serves the captured JFK-LAX board re-dated to `_DEP`; Matrix's default
answer is one B6 trip, B6999 unless a test names B6523, which Google lists too.
The chain search is answered from `test_verify`'s fake, routed searches with
`chain` and unrouted ones with `probe`."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import pytest
import stamina
from typer.testing import CliRunner

from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.client import MatrixClient
from flight_cli.domain import SearchOptions
from test_verify import (
    _DEP,
    _SEARCH,
    _URL,
    _booked,
    _chain,
    _details_of,
    _Matrix,
    _row_solution,
    _served,
    _solution,
)

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

_LINE = "Matrix asked for row "


def _b6(price: str, flight: str = "B6999") -> dict[str, Any]:
    """Matrix's default answer: one nonstop B6 trip at `price`."""
    return _solution("B6-1", price, f"{_DEP}T07:00-04:00", f"{_DEP}T10:08-07:00", [flight], [])


def _low() -> Any:
    """The merged table's row 1: the board's first row at its lowest price,
    since Google rows keep the board's order among equal prices."""
    rows = list(gfid._rows_from_page_html(PageFetch(_served(), _URL, 200)))
    return min((r for r in rows if r.flight.price is not None), key=lambda r: r.flight.price or 0)


def _chain_text(row: Any) -> str:
    legs = row.flight.legs
    return f"{_booked(row).replace('+', ' ')} {legs[0].departure_datetime.date().isoformat()}"


def _price(row: Any) -> str:
    return f"USD{row.flight.price:.2f}"


class _Stalling(_Matrix):
    """Matrix that answers the default search and stalls the chain search."""

    async def stall(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        if any(s.get("routeLanguage") for s in body["inputs"]["slices"]):
            self.bodies.append(body)
            await anyio.sleep(30)
        return self.handler(request)


class _ChainError(_Matrix):
    """Matrix that answers the default search and refuses the chain search."""

    def refuse(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        if any(s.get("routeLanguage") for s in body.get("inputs", {}).get("slices", [])):
            self.bodies.append(body)
            return httpx.Response(200, json={"error": {"message": "boom", "type": "internal"}})
        return self.handler(request)


class _ChainStatus(_Matrix):
    """Matrix that answers the default search and the chain search with HTTP `status`."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status

    def refuse(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        if any(s.get("routeLanguage") for s in body.get("inputs", {}).get("slices", [])):
            self.bodies.append(body)
            return httpx.Response(self.status, json={})
        return self.handler(request)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    fake: _Matrix,
    handler: Callable[[httpx.Request], Any],
) -> None:
    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: val for k, val in kw.items() if k == "impersonate"},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()
    _install(monkeypatch, tmp_path, fake, fake.handler)
    return fake


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli.app, [*_SEARCH, *args])


def _under(out: str) -> str:
    """The text under the merged table, whitespace folded."""
    _, after = out.split("Google Flights + Matrix", 1)
    lines = after.splitlines()
    end = next(i for i, ln in enumerate(lines) if ln.strip().startswith("└"))
    return " ".join(" ".join(lines[end + 1 :]).split())


def _routed(matrix: _Matrix) -> list[dict[str, Any]]:
    return [
        b for b in matrix.searches() if any(s.get("routeLanguage") for s in b["inputs"]["slices"])
    ]


def test_a_merged_search_compares_with_matrix_rather_than_calling_it_authoritative(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Red at the base: stderr said "refining with Matrix (authoritative fares)"."""
    matrix.probe = _chain(_b6("USD150.00"))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    assert "comparing with Matrix's fares" in result.stderr
    assert "authoritative" not in result.stderr


def test_matrix_prices_googles_low_row_as_its_exact_flights(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(a) Red at the base: no line, and one Matrix search."""
    low = _low()
    price = _price(low)
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-1", price, low))
    matrix.details = {"DL-1": _details_of(low)}
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    line = f"{_LINE}1's flights ({_chain_text(low)}): Matrix {price} · Google {price} · same price"
    assert line in under, under
    assert under.count(_LINE) == 1
    assert "Asking Matrix for row 1's exact flights (at most 60 s)…" in result.stderr
    # The default search, unrouted for Matrix's whole answer, then one chain
    # search at the default page, one booking details and no fare rules.
    default, chain = matrix.searches()
    assert not any(s.get("routeLanguage") for s in default["inputs"]["slices"])
    assert default["inputs"]["page"]["size"] == 500
    (leg,) = chain["inputs"]["slices"]
    assert leg["routeLanguage"] == _booked(low).replace("+", " ")
    assert leg["date"] == str(_DEP)
    assert chain["inputs"]["page"]["size"] == SearchOptions().page_size
    assert matrix.summarized() == [("viewDetails", "DL-1")]


def test_a_row_both_sides_price_is_never_the_checked_row(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """Red at the base: no line. Matrix's B6523 shares Google's flights and
    day, so row 1 is on both sides, Google's price under Matrix's; the check
    asks for row 2, the first row on Google alone."""
    low = _low()
    price = _price(low)
    matrix.probe = _chain(_b6("USD999.00", "B6523"))
    matrix.chain = _chain(_row_solution("DL-1", price, low))
    matrix.details = {"DL-1": _details_of(low)}
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert f"{_LINE}2's flights ({_chain_text(low)}): Matrix {price} · Google {price}" in under
    assert under.count(_LINE) == 1


def test_matrix_at_or_under_googles_low_asks_nothing_more(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(b) Guard: green at the base."""
    matrix.probe = _chain(_b6(_price(_low())))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    assert _LINE not in result.stdout
    assert "Asking Matrix" not in result.stderr
    assert len(matrix.searches()) == 1
    assert matrix.summarized() == []


def test_no_fare_on_the_exact_flights_is_said_without_a_price(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(c) Red at the base: no line."""
    low = _low()
    matrix.probe = _chain(_b6("USD999.00"))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): not priced as these flights: "
        "Matrix returned no fare on these exact flights"
    ) in under, under
    assert "Matrix USD" not in under
    assert len(matrix.searches()) == 2
    assert matrix.summarized() == []


def test_the_same_flights_landing_a_day_later_are_another_itinerary(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(d) Red at the base: no line."""
    low = _low()
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-2", "USD150.00", low, lands_later=1))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): not priced as these flights: "
        "Matrix prices these flights only on 1 other itinerary, with a flight on another "
        "day, at another time or between other airports"
    ) in under, under
    assert "150.00" not in result.stdout
    assert matrix.summarized() == []


@pytest.mark.parametrize("listed", ["no candidate", "a candidate flown a day later"])
def test_an_answer_longer_than_its_page_is_no_answer_rather_than_another_itinerary(
    gf_session: Callable[..., Any], matrix: _Matrix, listed: str
) -> None:
    """Matrix found the row's own itinerary, but past the chain's page, under
    trips on the same flights landing a day later."""
    low = _low()
    page = SearchOptions().page_size
    later = [_row_solution(f"DL-{i}", "USD100.00", low, lands_later=1) for i in range(page)]
    if listed == "a candidate flown a day later":
        later[0] = _row_solution("DL-0", "USD100.00", low)
        matrix.details = {"DL-0": _details_of(low, later={0: 1})}
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(*later, _row_solution("DL-exact", _price(low), low))
    reason = (
        f"Matrix listed only {page:d} of its {page + 1:d} itineraries on these flights, "
        "and none listed is these exact flights."
    )
    gf_session(_served())
    table = _run("-n", "10")
    gf_session(_served())
    document = _run("-n", "10", "--enrich", "--format", "json")
    assert table.exit_code == 0, table.output
    under = _under(table.stdout)
    assert f"{_LINE}1's flights ({_chain_text(low)}): no answer: {reason}" in under, under
    assert "other itinerar" not in under
    assert document.exit_code == 0, document.output
    low_check = json.loads(document.stdout)["cross_check"]["low_check"]
    assert (low_check["outcome"], low_check["reason"]) == ("no-answer", reason)


def test_booking_details_silent_on_a_flights_departure_are_no_answer(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """The chain's one candidate is the row; its booking details leave out the
    flight's departure, so they cannot show it is another itinerary."""
    low = _low()
    details = _details_of(low)
    (segment,) = details["bookingDetails"]["itinerary"]["slices"][0]["segments"]
    del segment["departure"], segment["legs"][0]["departure"]
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-1", _price(low), low))
    matrix.details = {"DL-1": details}
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): no answer: Matrix returned booking details "
        "that do not state every flight's number, airports and times, so this itinerary "
        "cannot be checked flight by flight."
    ) in under, under
    assert "other itinerar" not in under
    assert matrix.summarized() == [("viewDetails", "DL-1")]


def test_a_chain_past_the_bound_is_no_answer_and_leaves_the_table(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """(e) Red at the base: no line."""
    fake = _Stalling()
    _install(monkeypatch, tmp_path, fake, fake.stall)
    monkeypatch.setattr(cli, "_LOW_CHECK_SECONDS", 0.05)
    low = _low()
    fake.probe = _chain(_b6("USD999.00"))
    fake.chain = _chain(_row_solution("DL-1", _price(low), low))
    gf_session(_served())
    started = time.monotonic()
    result = _run("-n", "10")
    took = time.monotonic() - started
    assert result.exit_code == 0, result.output
    assert took < 2, took
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): no answer: Matrix did not answer within 0.05 s"
    ) in under, under
    assert "Matrix listed 1 of 1 solutions (to USD999.00)" in under
    assert "Asking Matrix for row 1's exact flights (at most 0.05 s)…" in result.stderr
    assert len(_routed(fake)) == 1


def test_a_chain_error_is_no_answer_with_the_error(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """(f) Red at the base: no line."""
    fake = _ChainError()
    _install(monkeypatch, tmp_path, fake, fake.refuse)
    low = _low()
    fake.probe = _chain(_b6("USD999.00"))
    gf_session(_served())
    result = _run("-n", "10")
    assert result.exit_code == 0, result.output
    under = _under(result.stdout)
    assert (
        f"{_LINE}1's flights ({_chain_text(low)}): no answer: "
        "Matrix returned an error (internal): boom"
    ) in under, under
    assert len(_routed(fake)) == 1


@pytest.mark.parametrize(
    ("status", "phrase"), [(429, "Too Many Requests"), (503, "Service Unavailable")]
)
def test_a_chain_http_error_is_no_answer_without_the_api_key(
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    status: int,
    phrase: str,
) -> None:
    """The error's own text quotes the request URL, `key=` and all; neither
    the line nor the document repeats it."""
    fake = _ChainStatus(status)
    _install(monkeypatch, tmp_path, fake, fake.refuse)
    low = _low()
    fake.probe = _chain(_b6("USD999.00"))
    reason = f"Matrix answered HTTP {status:d} {phrase}"
    with stamina.set_testing(True, attempts=1):
        gf_session(_served())
        table = _run("-n", "10")
        gf_session(_served())
        document = _run("-n", "10", "--enrich", "--format", "json")
    assert table.exit_code == 0, table.output
    under = _under(table.stdout)
    assert f"{_LINE}1's flights ({_chain_text(low)}): no answer: {reason}" in under, under
    assert "test-key" not in table.stdout
    assert document.exit_code == 0, document.output
    low_check = json.loads(document.stdout)["cross_check"]["low_check"]
    assert (low_check["outcome"], low_check["reason"]) == ("no-answer", reason)
    assert "test-key" not in document.stdout


def test_the_document_carries_the_check(gf_session: Callable[..., Any], matrix: _Matrix) -> None:
    """(g) Red at the base: no `low_check` key."""
    low = _low()
    price = _price(low)
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-1", price, low))
    matrix.details = {"DL-1": _details_of(low)}
    gf_session(_served())
    result = _run("-n", "10", "--enrich", "--format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert set(doc) == {"search", "cross_check"}
    assert doc["cross_check"]["low_check"] == {
        "row": 1,
        "google_low": price,
        "matrix_low": "USD999.00",
        "outcome": "match",
        "matrix_price": price,
        "delta": 0.0,
        "reason": None,
        "routing": [_booked(low).replace("+", " ")],
    }
    assert doc["cross_check"]["rows"][0]["google_price"] == price


def test_the_document_carries_null_where_no_row_is_checked(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(g) Red at the base: no `low_check` key."""
    matrix.probe = _chain(_b6("USD150.00"))
    gf_session(_served())
    result = _run("-n", "10", "--enrich", "--format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert set(doc) == {"search", "cross_check"}
    assert doc["cross_check"]["low_check"] is None
    assert len(matrix.searches()) == 1


def test_a_party_is_priced_on_both_sides_for_the_party(
    gf_session: Callable[..., Any], matrix: _Matrix
) -> None:
    """(h) Red at the base: no line. Google prices the party; Matrix's chain
    lists USD110 a passenger and USD220 for two."""
    low = _low()
    price = _price(low)
    sol = _row_solution("DL-1", "USD110.00", low)
    sol["displayTotal"] = "USD220.00"
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(sol)
    matrix.details = {"DL-1": _details_of(low)}
    gf_session(_served())
    result = _run("-n", "10", "--adults", "2")
    assert result.exit_code == 0, result.output
    gap = 220.0 - low.flight.price
    line = (
        f"{_LINE}1's flights ({_chain_text(low)}): Matrix USD220.00 · Google {price} · "
        f"Matrix USD{gap:.2f} dearer"
    )
    assert line in _under(result.stdout)
    assert "USD110.00" not in result.stdout
    assert all(b["inputs"]["pax"] == {"adults": 2} for b in matrix.searches())

    gf_session(_served())
    result = _run("-n", "10", "--adults", "2", "--enrich", "--format", "json")
    assert result.exit_code == 0, result.output
    low_check = json.loads(result.stdout)["cross_check"]["low_check"]
    assert low_check["matrix_price"] == sol["displayTotal"]
    assert low_check["delta"] == round(low.flight.price - 220.0, 2)


@pytest.mark.parametrize("flag", ["--fast", "--verify"])
def test_fast_and_verify_runs_ask_matrix_nothing_more(
    gf_session: Callable[..., Any], matrix: _Matrix, flag: str
) -> None:
    """(i) Guard: green at the base. `--fast` asks Matrix nothing, and
    `--verify` asks only its own chain."""
    low = _low()
    matrix.probe = _chain(_b6("USD999.00"))
    matrix.chain = _chain(_row_solution("DL-1", _price(low), low))
    matrix.details = {"DL-1": _details_of(low)}
    gf_session(_served())
    result = _run("-n", "10", flag)
    assert result.exit_code == 0, result.output
    assert _LINE not in result.stdout
    assert "Asking Matrix for row" not in result.stderr
    if flag == "--fast":
        assert matrix.bodies == []
    else:
        assert [b["inputs"]["page"]["size"] for b in matrix.searches()] == [
            SearchOptions().page_size
        ]
        assert _routed(matrix) == matrix.searches()
