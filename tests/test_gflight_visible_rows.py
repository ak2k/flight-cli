# pyright: reportPrivateUsage=false
"""`-n` is one number for everything the user can act on.

Google's search page serves its whole board — around thirty rows — whatever
count is asked of it, so the count is applied on the way out. What is under
test is that the table, the JSON document, the pinned deep link and the award
matcher are all handed the SAME set, and that the wide board still reaches the
one thing that needs it — the Tier-2 post-filter, which runs before the trim
because a routing constraint is answered out of the whole board or answered
wrong.

WHICH rows survive is under test too: the cheapest, on every Google board. A
one-way board arrives in the page's order, Google's top flights first, and a
round trip's combinations in the order the fan-out built them, pin-major; both
are put in price order before the trim, ties kept in the order they arrived and
unpriced rows last.

The multi-cabin arms are where only the COUNT agrees: the JSON document carries
the first `-n` of each cabin's own board and the table carries the top `-n` of
the join, which is the same number of rows drawn from different sets.

No rows is a value and has its own shape: an empty board is `[]` under
`--format json` and a sentence on stdout otherwise, while a query that failed is
zero bytes and exit 1. That is how a consumer tells no rows from no answer.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from rich.console import Console

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
    # ROW ONE, which is the cheapest only when Google priced it: on a board it
    # priced no row of, "the cheapest" names a row that does not exist.
    assert "pinning itinerary #1 instead" in captured.err, captured.err
    assert "itinerary #1 pinned" in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out


def test_a_pick_past_the_visible_table_claims_no_pin_the_matrix_link_cannot_make(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Google row carries none of the server ids a Matrix link pins, so with
    the Google link off the one link below the sentence pins nothing."""
    gf_session(gf_board(_BOARD_ROWS))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=False,
        matrix_url=True,
        google_url=False,
        pick=6,
    )
    captured = capsys.readouterr()
    err = " ".join(captured.err.split())
    assert "--pick 6 is out of range (1-5)." in err, err
    assert "pinning" not in err, err
    assert "Matrix deep-link:" in captured.out, captured.out


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

    The link goes to row 1, and on this capture row 1 is the cheapest: the page
    lists 6590, 6616 and then 6072, and `-n 3` prints 6072, 6590 and 6616. The
    label still names the row by its number. Row 1 is the cheapest only when
    Google priced it, and a board of rows it did not price has a row 1 and no
    cheapest, so "cheapest itinerary" is a label this path cannot always make
    true, and the number is one it always can."""
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
    # The row it names is the row the table put first, which the page listed
    # third.
    where = [captured.out.index(p) for p in ("USD6072.00", "USD6590.00", "USD6616.00")]
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


def _decoded_tfs(printed: str) -> bytes:
    """The protobuf bytes behind the emitted Google Flights link.

    Flight numbers travel through `tfs=` as plain ASCII, so decoding is enough
    to say WHICH itinerary a link pins — the one question the rendered page
    cannot answer, since both tables are drawn whatever the pin resolves to.

    Whitespace is stripped first because rich hard-wraps a URL across lines at
    the console width, splitting the blob."""
    import base64
    import re

    m = re.search(r"tfs=([A-Za-z0-9_-]+)", "".join(printed.split()))
    assert m is not None, printed
    blob = m.group(1)
    return base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))


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
    matrix: SearchResult | None = None,
    sel: Any = None,
    run_pp: bool = False,
    matrix_url: bool = True,
    google_url: bool = True,
) -> None:
    """The real enriched path with both halves answered in process.

    The two link flags default to the pair every caller here wants — links on,
    so a pin has something to label. They are parameters because what the path
    says about its links is under test, and `--no-matrix-url --no-google-url`
    is the arm where no link follows the sentence."""
    answer = _dearer_matrix() if matrix is None else matrix

    async def _stashes_matrix(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix"] = answer

    class _Chain:
        """Matrix answering the chain search a Google row under all its fares
        is asked: no fare on those flights."""

        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _Chain:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, *_a: object, **_kw: object) -> SearchResult:
            return _empty_matrix()

    def _gf(*_a: object, **_kw: object) -> list[Any]:
        return rows

    monkeypatch.setattr(cli, "_gflight_results", _gf)
    monkeypatch.setattr(cli, "_matrix_into", _stashes_matrix)
    monkeypatch.setattr(cli, "MatrixClient", _Chain)
    cli._run_enriched_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=top_n,
        run_pp=run_pp,
        sel=sel,
        matrix_url=matrix_url,
        google_url=google_url,
        pick=pick,
        rps=1.0,
        impersonate="chrome",
        no_cache=True,
    )


def _empty_matrix() -> SearchResult:
    """Matrix answering with no solutions — an ordinary no-service outcome, not
    a failure: the half answered, and the answer is that there is nothing."""
    from flight_cli.models import SearchResult as _SR

    return _SR.model_validate({"solutions": [], "solutionCount": 0})


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
    # WHICH row, read out of the link rather than off the screen. Row 1 of the
    # merged table is a Google-only itinerary — the cheapest of the five merged
    # rows, and the FIRST table's row 3 — so its flight numbers in the emitted
    # `tfs=` are the pin naming the row the merged table numbered 1. A price
    # asserted on the page says nothing here: the merged table is rendered from
    # `merged` whatever `shown` holds, and `USD6072.00` matches the first
    # table's row 3, which the merged table prints as a bare `6072.00`.
    pinned = _decoded_tfs(captured.out)
    assert b"627" in pinned, pinned
    assert b"854" not in pinned and b"144" not in pinned, pinned


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


def _identified_matrix() -> Any:
    """A Matrix half carrying the three server IDs a pinned Matrix link is built
    from, and slices, so its rows reach the merged table as themselves."""
    from flight_cli.models import SearchResult

    def _slice(flight: str) -> dict[str, Any]:
        return {
            "flights": [flight],
            "departure": f"{_DEP.isoformat()}T09:00:00",
            "arrival": f"{_DEP.isoformat()}T12:00:00",
            "origin": {"code": "HNL"},
            "destination": {"code": "MIA"},
            "stops": [],
        }

    return SearchResult.model_validate(
        {
            "session": "sess-1",
            "solutionSet": "set-1",
            "solutionCount": 3,
            "solutions": [
                {
                    "id": f"sol-{i}",
                    "displayTotal": f"USD{500 + i}.00",
                    "itinerary": {"slices": [_slice(f"AA{i}")], "carriers": []},
                }
                for i in (1, 2, 3)
            ],
        }
    )


def _matrix_pin(printed: str) -> dict[str, Any]:
    """The `solution` block behind the pinned Matrix link: the server IDs the
    SPA opens the row from, which the label above the link does not show. Read
    off a console wide enough to print the URL on one line."""
    import base64
    import re
    import urllib.parse

    m = re.search(r"matrix\.itasoftware\.com/itinerary\?search=(\S+)", printed)
    assert m is not None, printed
    payload: Any = json.loads(base64.b64decode(urllib.parse.unquote(m.group(1))))
    return cast("dict[str, Any]", payload["solution"])


def test_an_enriched_pin_on_a_matrix_row_keeps_its_server_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pinned link is built from a result rebuilt around the merged rows,
    and a Matrix link needs the session and solutionSet the response carried as
    well as the row's own id. A rebuild that dropped either would degrade the
    link to a plain deep link."""
    monkeypatch.setattr(cli, "console", Console(width=1000, no_color=True, highlight=False))
    _enriched(monkeypatch, [], top_n=3, pick=2, matrix=_identified_matrix())
    out = capsys.readouterr().out

    assert "Matrix (itinerary #2 pinned)" in out, out
    pin = _matrix_pin(out)
    assert (pin["sessionId"], pin["rh"], pin["Si"]) == ("sess-1", "set-1", "sol-2"), pin


def test_an_enriched_out_of_range_pick_claims_no_pin_the_google_link_does_not_make(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Row 1 is a Matrix connection no Google row matched, so no source states
    its second flight's day and the Google link below it is the unpinned one."""
    from flight_cli.models import SearchResult

    matrix = SearchResult.model_validate(
        {
            "session": "sess-1",
            "solutionSet": "set-1",
            "solutions": [
                {
                    "id": "sol-1",
                    "displayTotal": "USD501.00",
                    "itinerary": {
                        "slices": [
                            {
                                "flights": ["AA1", "AA2"],
                                "departure": f"{_DEP.isoformat()}T21:00:00",
                                "arrival": f"{(_DEP + timedelta(days=1)).isoformat()}T12:00:00",
                                "origin": {"code": "HNL"},
                                "destination": {"code": "MIA"},
                                "stops": [{"code": "DFW"}],
                            }
                        ],
                        "carriers": [],
                    },
                }
            ],
        }
    )
    _enriched(monkeypatch, [], top_n=3, pick=9, matrix=matrix, matrix_url=False)
    captured = capsys.readouterr()
    err = " ".join(captured.err.split())

    assert "--pick 9 is out of range (1-1)." in err, err
    assert "pinning" not in err, err
    assert "Google Flights (tfs= structured):" in captured.out, captured.out


def test_an_empty_merged_board_reports_no_range_and_claims_no_pin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both halves answer with nothing, which is an outcome and not a failure.

    Nothing is numbered, so `--pick 1` names no row — and `(1-0)` is not a
    narrower way of saying that. It is an empty interval, so it cannot tell the
    user what a valid pick would be, and the clause that follows it would
    promise a pin that does not happen: the links fall back to their unpinned
    form because there is no row to build one from.

    Unlike its Google-only sibling this arm does not return early on an empty
    board — it renders a header-only merged table and carries on — so the
    silence has to be decided here rather than inherited."""
    _enriched(monkeypatch, [], top_n=3, pick=1, matrix=_empty_matrix())
    captured = capsys.readouterr()
    both = captured.out + captured.err

    assert "out of range" not in both, both
    assert "(1-0)" not in both, both
    assert "pinned" not in both, both
    # The links still print, in the form that claims nothing.
    assert "Matrix deep-link:" in captured.out, captured.out
    assert "Google Flights (tfs= structured):" in captured.out, captured.out


def test_a_pick_is_refused_where_this_mode_numbers_nothing(
    gf_rows: Callable[..., list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--awards-only` prints no merged table, and the award renderer it prints
    instead has no `#` column.

    So a pick on this arm names no row anywhere — not one out of range, one
    that does not exist — and clamping it against the Matrix solution list
    labels a link `itinerary #3` for a numbering the user was never shown. The
    refusal says that once, on stderr, and both links fall back to the form
    that claims nothing: with no numbered list, `cheapest itinerary` is as
    unfounded a label as `itinerary #3`."""
    sel = cli.ProviderSelection(
        provider_filter=None, cash_only=False, awards_only=True, provider_opts={}
    )

    def _no_awards(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    _enriched(
        monkeypatch,
        gf_rows("ds1_metadata_blocks_kept.json"),
        top_n=3,
        pick=3,
        sel=sel,
        run_pp=True,
    )
    captured = capsys.readouterr()

    assert captured.err.count("--pick 3 names a row in the results table") == 1, captured.err
    assert "itinerary #" not in captured.out, captured.out
    assert "(1-3)" not in captured.out + captured.err, captured.out + captured.err
    assert "Matrix deep-link:" in captured.out, captured.out
    assert "Google Flights (tfs= structured):" in captured.out, captured.out


def test_the_refusal_says_nothing_about_links_where_none_follow(
    gf_rows: Callable[..., list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same refusal with both links suppressed drops its second clause.

    `--no-matrix-url --no-google-url` is the arm where this mode prints no link
    at all, so a clause telling the user how the links below are labelled
    describes a surface the run does not produce — the one thing the pick
    reporter exists to avoid. The refusal itself is still owed: the number was
    typed and it still names no row.

    The sibling above drives the same call with links on, so between them the
    only thing that varies is whether a link follows."""
    sel = cli.ProviderSelection(
        provider_filter=None, cash_only=False, awards_only=True, provider_opts={}
    )

    def _no_awards(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    _enriched(
        monkeypatch,
        gf_rows("ds1_metadata_blocks_kept.json"),
        top_n=3,
        pick=3,
        sel=sel,
        run_pp=True,
        matrix_url=False,
        google_url=False,
    )
    captured = capsys.readouterr()

    # Whitespace-collapsed: the sentence is printed through a console that
    # wraps at its own width, and a clause split over a line break is still the
    # clause. Asserted on the raw stream, "the links below" is absent from a
    # stderr that says it — which is a pass for the wrong reason.
    printed = " ".join(captured.err.split())

    assert (
        printed.count("--pick 3 names a row in the results table, and this mode prints none.") == 1
    ), printed
    assert "the links below" not in printed, printed
    # The premise of the assertion above: nothing on this arm prints a link.
    assert "Matrix deep-link:" not in captured.out, captured.out
    assert "Google Flights (tfs= structured):" not in captured.out, captured.out


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
    gf_session(gf_board(_BOARD_ROWS, distinct_at=keeper, one_flight=True))
    cli._run_gflight_path(
        legs=_one_way(route_language="AS627"),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=True,
    )
    rows = _json_rows(capsys)
    assert len(rows) == 1
    assert [leg["flight_number"] for leg in rows[0]["legs"]] == ["627", "627"]


def _prices(rows: list[Any]) -> list[float]:
    """Every price the emitted document carries, in the order it carries it."""
    out: list[float] = []
    for row in rows:
        members: list[Any] = cast("list[Any]", row) if isinstance(row, list) else [row]
        out.extend(float(m["price"]) for m in members)
    return out


def test_a_one_way_board_is_trimmed_to_its_cheapest_rows(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which rows survive, not how many.

    The captured board is not price-ordered: the page hands over its top
    flights block followed by the rest, and its cheapest row is listed third.
    `-n 2` is the two cheapest rows in price order, the order every other
    answer this command prints is already in."""
    gf_session(gf_capture("ds1_metadata_blocks_kept.json"))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=2,
        json_out=True,
    )
    assert _prices(_json_rows(capsys)) == [6072.0, 6590.0]


def test_a_round_trips_combinations_are_trimmed_by_price(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    gf_answering: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A round trip's combinations arrive in the order they were built, which
    ranks nothing.

    The combinations are built pin-major — every return against outbound one,
    then every return against outbound two — so the first `-n` of them are the
    returns of the first outbound and nothing else. On this fixture pair `-n 3`
    would be three trips from one outbound, with cheaper trips from the next
    outbound left off the table entirely.

    They are ordered by their terminal member's price, which is the number
    every surface prints."""
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
    # documented stability of the sort a thing a test can lose. The pins are
    # the outbounds cheapest first, tfMS2d (6072) then Ulft7e (6590), and the
    # ties keep that order.
    assert [(r[0]["flight_id"], r[-1]["flight_id"]) for r in rows] == [
        ("tfMS2d", "iQwZab"),
        ("tfMS2d", "zIxVxf"),
        ("Ulft7e", "iQwZab"),
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


def test_a_one_way_table_handed_the_whole_board_prints_its_cheapest_rows(
    gf_rows: Callable[[str], list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The enriched first paint hands the renderer the board as the page listed
    it, 6590 first and 6072 third; its trim keeps the cheapest."""
    cli._render_gflight_table(
        gf_rows("ds1_metadata_blocks_kept.json"),
        legs=_one_way(),
        top_n=1,
        match_carriers=frozenset(),
    )
    out = capsys.readouterr().out
    assert "6072.00" in out, out
    assert "6590.00" not in out, out


_NO_PRICE_CELL = "—"
_ROW = re.compile(r"^│\s*\d+\s*│")


def test_a_round_trip_row_google_did_not_price_is_shown_last_and_reads_as_a_dash(
    gf_session: Callable[..., Any],
    gf_capture: Callable[[str], str],
    gf_unpriced: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Google prices a shopping-list row or it does not, and the board is
    served either way.

    An unpriced row is ordinary — fli's decoder reads an empty price head as
    "no aggregate price", not as a malformed row — so the table shows it. It
    cannot be ranked against a number it does not have, so it sorts last, and
    its cell carries the placeholder every other absent amount here uses rather
    than a currency prefix over nothing, which would read as a fare of zero.

    Dropping it instead is the option this rules out: `-n` is the count of rows
    the user asked to see, and a board silently short by the ones Google would
    not quote makes that number a lie."""
    gf_session(
        gf_capture("ds1_metadata_blocks_kept.json"),
        gf_unpriced(
            "ds1_return_leg_pinned.json",
            index=1,
            origin="MIA",
            destination="HNL",
            date=_RET.isoformat(),
        ),
    )
    cli._run_gflight_path(
        legs=_round_trip(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=9,
        json_out=False,
    )
    captured = capsys.readouterr()
    # Three outbounds against three returns, one of which carries no price.
    rows = [ln for ln in captured.out.splitlines() if "│" in ln]
    assert rows, captured.out
    # The `b` member is the one the combination is priced from, so the dash is
    # on the terminal row of each unpriced trip.
    dashed = [ln for ln in rows if _NO_PRICE_CELL in ln and "USD" not in ln]
    assert len(dashed) == 3, captured.out
    assert all(ln.lstrip("│ ").startswith(("7b", "8b", "9b")) for ln in dashed), dashed
    # Ordered last, not dropped: the priced combinations still occupy 1..6.
    assert "9b" in captured.out, captured.out
    assert "USD0.00" not in captured.out, captured.out


def test_a_one_way_row_google_did_not_price_is_shown_last(
    gf_session: Callable[..., Any],
    gf_unpriced: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one-way board is sorted too, and an unpriced row goes after every
    priced one rather than being dropped: with `-n` covering the whole board it
    is the last row of the table. Its price cell is a second, independent place
    the absence has to be answered, and the one whose failure is the
    renderer's own line and exit 1 rather than a traceback."""
    gf_session(gf_unpriced("ds1_metadata_blocks_kept.json", index=1))
    cli._run_gflight_path(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=3,
        json_out=False,
    )
    captured = capsys.readouterr()
    assert "unsupported format string" not in captured.out + captured.err, captured
    assert "could not be rendered" not in captured.out + captured.err, captured
    # All three rows: the unpriced one is row 2 on the board and row 3 on the
    # table.
    prices = [ln.split("│")[2].strip() for ln in captured.out.splitlines() if _ROW.match(ln)]
    assert prices == ["USD6072.00", "USD6590.00", _NO_PRICE_CELL], captured.out
    assert "USD0.00" not in captured.out, captured.out


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


def test_a_multi_cabin_json_arm_trims_a_one_way_board_to_its_cheapest_rows(
    gf_rows: Callable[[str], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-cabin arm's one-way rows go through the same order."""
    board = gf_rows("ds1_metadata_blocks_kept.json")

    def _fan_out(**_kw: object) -> dict[Cabin, list[Any]]:
        return {Cabin.COACH: board}

    monkeypatch.setattr(cli, "_run_gflight_multi", _fan_out)
    cli._run_gflight_path_multi(
        legs=_one_way(),
        opts=SearchOptions(cabin=Cabin.COACH),
        cabins=(Cabin.COACH,),
        sort_by=Cabin.COACH,
        top_n=2,
        json_out=True,
        run_pp=False,
        sel=cli._resolve_providers(
            providers=None, cash_only=True, awards_only=False, provider_opt=()
        ),
    )
    dumped: Any = json.loads(capsys.readouterr().out)
    assert [r["price"] for r in dumped["COACH"]] == [6072.0, 6590.0], dumped


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
