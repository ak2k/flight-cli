# pyright: reportPrivateUsage=false
"""`-n` is one number for everything the user can act on.

Google's search page serves its whole board — around thirty rows — whatever
count is asked of it, so the count is applied on the way out. What is under
test is that the table, the JSON document, the pinned deep link and the award
matcher are all handed the SAME set, and that the wide board still reaches the
one thing that needs it — the Tier-2 post-filter, which runs before the trim
because a routing constraint is answered out of the whole board or answered
wrong.

WHICH rows survive is under test too, and the answer differs by set. A one-way
board is trimmed in Google's ranking, which the page decides and nothing here
reproduces; a round trip's combinations carry no ranking of their own — they
are built pin-major by the fan-out — so they are trimmed by price.

The multi-cabin arms are where only the COUNT agrees: the JSON document carries
the first `-n` of each cabin's own board and the table carries the top `-n` of
the join, which is the same number of rows drawn from different sets.

No rows is a value and has its own shape: an empty board is `[]` under
`--format json` and a sentence on stdout otherwise, while a query that failed is
zero bytes and exit 1. That is how a consumer tells no rows from no answer.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from flight_cli import cli
from flight_cli.domain import Cabin, Leg, SearchOptions

if TYPE_CHECKING:
    from collections.abc import Callable

    from flight_cli.models import SearchResult

# fli's own validator rejects a past travel date, so these are derived rather
# than pinned: a literal rots the suite on the day it passes.
_DEP = date.today() + timedelta(days=45)
_RET = date.today() + timedelta(days=52)
_BOARD_ROWS = 30


def _one_way(route_language: str | None = None) -> tuple[Leg, ...]:
    return (Leg.of("HNL", "MIA", _DEP, route_language=route_language),)


def _round_trip() -> tuple[Leg, ...]:
    return (Leg.of("HNL", "MIA", _DEP), Leg.of("MIA", "HNL", _RET))


def _served_return(gf_answering: Callable[..., str]) -> str:
    """The return board Google served with an outbound pinned, answering the
    leg these tests ask for.

    The capture is a real page for a real day, and a search built from `today`
    names another one — a board answering neither the route nor the date asked
    for is what a page that dropped the pin looks like, and the pin loop refuses
    it. Re-pointing keeps every price, id and carrier the pairing argument rests
    on."""
    return gf_answering(
        "ds1_return_leg_pinned.json", origin="MIA", destination="HNL", date=_RET.isoformat()
    )


def _json_rows(capsys: pytest.CaptureFixture[str]) -> list[Any]:
    out = capsys.readouterr().out
    parsed: Any = json.loads(out)
    return list(parsed)


@pytest.mark.parametrize("top_n", [1, 5, 12])
def test_the_json_document_carries_the_rows_the_table_numbered(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
    top_n: int,
) -> None:
    """`--format json -n 5` is the same five itineraries the table numbers 1-5.
    Emitting the whole board there hands a machine consumer rows that no human
    reading the same query would have been shown."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=top_n,
        json_out=True,
    )
    assert len(_json_rows(capsys)) == min(top_n, _BOARD_ROWS)


def test_a_round_trips_json_document_counts_combinations_not_boards(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    gf_answering: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A round trip multiplies: three outbounds pinned against three returns is
    nine itineraries out of two boards. `-n 2` means two of them, each still a
    two-member itinerary."""
    # The matched pair of live captures: an outbound board, then the return
    # board Google served with one of those outbounds pinned.
    gf_session(
        gf_capture("ds1_metadata_blocks_kept.json"),
        _served_return(gf_answering),
    )
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=2,
        json_out=True,
    )
    rows = _json_rows(capsys)
    assert len(rows) == 2
    assert all(len(r) == 2 for r in rows), rows


def test_a_pick_past_the_visible_table_warns_and_falls_back_to_the_cheapest(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--pick 6` on an `-n 5` table names a row that was never printed. It has
    to take the out-of-range path — the warning AND the cheapest row — rather
    than quietly pinning something out of the part of the board the user never
    saw, which is indistinguishable in the emitted link from the row they
    asked for.

    Both halves, because something IS pinned: a regression that printed the
    warning and then honoured the index would emit a link to a row nobody was
    shown under a label saying it is the one they asked for.

    The warning is on stderr, where every other note on this path goes, so the
    stdout of a `--format json` run stays a document."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=False,
        google_url=True,
        pick=6,
    )
    captured = capsys.readouterr()
    assert "--pick 6 is out of range (1-5)" in captured.err, captured.err
    assert "--pick 6 is out of range" not in captured.out, captured.out
    # Both halves, because a link IS emitted here: the sentence promises a
    # fallback and the label on stdout is that fallback happening. Both name
    # ROW ONE — a one-way board keeps Google's ranking, so "the cheapest" would
    # describe a different row from the one the link opens.
    assert "pinning itinerary #1 instead" in captured.err, captured.err
    assert "itinerary #1 pinned" in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out


def test_a_pick_inside_the_visible_table_still_pins(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of the same rule: the last row the table printed is a
    valid pick. A trim that took the range with it would refuse every pick but
    the cheapest."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=False,
        google_url=True,
        pick=5,
    )
    assert "out of range" not in capsys.readouterr().out


def test_the_pin_label_names_the_row_it_pinned_on_a_one_way_board(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pin is right and its SENTENCE can still be false.

    A one-way board keeps Google's own ranking, so row 1 need not be the
    cheapest — on this capture `-n 3` prints 6590, 6616 and then 6072. The link
    goes to row 1, correctly; a label reading "cheapest itinerary" over it names
    a row the table shows two places further down, and nothing on screen says
    which of the two the link honoured.

    So the rule is that the label names the row it pins. "Cheapest" is then a
    word this path cannot print, because it has no way to be true."""
    gf_session(gf_capture("ds1_metadata_blocks_kept.json"))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=3,
        json_out=False,
        google_url=True,
    )
    captured = capsys.readouterr()

    assert "itinerary #1 pinned" in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out
    # The row it names is the row the table put first, and it is not the
    # cheapest of the three — without that the assertion above holds vacuously.
    where = [captured.out.index(p) for p in ("USD6590.00", "USD6616.00", "USD6072.00")]
    assert where == sorted(where), captured.out


def test_the_award_matcher_is_given_the_rows_the_user_saw(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Awards are fanned out per itinerary and priced against its cash fare, so
    a matcher handed the whole board spends thirty lookups to report on rows
    the user cannot pick."""
    seen: list[SearchResult] = []

    def _capture(sr: SearchResult, **_kw: object) -> None:
        seen.append(sr)

    monkeypatch.setattr(cli, "run_pp_for_search", _capture)
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=4,
        json_out=False,
        run_pp=True,
        sel=cli._resolve_providers(
            providers=None, cash_only=False, awards_only=False, provider_opt=()
        ),
    )
    assert len(seen) == 1
    assert len(seen[0].solutions) == 4


# ─────────── the enriched path: --pick names a row on the MERGED table ───────


def _dearer_matrix() -> Any:
    """A Matrix half whose every fare is dearer than the Google board's.

    Its solutions carry no itinerary structure, so the merge cannot match them
    to a Google row and keeps both sets — which is the ordinary shape here, and
    the one where the merged table is longer than either list that built it."""
    from flight_cli.models import SearchResult

    return SearchResult.model_validate(
        {
            "solutions": [{"displayTotal": "USD9000.00"}, {"displayTotal": "USD9500.00"}],
            "solutionCount": 2,
        }
    )


def _enriched(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    top_n: int,
    pick: int | None = None,
) -> None:
    """The real enriched path with both halves answered in process."""
    matrix = _dearer_matrix()

    async def _stashes_matrix(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = matrix

    def _gf(*_a: object, **_kw: object) -> list[Any]:
        return rows

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    monkeypatch.setattr(cli, "_matrix_into", _stashes_matrix)
    cli._run_enriched_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=top_n,
        run_pp=False,
        sel=None,
        matrix_url=True,
        google_url=True,
        pick=pick,
        rps=1.0,
        impersonate="chrome",
        no_cache=True,
    )


def test_the_enriched_pin_names_a_row_on_the_table_that_was_printed(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--pick N` means the row numbered N in the table on screen.

    The merged table is price-sorted and holds Google-only rows the Matrix half
    never had, so its order and its length both differ from the Matrix solution
    list. Indexed against that list instead, `--pick 1` emits links for whatever
    row the merge happened to move — a wrong itinerary under the number the user
    read off the screen, and nothing on either stream to say so."""
    _enriched(monkeypatch, gf_rows("ds1_metadata_blocks_kept.json"), top_n=3, pick=1)
    captured = capsys.readouterr()

    assert "itinerary #1 pinned" in captured.out, captured.out
    assert "out of range" not in captured.out + captured.err, captured.out
    # Pinned from the merged row's own slices — a Matrix solution with no
    # itinerary structure could not have produced this line at all.
    assert "Google Flights (itinerary #1 pinned):" in captured.out, captured.out
    # Row 1 of the printed table is the cheapest of the five merged rows, and
    # it is a Google-only one: the pinned URL carries ITS flights.
    assert "USD6072.00" in captured.out, captured.out


def test_the_enriched_default_pin_labels_the_row_it_printed_first(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no `--pick` the link goes to the first row of the merged table, so
    that is what the label says. "Cheapest itinerary" over a link built from a
    different list is the same false sentence one row further along."""
    _enriched(monkeypatch, gf_rows("ds1_metadata_blocks_kept.json"), top_n=3)
    captured = capsys.readouterr()

    assert "itinerary #1 pinned" in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out


def test_the_enriched_pick_range_is_the_count_the_table_printed(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Five rows merge and three are printed, so four is out of range and two of
    the three numbers between them are not.

    The range a pick is measured against is the VISIBLE count, decided at the
    trim. Measured against the merged list it would accept a row nobody saw;
    measured against the Matrix solutions it would refuse row 3, which the table
    printed. The warning takes stderr, so a `--format json` run stays a
    document."""
    _enriched(monkeypatch, gf_rows("ds1_metadata_blocks_kept.json"), top_n=3, pick=4)
    captured = capsys.readouterr()

    assert "--pick 4 is out of range (1-3)" in captured.err, captured.err
    assert "out of range" not in captured.out, captured.out
    assert "itinerary #1 pinned" in captured.out, captured.out


def test_a_pick_the_enriched_table_printed_is_honoured(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half: row 3 of a three-row table is a valid pick, even though
    the Matrix half contributed only two solutions. A range taken from the wrong
    list refuses a row the user is looking at."""
    _enriched(monkeypatch, gf_rows("ds1_metadata_blocks_kept.json"), top_n=3, pick=3)
    captured = capsys.readouterr()

    assert "out of range" not in captured.out + captured.err, captured.out + captured.err
    assert "itinerary #3 pinned" in captured.out, captured.out


def test_the_routing_post_filter_still_reads_the_whole_board(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The trim is on the way OUT, and this is why it cannot move into the
    query. A Tier-2 routing constraint is evaluated over every row Google
    served: the one flight that satisfies it here sits at row 25 of 30, well
    past an `-n 5` window, and a search narrowed to five rows before filtering
    would answer a satisfiable constraint with 'no results'."""
    keeper = 24
    gf_session(gf_board(_BOARD_ROWS, distinct_at=keeper))
    cli._run_gflight_path(
        legs=_one_way(route_language="AS627"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=True,
    )
    rows = _json_rows(capsys)
    assert len(rows) == 1
    assert [leg["flight_number"] for leg in rows[0]["legs"]] == ["627", "305"]


def _prices(rows: list[Any]) -> list[float]:
    """Every price the emitted document carries, in the order it carries it."""
    out: list[float] = []
    for row in rows:
        members: list[Any] = cast("list[Any]", row) if isinstance(row, list) else [row]
        out.extend(float(m["price"]) for m in members)
    return out


def test_a_one_way_board_is_trimmed_in_the_order_google_ranked_it(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which rows survive, not how many.

    The captured board is deliberately not price-ordered — Google's ranking is
    a composite of price, duration and stops, and the page hands over its "best
    flights" block followed by the rest. Reproducing that ranking is not
    possible from here, so the trim keeps it: `-n 2` is the two rows the page
    put first, which is what the table showed the day the capture was taken.

    A sort by price here would look like an improvement and would answer a
    one-way query in an order Google did not choose."""
    gf_session(gf_capture("ds1_metadata_blocks_kept.json"))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=2,
        json_out=True,
    )
    assert _prices(_json_rows(capsys)) == [6590.0, 6616.0]


def test_a_round_trips_combinations_are_trimmed_by_price(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    gf_answering: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A round trip is the other set, and it carries no ranking of its own.

    The combinations are built pin-major — every return against outbound one,
    then every return against outbound two — so the first `-n` of them are the
    returns of the first outbound and nothing else. On this fixture pair `-n 3`
    would be three trips from one outbound, with cheaper trips from the next
    outbound left off the table entirely.

    Each combination is priced at its terminal member, so ordering by that is
    ordering by the number every surface prints."""
    gf_session(
        gf_capture("ds1_metadata_blocks_kept.json"),
        _served_return(gf_answering),
    )
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=3,
        json_out=True,
    )
    rows = _json_rows(capsys)
    totals = [float(row[-1]["price"]) for row in rows]
    assert totals == sorted(totals), totals
    outbounds = {row[0]["flight_id"] for row in rows}
    assert len(outbounds) > 1, f"every visible trip is one outbound: {rows}"
    # The visible three by name. Six of the nine combinations this pair builds
    # tie at the same total, so a monotonic check passes on a pin-major list and
    # the tie-break alone decides every row on screen — cardinality and ordering
    # are both satisfied by the wrong three. Naming them is what makes the
    # documented stability of the sort a thing a test can lose.
    assert [(r[0]["flight_id"], r[-1]["flight_id"]) for r in rows] == [
        ("Ulft7e", "iQwZab"),
        ("Ulft7e", "zIxVxf"),
        ("FWXCne", "iQwZab"),
    ], rows


def test_a_round_trip_table_prints_its_rows_in_price_order(
    gf_rows: Callable[[str], list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The table applies the count itself, so it orders for itself too.

    The enriched first paint reaches a trim only here — it hands the renderer
    the whole board — so a table drawn from a pin-major list would show the
    same wrong three rows a pin-major order produces."""
    board = gf_rows("ds1_metadata_blocks_kept.json")
    dearest, cheapest = board[1], board[2]
    cli._render_gflight_table(
        [(dearest, dearest), (cheapest, cheapest)],
        legs=_round_trip(),
        top_n=1,
        match_carriers=frozenset(),
    )
    out = capsys.readouterr().out
    assert f"{cheapest.flight.price:.2f}" in out, out
    assert f"{dearest.flight.price:.2f}" not in out, out


def test_a_multi_cabin_json_arm_trims_each_cabins_combinations_by_price(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-cabin arm has its own trim, and the same set reaches it."""
    board = gf_rows("ds1_metadata_blocks_kept.json")
    dearest, cheapest = board[1], board[2]
    cabins = (Cabin.COACH,)

    def _fan_out(**_kw: object) -> dict[Cabin, list[Any]]:
        return {Cabin.COACH: [(dearest, dearest), (cheapest, cheapest)]}

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    cli._run_gflight_path_multi(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=cabins,
        sort_by=Cabin.COACH,
        top_n=1,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    dumped: Any = json.loads(capsys.readouterr().out)
    assert [m["price"] for m in dumped["COACH"][0]] == [
        cheapest.flight.price,
        cheapest.flight.price,
    ], dumped


def test_an_empty_board_under_json_is_an_empty_document(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No rows is a value. A sentence in the document's place is a parse error
    to the consumer that asked for one, and indistinguishable from a crash by
    the exit code, which stays 0 either way."""
    gf_session(gf_board(1, distinct_at=0))
    cli._run_gflight_path(
        # A routing constraint nothing on the board satisfies, so the post-filter
        # empties it — the shape a user actually meets.
        legs=_one_way(route_language="XX9999"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=True,
    )
    out = capsys.readouterr().out
    assert json.loads(out) == [], out


def test_awards_and_json_together_emit_one_document(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--format json` with awards on writes its document from the award
    renderer, further down than the early return above it — so every human
    surface between the two has to stand aside as well.

    A table and a set of URL lines around a document is not a document: the
    consumer that asked for one cannot parse any of it, and the exit code says
    the command succeeded."""

    def _award_document(_sr: object, **_kw: object) -> None:
        sys.stdout.write(json.dumps({"legs": [], "matches": []}))

    monkeypatch.setattr(cli, "run_pp_for_search", _award_document)
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=3,
        json_out=True,
        run_pp=True,
        google_url=True,
        matrix_url=True,
        sel=cli._resolve_providers(
            providers=None, cash_only=False, awards_only=False, provider_opt=()
        ),
    )
    out = capsys.readouterr().out
    assert json.loads(out) == {"legs": [], "matches": []}, out


def test_an_out_of_range_pick_leaves_an_awards_json_document_parseable(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The trim narrows the range a pick is measured against, so `--pick 12`
    against a 30-row board is in range one day and out of it the next. The
    notice that fires then must not land in the document."""

    def _award_document(_sr: object, **_kw: object) -> None:
        sys.stdout.write(json.dumps({"legs": [], "matches": []}))

    monkeypatch.setattr(cli, "run_pp_for_search", _award_document)
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=True,
        run_pp=True,
        google_url=True,
        pick=6,
        sel=cli._resolve_providers(
            providers=None, cash_only=False, awards_only=True, provider_opt=()
        ),
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"legs": [], "matches": []}, captured.out
    assert "out of range (1-5)" in captured.err, captured.err
    assert "cheapest itinerary" not in captured.err, captured.err


def test_an_out_of_range_pick_under_json_reports_the_range_and_promises_nothing(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A number that names no row is worth saying whatever the format is: the
    user typed it, and silence reads as acceptance.

    What must NOT be said is the rest of the old sentence. `--format json`
    emits no deep link, so nothing is pinned and nothing falls back — a run
    that claims otherwise is the same defect the notice itself exists to
    report, one clause further in."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=True,
        google_url=True,
        matrix_url=True,
        pick=6,
    )
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)) == 5, captured.out
    assert "--pick 6 is out of range (1-5)" in captured.err, captured.err
    assert "cheapest" not in captured.err, captured.err


def test_an_out_of_range_pick_with_no_link_asked_for_promises_nothing_either(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The format is not the only way to reach a run that pins nothing: both
    URL flags off emits no link either, and the fallback clause is untrue there
    for exactly the same reason."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=False,
        matrix_url=False,
        google_url=False,
        pick=6,
    )
    captured = capsys.readouterr()
    assert "--pick 6 is out of range (1-5)" in captured.err, captured.err
    assert "cheapest" not in captured.err, captured.err


def test_multi_cabin_json_gives_each_cabin_the_count_that_was_asked_for(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cabins are queried WIDER than the user asked, so the join has
    overlap to work with. That bump is machinery, not an answer: quoting it
    back answers `-n 2` with ten rows a cabin."""
    parsed = gf_rows("ds1_metadata_blocks_kept.json")
    assert len(parsed) > 2, "a board no wider than the count proves no trim"
    cabins = (Cabin.COACH, Cabin.BUSINESS)

    def _fan_out(**_kw: object) -> dict[Cabin, list[Any]]:
        return dict.fromkeys(cabins, parsed)

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    cli._run_gflight_path_multi(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=cabins,
        sort_by=Cabin.COACH,
        top_n=2,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    dumped: Any = json.loads(capsys.readouterr().out)
    assert sorted(dumped) == ["BUSINESS", "COACH"]
    assert all(len(v) == 2 for v in dumped.values()), dumped
