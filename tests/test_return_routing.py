# pyright: reportPrivateUsage=false
"""What a round trip's return slice is asked, on `search`, `calendar` and `detail`.

A slice's routing reads from its own origin, so the outbound's `UA LH` copied
onto the return asks for UA then LH from the far end. The return carries only
what the user meant for it: `--routing-ret`/`--ext-ret` when given (`''` for
none), else the outbound's codes when they read the same both ways. Every
backend runner is replaced by a recorder, so each test sees the legs, the wire
body and the Matrix link a command would have sent, and nothing reaches the
network."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from flight_cli import cli, links, wire
from flight_cli.domain import SpecificDateSearch

if TYPE_CHECKING:
    from click.testing import Result

D, R = "2026-10-20", "2026-10-27"

_SEARCH_PATHS = (
    "_run_matrix_path",
    "_run_gflight_path",
    "_run_enriched_path",
    "_run_gflight_path_multi",
    "_run_matrix_path_multi",
)
_CALENDAR_PATHS = ("_calendar_without_fast", "_run_fast_calendar_grid", "_run_fast_browser_grid")


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every backend runner of the three commands, recording which ran and the
    search it was handed."""
    calls: list[tuple[str, Any]] = []

    def search_path(name: str) -> Any:
        def _path(**kw: Any) -> None:
            calls.append((name, SpecificDateSearch(legs=kw["legs"], options=kw["opts"])))

        return _path

    def calendar_path(name: str) -> Any:
        def _path(search: Any, **_kw: Any) -> None:
            calls.append((name, search))

        return _path

    def detail_run(search: Any, *_a: Any) -> None:
        calls.append(("_run", search))
        raise typer.Exit(0)

    for name in _SEARCH_PATHS:
        monkeypatch.setattr(cli, name, search_path(name))
    for name in _CALENDAR_PATHS:
        monkeypatch.setattr(cli, name, calendar_path(name))
    monkeypatch.setattr(cli, "_run", detail_run)
    return calls


_COMMANDS = {
    "search": ["search", "JFK", "LHR", "--dep", D, "--return", R, "--cash-only"],
    "search-matrix": [
        "search",
        "JFK",
        "LHR",
        "--dep",
        D,
        "--return",
        R,
        "--cash-only",
        "--backend",
        "matrix",
    ],
    "search-gflight": [
        "search",
        "JFK",
        "LHR",
        "--dep",
        D,
        "--return",
        R,
        "--cash-only",
        "--backend",
        "gflight",
    ],
    "calendar": ["calendar", "JFK", "LHR", "--start", D],
    "detail": ["detail", "JFK", "LHR", "--dep", D, "--return", R],
}
_OUTPUT_OFF = ["--no-matrix-url", "--no-google-url"]


def _invoke(command: str, *args: str) -> Result:
    return CliRunner().invoke(cli.app, [*_COMMANDS[command], *_OUTPUT_OFF, *args])


def _flat(text: str) -> str:
    """`text` as one line, without the frame typer draws around an error."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _slices(search: Any) -> list[dict[str, Any]]:
    return wire.to_wire(search).as_json()["inputs"]["slices"]


def _link_slice(search: Any) -> dict[str, Any]:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(links.matrix_deep_link(search)).query)
    return json.loads(base64.b64decode(query["search"][0]))["slices"][0]


# ───────────────────── a routing that depends on direction ─────────────────────


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("routing", ["UA LH", "DL747"])
def test_a_direction_dependent_routing_is_refused_without_routing_ret(
    ran: list[tuple[str, Any]], command: str, routing: str
) -> None:
    """RED at base: every command copied the routing onto the return (or, on
    `--backend gflight` for 'UA LH', refused it as not Google's to serve)."""
    result = _invoke(command, "--routing", routing)
    assert result.exit_code == 2, result.output
    err = _flat(result.stderr)
    assert "--routing-ret" in err
    assert f"--routing {routing!r}" in err
    assert "Using Matrix" not in err
    assert result.stdout == ""
    assert ran == []


def test_the_refusal_offers_the_mirror_of_a_chain_and_nothing_on_the_return(
    ran: list[tuple[str, Any]],
) -> None:
    """RED at base."""
    err = _flat(_invoke("search", "--routing", "UA LH").stderr)
    assert err == (
        "--routing 'UA LH' is an ordered chain, which the return would fly in the "
        "outbound's order. Give the return its own: --routing-ret 'LH UA' to fly it in "
        "reverse, or --routing-ret '' for no routing on the return."
    )
    assert ran == []


@pytest.mark.parametrize("routing", ["DL747", "AA25 UA814"])
def test_a_flight_number_is_offered_the_return_flight_not_a_mirror(
    ran: list[tuple[str, Any]], routing: str
) -> None:
    """RED at base. `AA25 UA814` reversed names flights that do not fly back."""
    err = _flat(_invoke("search", "--routing", routing).stderr)
    assert "--routing-ret with the return flight's number" in err
    assert "--routing-ret '' for no routing on the return" in err
    assert "in reverse" not in err
    assert ran == []


def test_the_refusal_escapes_the_routing_it_quotes(ran: list[tuple[str, Any]]) -> None:
    """RED at base. A bracket in the user's own text must print as text: one
    reaching the console unescaped is a style tag, or a MarkupError."""
    result = _invoke("search", "--backend", "matrix", "--routing", "BA[/x] AA")
    assert result.exit_code == 2, result.output
    assert "--routing 'BA[/x] AA'" in _flat(result.stderr)
    assert ran == []


# ───────────────────── the return's own codes ──────────────────────────────


@pytest.mark.parametrize("command", ["search", "calendar", "detail"])
def test_routing_ret_reaches_the_return_slice_and_the_link(
    ran: list[tuple[str, Any]], command: str
) -> None:
    """RED at base on search, which had no --routing-ret. Green at base on
    calendar and detail, whose `routing_return or routing` took a non-empty one."""
    result = _invoke(command, "--routing", "UA LH", "--routing-ret", "LH UA")
    assert result.exit_code == 0, result.output
    [(_, search)] = ran
    assert [s.get("routeLanguage") for s in _slices(search)] == ["UA LH", "LH UA"]
    if command != "calendar":
        assert _link_slice(search)["routingRet"] == "LH UA"


@pytest.mark.parametrize("command", ["search", "calendar", "detail"])
def test_ext_ret_reaches_the_return_slice_only(ran: list[tuple[str, Any]], command: str) -> None:
    """RED at base on search; green at base on calendar and detail."""
    result = _invoke(command, "--ext-ret", "MAXSTOPS 0")
    assert result.exit_code == 0, result.output
    [(_, search)] = ran
    assert [s.get("commandLine") for s in _slices(search)] == [None, "MAXSTOPS 0"]


@pytest.mark.parametrize("command", ["search", "calendar", "detail"])
def test_an_empty_routing_ret_leaves_the_return_unconstrained(
    ran: list[tuple[str, Any]], command: str
) -> None:
    """RED at base: `''` is falsy, so calendar and detail copied the outbound's
    routing, and search had no flag. A return slice with no `routeLanguage` is
    unconstrained (live, 2026-10-01)."""
    result = _invoke(command, "--routing", "UA LH", "--routing-ret", "")
    assert result.exit_code == 0, result.output
    [(_, search)] = ran
    slices = _slices(search)
    assert slices[0]["routeLanguage"] == "UA LH"
    assert "routeLanguage" not in slices[1]


@pytest.mark.parametrize("command", ["search", "calendar", "detail"])
def test_an_empty_ext_ret_drops_the_outbound_codes_from_the_return(
    ran: list[tuple[str, Any]], command: str
) -> None:
    """RED at base, for the same falsy `''` (and no flag on search)."""
    result = _invoke(command, "--ext", "MAXSTOPS 1", "--ext-ret", "")
    assert result.exit_code == 0, result.output
    [(_, search)] = ran
    assert [s.get("commandLine") for s in _slices(search)] == ["MAXSTOPS 1", None]


def test_the_return_codes_on_a_one_way_search_name_return(ran: list[tuple[str, Any]]) -> None:
    """RED at base (no such option)."""
    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LHR", "--dep", D, "--cash-only", "--routing-ret", "LH UA", *_OUTPUT_OFF],
    )
    assert result.exit_code == 2, result.output
    assert "need a --return" in _flat(result.stderr)
    assert "Drop them, or add --return" in _flat(result.stderr)
    assert ran == []


def test_the_return_codes_beside_a_slice_name_its_own_fields(
    ran: list[tuple[str, Any]],
) -> None:
    """RED at base (no such option)."""
    result = CliRunner().invoke(
        cli.app,
        ["search", "--slice", f"JFK-LHR:{D}", "--slice", f"LHR-JFK:{R}", "--ext-ret", "MAXSTOPS 0"],
    )
    assert result.exit_code == 2, result.output
    assert "its r= and e= fields" in _flat(result.stderr)
    assert ran == []


def test_a_return_with_codes_of_its_own_goes_to_matrix_with_the_reason(
    ran: list[tuple[str, Any]],
) -> None:
    """RED at base (no such option). Google's page writes one filter set on every
    slice, so it cannot ask the return a different question."""
    result = _invoke("search", "--routing", "AA+", "--routing-ret", "~BA+")
    assert result.exit_code == 0, result.output
    assert "different routing or extension codes on the outbound and the return" in _flat(
        result.stderr
    )
    [(name, _)] = ran
    assert name == "_run_matrix_path"


# ───────────────────── controls: what the base sent ───────────────────────────


def test_a_one_way_chain_is_sent_as_before(ran: list[tuple[str, Any]]) -> None:
    """Green at base and tip."""
    result = CliRunner().invoke(
        cli.app,
        ["search", "JFK", "LHR", "--dep", D, "--cash-only", "--routing", "BA AA", *_OUTPUT_OFF],
    )
    assert result.exit_code == 0, result.output
    assert _flat(result.stderr) == (
        "Using Matrix: Google Flights can't serve routing 'BA AA' not GF-expressible."
    )
    [(name, search)] = ran
    assert name == "_run_matrix_path"
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in _slices(search)] == [
        ("BA AA", None)
    ]
    assert _link_slice(search)["routing"] == "BA AA"


@pytest.mark.parametrize(
    ("args", "codes", "path", "stderr"),
    [
        (
            ["--routing", "AA+", "--ext", "MAXSTOPS 1"],
            ("AA+", "MAXSTOPS 1"),
            "_run_enriched_path",
            "",
        ),
        (
            ["--routing", "AA+", "--ext", "MAXSTOPS 1", "--fast"],
            ("AA+", "MAXSTOPS 1"),
            "_run_gflight_path",
            "",
        ),
        (
            ["--routing", "F* X:LHR F*"],
            ("F* X:LHR F*", None),
            "_run_matrix_path",
            "Using Matrix: Google Flights can't serve a connecting-airport filter (LHR).",
        ),
    ],
)
def test_a_routing_that_reads_the_same_both_ways_is_copied_as_before(
    ran: list[tuple[str, Any]],
    args: list[str],
    codes: tuple[str, str | None],
    path: str,
    stderr: str,
) -> None:
    """Green at base and tip: the same legs, wire slices, link, backend and
    stderr; equal legs add no reason of their own."""
    result = _invoke("search", *args)
    assert result.exit_code == 0, result.output
    assert _flat(result.stderr) == stderr
    [(name, search)] = ran
    assert name == path
    routing, extension = codes
    assert [(s.get("routeLanguage"), s.get("commandLine")) for s in _slices(search)] == [
        codes,
        codes,
    ]
    assert {k: _link_slice(search)[k] for k in ("routing", "ext", "routingRet", "extRet")} == {
        "routing": routing,
        "ext": extension or "",
        "routingRet": routing,
        "extRet": extension or "",
    }


def test_a_symmetric_routing_on_calendar_and_detail_is_copied_as_before(
    ran: list[tuple[str, Any]],
) -> None:
    """Green at base and tip."""
    for command in ("calendar", "detail"):
        result = _invoke(command, "--routing", "AA+", "--ext", "MAXSTOPS 1")
        assert result.exit_code == 0, result.output
    assert [
        [(s.get("routeLanguage"), s.get("commandLine")) for s in _slices(search)]
        for _, search in ran
    ] == [[("AA+", "MAXSTOPS 1")] * 2] * 2


def test_slices_carry_their_own_codes_as_before(ran: list[tuple[str, Any]]) -> None:
    """Green at base and tip: a --slice's r= is that slice's, chain or not."""
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "--slice",
            f"JFK-LHR:{D}:r=UA LH",
            "--slice",
            f"LHR-JFK:{R}:r=UA LH",
            "--cash-only",
            *_OUTPUT_OFF,
        ],
    )
    assert result.exit_code == 0, result.output
    [(name, search)] = ran
    assert name == "_run_matrix_path"
    assert [s.get("routeLanguage") for s in _slices(search)] == ["UA LH", "UA LH"]
