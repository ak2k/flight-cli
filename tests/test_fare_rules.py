# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_http_cache.py established; the constructor has no
# transport injection point.
"""`search --fare-rules`: the fare basis, booking codes and fare rules of one
itinerary, fetched from Matrix's `/v1/summarize` with the search's session.

Driven through the real client, wire bodies and response models against the
bodies Matrix sent for a JFK-LHR round trip: one search, one booking-details
call, and one fare-rules call per fare key — two here, on different bases."""

from __future__ import annotations

import json
import pathlib
from datetime import date, timedelta
from typing import Any, cast

import httpx
import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.client import MatrixClient
from flight_cli.models import FareRule

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_SEARCH = ["search", "JFK", "LHR", "--dep", _DEP.isoformat(), "--return", _RET.isoformat()]
_QUIET = ["--cash-only", "--no-matrix-url", "--no-google-url"]


def _fixture(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((FIXTURES / name).read_text()))


class _Matrix:
    """Matrix over a MockTransport: records each body and answers from the
    captured round trip, or with `summarize` when a test overrides it."""

    def __init__(self, *, search: dict[str, Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.search = search or _fixture("matrix_currency/specific_jfk_lhr_rt_gbp_resp.json")
        self.summarize: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        self.bodies.append(body)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json=self.search)
        if self.summarize is not None:
            return httpx.Response(200, json=self.summarize)
        if body["summarizerSet"] == "viewDetails":
            return httpx.Response(
                200, json=_fixture("summarize/booking_details_jfk_lhr_rt_gbp.json")
            )
        key = body["inputs"]["fareKeys"].replace("/", "_")
        return httpx.Response(200, json=_fixture(f"summarize/fare_rules_jfk_lhr_rt_{key}.json"))

    def summarize_bodies(self) -> list[dict[str, Any]]:
        return [b for b in self.bodies if "summarizerSet" in b and "name" not in b]


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> _Matrix:
    fake = _Matrix()

    def _client(**kw: Any) -> MatrixClient:
        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            rps=1000.0,
            **{k: v for k, v in kw.items() if k == "impersonate"},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        return c

    monkeypatch.setattr(cli, "MatrixClient", _client)
    return fake


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli.app, [*_SEARCH, *args])


# ───────────────────────────── the table path ───────────────────────────────


def test_the_rules_of_every_fare_print_after_the_table(matrix: _Matrix) -> None:
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Using Matrix: Google Flights can't serve fare rules." in result.stderr
    assert out.index("Itineraries") < out.index("Fare rules")
    # Per segment: fare basis, booking code, cabin; then the fare calculation.
    assert "JFK→LHR  AA  fare basis OLN0T0BV  booking code B  COACH" in out
    assert "LHR→JFK  AA  fare basis OLN0T1BV  booking code B  COACH" in out
    assert "Fare calculation: NYC AA LON M 0.50OLN0T0BV" in out
    # Each fare: identity and title, then penalties, changes and refunds.
    assert "AA OLN0T0BV  NYC→LON  BASIC SEASON ECONOMY RT UNBUNDLED FARES B" in out
    assert "AA OLN0T1BV  LON→NYC" in out
    for heading in ("Penalties", "Voluntary changes", "Voluntary refunds"):
        assert out.count(heading) == 2, heading
    assert "TICKET IS NON-REFUNDABLE." in out
    assert "CHANGES NOT PERMITTED." in out
    # The NOTE asides are dropped from the table; JSON keeps them.
    assert "DEATH OF THE PASSENGER" not in out
    assert "This ticket is non-refundable." in out.split("Notes", 1)[1]

    kinds = [(b["summarizerSet"], b["inputs"].get("fareKeys")) for b in matrix.summarize_bodies()]
    assert kinds == [("viewDetails", None), ("viewRules", "0/0"), ("viewRules", "0/1")]
    first = matrix.search["solutionList"]["solutions"][0]["id"]
    assert all(
        b["session"] == matrix.search["session"]
        and b["inputs"]["solution"] == f"{matrix.search['solutionSet']}/{first}"
        for b in matrix.summarize_bodies()
    )


def test_the_search_is_not_served_from_the_cache(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summarize reads the search's session, which a cached answer may no
    longer have."""
    real = cli._run
    no_cache: list[bool] = []

    def _spy(search: Any, rps: float, impersonate: str, nc: bool) -> Any:
        no_cache.append(nc)
        return real(search, rps, impersonate, nc)

    monkeypatch.setattr(cli, "_run", _spy)
    assert _run("--fare-rules", *_QUIET).exit_code == 0
    assert no_cache == [True]
    _ = matrix


def test_pick_chooses_the_itinerary(matrix: _Matrix) -> None:
    result = _run("--fare-rules", "--pick", "2", "-n", "3", *_QUIET)
    assert result.exit_code == 0, result.output
    second = matrix.search["solutionList"]["solutions"][1]["id"]
    assert all(b["inputs"]["solution"].endswith(second) for b in matrix.summarize_bodies())
    assert "Fare rules · itinerary #2" in result.stdout


def test_a_pick_off_the_table_says_which_rules_are_shown(matrix: _Matrix) -> None:
    result = _run("--fare-rules", "--pick", "9", "-n", "3", *_QUIET)
    assert result.exit_code == 0, result.output
    assert "--pick 9 is out of range (1-3); showing itinerary #1's fare rules instead." in (
        result.stderr
    )
    assert "Fare rules · itinerary #1" in result.stdout
    _ = matrix


def test_a_summarize_error_fails_the_run_after_the_table(matrix: _Matrix) -> None:
    matrix.summarize = {"error": {"message": "session [/x] expired", "type": "INVALID"}}
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 1
    assert "Itineraries" in result.stdout
    assert "Matrix returned an error (INVALID): session [/x] expired" in result.stderr


def _keyless_details() -> dict[str, Any]:
    details = _fixture("summarize/booking_details_jfk_lhr_rt_gbp.json")
    for ticket in details["bookingDetails"]["tickets"]:
        for pricing in ticket["pricings"]:
            for fare in pricing["fares"]:
                del fare["key"]
    return details


def _answer_details_with(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch, details: dict[str, Any]
) -> None:
    real = matrix.handler

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path != "/v1/search" and body["summarizerSet"] == "viewDetails":
            matrix.bodies.append(body)
            return httpx.Response(200, json=details)
        return real(request)

    monkeypatch.setattr(matrix, "handler", _handler)


def test_a_fare_without_a_rules_key_says_its_rules_are_missing(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fare the booking details name without a key cannot be asked for its
    rules; the block says so for that fare instead of reading as complete."""
    _answer_details_with(matrix, monkeypatch, _keyless_details())
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "fare basis OLN0T0BV" in out
    assert "Matrix returned no rules for fare OLN0T0BV." in out
    assert "Matrix returned no rules for fare OLN0T1BV." in out
    kinds = [b["summarizerSet"] for b in matrix.summarize_bodies()]
    assert kinds == ["viewDetails"]


def test_json_marks_a_fare_without_a_rules_key(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer_details_with(matrix, monkeypatch, _keyless_details())
    result = _run("--fare-rules", "--format", "json", "--cash-only")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["fare_rules"]["rules"] == [None, None]


@pytest.mark.parametrize("details", [{}, {"bookingDetails": {}}])
def test_booking_details_with_no_fares_fail_the_run_after_the_table(
    matrix: _Matrix, details: dict[str, Any]
) -> None:
    matrix.summarize = details
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 1, result.output
    assert "Itineraries" in result.stdout
    assert "Fare rules" not in result.stdout
    assert "Matrix returned no booking details for itinerary #1." in result.stderr


def test_an_empty_search_asks_for_no_rules(matrix: _Matrix) -> None:
    matrix.search = {"solutionCount": 0, "session": "s", "solutionSet": "ss"}
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 0, result.output
    assert "No itinerary to show fare rules for." in result.stderr
    assert matrix.summarize_bodies() == []


# ──────────────────────────────── JSON ──────────────────────────────────────


def test_json_carries_the_search_and_the_whole_rule_bodies(matrix: _Matrix) -> None:
    result = _run("--fare-rules", "--format", "json", "--cash-only")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["search"] == matrix.search
    rules = doc["fare_rules"]
    assert rules["itinerary"] == 1
    details = _fixture("summarize/booking_details_jfk_lhr_rt_gbp.json")
    assert rules["booking_details"] == details["bookingDetails"]
    assert [r["code"] for r in rules["rules"]] == ["OLN0T0BV", "OLN0T1BV"]
    assert "DEATH OF THE PASSENGER" in json.dumps(rules["rules"])


def test_json_of_an_empty_search_has_no_rules(matrix: _Matrix) -> None:
    matrix.search = {"solutionCount": 0}
    result = _run("--fare-rules", "--format", "json", "--cash-only")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"search": matrix.search, "fare_rules": None}


def test_json_without_the_flag_is_the_raw_search(matrix: _Matrix) -> None:
    result = _run("--backend", "matrix", "--format", "json", "--cash-only")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == matrix.search
    assert matrix.summarize_bodies() == []


# ───────────────────────────── refusals ─────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("--cabin", "economy,business", "--cash-only"), "takes one --cabin"),
        (("--awards-only",), "--awards-only prints none"),
        (("--backend", "gflight", "--cash-only"), "fare rules"),
    ],
)
def test_fare_rules_refuse_what_has_no_one_row(
    matrix: _Matrix, args: tuple[str, ...], said: str
) -> None:
    result = _run("--fare-rules", *args)
    assert result.exit_code == 2, result.output
    assert said in result.output
    assert matrix.bodies == []


def test_json_with_awards_on_is_refused_naming_cash_only(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With awards on, the JSON document is the award match, written by the
    award renderer; there is no document to put the rules in."""

    def _awards_on(_sel: object) -> bool:
        return True

    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = _run("--fare-rules", "--format", "json")
    assert result.exit_code == 2
    assert "--cash-only" in result.stderr
    assert matrix.bodies == []


def test_the_backend_picker_sends_fare_rules_to_matrix() -> None:
    kw: dict[str, Any] = {
        "routing": None,
        "extension": None,
        "slice_specs": None,
        "depart_times": None,
        "return_times": None,
        "stops": None,
        "children": 0,
        "seniors": 0,
        "youth": 0,
        "inf_seat": 0,
        "inf_lap": 0,
        "origin": "JFK",
        "destination": "LAX",
        "allow_airport_changes": True,
        "show_only_available": True,
    }
    assert cli._pick_backend(backend="auto", **kw) == "gflight"
    assert cli._pick_backend(backend="auto", fare_rules=True, **kw) == "matrix"
    with pytest.raises(typer.BadParameter, match="fare rules"):
        cli._pick_backend(backend="gflight", fare_rules=True, **kw)


# ─────────────────────────── the rule text ──────────────────────────────────


def test_rule_text_drops_its_note_asides_and_keeps_the_rest() -> None:
    rule = FareRule(
        category=16,
        blocks=[
            "  CANCELLATIONS\n    ANY TIME\n      NON-REFUNDABLE.\n"
            "         NOTE -\n          WAIVED ON DEATH.\n          SEE AA.COM.\n"
            "  CHANGES\n    NOT PERMITTED."
        ],
    )
    lines, cut = cli._rule_lines(rule)
    assert lines == [
        "CANCELLATIONS",
        "  ANY TIME",
        "    NON-REFUNDABLE.",
        "CHANGES",
        "  NOT PERMITTED.",
    ]
    assert cut == 3
    long = FareRule(category=33, blocks=["\n".join(f"LINE {i}" for i in range(40))])
    lines, cut = cli._rule_lines(long)
    assert len(lines) == 40
    assert cut == 0


def test_a_refund_rule_is_printed_to_its_last_alternative(matrix: _Matrix) -> None:
    """Rule text lists alternatives under `OR -`, and a line after one can
    qualify it: the captured refund rule's last alternative is "REFUND MAY BE
    REQUESTED ANYTIME." and then "FARE AND TAXES ARE NONREFUNDABLE."."""
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 0, result.output
    blocks = [s.split("\n\n", 1)[0] for s in result.stdout.split("Voluntary refunds")[1:]]
    assert len(blocks) == 2
    for block in blocks:
        assert "REFUND MAY BE REQUESTED ANYTIME." in block
        assert "FARE AND TAXES ARE NONREFUNDABLE. IF MIX OF PER FARE" in block
        assert block.rstrip().endswith("PRICING UNIT AND COLLECT HIGHEST.")


def test_hostile_rule_text_is_printed_as_text(
    matrix: _Matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every printed field is Matrix's; a markup tag or a terminal control in
    one must neither raise nor style nor reach the terminal."""
    hostile = "[/x]BAD[bold]\x1b[2J"
    rules = _fixture("summarize/fare_rules_jfk_lhr_rt_0_0.json")
    rules["fareRules"]["code"] = hostile
    rules["fareRules"]["ruleSets"][0]["rules"][0]["blocks"] = [hostile]
    details = _fixture("summarize/booking_details_jfk_lhr_rt_gbp.json")
    details["bookingDetails"]["tickets"][0]["pricings"][0]["notes"] = [hostile]

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json=matrix.search)
        return httpx.Response(
            200, json=details if body["summarizerSet"] == "viewDetails" else rules
        )

    monkeypatch.setattr(matrix, "handler", _handler)
    result = _run("--fare-rules", *_QUIET)
    assert result.exit_code == 0, result.output
    assert "[/x]BAD[bold]" in result.stdout
    assert "\x1b" not in result.stdout
