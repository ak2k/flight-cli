# pyright: reportPrivateUsage=false
"""`--format json` puts exactly ONE JSON document on stdout, on every arm.

A document with a table drawn above it, or a deep link printed after it, is not
a document: the consumer that asked for one cannot parse any of it, and the exit
code says the command succeeded. So every human surface on a `--format json` run
— the rendered table, the URL lines, a retry warning from the transport
underneath — has to stand aside or take stderr.

The guard is at the CALL SITES rather than inside the renderers, because
`_emit_urls` is shared text that cannot know which format asked for it. That is
the right shape and it is also the fragile one: a call site is easy to add and
the guard is easy to leave off, and nothing about a missing one is visible in a
green suite. This file drives the arms instead, over the axes that decide which
surface runs — the backend, whether awards run, and whether one cabin was asked
for or several.

Awards ON with `--awards-only` OFF is the arm that matters most and the easiest
to miss: `--awards-only` suppresses the table by itself, so a probe that only
ever passes it reads clean while the gate it was meant to test is gone.

Every assertion here parses stdout and then checks for LEFTOVER BYTES. A table
printed above the document shows up as a parse failure; a URL line printed after
it parses fine and is caught only by the leftovers.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import stamina
from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.models import SearchResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from flight_cli.domain import Cabin

# fli's validator rejects a past travel date, so this is derived: a literal
# rots the suite on the day it passes.
_DEP = date.today() + timedelta(days=45)
_RET = _DEP + timedelta(days=7)
_AWARD_DOCUMENT: dict[str, list[Any]] = {"legs": [], "matches": []}
# Captured before the fixture below replaces it, so the one test that wants
# the real transport underneath can put it back.
_REAL_RUN = cli._run


def _matrix_result() -> SearchResult:
    """A Matrix answer with one solution and a `raw` body to serialise."""
    return SearchResult.model_validate(
        {"solutions": [{"displayTotal": "USD500.00"}], "solutionCount": 1, "raw": {"solutions": 1}}
    )


def _one_gf_row() -> Any:
    """A real `GFlightWithId`, parsed from the committed capture.

    Parsed rather than constructed: the renderers read fields fli's own decoders
    fill in, and a hand-built row agrees with them only until one of them moves.
    """
    import json as _json
    import pathlib as _pathlib

    from flight_cli import _gflight_ids as gfid

    fixture = (
        _pathlib.Path(__file__).parent / "fixtures" / "gflight_page" / "ds1_jfk_lax_3rows.json"
    )
    rows = gfid._rows_from_ds1(_json.loads(fixture.read_text())).rows
    return gfid._parse_flight_with_id(rows[0])


@pytest.fixture(autouse=True)
def _hermetic_arms(  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Both backends and the award fan-out answered in process.

    `_should_run_awards` is decided here too. Its real answer depends on which
    provider credentials the MACHINE has, so left alone it would make "awards
    on" a property of the developer's laptop — and the arm this file exists for
    is precisely the one where awards run."""
    row = _one_gf_row()

    def _matrix(*_a: object, **_kw: object) -> SearchResult:
        return _matrix_result()

    def _matrix_by_cabin(**kw: Any) -> dict[Cabin, SearchResult]:
        return {c: _matrix_result() for c in cast("tuple[Cabin, ...]", kw["cabins"])}

    def _gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return [row]

    def _gf_by_cabin(**kw: Any) -> dict[Cabin, list[Any]]:
        return {c: [row] for c in cast("tuple[Cabin, ...]", kw["cabins"])}

    def _awards(_sr: object, *, json_out: bool = False, **_kw: object) -> None:
        # The award renderer owns stdout when it runs, and under `--format json`
        # what it owes is the document — which is why every surface ABOVE it has
        # to stand aside rather than print first.
        if json_out:
            sys.stdout.write(json.dumps(_AWARD_DOCUMENT))

    def _awards_run(sel: Any) -> bool:
        return not cast("bool", sel.cash_only)

    monkeypatch.setattr(cli, "_run", _matrix)
    monkeypatch.setattr(cli, "_run_matrix_multi", _matrix_by_cabin)
    monkeypatch.setattr(cli, "_gflight_results", _gf_rows)
    monkeypatch.setattr(cli, "_run_gflight_multi", _gf_by_cabin)
    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_run)
    yield


def _one_document(stdout: str) -> Any:
    """The single JSON value on stdout, or an assertion naming what else is
    there.

    `raw_decode` AND the leftover assertion below, which are one check in two
    halves. `raw_decode` is the blind one on its own: it stops at the end of the
    first complete value and reports success, whatever follows it. `json.loads`
    would refuse a trailing deep link by itself, with "Extra data" and a
    character offset — a true failure that names none of the bytes. Taking the
    rest of the stream back is what lets the assertion print the prose that was
    printed beside the document, which is the thing a reader has to see. A table
    printed ABOVE it is a parse failure either way."""
    assert stdout, "a run that asked for a document got zero bytes"
    value, end = json.JSONDecoder().raw_decode(stdout)
    leftover = stdout[end:].strip()
    assert not leftover, f"{len(leftover)} bytes of prose beside the document: {leftover[:200]!r}"
    return value


_BACKENDS = [
    pytest.param(["--backend", "matrix"], id="matrix"),
    pytest.param(["--backend", "gflight"], id="gflight"),
]
_AWARD_MODES = [
    pytest.param([], id="awards-on"),
    pytest.param(["--cash-only"], id="cash-only"),
    pytest.param(["--awards-only"], id="awards-only"),
]
_CABINS = [
    pytest.param([], id="one-cabin"),
    pytest.param(["--cabin", "y,j"], id="multi-cabin"),
]


@pytest.mark.parametrize("cabins", _CABINS)
@pytest.mark.parametrize("awards", _AWARD_MODES)
@pytest.mark.parametrize("backend", _BACKENDS)
def test_every_arm_puts_one_json_document_on_stdout_and_nothing_else(
    backend: list[str], awards: list[str], cabins: list[str]
) -> None:
    """Twelve arms, one rule.

    Driven through the real command rather than the path functions, because
    "which surfaces run" is decided by the dispatch and a call to one path
    function pins one call site. The links are asked for explicitly: `_emit_urls`
    is the surface that prints AFTER the document, so a run that emitted no link
    at all would satisfy this without testing anything."""
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "-n",
            "3",
            "--format",
            "json",
            "--matrix-url",
            "--google-url",
            *backend,
            *awards,
            *cabins,
        ],
    )

    assert result.exit_code == 0, result.output
    _one_document(result.stdout)


@pytest.mark.parametrize("cabins", _CABINS)
@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_table_gate_is_the_one_awards_leave_standing(
    backend: list[str], cabins: list[str]
) -> None:
    """The arm a wrong probe hides.

    With awards on and `--awards-only` off, the early return above the table is
    not taken — the document is written further down by the award renderer — so
    the table gate is the only thing between a rendered table and the consumer's
    stdout. Under `--awards-only` the table is suppressed by its own term and
    the gate could be missing entirely without a symptom, which is why the
    surrounding grid is not enough on its own."""
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "-n",
            "3",
            "--format",
            "json",
            "--matrix-url",
            "--google-url",
            *backend,
            *cabins,
        ],
    )

    assert result.exit_code == 0, result.output
    assert _one_document(result.stdout) == _AWARD_DOCUMENT, result.stdout


def test_the_detail_command_puts_one_json_document_on_stdout() -> None:
    """`detail` is the other command with a `--format json` arm and its own
    `_emit_urls` call site below it.

    Nothing else in this file drives it, and nothing else drives its stdout in
    any format: the guard above its URL lines is an early `return`, which is
    exactly the kind of call site the module docstring calls easy to leave off.
    Both URL flags are asked for, so a missing guard prints two link lines after
    the document rather than nothing at all."""
    result = CliRunner().invoke(
        cli.app,
        [
            "detail",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--format",
            "json",
            "--matrix-url",
            "--google-url",
        ],
    )

    assert result.exit_code == 0, result.output
    _one_document(result.stdout)


def test_a_row_google_did_not_price_still_leaves_one_document(
    monkeypatch: pytest.MonkeyPatch,
    gf_rows: Callable[..., list[Any]],
) -> None:
    """The document is what a run that asked for one owes, priced rows or not.

    An unpriced row reaches the document through the same trim and sort every
    other row does, and the sort is the part with no answer for it: a key that
    read the absence as a number ended the command with a bare traceback and an
    empty stdout, which is the one outcome the whole of this file is about.

    `price: null` is written by fli's own `model_dump`, so nothing here adds a
    branch for it — what is under test is that the run REACHES the dump."""
    board = gf_rows("ds1_metadata_blocks_kept.json", unpriced=1)
    combinations = [(board[0], board[0]), (board[0], board[1])]

    def _gf(*_a: object, **_kw: object) -> list[Any]:
        return combinations

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--return",
            _RET.isoformat(),
            "-n",
            "3",
            "--format",
            "json",
            "--backend",
            "gflight",
            "--cash-only",
            "--matrix-url",
            "--google-url",
        ],
    )

    assert result.exit_code == 0, result.output
    rows = cast("list[list[dict[str, Any]]]", _one_document(result.stdout))
    # Both combinations, and the unpriced one last: shown, not dropped.
    assert [r[-1]["price"] for r in rows] == [board[0].flight.price, None], rows


def test_the_awards_only_json_serializer_is_driven_by_something(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--awards-only --format json` writes its document from a serializer no
    other arm in this file reaches.

    Every arm above stubs `run_pp_for_search` wholesale, so the real function's
    own two `sys.stdout.write` calls — the ones a stray `console.print` above
    would splice prose into — are covered by nothing. This drives the real one
    with only the network below it replaced."""
    from flight_cli.pp import cli as pp_cli
    from flight_cli.providers.base import AwardFlight, LegQuery

    award = AwardFlight(
        origin="JFK",
        destination="LAX",
        departure=f"{_DEP.isoformat()}T08:00:00",
        arrival=f"{_DEP.isoformat()}T11:20:00",
        flight_number="AA100",
        provider="PointsPath",
        program="American Airlines",
    )

    async def _gather(*_a: object, **_kw: object) -> tuple[list[list[Any]], list[Any]]:
        return ([[award]], [])

    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)

    pp_cli.run_pp_for_search(
        _matrix_result(),
        legs=[LegQuery("JFK", "LAX", _DEP.isoformat(), 0, "outbound JFK→LAX")],
        pp_only=True,
        json_out=True,
    )

    document = cast("list[dict[str, Any]]", _one_document(capsys.readouterr().out))
    assert [leg["leg"] for leg in document] == ["outbound JFK→LAX"], document
    assert document[0]["awards"][0]["flight_number"] == "AA100", document


def test_a_retry_warning_lands_on_stderr_and_not_in_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The transport under a `--format json` run can speak while the run is in
    flight, and structlog's own default writes to STDOUT.

    A retry warning there splices a diagnostic into a machine consumer's input
    — the document is still on stdout, so the run looks fine to a human and is
    a parse error to the thing that asked for it. The whole arm is driven, with
    a real Matrix client over a mock transport that fails once, because the
    routing is decided by `configure()` and pinning the factory alone would say
    nothing about the command.

    stamina's testing mode removes the waits, not the retry: the hook that
    emits the warning still fires, which is the part under test."""
    calls = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "brownout"})
        return httpx.Response(200, json={"result": {"solutions": []}})

    def _client(**kw: Any) -> Any:
        from flight_cli.client import MatrixClient

        c = MatrixClient(
            api_key="test-key",
            cache_dir=str(tmp_path),
            cache_read=False,
            cache_write=False,
            **{k: v for k, v in kw.items() if k in {"rps", "impersonate"}},
        )
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        return c

    monkeypatch.setattr(cli, "_run", _REAL_RUN)
    monkeypatch.setattr(cli, "MatrixClient", _client)

    with stamina.set_testing(True, attempts=3):
        result = CliRunner().invoke(
            cli.app,
            [
                "search",
                "JFK",
                "LAX",
                "--dep",
                _DEP.isoformat(),
                "-n",
                "3",
                "--format",
                "json",
                "--backend",
                "matrix",
                "--cash-only",
            ],
        )

    assert result.exit_code == 0, result.output
    assert calls["n"] == 2, calls
    _one_document(result.stdout)
    assert "retry_scheduled" in result.stderr, result.stderr
