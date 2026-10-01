# pyright: reportPrivateUsage=false
"""On auto without `--fast`, `flight search --format json` answers whenever the
table answers.

The default table is a weave: Google and Matrix run side by side, and a Google
wall leaves Matrix's half to answer. JSON asks Google alone first, so a wall
there hands the whole search to Matrix and says so on stderr, as
`Using Matrix: <reason>.` An empty Google board is an answer rather than a wall,
and with awards on it still runs the providers, because the award renderer is
what writes an awards run's document.

The document a caller parses has one shape per flag set, whichever backend
answered, except cash-only, where the shape names the backend:

- `--cash-only`: Google's rows, a list; or Matrix's raw object.
- awards on: `[{leg, slice_index, matches}]`.
- `--awards-only`: `[{leg, slice_index, awards}]`.

`--fast` means Google alone, and `--backend gflight`, `--bags` and `--sellers`
ask for an answer only Google gives, so each keeps exit 1 and an empty stdout on
a wall. Their bytes are pinned as literals, so a change to any of them shows here
as a diff rather than agreeing with itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli._gf_errors import (
    GfBrowserUnavailableError,
    GfConsentError,
    GfThrottledError,
    GfTransportError,
)
from flight_cli._gflight_ids import Board
from flight_cli.client import MatrixApiError
from flight_cli.models import SearchResult
from flight_cli.pp import cli as pp_cli
from flight_cli.providers.base import AwardFlight, LegQuery
from test_json_document import _one_document, _one_gf_row

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_DEP = date.today() + timedelta(days=45)
_MATRIX_BODY: dict[str, Any] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json"
    ).read_text()
)
_EMPTY_MATRIX_BODY: dict[str, Any] = {"solutionList": {"solutions": []}, "solutionCount": 0}


@dataclass
class _Arms:
    """What the stubs below answer, and how often each was asked."""

    matrix_body: dict[str, Any] = field(default_factory=lambda: _MATRIX_BODY)
    matrix_error: MatrixApiError | None = None
    matrix_calls: int = 0
    provider_calls: int = 0


@pytest.fixture(autouse=True)
def arms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Arms:
    """Matrix, the award providers and the award decision answered in process.

    `MatrixClient` rather than `_run`, so the weave's Matrix task and the Matrix
    path read one answer, and `_run`'s own failure handling is the real one.
    The award renderer is real too: its two serializers are the shapes under
    test, so only the provider fan-out and the token check beneath it are
    replaced. `COLUMNS` is wide so rich wraps no line the literals below pin."""
    state = _Arms()
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))

    class _Matrix:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _Matrix:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, _search: object, **_kw: object) -> SearchResult:
            state.matrix_calls += 1
            if state.matrix_error is not None:
                raise state.matrix_error
            return SearchResult.from_api(state.matrix_body)

    async def _gather(
        *, legs: list[LegQuery], **_kw: object
    ) -> tuple[list[list[AwardFlight]], list[Any]]:
        state.provider_calls += 1
        return ([[_award(lg)] for lg in legs], [])

    def _awards_run(sel: Any) -> bool:
        return not cast("bool", sel.cash_only)

    monkeypatch.setattr(cli, "MatrixClient", _Matrix)
    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "get_valid_tokens", lambda: None)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_run)
    return state


def _award(leg: LegQuery) -> AwardFlight:
    return AwardFlight(
        origin=leg.origin,
        destination=leg.destination,
        departure=f"{leg.date}T08:00:00",
        arrival=f"{leg.date}T11:20:00",
        flight_number="AA100",
        provider="PointsPath",
        program="American Airlines",
    )


def _google(monkeypatch: pytest.MonkeyPatch, outcome: Callable[[], Board[Any]]) -> None:
    def _results(*_a: object, **_kw: object) -> Board[Any]:
        return outcome()

    monkeypatch.setattr(cli, "_gflight_results", _results)


def _raising(make: Callable[[], Exception]) -> Callable[[], Board[Any]]:
    # A fresh exception per call: a raised instance keeps the traceback of every
    # raise it has been through.
    def outcome() -> Board[Any]:
        raise make()

    return outcome


def _search(*extra: str, command: str = "search") -> Result:
    return CliRunner().invoke(
        cli.app, [command, "JFK", "LHR", "--dep", _DEP.isoformat(), "-n", "2", *extra]
    )


def _hand_off_lines(r: Result) -> list[str]:
    return [ln for ln in r.stderr.splitlines() if "Using Matrix" in ln]


# Each wall, and the reason the hand-off line gives for it: the words the
# merged table prints beside Matrix's answer for the same wall.
_WALLS: dict[str, tuple[Callable[[], Exception], str]] = {
    "throttle": (
        lambda: GfThrottledError("Google Flights rate-limited the request (/sorry/)"),
        "Google Flights rate-limited",
    ),
    "consent": (lambda: GfConsentError("consent page"), "Google Flights served its consent page"),
    "browser": (
        lambda: GfBrowserUnavailableError("Chrome could not start: no such file."),
        "Google Flights' browser rung is unavailable — Chrome could not start: no such "
        "file. Retry, or use `--gf-transport http` (or `--backend matrix`)",
    ),
    "transport": (
        lambda: GfTransportError("Google Flights could not be reached: connection reset"),
        "Google Flights was unreachable",
    ),
    "untyped": (lambda: RuntimeError("boom"), "Google Flights query failed: boom"),
}


# ── A wall hands auto JSON to Matrix ────────────────────────────────────────


@pytest.mark.parametrize("wall", list(_WALLS))
def test_a_wall_hands_auto_json_to_matrix(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms, wall: str
) -> None:
    make, reason = _WALLS[wall]
    _google(monkeypatch, _raising(make))

    r = _search("--cash-only", "--format", "json")

    assert r.exit_code == 0, r.output
    assert _one_document(r.stdout) == _MATRIX_BODY
    assert _hand_off_lines(r) == [f"Using Matrix: {reason}."], r.stderr
    assert arms.matrix_calls == 1


@pytest.mark.parametrize(
    ("wall", "line"),
    [
        pytest.param(
            lambda: GfBrowserUnavailableError("Chrome said [/x] no.", remedy="Then do [/y]."),
            "Using Matrix: Google Flights' browser rung is unavailable — Chrome said [/x] "
            "no. Then do [/y].",
            id="refusal-note",
        ),
        pytest.param(
            lambda: RuntimeError("bad [/x] thing."),
            "Using Matrix: Google Flights query failed: bad [/x] thing.",
            id="untyped",
        ),
    ],
)
def test_the_hand_off_line_escapes_remote_text_exactly_once(
    monkeypatch: pytest.MonkeyPatch, wall: Callable[[], Exception], line: str
) -> None:
    """What backs `("_run_gflight_path", "note")` in the escape gate's allowlist.

    The note arrives console-ready, so the brackets print as themselves: a
    second escape shows as a backslash, none as a `MarkupError` or a dropped
    tag. The full stop the remote text ends with is the line's own, not a
    second one."""
    _google(monkeypatch, _raising(wall))

    r = _search("--cash-only", "--format", "json")

    assert r.exit_code == 0, r.output
    assert _hand_off_lines(r) == [line], r.stderr
    assert "\\[" not in r.stderr, r.stderr


def test_matrix_failing_after_the_hand_off_ends_as_the_matrix_path_does(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    """The hand-off adds its line and nothing else: Matrix's own failure is
    reported and exits as `--backend matrix` reports and exits it."""
    _google(monkeypatch, _raising(_WALLS["throttle"][0]))
    arms.matrix_error = MatrixApiError("bad request", kind="input")

    handed = _search("--cash-only", "--format", "json")
    direct = _search("--cash-only", "--format", "json", "--backend", "matrix")

    assert (handed.exit_code, handed.stdout) == (1, "")
    assert (direct.exit_code, direct.stdout) == (1, "")
    assert handed.stderr == "Using Matrix: Google Flights rate-limited.\n" + direct.stderr


# ── An empty board still runs the awards ────────────────────────────────────

_AUTO_AND_GFLIGHT = [
    pytest.param([], id="auto"),
    pytest.param(["--backend", "gflight"], id="gflight"),
]
_AWARDS_ON = [
    pytest.param([], id="awards-on"),
    pytest.param(["--awards-only"], id="awards-only"),
]


@pytest.mark.parametrize("awards", _AWARDS_ON)
@pytest.mark.parametrize("backend", _AUTO_AND_GFLIGHT)
def test_an_empty_board_writes_the_document_matrix_empty_answer_writes(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms, backend: list[str], awards: list[str]
) -> None:
    _google(monkeypatch, Board[Any])
    arms.matrix_body = _EMPTY_MATRIX_BODY

    google = _search(*backend, *awards, "--format", "json")
    google_providers, arms.provider_calls = arms.provider_calls, 0
    matrix = _search("--backend", "matrix", *awards, "--format", "json")

    assert google.exit_code == 0, google.output
    assert google_providers == 1
    assert arms.matrix_calls == 1  # the `--backend matrix` run's, and no other
    assert _one_document(google.stdout) == _one_document(matrix.stdout)


@pytest.mark.parametrize("backend", _AUTO_AND_GFLIGHT)
def test_an_empty_board_cash_only_is_still_an_empty_list(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms, backend: list[str]
) -> None:
    _google(monkeypatch, Board[Any])

    r = _search(*backend, "--cash-only", "--format", "json")

    assert (r.exit_code, r.stdout) == (0, "[]"), r.output
    assert (arms.matrix_calls, arms.provider_calls) == (0, 0)


def test_an_empty_board_table_keeps_its_line_then_prints_the_awards(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    _google(monkeypatch, Board[Any])

    r = _search("--fast")

    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("Google Flights: no results.\n"), r.stdout
    assert "Leg: one-way JFK→LHR" in r.stdout, r.stdout
    assert arms.provider_calls == 1


def test_an_empty_board_awards_only_table_prints_no_cash_line(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms
) -> None:
    """The row filter's reason stays on stderr; stdout is the awards alone."""
    _google(monkeypatch, lambda: Board[Any]((), dropped=3))

    r = _search("--fast", "--backend", "gflight", "--awards-only")

    assert r.exit_code == 0, r.output
    assert "Google Flights" not in r.stdout, r.stdout
    assert "Leg: one-way JFK→LHR" in r.stdout, r.stdout
    assert "Google Flights: no itinerary matched" in r.stderr, r.stderr
    assert arms.provider_calls == 1


# ── The shapes, named ───────────────────────────────────────────────────────

_GOOGLE: dict[str, Callable[[], Board[Any]]] = {
    "answers": lambda: Board[Any]([_one_gf_row()]),
    "empty": Board[Any],
    "emptied": lambda: Board[Any]((), dropped=4),
    **{wall: _raising(make) for wall, (make, _) in _WALLS.items()},
}
_FLAGS = {"cash-only": ["--cash-only"], "awards-on": [], "awards-only": ["--awards-only"]}


def _shape(doc: Any) -> str:
    """Which of the four documents `doc` is, or an assertion saying why none."""
    if isinstance(doc, dict):
        assert {"solutionList", "solutionCount"} <= cast("dict[str, Any]", doc).keys(), doc
        return "matrix"
    assert isinstance(doc, list), doc
    items = cast("list[Any]", doc)
    if all(isinstance(it, dict) and "flight_id" in it for it in items):
        return "google"
    assert len(items) == 1, items  # one leg
    keys = set(cast("dict[str, Any]", items[0]))
    assert keys in ({"leg", "slice_index", "matches"}, {"leg", "slice_index", "awards"}), keys
    return "matches" if "matches" in keys else "awards"


@pytest.mark.parametrize("google", [*_GOOGLE, "matrix-only-flag"])
@pytest.mark.parametrize("flags", list(_FLAGS))
def test_auto_json_is_one_document_of_the_shape_the_flags_name(
    monkeypatch: pytest.MonkeyPatch, flags: str, google: str
) -> None:
    """A cash-only list is Google's and an object Matrix's; with awards on the
    shape is the flags' alone, whichever backend answered."""
    _google(monkeypatch, _GOOGLE.get(google, _GOOGLE["answers"]))
    extra = ["--seniors", "1"] if google == "matrix-only-flag" else []

    r = _search(*_FLAGS[flags], *extra, "--format", "json")

    assert r.exit_code == 0, r.output
    want = {"awards-on": "matches", "awards-only": "awards"}.get(flags) or (
        "google" if google in {"answers", "empty"} else "matrix"
    )
    assert _shape(_one_document(r.stdout)) == want, r.stdout


# ── What stays ──────────────────────────────────────────────────────────────

_STAYS = {
    "fast-json": ["--fast", "--format", "json"],
    "fast-table": ["--fast"],
    "gflight-json": ["--backend", "gflight", "--format", "json"],
    "bags-json": ["--bags", "1", "--format", "json"],
    "sellers-json": ["--sellers", "--format", "json"],
}
_THROTTLE_MESSAGE = (
    "Google Flights rate-limited the request. Wait a moment and retry, use "
    "--gf-transport browser, or use --backend matrix.\n"
)
_DEPRECATED = (
    "DeprecationWarning: The command 'gflight' is deprecated.\n"
    "`flight gflight` is deprecated; use `flight search` (or `flight search "
    "--backend gflight` to force).\n"
)
# (exit code, stdout, stderr) on each surface that asks Google alone, for one
# typed wall and one untyped failure.
_STAYED: dict[tuple[str, str], tuple[int, str, str]] = {
    ("fast-json", "throttle"): (1, "", _THROTTLE_MESSAGE),
    ("fast-json", "untyped"): (1, "", "Google Flights query failed: boom\n"),
    ("fast-table", "throttle"): (1, "", _THROTTLE_MESSAGE),
    ("fast-table", "untyped"): (1, "", "Google Flights query failed: boom\n"),
    ("gflight-json", "throttle"): (1, "", _THROTTLE_MESSAGE),
    ("gflight-json", "untyped"): (1, "", "Google Flights query failed: boom\n"),
    ("bags-json", "throttle"): (
        1,
        "",
        "Google Flights rate-limited the request. Wait a moment and retry, use "
        "--gf-transport browser, or drop --bags to search Matrix, which prices no bags.\n",
    ),
    ("bags-json", "untyped"): (1, "", "Google Flights query failed: boom\n"),
    ("sellers-json", "throttle"): (1, "", _THROTTLE_MESSAGE),
    ("sellers-json", "untyped"): (1, "", "Google Flights query failed: boom\n"),
    ("gflight-command", "throttle"): (1, "", _DEPRECATED + _THROTTLE_MESSAGE),
    ("gflight-command", "untyped"): (
        1,
        "",
        _DEPRECATED + "Google Flights query failed: boom\n",
    ),
}


@pytest.mark.parametrize("wall", ["throttle", "untyped"])
@pytest.mark.parametrize("surface", [*_STAYS, "gflight-command"])
def test_a_wall_where_google_alone_was_asked_for_still_exits_1(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms, surface: str, wall: str
) -> None:
    _google(monkeypatch, _raising(_WALLS[wall][0]))

    if surface == "gflight-command":
        r = _search("--format", "json", command="gflight")
    else:
        r = _search("--cash-only", *_STAYS[surface])

    assert (r.exit_code, r.stdout, r.stderr) == _STAYED[surface, wall]
    assert arms.matrix_calls == 0


@pytest.mark.parametrize(
    ("wall", "stream", "note"),
    [
        ("throttle", "stdout", "Google Flights rate-limited — showing Matrix only.\n"),
        ("untyped", "stderr", "Google Flights query failed: boom\n"),
    ],
)
def test_the_default_table_still_weaves_a_wall_without_a_hand_off(
    monkeypatch: pytest.MonkeyPatch, arms: _Arms, wall: str, stream: str, note: str
) -> None:
    """The merged table names the wall beside Matrix's answer itself; the
    hand-off line is JSON's alone."""
    _google(monkeypatch, _raising(_WALLS[wall][0]))

    r = _search("--cash-only")

    assert r.exit_code == 0, r.output
    assert getattr(r, stream).startswith(note), r.output
    assert "Using Matrix" not in r.output, r.output
    assert arms.matrix_calls == 1


@pytest.mark.parametrize("wall", ["throttle", "untyped"])
def test_multi_cabin_json_still_exits_1_when_every_google_cabin_fails(
    monkeypatch: pytest.MonkeyPatch, wall: str
) -> None:
    _google(monkeypatch, _raising(_WALLS[wall][0]))

    r = _search("--cash-only", "--cabin", "y,j", "--format", "json")

    assert (r.exit_code, r.stdout) == (1, ""), r.output
    assert "Using Matrix" not in r.stderr, r.stderr
