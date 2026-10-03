# pyright: reportPrivateUsage=false
"""Itineraries Google sells as separate tickets, read off its Cheapest tab.

The two FLL-LGA captures are the same round-trip query's outbound boards:
`ds1_fll_lga_rt_best.json` is the default board (58 rows, none marked) and
`ds1_fll_lga_rt_cheapest.json` the Cheapest tab (86 rows: 28 self transfers,
5 separate tickets booked together, 53 one ticket).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import _gflight_ids as gfid
from flight_cli import cli
from flight_cli._gf_common import PageFetch
from flight_cli.domain import Cabin, Leg, SearchOptions
from flight_cli.links import build_search_tfs, google_flights_search_page_url

if TYPE_CHECKING:
    from collections.abc import Callable

_ROOT = Path(__file__).resolve().parents[1]
_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_BEST = "ds1_fll_lga_rt_best.json"
_CHEAPEST = "ds1_fll_lga_rt_cheapest.json"
_LAX = "ds1_jfk_lax_tfu.json"
_LHR = "ds1_jfk_lhr_tfu.json"
_URL = "https://www.google.com/travel/flights?tfs=abc"
_CHEAPEST_TFU = "tfu=EggIABABIAIoASIA"
_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_FLL_LGA = [
    *("FLL", "LGA", "--dep", _DEP.isoformat(), "--return", _RET.isoformat()),
    *("--backend", "gflight", "--fast", "-n", "1000"),
]
_THROTTLE_PAGE = "<html>Our systems have detected unusual traffic</html>"
_SHAPELESS_PAGE = "<html><body>no flight data here</body></html>"
_KEY = (
    "† separate tickets: Google sells this trip as more than one booking. "
    "‡ self transfer: separate tickets, and you collect and recheck bags between flights."
)
_OUTBOUND_ONLY = (
    "A round trip on separate tickets lists its outbound only: Google prices the whole "
    "trip but serves no return for it."
)


def _served(name: str, *, origin: str | None = None, destination: str | None = None) -> str:
    day = _RET if origin == "LGA" else _DEP
    return _page(
        _answering(_ds1(name), origin=origin, destination=destination, date=day.isoformat())
    )


def _parsed(name: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(_page(_ds1(name)), _URL, 200))


def _fll_lga_pages() -> list[str]:
    """The default board, a return board for each of the ten pins, then the
    Cheapest tab."""
    ret = _served("ds1_return_leg_pinned.json", origin="LGA", destination="FLL")
    return [_served(_BEST), *[ret] * gfid.pinned_fanout(1000), _served(_CHEAPEST)]


def _round_trip() -> tuple[Leg, ...]:
    return (Leg.of("FLL", "LGA", _DEP), Leg.of("LGA", "FLL", _RET))


def _lax_with_marked_twin(index: int, *, price: int | None = None) -> str:
    """The LAX capture with row `index` sold as a self transfer, at `price`
    when given: the Cheapest twin of the unmarked board."""
    payload: list[Any] = json.loads(_ds1(_LAX))
    row = gfid._rows_from_ds1(payload).rows[index]
    row[7] = [1]
    if price is not None:
        row[1][0][1] = price
    return _page(
        _answering(json.dumps(payload), origin=None, destination=None, date=_DEP.isoformat())
    )


def _document(capsys: pytest.CaptureFixture[str], **kw: Any) -> list[Any]:
    cli._run_gflight_path(opts=SearchOptions(cabin=Cabin.COACH), top_n=1000, json_out=True, **kw)
    return json.loads(capsys.readouterr().out)


def _members(doc: list[Any]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = [r if isinstance(r, list) else [r] for r in doc]
    return [m for r in rows for m in r]


# ──────────────────────────────── the request ─────────────────────────────


def test_the_cheapest_tab_url_differs_from_the_board_url_only_in_tfu() -> None:
    from flight_cli.fli_bridge import to_fli_filter

    filters = to_fli_filter(
        cli.SpecificDateSearch(legs=_round_trip(), options=SearchOptions(cabin=Cabin.COACH))
    )
    tfs = build_search_tfs(filters)
    base = urllib.parse.urlsplit(google_flights_search_page_url(tfs))
    cheapest = urllib.parse.urlsplit(google_flights_search_page_url(tfs, cheapest=True))
    base_q = urllib.parse.parse_qs(base.query)
    cheap_q = urllib.parse.parse_qs(cheapest.query)
    assert base_q.pop("tfu") == ["EgQIABABIgA"]
    assert cheap_q.pop("tfu") == ["EggIABABIAIoASIA"]
    assert base_q == cheap_q
    assert base._replace(query="") == cheapest._replace(query="")
    assert google_flights_search_page_url(tfs, cheapest=False) == google_flights_search_page_url(
        tfs
    )
    assert gfid.search_page_url(filters, cheapest=True).endswith(f"&{_CHEAPEST_TFU}")


# ──────────────────────────────── the decode ──────────────────────────────


@pytest.mark.parametrize(
    ("name", "self_transfers", "separate", "one_ticket"),
    [(_CHEAPEST, 28, 5, 53), (_BEST, 0, 0, 58)],
)
def test_row_seven_says_how_google_sells_each_row(
    name: str, self_transfers: int, separate: int, one_ticket: int
) -> None:
    board = _parsed(name)
    kinds = [r.ticketing for r in board]
    assert kinds.count("self_transfer") == self_transfers
    assert kinds.count("separate_tickets") == separate
    assert kinds.count(None) == one_ticket
    assert all(
        r.flight.self_transfer is (r.ticketing == "self_transfer") for r in board
    )  # never None: every row of both captures states its ticketing


@pytest.mark.parametrize(
    ("slot", "ticketing", "self_transfer"),
    [
        ([1], "self_transfer", True),
        ([2], "separate_tickets", False),
        ([2, 1], "self_transfer", True),
        ([], None, False),
        ([5], None, False),
        (None, None, None),
        (7, None, None),
    ],
)
def test_a_row_seven_slot_decodes_or_says_nothing(
    slot: Any, ticketing: str | None, self_transfer: bool | None
) -> None:
    raw: list[Any] = json.loads(json.dumps(gfid._rows_from_ds1(json.loads(_ds1(_BEST))).rows[0]))
    raw[7] = slot
    row = gfid._parse_flight_with_id(raw)
    assert row.ticketing == ticketing
    assert row.flight.self_transfer is self_transfer
    del raw[7:]
    short = gfid._parse_flight_with_id(raw)
    assert (short.ticketing, short.flight.self_transfer) == (None, None)


# ───────────────────────────── fetch and merge ────────────────────────────


def test_a_round_trip_adds_each_separate_ticket_outbound_as_a_row_of_its_own(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The base combinations as the base builds them, then 33 one-member rows
    at Google's round-trip total, for one more GET of the Cheapest tab."""
    base_fake = gf_session(*_fll_lga_pages())
    base = _document(capsys, legs=_round_trip())
    fake = gf_session(*_fll_lga_pages())
    doc = _document(capsys, legs=_round_trip(), separate_tickets="show")
    assert len(fake.gets) == len(base_fake.gets) + 1 == 12
    assert fake.gets[:-1] == base_fake.gets
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert [r for r in doc if len(r) == 2] == base
    alone = [r for r in doc if len(r) == 1]
    assert len(alone) == 33
    assert all(m["separate_tickets"] is True for (m,) in alone)
    totals = {r.flight_id: r.flight.price for r in _parsed(_CHEAPEST) if r.ticketing}
    assert sorted((m["flight_id"], m["price"]) for (m,) in alone) == sorted(totals.items())


def test_a_one_way_adds_the_marked_row_of_the_cheapest_tab(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Its flights are on the base board too, one ticket at the same fare; it
    is still added, as a different booking."""
    legs = (Leg.of("JFK", "LAX", _DEP),)
    gf_session(_served(_LAX))
    base = _document(capsys, legs=legs)
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    doc = _document(capsys, legs=legs, separate_tickets="show")
    assert len(fake.gets) == 2
    marked = [r for r in doc if r["separate_tickets"]]
    assert [(r["flight_id"], r["self_transfer"]) for r in marked] == [("aR5Sef", True)]
    assert [r for r in doc if not r["separate_tickets"]] == base
    assert len(doc) == len(base) + 1


def test_a_refused_cheapest_tab_leaves_the_base_answer_and_says_why(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    legs = (Leg.of("JFK", "LAX", _DEP),)
    gf_session(_served(_LAX))
    base = _document(capsys, legs=legs)
    gf_session(_served(_LAX), _THROTTLE_PAGE)
    cli._run_gflight_path(
        legs=legs,
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=True,
        separate_tickets="show",
    )
    out, err = capsys.readouterr()
    assert json.loads(out) == base
    said = " ".join(err.split())
    assert said.count("Itineraries on separate tickets not read") == 1
    assert "Itineraries on separate tickets not read: Google Flights rate-limited." in said


def test_pinning_that_stopped_on_a_wall_skips_the_cheapest_tab(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The second pin meets the throttle; the Cheapest tab would meet it too."""
    pages = _fll_lga_pages()
    fake = gf_session(pages[0], pages[1], _THROTTLE_PAGE)
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=1000,
        json_out=True,
        separate_tickets="show",
    )
    out, err = capsys.readouterr()
    assert all(len(r) == 2 for r in json.loads(out))
    assert not any(_CHEAPEST_TFU in url for url in fake.gets)
    assert "Itineraries on separate tickets not read: Google Flights rate-limited." in " ".join(
        err.split()
    )


def test_a_round_trip_whose_every_return_board_refused_still_shows_its_separate_tickets(
    gf_session: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No pin's return board parses, so no one-ticket trip was served; a
    separate-ticket outbound needs no return board and is served all the same."""
    pins = gfid.pinned_fanout(1000)
    pages = [_served(_BEST), *[_SHAPELESS_PAGE] * pins, _served(_CHEAPEST)]
    fake = gf_session(*pages)
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        doc = _document(capsys, legs=_round_trip(), separate_tickets="show")
    assert len(fake.gets) == pins + 2
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert len(doc) == 33
    assert all(len(r) == 1 and r[0]["separate_tickets"] is True for r in doc)
    assert f"{pins} of {pins} return boards unavailable" in caplog.text
    gf_session(*pages)
    with pytest.raises(typer.Exit) as hidden:
        _document(capsys, legs=_round_trip(), separate_tickets="hide")
    assert hidden.value.exit_code == 1


@pytest.mark.parametrize(
    ("asked", "checks"),
    [
        (["--ext", "-AIRLINES AA"], "a carrier exclusion (AA)"),
        (["--return-times", "morning"], "a return-time window (morning)"),
    ],
)
def test_a_return_only_the_row_filter_checks_keeps_separate_tickets_unread(
    gf_session: Callable[..., Any], asked: list[str], checks: str
) -> None:
    """A separate-ticket round trip comes without its return, so a check that
    only the row filter makes, never Google's query, could not be made on it."""
    fake = gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json", *asked])
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 11
    assert not any(_CHEAPEST_TFU in url for url in fake.gets)
    assert all(len(r) == 2 for r in json.loads(result.stdout))
    said = " ".join(result.stderr.split())
    assert said.count("Itineraries on separate tickets") == 1
    assert (
        f"Itineraries on separate tickets not read: Google lists no return for them "
        f"to check against {checks}." in said
    )
    gf_session(*_fll_lga_pages())
    hidden = CliRunner().invoke(
        cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json", *asked, "--no-separate-tickets"]
    )
    assert hidden.exit_code == 0, hidden.output
    assert "separate tickets" not in hidden.stderr


def test_an_excluded_carrier_hands_a_round_trip_to_matrix_as_at_the_base(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pinned return flies the excluded carrier, so Google's answer is
    empty and auto hands the search to Matrix."""
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    fake = gf_session(*_fll_lga_pages())
    args = ["auto" if a == "gflight" else a for a in _FLL_LGA]
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, *args, "--format", "json", "--ext", "-AIRLINES AA"]
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]
    assert len(fake.gets) == 11
    assert "Using Matrix: no Google Flights itinerary matched a carrier exclusion (AA)" in (
        " ".join(result.stderr.split())
    )


def test_an_outbound_window_still_reads_the_cheapest_tab(
    gf_session: Callable[..., Any],
) -> None:
    """The outbound is the row the filter sees, so its window is checked."""
    fake = gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json", "--depart-times", "morning,midday"]
    )
    assert result.exit_code == 0, result.output
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert "not read" not in result.stderr


@pytest.mark.parametrize("flag", ["run_pp", "sellers", "verify"])
def test_a_path_that_renders_through_the_adapter_reads_no_cheapest_tab(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Awards, `--sellers` and `--verify` print rows the adapter built, which
    carries no mark, so no separate-ticket row may reach them."""
    asked: list[Any] = []

    def _gf(*_a: Any, separate_tickets: str = "off", **_kw: Any) -> gfid.Board[Any]:
        asked.append(separate_tickets)
        return gfid.Board()

    monkeypatch.setattr(cli, "_gflight_results", _gf)

    def _no_awards(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    # An empty board leaves `--sellers` and `--verify` no row to open: they exit.
    with contextlib.suppress(typer.Exit):
        cli._run_gflight_path(
            legs=(Leg.of("JFK", "LAX", _DEP),),
            opts=SearchOptions(cabin=Cabin.COACH),
            top_n=10,
            json_out=False,
            separate_tickets="show",
            run_pp=flag == "run_pp",
            sellers=flag == "sellers",
            verify=flag == "verify",
        )
    assert asked == ["off"]


def test_the_enriched_table_reads_no_cheapest_tab_and_takes_the_opt_out(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged table cannot mark a row, so the default search makes the
    GET it always made, and `--no-separate-tickets` has nothing to hide."""

    async def _no_matrix(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = None

    monkeypatch.setattr(cli, "_matrix_into", _no_matrix)
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--no-separate-tickets",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 1
    assert "hidden" not in result.stderr


def test_a_matrix_search_takes_the_opt_out_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "matrix",
            "--no-separate-tickets",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]


@pytest.mark.parametrize("asked", [["--fast"], ["--format", "json"]])
def test_the_skill_and_the_help_name_the_searches_that_read_the_cheapest_tab(
    gf_session: Callable[..., Any], asked: list[str]
) -> None:
    """Each search the skill names reads the tab. A `--backend gflight` table
    without `--fast` is the enriched one, which does not."""
    import click

    fake = gf_session(*_fll_lga_pages())
    args = [a for a in _FLL_LGA if a != "--fast"]
    result = CliRunner().invoke(cli.app, [*_SEARCH, *args, *asked], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert _CHEAPEST_TFU in fake.gets[-1]
    skill = _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md"
    (line,) = [ln for ln in skill.read_text().splitlines() if "come from its Cheapest tab" in ln]
    assert f"`{' '.join([*asked, '--cash-only'])}`" in line
    assert "--backend gflight" not in line
    group = typer.main.get_command(cli.app)
    assert isinstance(group, click.Group)
    (flag,) = [
        p
        for p in group.commands["search"].params
        if isinstance(p, click.Option) and "--no-separate-tickets" in p.opts
    ]
    assert "--cash-only" in (flag.help or "")


def test_the_deprecated_command_reads_no_cheapest_tab(gf_session: Callable[..., Any]) -> None:
    fake = gf_session(_served(_LAX), _lax_with_marked_twin(5))
    result = CliRunner().invoke(cli.app, ["gflight", "JFK", "LAX", "--dep", _DEP.isoformat()])
    assert result.exit_code == 0, result.output
    assert len(fake.gets) == 1


# ──────────────────────────────── the table ───────────────────────────────

_ROW = re.compile(r"^│\s*(\d+[ab]?)\s*│([^│]*)│")


def _price_cells(stdout: str) -> dict[str, str]:
    return {m[1]: m[2].strip() for line in stdout.splitlines() if (m := _ROW.match(line))}


def test_the_table_marks_exactly_the_separate_ticket_rows_and_keys_them_once(
    gf_session: Callable[..., Any],
) -> None:
    gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    cells = _price_cells(result.stdout)
    marked = {label: cell for label, cell in cells.items() if cell.endswith(("†", "‡"))}
    assert sum(cell.endswith(" ‡") for cell in marked.values()) == 28
    assert sum(cell.endswith(" †") for cell in marked.values()) == 5
    assert all(label.isdigit() for label in marked)
    assert len(cells) - len(marked) == 60  # 30 pairs, an `a` and a `b` row each
    said = " ".join(result.stdout.split())
    assert said.count(_KEY) == 1
    assert said.count(_OUTBOUND_ONLY) == 1


def test_a_board_with_no_separate_ticket_row_prints_what_the_base_prints(
    gf_session: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cheapest tab is read and holds no marked row, so nothing it carries
    reaches the table."""
    monkeypatch.setenv("COLUMNS", "200")
    fake = gf_session(_served(_LHR))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=10,
        json_out=False,
        separate_tickets="show",
    )
    shown = capsys.readouterr().out
    assert len(fake.gets) == 2
    gf_session(_served(_LHR))
    cli._run_gflight_path(
        legs=(Leg.of("JFK", "LHR", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=10,
        json_out=False,
    )
    assert shown == capsys.readouterr().out
    assert "†" not in shown
    assert "‡" not in shown


def test_a_link_never_pins_a_separate_ticket_row(gf_session: Callable[..., Any]) -> None:
    """Row 1 is the marked twin, priced below the board: a link pinned to it
    would open a one-ticket page, so the link pre-fills the search instead."""
    args = [
        *("search", "--cash-only", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()),
        *("--backend", "gflight", "--fast", "-n", "5"),
    ]
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    marked = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})
    assert marked.exit_code == 0, marked.output
    assert _price_cells(marked.stdout)["1"].endswith(" ‡")
    assert "Google Flights (tfs= structured):" in marked.stdout
    assert "pinned" not in marked.stdout
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=100))
    hidden = CliRunner().invoke(cli.app, [*args, "--no-separate-tickets"], env={"COLUMNS": "200"})
    assert hidden.exit_code == 0, hidden.output
    assert "Google Flights (itinerary #1 pinned):" in hidden.stdout


def test_the_price_insight_counts_the_separate_ticket_fares_a_filtered_board_shows(
    gf_session: Callable[..., Any],
) -> None:
    """JetBlue sells this trip at USD307 on one ticket, above Google's usual
    USD120-260, and at USD247 on separate tickets, inside it."""
    ret = _served(_BEST, origin="LGA", destination="FLL")
    pages = [_served(_BEST), *[ret] * 5, _served(_CHEAPEST)]
    asked = [*_SEARCH, *_FLL_LGA, "--ext", "AIRLINES B6"]
    fake = gf_session(*pages)
    shown = CliRunner().invoke(cli.app, asked, env={"COLUMNS": "200"})
    assert shown.exit_code == 0, shown.output
    assert len(fake.gets) == 7
    assert _CHEAPEST_TFU in fake.gets[-1]
    assert any(cell.endswith(" †") for cell in _price_cells(shown.stdout).values())
    assert "Price insight: prices are typical for this trip" in " ".join(shown.stdout.split())
    gf_session(*pages)
    hidden = CliRunner().invoke(cli.app, [*asked, "--no-separate-tickets"], env={"COLUMNS": "200"})
    assert hidden.exit_code == 0, hidden.output
    assert "Price insight: prices are high for this trip" in " ".join(hidden.stdout.split())


def test_the_price_insight_counts_a_separate_ticket_fare_below_googles_cheapest(
    gf_session: Callable[..., Any],
) -> None:
    """Google's own cheapest, USD204, is inside its usual USD85-225; the
    self transfer the table shows at USD80 is below it."""
    args = [
        *("search", "--cash-only", "--no-matrix-url", "JFK", "LAX", "--dep", _DEP.isoformat()),
        *("--backend", "gflight", "--fast", "-n", "5"),
    ]
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=80))
    shown = CliRunner().invoke(cli.app, args, env={"COLUMNS": "200"})
    assert shown.exit_code == 0, shown.output
    assert _price_cells(shown.stdout)["1"].endswith(" ‡")
    assert "Price insight: prices are low for this trip" in " ".join(shown.stdout.split())
    gf_session(_served(_LAX), _lax_with_marked_twin(5, price=80))
    hidden = CliRunner().invoke(cli.app, [*args, "--no-separate-tickets"], env={"COLUMNS": "200"})
    assert hidden.exit_code == 0, hidden.output
    assert "Price insight: prices are typical for this trip" in " ".join(hidden.stdout.split())


# ──────────────────────────────── the JSON ────────────────────────────────


def test_every_member_states_how_it_is_sold(gf_session: Callable[..., Any]) -> None:
    gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json"])
    assert result.exit_code == 0, result.output
    doc: list[Any] = json.loads(result.stdout)
    members = _members(doc)
    assert all(type(m["separate_tickets"]) is bool for m in members)
    assert sum(m["separate_tickets"] for m in members) == 33
    assert sum(m["self_transfer"] is True for m in members) == 28
    assert all(m["separate_tickets"] for m in members if m["self_transfer"])
    assert all(len(r) == 1 for r in doc if any(m["separate_tickets"] for m in r))


def test_a_row_that_states_nothing_is_null_in_the_document() -> None:
    raw: list[Any] = json.loads(json.dumps(gfid._rows_from_ds1(json.loads(_ds1(_BEST))).rows[0]))
    raw[7] = None
    (row,) = cli._gflight_json_document([gfid._parse_flight_with_id(raw)])
    assert row["separate_tickets"] is None
    assert row["self_transfer"] is None


# ─────────────────────────────── the opt-out ──────────────────────────────


def test_the_opt_out_prints_the_base_rows_and_counts_what_it_hid(
    gf_session: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    gf_session(*_fll_lga_pages())
    base = _document(capsys, legs=_round_trip())
    fake = gf_session(*_fll_lga_pages())
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, *_FLL_LGA, "--format", "json", "--no-separate-tickets"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == base
    assert len(fake.gets) == 12
    said = " ".join(result.stderr.split())
    assert (
        "Google Flights: 33 itineraries on separate tickets hidden (--no-separate-tickets)." in said
    )


@pytest.mark.parametrize(
    ("cheapest", "asked", "note"),
    [
        (
            _served(_CHEAPEST),
            ["--no-separate-tickets"],
            "Google Flights: 4 itineraries on separate tickets hidden (--no-separate-tickets).",
        ),
        (
            _THROTTLE_PAGE,
            [],
            "Itineraries on separate tickets not read: Google Flights rate-limited.",
        ),
    ],
    ids=["hidden", "unread"],
)
def test_a_search_handed_to_matrix_still_says_what_became_of_separate_tickets(
    gf_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    cheapest: str,
    asked: list[str],
    note: str,
) -> None:
    """No one-ticket JetBlue trip is under USD250, so Google's answer is empty
    and auto hands the search to Matrix; its four separate-ticket trips at
    USD247 would have answered it, had they been shown or read."""
    ran: list[bool] = []

    def _matrix(**_kw: object) -> None:
        ran.append(True)

    monkeypatch.setattr(cli, "_run_matrix_path", _matrix)
    gf_session(_served(_BEST), cheapest)
    args = ["auto" if a == "gflight" else a for a in _FLL_LGA]
    result = CliRunner().invoke(
        cli.app, [*_SEARCH, *args, "--ext", "AIRLINES B6", "--max-price", "250", *asked]
    )
    assert result.exit_code == 0, result.output
    assert ran == [True]
    said = " ".join(result.stderr.split())
    assert said.count("separate tickets") == 1
    assert note in said
    assert "Using Matrix: no Google Flights itinerary matched" in said


def test_the_opt_out_says_nothing_when_it_hid_nothing(gf_session: Callable[..., Any]) -> None:
    gf_session(_served(_LAX))
    result = CliRunner().invoke(
        cli.app,
        [
            *_SEARCH,
            "JFK",
            "LAX",
            "--dep",
            _DEP.isoformat(),
            "--backend",
            "gflight",
            "--fast",
            "--format",
            "json",
            "--no-separate-tickets",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "separate tickets" not in result.stderr
