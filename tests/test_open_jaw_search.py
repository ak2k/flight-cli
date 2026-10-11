# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search` on an open jaw (two `--slice` that are not a round trip):
Google Flights' cheapest one-way per slice, combined as separate tickets ahead
of Matrix's one-ticket answer.

Google is faked at `cli._gflight_results`, answering each one-way by its leg's
origin, and Matrix at `cli._run`. No test reaches Google, Matrix or Chrome."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from flight_cli import _envelope, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_postfilter import StopDrops
from flight_cli.domain import Leg, SearchOptions
from flight_cli.models import SearchResult
from test_envelope import _envelope_of, _notes
from test_gf_rung_parity import _board
from test_split_ticket import _row

if TYPE_CHECKING:
    from click.testing import Result


def _days() -> tuple[dt.date, dt.date]:
    """The first slice's day and the second's a week later, both ahead of today."""
    out = dt.date.today() + dt.timedelta(days=45)
    return out, out + dt.timedelta(days=7)


def _first() -> list[Any]:
    """JFK-LHR one-ways, the cheapest unpriced and a self transfer beside them:
    neither is a ticket a combination can sum."""
    out, _ = _days()
    return [
        _row("BA9", "JFK", "LHR", out, None),
        _row("BA1", "JFK", "LHR", out, 295.0),
        _row("BA2", "JFK", "LHR", out, 400.0),
    ]


def _second() -> list[Any]:
    _, back = _days()
    return [_row("AF1", "CDG", "JFK", back, 566.0), _row("AF2", "CDG", "JFK", back, 600.0)]


@dataclass
class _Google:
    """`_gflight_results`, answering each one-way by its leg's origin and
    recording every call."""

    boards: dict[str, list[Any] | Exception]
    calls: list[tuple[tuple[Leg, ...], SearchOptions, dict[str, object]]] = field(
        default_factory=list[tuple[tuple[Leg, ...], SearchOptions, dict[str, object]]]
    )

    def __call__(
        self,
        legs: tuple[Leg, ...],
        opts: SearchOptions,
        _top_n: int,
        _gf_mode: str = "http",
        _gf_headed: bool = False,
        **kw: object,
    ) -> gfid.Board[Any]:
        self.calls.append((legs, opts, kw))
        answer = self.boards[legs[0].origins[0]]
        if isinstance(answer, Exception):
            raise answer
        return gfid.Board(answer)


def _google(
    monkeypatch: pytest.MonkeyPatch,
    first: list[Any] | Exception | None = None,
    second: list[Any] | Exception | None = None,
) -> _Google:
    google = _Google(
        {
            "JFK": _first() if first is None else first,
            "CDG": _second() if second is None else second,
            "LHR": _second() if second is None else second,
        }
    )
    monkeypatch.setattr(cli, "_gflight_results", google)
    return google


def _matrix_body(legs: tuple[Leg, ...]) -> dict[str, Any]:
    slices = [
        {
            "flights": [f"UA{i:d}"],
            "departure": f"{leg.date}T08:00",
            "arrival": f"{leg.date}T20:00",
            "origin": {"code": leg.origins[0]},
            "destination": {"code": leg.destinations[0]},
        }
        for i, leg in enumerate(legs, 1)
    ]
    solution = {"displayTotal": "USD812.00", "itinerary": {"slices": slices}}
    return {"solutionList": {"solutions": [solution]}, "solutionCount": 1}


@dataclass
class _Matrix:
    """`cli._run`, answering every search with one USD812 itinerary."""

    searches: list[Any] = field(default_factory=list[Any])

    def __call__(self, search: Any, *_a: object) -> SearchResult:
        self.searches.append(search)
        return SearchResult.from_api(_matrix_body(search.legs))


def _matrix(monkeypatch: pytest.MonkeyPatch) -> _Matrix:
    matrix = _Matrix()
    monkeypatch.setattr(cli, "_run", matrix)
    return matrix


def _search(*extra: str, slices: tuple[str, ...] | None = None) -> Result:
    out, back = _days()
    given = slices or (f"JFK-LHR:{out}", f"CDG-JFK:{back}")
    args = ["search", *(a for s in given for a in ("--slice", s)), "--no-matrix-url"]
    if "--google-url" not in extra:
        args.append("--no-google-url")
    return CliRunner().invoke(cli.app, [*args, *extra], env={"COLUMNS": "200", "NO_COLOR": "1"})


def _table_rows(stdout: str, title: str) -> list[list[str]]:
    """The cells of each body row of the table titled `title`."""
    lines = stdout.splitlines()
    start = next(i for i, ln in enumerate(lines) if title in ln)
    rows: list[list[str]] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("└"):
            break
        if ln.startswith("│"):
            rows.append([c.strip() for c in ln.strip("│").split("│")])
    return rows


_KEY = (
    "† separate tickets: one one-way ticket per slice, each bought on its own; a missed "
    "flight on one is not protected on the next."
)
_TITLE = "Separate tickets on Google Flights · JFK→LHR + CDG→JFK (USD)"


# ──────────────────────────────── the table ───────────────────────────────


def test_an_open_jaw_shows_separate_tickets_before_matrixs_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each total is the sum of the two tickets printed under it, marked `†`,
    cheapest first; each one-way is asked once, alone and without the cap,
    and Matrix is asked the open jaw itself. Red at the base: Google is never
    asked."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--cash-only", "--max-price", "1000")
    assert result.exit_code == 0, result.output
    rows = _table_rows(result.stdout, _TITLE)
    out, back = _days()
    totals = [(r[0], r[1]) for r in rows if r[0]]
    assert totals == [
        ("1", "USD861.00 †"),
        ("2", "USD895.00 †"),
        ("3", "USD966.00 †"),
        ("4", "USD1000.00 †"),
    ]
    tickets = [(r[2], r[3]) for r in rows if r[3]]
    assert [(cell.split()[:3], price) for cell, price in tickets[:2]] == [
        (["JFK→LHR", "BA1", f"{out:%b%d}"], "USD295.00"),
        (["CDG→JFK", "AF1", f"{back:%b%d}"], "USD566.00"),
    ]
    assert _KEY in " ".join(result.stdout.split())
    stdout = result.stdout
    assert stdout.index(_TITLE) < stdout.index("Itineraries")
    assert [(legs, opts.max_price, kw) for legs, opts, kw in google.calls] == [
        ((matrix.searches[0].legs[0],), None, {}),
        ((matrix.searches[0].legs[1],), None, {}),
    ]
    assert [lg.origins for lg in matrix.searches[0].legs] == [("JFK",), ("CDG",)]


def test_a_party_titles_its_totals_with_the_party(monkeypatch: pytest.MonkeyPatch) -> None:
    _google(monkeypatch)
    _matrix(monkeypatch)
    result = _search("--cash-only", "--adults", "2")
    assert result.exit_code == 0, result.output
    assert "Separate tickets on Google Flights · JFK→LHR + CDG→JFK (USD, 2 travelers)" in (
        " ".join(result.stdout.split())
    )


def test_google_url_links_each_ticket_of_the_cheapest(monkeypatch: pytest.MonkeyPatch) -> None:
    _google(monkeypatch)
    _matrix(monkeypatch)
    result = _search("--cash-only", "--google-url")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    for j in (1, 2):
        at = next(i for i, ln in enumerate(lines) if f"Google Flights (#1, ticket {j:d} " in ln)
        assert lines[at + 1].strip().startswith("https://www.google.com/travel/flights")


def test_a_flight_number_carrying_markup_and_an_escape_prints_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ticket cell is composed from leaves `_fmt_slice_cell` wraps where it
    reads them: a tag reads as text and an ESC never reaches the terminal."""
    out, _ = _days()
    _google(monkeypatch, first=[_row("BA[bold]7\x1b[2J", "JFK", "LHR", out, 295.0)])
    _matrix(monkeypatch)
    result = _search("--cash-only")
    assert result.exit_code == 0, result.output
    assert "BA[bold]7[2J" in result.stdout
    assert "\x1b" not in result.stdout


# ──────────────────────────── nothing asked ──────────────────────────────


@pytest.mark.parametrize(
    ("extra", "slices", "said"),
    [
        pytest.param(
            (),
            ("JFK-LHR:{out}", "CDG-JFK:{back}:e=F BC=j"),
            "No separate tickets on Google Flights: Google Flights can't serve slice 2 "
            "(CDG→JFK) as a one-way: extension 'F BC=j' not expressible on GF.",
            id="slice-extension",
        ),
        pytest.param(
            ("--extension", "F BC=j"),
            None,
            "No separate tickets on Google Flights: Google Flights can't serve slice 1 "
            "(JFK→LHR) as a one-way: extension 'F BC=j' not expressible on GF.",
            id="top-level-extension",
        ),
        pytest.param(
            ("--no-separate-tickets",),
            None,
            "No separate tickets on Google Flights: --no-separate-tickets was given.",
            id="opted-out",
        ),
    ],
)
def test_an_open_jaw_google_cannot_hold_to_its_codes_asks_google_nothing(
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    slices: tuple[str, ...] | None,
    said: str,
) -> None:
    out, back = _days()
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    given = None if slices is None else tuple(s.format(out=out, back=back) for s in slices)
    result = _search("--cash-only", *extra, slices=given)
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert said in " ".join(result.stderr.split())
    assert "Separate tickets" not in result.stdout
    assert len(matrix.searches) == 1
    assert "Itineraries" in result.stdout


def test_an_open_jaw_holds_each_one_way_to_the_top_level_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google, _ = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--cash-only", "--routing", "BA+", "--extension", "MAXCONNECT 2:00")
    assert result.exit_code == 0, result.output
    held = [(legs[0].route_language, legs[0].extension) for legs, _, _ in google.calls]
    assert held == [("BA+", "MAXCONNECT 2:00")] * 2
    assert "Separate tickets" in result.stdout


# ─────────────────────────── no combination ──────────────────────────────


@pytest.mark.parametrize(
    ("boards", "said"),
    [
        pytest.param(
            {"second": RuntimeError("boom")},
            "No separate tickets on Google Flights: the CDG→JFK one-way failed (boom).",
            id="failed",
        ),
        pytest.param(
            {"first": []},
            "No separate tickets on Google Flights: Google Flights priced no JFK→LHR one-way.",
            id="empty",
        ),
        pytest.param(
            {"second": "eur"},
            "No separate tickets on Google Flights: Google Flights priced no CDG→JFK one-way "
            "in USD.",
            id="off-currency",
        ),
        pytest.param(
            {"second": "same-day"},
            "No separate tickets on Google Flights: no pair where the second ticket leaves "
            "after the first lands (from another airport, on a later day).",
            id="no-flyable-pair",
        ),
    ],
)
def test_no_combination_is_said_and_matrix_still_answers(
    monkeypatch: pytest.MonkeyPatch, boards: dict[str, Any], said: str
) -> None:
    out, back = _days()
    given: dict[str, Any] = dict(boards)
    if given.get("second") == "eur":
        given["second"] = [_row("AF1", "CDG", "JFK", back, 500.0, currency="EUR")]
    if given.get("second") == "same-day":
        given["second"] = [_row("AF1", "CDG", "JFK", out, 500.0, at=dt.time(23, 0))]
    _google(monkeypatch, **given)
    matrix = _matrix(monkeypatch)
    result = _search("--cash-only")
    assert result.exit_code == 0, result.output
    assert said in " ".join(result.stderr.split())
    assert "Separate tickets" not in result.stdout
    assert len(matrix.searches) == 1
    assert "Itineraries" in result.stdout


def test_a_row_in_another_currency_is_counted_and_never_summed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, back = _days()
    second = [_row("AF7", "CDG", "JFK", back, 100.0, currency="EUR"), *_second()]
    _google(monkeypatch, second=second)
    _matrix(monkeypatch)
    result = _search("--cash-only")
    assert result.exit_code == 0, result.output
    assert (
        "Google Flights priced 1 one-way row in another currency than USD; no total adds it."
        in " ".join(result.stderr.split())
    )
    rows = _table_rows(result.stdout, _TITLE)
    assert next(r[1] for r in rows if r[0]) == "USD861.00 †"
    assert not any("AF7" in r[2] or "EUR" in r[3] for r in rows)


# ─────────────────────────── as at the base ──────────────────────────────


@pytest.mark.parametrize(
    ("extra", "slices"),
    [
        pytest.param(("--backend", "matrix"), None, id="backend-matrix"),
        pytest.param((), ("JFK-LHR:{out}", "LHR-JFK:{back}"), id="slice-round-trip"),
        pytest.param(("--format", "json"), None, id="json-without-split"),
    ],
)
def test_other_searches_ask_google_nothing(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], slices: tuple[str, ...] | None
) -> None:
    """Green at the base."""
    out, back = _days()
    later = back + dt.timedelta(days=3)
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    given = (
        None if slices is None else tuple(s.format(out=out, back=back, later=later) for s in slices)
    )
    result = _search("--cash-only", *extra, slices=given)
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert len(matrix.searches) == 1
    assert "Separate tickets" not in result.output
    if "json" in extra:
        assert json.loads(result.stdout) == _matrix_body(matrix.searches[0].legs)


def test_a_multi_cabin_open_jaw_asks_google_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the base."""
    google = _google(monkeypatch)
    answered: list[tuple[Leg, ...]] = []

    def _multi(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        answered.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _multi)
    result = _search("--cash-only", "--cabin", "economy,business")
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert len(answered) == 1


def test_an_awards_only_open_jaw_asks_google_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green at the base."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    awarded: list[object] = []

    def _awards(*a: object, **_kw: object) -> None:
        awarded.append(a)

    def _yes(_sel: cli.ProviderSelection) -> bool:
        return True

    monkeypatch.setattr(cli, "run_pp_for_search", _awards)
    monkeypatch.setattr(cli, "_should_run_awards", _yes)
    result = _search("--awards-only")
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert len(matrix.searches) == len(awarded) == 1


# ──────────────────────────── --split's JSON ─────────────────────────────


def test_json_split_carries_the_combinations_beside_matrixs_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which refuses `--split` on any `--slice`."""
    _google(monkeypatch)
    matrix = _matrix(monkeypatch)
    result = _search("--cash-only", "--split", "--format", "json", "-n", "2")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["search"] == _matrix_body(matrix.searches[0].legs)
    st = doc["split_ticket"]
    assert st["currency"] == "USD"
    assert [c["total"] for c in st["combinations"]] == [861, 895]
    for combo in st["combinations"]:
        assert (combo["separate_tickets"], combo["currency"]) == (True, "USD")
        tickets = combo["tickets"]
        assert combo["total"] == sum(t["price"] for t in tickets)
        assert [t["flight_id"].split("-")[1][:2] for t in tickets] == ["BA", "AF"]
        assert all(t["google_flights_url"].startswith("https://") for t in tickets)


@pytest.mark.parametrize(
    ("boards", "slices", "error"),
    [
        pytest.param(
            {"first": RuntimeError("boom")}, None, "the JFK→LHR one-way failed (boom)", id="failed"
        ),
        pytest.param(
            {},
            ("JFK-LHR:{out}", "CDG-JFK:{back}:e=F BC=j"),
            "Google Flights can't serve slice 2 (CDG→JFK) as a one-way: extension 'F BC=j' "
            "not expressible on GF",
            id="unservable-slice",
        ),
    ],
)
def test_json_split_names_why_there_is_no_combination(
    monkeypatch: pytest.MonkeyPatch,
    boards: dict[str, Any],
    slices: tuple[str, ...] | None,
    error: str,
) -> None:
    out, back = _days()
    _google(monkeypatch, **boards)
    matrix = _matrix(monkeypatch)
    given = None if slices is None else tuple(s.format(out=out, back=back) for s in slices)
    result = _search("--cash-only", "--split", "--format", "json", slices=given)
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["search"] == _matrix_body(matrix.searches[0].legs)
    assert doc["split_ticket"] == {"error": error}


@pytest.mark.parametrize(
    ("boards", "slices", "complete"),
    [
        pytest.param({}, None, True, id="combinations"),
        pytest.param({"first": []}, None, True, id="empty-board"),
        pytest.param({"second": RuntimeError("boom")}, None, False, id="failed-board"),
        pytest.param({}, ("JFK-LHR:{out}", "CDG-JFK:{back}:e=F BC=j"), False, id="not-asked"),
    ],
)
def test_the_envelope_carries_the_json_documents_split_ticket(
    monkeypatch: pytest.MonkeyPatch,
    boards: dict[str, Any],
    slices: tuple[str, ...] | None,
    complete: bool,
) -> None:
    """A board with no combination is an answer; one that failed, or a slice
    Google was not asked about, leaves the tickets asked for unpriced."""
    out, back = _days()
    given = None if slices is None else tuple(s.format(out=out, back=back) for s in slices)
    argv = ("--cash-only", "--split", "-n", "3")
    _google(monkeypatch, **boards)
    _matrix(monkeypatch)
    doc = json.loads(_search(*argv, "--format", "json", slices=given).stdout)
    _google(monkeypatch, **boards)
    _matrix(monkeypatch)
    env = _envelope_of(_search(*argv, "--format", "envelope", slices=given))
    assert (env["backend"], env["complete"]) == ("matrix", complete)
    assert env["split_ticket"] == doc["split_ticket"]
    assert _notes(env, "split_ticket") == []


@pytest.mark.parametrize(
    ("origin", "damage", "note"),
    [
        pytest.param(
            "JFK",
            {"unread": 3},
            "Google Flights JFK→LHR one-way: 3 rows its pages served could not be read and "
            "are left out of the answer",
            id="unread",
        ),
        pytest.param("CDG", {"partial": True}, None, id="partial"),
    ],
)
def test_a_board_short_of_rows_leaves_the_envelope_incomplete(
    monkeypatch: pytest.MonkeyPatch, origin: str, damage: dict[str, Any], note: str | None
) -> None:
    """A slice's board that could not read some rows, or is missing a page, may
    lack its cheapest tickets: the combinations stand, and the envelope says it
    is narrower than asked. Red at the base, which asks Google nothing."""
    google = _google(monkeypatch)
    _matrix(monkeypatch)

    def _short(legs: tuple[Leg, ...], *a: Any, **kw: Any) -> gfid.Board[Any]:
        board = google(legs, *a, **kw)
        if legs[0].origins[0] == origin:
            for name, value in damage.items():
                setattr(board, name, value)
        return board

    monkeypatch.setattr(cli, "_gflight_results", _short)
    env = _envelope_of(_search("--cash-only", "--split", "--format", "envelope", "-n", "1"))
    assert env["complete"] is False
    assert [c["total"] for c in env["split_ticket"]["combinations"]] == [861]
    if note is not None:
        assert note in env["notes"]


def test_split_on_a_table_prints_what_the_table_prints_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _google(monkeypatch)
    _matrix(monkeypatch)
    plain = _search("--cash-only")
    _google(monkeypatch)
    _matrix(monkeypatch)
    split = _search("--cash-only", "--split")
    assert split.exit_code == plain.exit_code == 0, split.output
    assert split.stdout == plain.stdout
    assert _TITLE in split.stdout


@pytest.mark.parametrize(
    ("extra", "slices", "said"),
    [
        pytest.param(
            (),
            ("JFK-LHR:{out}", "LHR-JFK:{back}"),
            "--split prices a round trip as two one-ways, and --slice is a multi-city search.",
            id="slice-round-trip",
        ),
        pytest.param(
            ("--fare-rules",),
            None,
            "--split cannot run beside --fare-rules; drop one of them.",
            id="fare-rules",
        ),
    ],
)
def test_split_is_refused_where_it_cannot_join(
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    slices: tuple[str, ...] | None,
    said: str,
) -> None:
    """The round-trip arm is green at the base, in the base's words."""
    out, back = _days()
    later = back + dt.timedelta(days=3)
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    given = (
        None if slices is None else tuple(s.format(out=out, back=back, later=later) for s in slices)
    )
    result = _search("--cash-only", "--split", *extra, slices=given)
    assert result.exit_code == 2, result.output
    assert said in result.stderr
    assert google.calls == [] and matrix.searches == []


def _awards_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An award provider is configured: awards run unless `--cash-only`."""

    def _configured(sel: cli.ProviderSelection) -> bool:
        return not sel.cash_only

    def _awards(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "_should_run_awards", _configured)
    monkeypatch.setattr(cli, "run_pp_for_search", _awards)


def test_split_joins_the_envelope_an_award_search_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The envelope carries an open jaw's tickets beside the award rows with no
    flag, so `--split` asks for what it already holds and is not refused."""
    _awards_on(monkeypatch)
    google, _ = _google(monkeypatch), _matrix(monkeypatch)
    plain = _envelope_of(_search("-n", "3", "--format", "envelope"))
    asked = google.calls
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("-n", "3", "--format", "envelope", "--split")
    assert result.exit_code == 0, result.output
    env = _envelope_of(result)
    assert env["split_ticket"] == plain["split_ticket"]
    assert len(env["split_ticket"]["combinations"]) == 3
    assert env["complete"] is plain["complete"] is True
    assert google.calls == asked
    assert len(matrix.searches) == 1


def test_the_envelope_without_split_carries_the_tables_combinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table lists the separate tickets with no flag, so the envelope of
    the same search carries them, asked exactly as the table asks them. Red at
    the base, which asked Google nothing and explained a null `split_ticket`
    as "--split was not asked"."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    table = _search("--cash-only", "-n", "3")
    assert table.exit_code == 0, table.output
    shown = [r[1] for r in _table_rows(table.stdout, _TITLE) if r[0]]
    asked = [(legs, opts.max_price, kw) for legs, opts, kw in google.calls]
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    env = _envelope_of(_search("--cash-only", "-n", "3", "--format", "envelope"))
    assert (env["backend"], env["complete"]) == ("matrix", True)
    combos = env["split_ticket"]["combinations"]
    assert (
        [f"USD{c['total']:.2f} †" for c in combos]
        == shown
        == ["USD861.00 †", "USD895.00 †", "USD966.00 †"]
    )
    assert all(c["separate_tickets"] is True for c in combos)
    assert _notes(env, "split_ticket") == []
    assert [(legs, opts.max_price, kw) for legs, opts, kw in google.calls] == asked
    assert asked == [
        ((matrix.searches[0].legs[0],), None, {}),
        ((matrix.searches[0].legs[1],), None, {}),
    ]
    assert len(matrix.searches) == 1
    assert _envelope.active() is False


@pytest.mark.parametrize(
    ("boards", "extra", "error", "complete", "note"),
    [
        pytest.param(
            {"second": RuntimeError("boom")},
            (),
            "the CDG→JFK one-way failed (boom)",
            False,
            "No separate tickets on Google Flights: the CDG→JFK one-way failed (boom).",
            id="failed",
        ),
        pytest.param(
            {"first": []},
            (),
            "Google Flights priced no JFK→LHR one-way",
            True,
            "No separate tickets on Google Flights: Google Flights priced no JFK→LHR one-way.",
            id="empty",
        ),
        pytest.param(
            {},
            ("--no-separate-tickets",),
            "--no-separate-tickets was given",
            True,
            "No separate tickets on Google Flights: --no-separate-tickets was given.",
            id="opted-out",
        ),
    ],
)
def test_the_envelope_without_split_keeps_splits_rules(
    monkeypatch: pytest.MonkeyPatch,
    boards: dict[str, Any],
    extra: tuple[str, ...],
    error: str,
    complete: bool,
    note: str,
) -> None:
    """A failed one-way leaves the tickets the table would list unpriced; an
    empty board is an answer; an opt-out asks nothing and narrows nothing,
    since the table lists no tickets either. Red at the base, whose envelope
    held a null `split_ticket` and asked Google nothing."""
    google = _google(monkeypatch, **boards)
    _matrix(monkeypatch)
    env = _envelope_of(_search("--cash-only", *extra, "--format", "envelope"))
    assert (env["backend"], env["complete"]) == ("matrix", complete)
    assert note in env["notes"]
    assert _notes(env, "split_ticket") == []
    assert env["split_ticket"] == {"error": error}
    assert (google.calls == []) is bool(extra)


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        pytest.param(
            (),
            "No separate tickets on Google Flights: --format json carries them only with --split.",
            id="unasked",
        ),
        pytest.param(
            ("--no-separate-tickets",),
            "No separate tickets on Google Flights: --no-separate-tickets was given.",
            id="opted-out",
        ),
    ],
)
def test_json_without_split_says_why_it_carries_no_tickets(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], said: str
) -> None:
    """Matrix's own body is the document and Google is asked nothing (green at
    the base); one line says where the tickets the table lists went, or the
    blocker's line where one applies (red at the base, which said nothing)."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--cash-only", *extra, "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _matrix_body(matrix.searches[0].legs)
    assert google.calls == []
    stderr = " ".join(result.stderr.split())
    assert stderr.count("No separate tickets on Google Flights") == 1, stderr
    assert said in stderr


def test_json_with_awards_names_every_flag_the_tickets_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With awards on, `--format json` writes the award document, which `--split`
    cannot join: the line names `--cash-only` too, and that command carries them."""
    _awards_on(monkeypatch)
    google, _ = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--format", "json")
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert (
        "No separate tickets on Google Flights: --format json carries them only with --split "
        "and --cash-only." in " ".join(result.stderr.split())
    )
    _google(monkeypatch)
    _matrix(monkeypatch)
    followed = _search("--format", "json", "--split", "--cash-only")
    assert followed.exit_code == 0, followed.output
    assert json.loads(followed.stdout)["split_ticket"]["combinations"]


@pytest.mark.parametrize(
    ("second", "said"),
    [
        pytest.param(None, None, id="shown"),
        pytest.param(
            [],
            "No separate tickets on Google Flights: Google Flights priced no CDG→JFK one-way.",
            id="emptied",
        ),
    ],
)
def test_a_one_way_board_at_the_row_cap_says_where_it_stops(
    monkeypatch: pytest.MonkeyPatch, second: list[Any] | None, said: str | None
) -> None:
    """A ticket priced above the CDG→JFK board's cap may be missing from the
    combinations, or be the one a board with no ticket lacks, so the board says
    so, as a note: the envelope stays complete. Red before, which printed no
    line for a one-way board."""
    google = _google(monkeypatch, second=second)
    _matrix(monkeypatch)

    def capped(legs: tuple[Leg, ...], *a: Any, **kw: Any) -> gfid.Board[Any]:
        board = google(legs, *a, **kw)
        if legs[0].origins[0] == "CDG":
            board.capped_at = _board("ds1_nyc_lon_token").capped_at
        return board

    monkeypatch.setattr(cli, "_gflight_results", capped)
    line = (
        "Google Flights CDG→JFK one-way stops at 300 rows for this search: "
        "fares above USD1006.00 may be missing."
    )
    result = _search("--cash-only")
    assert result.exit_code == 0, result.output
    stderr = " ".join(result.stderr.split())
    assert stderr.count(line) == 1, stderr
    assert "JFK→LHR one-way stops" not in stderr
    assert said is None or said in stderr, stderr
    env = _envelope_of(_search("--cash-only", "--split", "--format", "envelope"))
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]
    assert line in env["notes"]


@pytest.mark.parametrize("emptied", [False, True], ids=["shown", "emptied"])
def test_a_one_way_board_counts_the_rows_it_drops_over_the_stop_ceiling(
    monkeypatch: pytest.MonkeyPatch, emptied: bool
) -> None:
    """As every other Google board shown says so: a line, which the envelope
    carries as a note, `complete` unchanged. A board the drops emptied says so
    too, since the line saying it priced no one-way does not name the ceiling.
    Red at the base, which counted none on a one-way board."""
    first: list[Any] | None = [] if emptied else None
    line = (
        "Google Flights JFK→LHR one-way returned 1 row over the stop ceiling it was asked "
        "for (0); it is not shown."
    )

    def _dropping(google: _Google) -> None:
        def _served(legs: tuple[Leg, ...], *a: Any, **kw: Any) -> gfid.Board[Any]:
            board = google(legs, *a, **kw)
            if legs[0].origins[0] == "JFK":
                board.stop_drops = StopDrops(rows=1, ceiling=0)
            return board

        monkeypatch.setattr(cli, "_gflight_results", _served)

    _dropping(_google(monkeypatch, first=first))
    _matrix(monkeypatch)
    result = _search("--cash-only", "--stops", "0")
    assert result.exit_code == 0, result.output
    assert " ".join(result.stderr.split()).count(line) == 1, result.stderr
    assert (_TITLE in result.stdout) is not emptied
    _dropping(_google(monkeypatch, first=first))
    _matrix(monkeypatch)
    env = _envelope_of(_search("--cash-only", "--stops", "0", "--format", "envelope"))
    assert (env["backend"], env["complete"]) == ("matrix", True), env["notes"]
    assert env["notes"].count(line) == 1


@pytest.mark.parametrize(
    ("slices", "on_one_ticket"),
    [
        pytest.param(None, True, id="open-jaw"),
        pytest.param(("JFK-LHR:{out}", "LHR-JFK:{back}"), False, id="slice-round-trip"),
        pytest.param(
            ("JFK-LHR:{out}", "CDG-JFK:{back}", "JFK-MIA:{later}"), True, id="three-slices"
        ),
    ],
)
def test_the_matrix_line_says_what_google_cannot_serve(
    monkeypatch: pytest.MonkeyPatch, slices: tuple[str, ...] | None, on_one_ticket: bool
) -> None:
    """A multi-city trip's line sits above Google's separate tickets for it, so
    it names the one ticket Google can't sell. Red at the base for the open jaw
    and three slices; a round trip keeps the base's words."""
    out, back = _days()
    later = back + dt.timedelta(days=3)
    _google(monkeypatch)
    _matrix(monkeypatch)
    given = (
        None if slices is None else tuple(s.format(out=out, back=back, later=later) for s in slices)
    )
    result = _search("--cash-only", slices=given)
    assert result.exit_code == 0, result.output
    said = "Using Matrix: Google Flights can't serve a multi-city itinerary" + (
        " on one ticket." if on_one_ticket else "."
    )
    assert said in " ".join(result.stderr.split()), result.stderr
