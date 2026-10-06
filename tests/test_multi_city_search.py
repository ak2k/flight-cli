# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false
"""`flight search` on a multi-city trip of three slices: Google Flights'
cheapest one-way per slice, combined as separate tickets, beside Matrix's
one-ticket answer.

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
from test_envelope import _envelope_of
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
