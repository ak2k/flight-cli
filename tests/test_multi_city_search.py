# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search` on a multi-city trip of three slices: Google Flights'
cheapest one-way per slice, combined as separate tickets, beside Matrix's
one-ticket answer on `--backend auto` and in its place under `--backend
gflight`.

Google and Matrix are faked as in `test_open_jaw_search`: Google answers each
one-way by its leg's origin, Matrix every search with one USD812 itinerary. No
test reaches Google, Matrix or Chrome."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

from flight_cli import cli
from flight_cli._gf_errors import GfThrottledError
from flight_cli.pp import cli as pp_cli
from test_envelope import _envelope_of, _notes
from test_open_jaw_search import _KEY, _Google, _matrix, _matrix_body, _search, _table_rows
from test_split_ticket import _row

if TYPE_CHECKING:
    from flight_cli.domain import Leg


def _days() -> tuple[dt.date, dt.date, dt.date]:
    out = dt.date.today() + dt.timedelta(days=45)
    return out, out + dt.timedelta(days=3), out + dt.timedelta(days=7)


def _slices() -> tuple[str, ...]:
    one, two, three = _days()
    return (f"SFO-ORD:{one}", f"ORD-BOS:{two}", f"BOS-SFO:{three}")


# A title wider than its table wraps, so it is read off the words of stdout.
_TITLE = "Separate tickets on Google Flights · SFO→ORD + ORD→BOS + BOS→SFO (USD)"
_OPEN_JAW_TITLE = "Separate tickets on Google Flights · JFK→LHR + CDG→JFK (USD)"


def _boards() -> dict[str, list[Any] | Exception]:
    """Every combination flyable: eight, from 116 + 158 + 151 = 425. The
    first board's cheapest row is unpriced, which no total sums."""
    one, two, three = _days()
    return {
        "SFO": [
            _row("UA9", "SFO", "ORD", one, None),
            _row("UA1", "SFO", "ORD", one, 116.0),
            _row("UA2", "SFO", "ORD", one, 150.0),
        ],
        "ORD": [_row("AA1", "ORD", "BOS", two, 158.0), _row("AA2", "ORD", "BOS", two, 170.0)],
        "BOS": [_row("WN1", "BOS", "SFO", three, 151.0), _row("WN2", "BOS", "SFO", three, 160.0)],
        # The open jaw's, for the arms that ask it too.
        "JFK": [_row("BA1", "JFK", "LHR", one, 295.0)],
        "CDG": [_row("AF1", "CDG", "JFK", three, 566.0)],
    }


_TOTALS = [425, 434, 437, 446, 459, 468, 471, 480]


def _google(monkeypatch: pytest.MonkeyPatch, **boards: list[Any] | Exception) -> _Google:
    google = _Google({**_boards(), **boards})
    monkeypatch.setattr(cli, "_gflight_results", google)
    return google


def _open_jaw_slices() -> tuple[str, ...]:
    one, _, three = _days()
    return (f"JFK-LHR:{one}", f"CDG-JFK:{three}")


def _run(*extra: str, slices: tuple[str, ...] | None = None) -> Any:
    return _search("--cash-only", *extra, slices=slices or _slices())


def _stderr(result: Any) -> str:
    return " ".join(result.stderr.split())


def _awards_on(_sel: cli.ProviderSelection) -> bool:
    return True


# ───────────────────────────── beside Matrix ──────────────────────────────


def test_three_slices_show_separate_tickets_before_matrixs_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each slice asked once, alone and without the cap, in slice order; each
    combination three priced tickets in slice order under a `†` total that
    sums them, cheapest first, no two alike; Matrix's table below as without
    them. Red at the base, which asked Google nothing."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _run("--max-price", "1000")
    assert result.exit_code == 0, result.output
    assert len(matrix.searches) == 1
    assert [(legs, opts.max_price, kw) for legs, opts, kw in google.calls] == [
        ((leg,), None, {}) for leg in matrix.searches[0].legs
    ]
    rows = _table_rows(result.stdout, "Separate tickets on Google Flights")
    combos: list[tuple[str, list[tuple[str, str]]]] = []
    for r in rows:
        if r[0]:
            combos.append((r[1], []))
        if r[3]:
            combos[-1][1].append((r[2], r[3]))
    assert [total for total, _ in combos] == [f"USD{t:d}.00 †" for t in _TOTALS]
    for total, tickets in combos:
        assert [cell.split()[0] for cell, _ in tickets] == ["SFO→ORD", "ORD→BOS", "BOS→SFO"]
        assert sum(float(price.removeprefix("USD")) for _, price in tickets) == float(
            total.removeprefix("USD").removesuffix(" †")
        )
    assert len({tuple(t) for _, t in combos}) == len(combos)
    assert _TITLE in " ".join(result.stdout.split())
    assert _KEY in " ".join(result.stdout.split())
    assert (
        "Using Matrix: Google Flights can't serve a multi-city itinerary on one ticket."
        in _stderr(result)
    )
    _google(monkeypatch)
    _matrix(monkeypatch)
    plain = _run("--max-price", "1000", "--no-separate-tickets")
    assert plain.exit_code == 0, plain.output
    assert result.stdout.endswith(plain.stdout)
    assert "Separate tickets" not in plain.stdout


def test_the_envelope_carries_the_tables_combinations_beside_matrixs_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, whose envelope held a null `split_ticket`."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    env = _envelope_of(_run("--format", "envelope"))
    assert (env["backend"], env["complete"], env["currency"]) == ("matrix", True, "USD")
    assert [r["price"] for g in env["results"] for r in g["rows"]] == [812.0]
    combos = env["split_ticket"]["combinations"]
    assert [c["total"] for c in combos] == _TOTALS
    assert all(c["separate_tickets"] is True and len(c["tickets"]) == 3 for c in combos)
    assert len(google.calls) == 3 and len(matrix.searches) == 1


def test_split_json_carries_the_combinations_beside_matrixs_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which refused `--split` on three slices."""
    _google(monkeypatch)
    matrix = _matrix(monkeypatch)
    result = _run("--split", "--format", "json", "-n", "2")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["search"] == _matrix_body(matrix.searches[0].legs)
    combos = doc["split_ticket"]["combinations"]
    assert [c["total"] for c in combos] == _TOTALS[:2]
    for combo in combos:
        assert combo["total"] == sum(t["price"] for t in combo["tickets"])
        assert [t["flight_id"].split("-")[1][:2] for t in combo["tickets"]] == ["UA", "AA", "WN"]


def test_a_throttle_on_the_first_slice_leaves_the_rest_unasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which asked Google nothing."""
    google = _google(monkeypatch, SFO=GfThrottledError("rate-limited"))
    matrix = _matrix(monkeypatch)
    env = _envelope_of(_run("--format", "envelope"))
    assert len(google.calls) == 1 and len(matrix.searches) == 1
    said = (
        "No separate tickets on Google Flights: the SFO→ORD one-way failed (rate-limited), and "
        "the ORD→BOS and BOS→SFO one-ways were not asked after it stopped the search."
    )
    assert said in env["notes"]
    assert (env["backend"], env["complete"]) == ("matrix", False)


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        pytest.param(
            (),
            "no combination where each ticket leaves after the one before it lands (from "
            "another airport, on a later day)",
            id="none-flyable",
        ),
        pytest.param(("--max-price", "400"), "no combination at or under USD 400", id="capped"),
    ],
)
def test_no_combination_is_said_in_three_slices_words(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], said: str
) -> None:
    """Under the no-flyable arm the middle ticket leaves ORD before the first
    lands there. Red at the base, which asked Google nothing."""
    one, *_ = _days()
    middle = [_row("AA1", "ORD", "BOS", one, 158.0, at=dt.time(6, 0))]
    _google(monkeypatch, **({} if extra else {"ORD": middle}))
    matrix = _matrix(monkeypatch)
    result = _run(*extra)
    assert result.exit_code == 0, result.output
    assert f"No separate tickets on Google Flights: {said}." in _stderr(result)
    assert "Separate tickets" not in result.stdout
    assert len(matrix.searches) == 1


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        pytest.param(
            (),
            "No separate tickets on Google Flights: each is priced in one cabin, and --cabin "
            "asks for 2.",
            id="multi-cabin",
        ),
        pytest.param(
            ("--no-separate-tickets",),
            "No separate tickets on Google Flights: --no-separate-tickets was given.",
            id="opted-out",
        ),
    ],
)
def test_a_multi_cabin_search_says_why_google_is_asked_nothing(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], said: str
) -> None:
    """Matrix answers every cabin; one line says why no separate tickets
    are shown. Red at the base, which said nothing."""
    google = _google(monkeypatch)
    answered: list[tuple[Leg, ...]] = []

    def _multi(*, legs: tuple[Leg, ...], **_kw: object) -> None:
        answered.append(legs)

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _multi)
    result = _run("--cabin", "economy,business", *extra)
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert len(answered) == 1
    assert _stderr(result).count("No separate tickets on Google Flights") == 1
    assert said in _stderr(result)


def test_an_awards_only_multi_cabin_search_says_nothing_of_separate_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Green at the base."""
    google = _google(monkeypatch)

    def _multi(**_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "_run_matrix_path_multi", _multi)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = _search("--awards-only", "--cabin", "economy,business", slices=_slices())
    assert result.exit_code == 0, result.output
    assert google.calls == []
    assert "separate tickets" not in _stderr(result).lower()


# ────────────────────────── a party with an infant ──────────────────────────


@pytest.mark.parametrize(
    ("slices", "empty", "route"),
    [
        pytest.param(None, "CDG", "CDG→JFK", id="open-jaw"),
        pytest.param(_slices(), "ORD", "ORD→BOS", id="three-slices"),
    ],
)
def test_an_infants_empty_board_narrows_the_answer(
    monkeypatch: pytest.MonkeyPatch, slices: tuple[str, ...] | None, empty: str, route: str
) -> None:
    """Google has served no rows for a party with an infant on routes with
    flights, so the empty board is no answer: the envelope is narrower than
    asked. Red at the base, which read it as "priced no one-way", complete."""
    boards: dict[str, list[Any] | Exception] = {empty: []}
    _google(monkeypatch, **boards)
    _matrix(monkeypatch)
    env = _envelope_of(
        _search("--cash-only", "--inf-lap", "1", "--format", "envelope", slices=slices)
    )
    said = (
        "No separate tickets on Google Flights: Google Flights served no rows for a party "
        f"with an infant on the {route} one-way, as it has on routes with flights."
    )
    assert said in env["notes"]
    assert not any("priced no" in n for n in env["notes"])
    assert (env["backend"], env["complete"]) == ("matrix", False)


# ──────────────────────────── --backend gflight ────────────────────────────


@pytest.mark.parametrize(
    ("slices", "title", "asked"),
    [
        pytest.param(_slices(), _TITLE, 3, id="three-slices"),
        pytest.param(_open_jaw_slices(), _OPEN_JAW_TITLE, 2, id="open-jaw"),
    ],
)
def test_backend_gflight_answers_with_separate_tickets_alone(
    monkeypatch: pytest.MonkeyPatch, slices: tuple[str, ...], title: str, asked: int
) -> None:
    """The table, its key and its links, and no line about Matrix, which is
    asked nothing. Red at the base, which refused the search (exit 2)."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _search("--cash-only", "--backend", "gflight", "--google-url", slices=slices)
    assert result.exit_code == 0, result.output
    assert matrix.searches == [] and len(google.calls) == asked
    assert title in " ".join(result.stdout.split())
    assert _KEY in " ".join(result.stdout.split())
    assert "Itineraries" not in result.stdout
    assert "Matrix" not in result.stderr
    lines = result.stdout.splitlines()
    for j in range(1, asked + 1):
        at = next(i for i, ln in enumerate(lines) if f"Google Flights (#1, ticket {j:d} " in ln)
        assert lines[at + 1].strip().startswith("https://www.google.com/travel/flights")


@pytest.mark.parametrize("split", [False, True], ids=["plain", "split"])
@pytest.mark.parametrize(
    ("slices", "totals"),
    [
        pytest.param(_slices(), _TOTALS[:3], id="three-slices"),
        pytest.param(_open_jaw_slices(), [861], id="open-jaw"),
    ],
)
def test_backend_gflight_json_is_an_empty_search_beside_the_tickets(
    monkeypatch: pytest.MonkeyPatch, slices: tuple[str, ...], totals: list[int], split: bool
) -> None:
    """`{"search": [], "split_ticket": …}`, `--split` or not. Red at the base,
    which refused the search (exit 2)."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    extra = ("--split",) if split else ()
    result = _search(
        "--cash-only", "--backend", "gflight", "--format", "json", "-n", "3", *extra, slices=slices
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert list(doc) == ["search", "split_ticket"] and doc["search"] == []
    assert [c["total"] for c in doc["split_ticket"]["combinations"]] == totals
    assert matrix.searches == [] and len(google.calls) == len(slices)


@pytest.mark.parametrize(
    ("slices", "totals"),
    [
        pytest.param(_slices(), _TOTALS[:3], id="three-slices"),
        pytest.param(_open_jaw_slices(), [861], id="open-jaw"),
    ],
)
def test_backend_gflight_envelope_holds_no_row_on_one_ticket(
    monkeypatch: pytest.MonkeyPatch, slices: tuple[str, ...], totals: list[int]
) -> None:
    """Google answered, with no row: the combinations are `split_ticket`'s,
    never a `results` row, and set no currency. Red at the base, which
    refused the search (exit 2)."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    env = _envelope_of(
        _search(
            "--cash-only", "--backend", "gflight", "--format", "envelope", "-n", "3", slices=slices
        )
    )
    assert (env["backend"], env["complete"], env["currency"]) == ("gflight", True, None)
    assert env["results"] == [{"cabin": "COACH", "rows": []}]
    assert _notes(env, "results") == [
        "results: Google Flights is asked no multi-city itinerary on one ticket; its separate "
        "tickets are in split_ticket"
    ]
    assert [c["total"] for c in env["split_ticket"]["combinations"]] == totals
    assert _notes(env, "split_ticket") == []
    assert matrix.searches == [] and len(google.calls) == len(slices)


@pytest.mark.parametrize("fmt", ["table", "json", "envelope"])
def test_backend_gflight_fails_on_a_failed_board(monkeypatch: pytest.MonkeyPatch, fmt: str) -> None:
    """Exit 1, a table's or JSON's stdout empty, the envelope incomplete. Red at
    the base, which refused the search (exit 2)."""
    google = _google(monkeypatch, ORD=RuntimeError("boom"))
    matrix = _matrix(monkeypatch)
    result = _run("--backend", "gflight", "--format", fmt)
    said = "No separate tickets on Google Flights: the ORD→BOS one-way failed (boom)."
    assert matrix.searches == [] and len(google.calls) == 2
    if fmt == "envelope":
        env = _envelope_of(result, code=1)
        assert env["complete"] is False and env["split_ticket"] == {
            "error": "the ORD→BOS one-way failed (boom)"
        }
        assert said in env["notes"]
        return
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert said in _stderr(result)


@pytest.mark.parametrize("fmt", ["table", "envelope"])
def test_backend_gflight_says_no_award_search_runs(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    """Awards are on wherever a provider is configured and --cash-only is not
    given; they match rows on one ticket, of which there are none. Red at the
    base, which refused the search (exit 2)."""
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
    result = _search("--backend", "gflight", "--format", fmt, slices=_slices())
    line = "No award search: awards are matched to rows on one ticket, and none is asked."
    assert matrix.searches == [] and len(google.calls) == 3
    if fmt == "envelope":
        env = _envelope_of(result)
        assert line in env["notes"]
        assert _notes(env, "awards") == [
            "awards: awards are matched to rows on one ticket, and none is asked"
        ]
        return
    assert result.exit_code == 0, result.output
    assert _stderr(result).count(line) == 1
    assert _TITLE in " ".join(result.stdout.split())


def test_backend_gflight_names_the_infants_empty_board_and_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red at the base, which refused the search (exit 2)."""
    _google(monkeypatch, ORD=[])
    _matrix(monkeypatch)
    env = _envelope_of(_run("--inf-lap", "1", "--backend", "gflight", "--format", "envelope"))
    assert (
        "No separate tickets on Google Flights: Google Flights served no rows for a party with "
        "an infant on the ORD→BOS one-way, as it has on routes with flights. For Matrix's "
        "answer, drop --backend gflight."
    ) in env["notes"]
    assert (env["backend"], env["complete"]) == ("gflight", False)


_ALONE = "--backend gflight answers a multi-city search with Google Flights' separate tickets alone"


@pytest.mark.parametrize(
    ("extra", "slices", "said"),
    [
        pytest.param(
            ("--no-separate-tickets",),
            None,
            f"{_ALONE}, and none is asked: --no-separate-tickets was given. Drop --backend "
            "gflight.",
            id="opted-out",
        ),
        pytest.param(
            ("--routing", "UA+"),
            None,
            f"{_ALONE}, and none is asked: --routing reaches no --slice, so the one-ways could "
            "not be held to it. Drop --backend gflight.",
            id="top-level-routing",
        ),
        pytest.param(
            (),
            ("SFO-ORD:{one}", "ORD-BOS:{two}:e=F BC=j", "BOS-SFO:{three}"),
            f"{_ALONE}, and none is asked: Google Flights can't serve slice 2 (ORD→BOS) as a "
            "one-way: extension 'F BC=j' not expressible on GF. Drop --backend gflight.",
            id="unservable-slice",
        ),
        pytest.param(
            ("--cabin", "economy,business"),
            None,
            f"{_ALONE}, and none is asked: each is priced in one cabin, and --cabin asks for 2. "
            "Drop --backend gflight.",
            id="multi-cabin",
        ),
        pytest.param(
            ("--awards-only",),
            None,
            f"--awards-only prints award space alone, and {_ALONE}. Drop either.",
            id="awards-only",
        ),
        pytest.param(
            ("--awards-only", "--format", "json"),
            None,
            f"--awards-only prints award space alone, and {_ALONE}. Drop --backend gflight, or "
            "drop --awards-only and add --cash-only.",
            id="awards-only-json",
        ),
        pytest.param(
            ("--award-json",),
            None,
            f"{_ALONE}, and an award search writes its own --format json document. Add "
            "--cash-only.",
            id="award-search-json",
        ),
        pytest.param(
            ("--sellers",),
            None,
            f"--sellers opens the booking page of a row on one ticket, and {_ALONE}. Drop it.",
            id="sellers",
        ),
        pytest.param(
            ("--enrich",),
            None,
            f"--enrich checks rows on one ticket against Matrix, and {_ALONE}. Drop it.",
            id="enrich",
        ),
        pytest.param(
            ("--bags", "1"),
            None,
            f"--bags prices bags on rows on one ticket, and {_ALONE}. Drop it.",
            id="bags",
        ),
        pytest.param(
            ("--exclude-basic",),
            None,
            f"--exclude-basic asks for rows on one ticket without basic economy, and {_ALONE}. "
            "Drop it.",
            id="exclude-basic",
        ),
        pytest.param(
            ("--arrive-times", "18:00-21:30"),
            None,
            f"--arrive-times holds rows on one ticket to a window, and {_ALONE}. Drop it.",
            id="arrive-times",
        ),
        pytest.param(
            ("--return", "{three}", "--return-arrive-times", "18:00-21:30"),
            None,
            f"--return-arrive-times holds rows on one ticket to a window, and {_ALONE}. Drop it.",
            id="return-arrive-times",
        ),
        pytest.param(
            ("--pick", "2"),
            None,
            f"--pick names a row on one ticket, and {_ALONE}. Drop it, or drop --backend gflight.",
            id="pick",
        ),
        pytest.param(
            ("--verify",),
            None,
            "--verify checks a Google Flights row on one ticket, and a --slice search shows none.",
            id="verify",
        ),
    ],
)
def test_backend_gflight_refuses_what_separate_tickets_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    slices: tuple[str, ...] | None,
    said: str,
) -> None:
    """Exit 2 before any request, naming the flag and a remedy that holds.
    Red at the base, which refused every one as "a multi-city itinerary"."""
    one, two, three = _days()
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    if "--award-json" in extra:
        monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
        argv = ["--backend", "gflight", "--format", "json"]
    elif "--awards-only" in extra:
        monkeypatch.setattr(cli, "_should_run_awards", _awards_on)
        argv = ["--backend", "gflight", *extra]
    else:
        argv = ["--cash-only", "--backend", "gflight"]
        argv += [a.format(one=one, two=two, three=three) for a in extra]
    given = tuple(s.format(one=one, two=two, three=three) for s in slices or _slices())
    result = _search(*argv, slices=given)
    assert result.exit_code == 2, result.output
    assert said in _stderr(result)
    assert google.calls == [] and matrix.searches == []


def _configured(sel: cli.ProviderSelection) -> bool:
    """An award provider is configured: awards run unless --cash-only."""
    return not sel.cash_only


@pytest.mark.parametrize(
    ("fmt", "remedy", "remedied"),
    [
        pytest.param(
            "table", "Drop either.", (("--awards-only",), ("--backend", "gflight")), id="table"
        ),
        pytest.param(
            "json",
            "Drop --backend gflight, or drop --awards-only and add --cash-only.",
            (("--awards-only",), ("--backend", "gflight", "--cash-only")),
            id="json",
        ),
    ],
)
def test_backend_gflight_awards_only_remedies_hold(
    monkeypatch: pytest.MonkeyPatch, fmt: str, remedy: str, remedied: tuple[tuple[str, ...], ...]
) -> None:
    """Each remedy the --awards-only refusal names answers once followed, with
    an award provider configured. Under --format json, dropping --awards-only
    alone still runs an award search, whose document has no place for the
    separate tickets, so that remedy adds --cash-only."""

    async def _gather(*, legs: list[Any], **_kw: Any) -> tuple[list[list[Any]], list[Any]]:
        return ([[] for _ in legs], [])

    monkeypatch.setattr(cli, "_should_run_awards", _configured)
    monkeypatch.setattr(pp_cli, "gather_awards", _gather)
    monkeypatch.setattr(pp_cli, "stored_tokens", lambda: None)
    monkeypatch.setattr(pp_cli, "load_tokens", lambda: None)
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    refused = _search("--backend", "gflight", "--awards-only", "--format", fmt, slices=_slices())
    assert refused.exit_code == 2, refused.output
    assert f"--awards-only prints award space alone, and {_ALONE}. {remedy}" in _stderr(refused)
    assert google.calls == [] and matrix.searches == []
    for argv in remedied:
        result = _search(*argv, "--format", fmt, slices=_slices())
        assert result.exit_code == 0, (argv, result.output)


def test_backend_gflight_still_refuses_a_slice_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ticket each way is a round trip Google sells as one; given as two
    --slice it stays refused. Green at the base."""
    one, _, three = _days()
    google, matrix = _google(monkeypatch), _matrix(monkeypatch)
    result = _run("--backend", "gflight", slices=(f"JFK-LHR:{one}", f"LHR-JFK:{three}"))
    assert result.exit_code == 2, result.output
    assert "--backend gflight can't serve this request: a multi-city itinerary." in _stderr(result)
    assert google.calls == [] and matrix.searches == []
