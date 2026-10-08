# pyright: reportPrivateUsage=false
"""`--awards-only` prints no numbered table, so `--pick` names no row on any arm.

The enriched arm refuses a pick there. The Google-only and Matrix-only arms
clamped it against a row list the user was never shown: they printed a range
(`1-5`) and a link labeled `itinerary #3` for a numbering no output carries.
All three arms answer the same way now: one stderr sentence, links unpinned.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from flight_cli import cli
from flight_cli.domain import Cabin, Leg, SearchOptions
from flight_cli.models import Itinerary, ItineraryDetails, SearchResult, Slice, SliceEndpoint

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

# fli's own validator rejects a past travel date, so this is derived.
_DEP = date.today() + timedelta(days=45)
_AWARDS_ONLY = cli.ProviderSelection(
    provider_filter=None, cash_only=False, awards_only=True, provider_opts={}
)
_REFUSAL = "--pick 3 names a row in the results table, and this mode prints none"


def _no_awards(*_a: object, **_kw: object) -> None:
    return None


def _matrix_answer(*_a: object, **_kw: object) -> SearchResult:
    return _matrix_result(6)


def _matrix_result(rows: int) -> SearchResult:
    sols = [
        Itinerary(
            id=f"sol-{i}",
            displayTotal=f"USD{i:03d}.00",
            itinerary=ItineraryDetails(
                slices=[
                    Slice(
                        flights=[f"AA{i}"],
                        departure=f"{_DEP.isoformat()}T06:00",
                        origin=SliceEndpoint(code="JFK"),
                        destination=SliceEndpoint(code="LHR"),
                    )
                ],
                carriers=[],
            ),
        )
        for i in range(1, rows + 1)
    ]
    return SearchResult(solutionCount=rows, solutions=sols).model_copy(  # pyright: ignore[reportCallIssue]
        update={"session": "s-1", "solution_set": "ss-1"}
    )


def _google_arm(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    pick: int | None,
    json_out: bool = False,
    links: bool = True,
) -> None:
    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    gf_session(gf_board(30))
    cli._run_gflight_path(
        legs=(Leg.of("HNL", "MIA", _DEP),),
        opts=SearchOptions(cabin=Cabin.COACH),
        top_n=5,
        json_out=json_out,
        run_pp=True,
        sel=_AWARDS_ONLY,
        matrix_url=links,
        google_url=links,
        pick=pick,
    )


def _matrix_arm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pick: int | None,
    json_out: bool = False,
    links: bool = True,
) -> None:
    monkeypatch.setattr(cli, "run_pp_for_search", _no_awards)
    monkeypatch.setattr(cli, "_run", _matrix_answer)
    cli._run_matrix_path(
        legs=(Leg(origins=("JFK",), destinations=("LHR",), date=_DEP),),
        opts=SearchOptions(page_size=5),
        rps=1.0,
        impersonate="chrome",
        no_cache=True,
        json_out=json_out,
        matrix_url=links,
        google_url=links,
        run_pp=True,
        sel=_AWARDS_ONLY,
        pick=pick,
    )


def test_the_google_arm_refuses_a_pick_where_no_table_is_numbered(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _google_arm(gf_session, gf_board, monkeypatch, pick=3)
    captured = capsys.readouterr()
    printed = " ".join(captured.err.split())

    assert printed.count(f"{_REFUSAL}; the links below are unpinned.") == 1, printed
    assert "out of range" not in printed, printed
    assert "itinerary #" not in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out
    assert "Matrix deep-link:" in captured.out, captured.out
    assert "Google Flights (tfs= structured):" in captured.out, captured.out


def test_the_matrix_arm_refuses_a_pick_where_no_table_is_numbered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _matrix_arm(monkeypatch, pick=3)
    captured = capsys.readouterr()
    printed = " ".join(captured.err.split())

    assert printed.count(f"{_REFUSAL}; the links below are unpinned.") == 1, printed
    assert "out of range" not in printed, printed
    assert "itinerary #" not in captured.out, captured.out
    assert "cheapest itinerary" not in captured.out, captured.out
    assert "Matrix deep-link:" in captured.out, captured.out
    assert "Google Flights (tfs= structured):" in captured.out, captured.out


def test_an_awards_only_run_with_no_pick_claims_no_pin_on_either_arm(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _google_arm(gf_session, gf_board, monkeypatch, pick=None)
    google = capsys.readouterr()
    _matrix_arm(monkeypatch, pick=None)
    matrix = capsys.readouterr()

    for captured in (google, matrix):
        assert "pick" not in captured.err, captured.err
        assert "pinned" not in captured.out, captured.out
        assert "Matrix deep-link:" in captured.out, captured.out


def test_the_refusal_drops_its_link_clause_where_no_link_follows(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _google_arm(gf_session, gf_board, monkeypatch, pick=3, links=False)
    google = capsys.readouterr()
    _matrix_arm(monkeypatch, pick=3, links=False)
    matrix = capsys.readouterr()

    for captured in (google, matrix):
        printed = " ".join(captured.err.split())
        assert printed.count(f"{_REFUSAL}.") == 1, printed
        assert "the links below" not in printed, printed
        assert "Matrix deep-link:" not in captured.out, captured.out


def test_the_refusal_keeps_stdout_for_the_award_document(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _google_arm(gf_session, gf_board, monkeypatch, pick=3, json_out=True, links=False)
    captured = capsys.readouterr()

    assert _REFUSAL in " ".join(captured.err.split()), captured.err
    assert captured.out == "", captured.out


def test_the_refusal_names_no_links_under_format_json(
    gf_session: Callable[..., Any],
    gf_board: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both link flags default on, and `--format json` prints no link."""
    _google_arm(gf_session, gf_board, monkeypatch, pick=3, json_out=True, links=True)
    google = capsys.readouterr()
    _matrix_arm(monkeypatch, pick=3, json_out=True, links=True)
    matrix = capsys.readouterr()

    for captured in (google, matrix):
        printed = " ".join(captured.err.split())
        assert printed.count(f"{_REFUSAL}.") == 1, printed
        assert "the links below" not in printed, printed
        assert captured.out == "", captured.out


def test_the_pick_help_says_awards_only_numbers_nothing() -> None:
    result = CliRunner().invoke(cli.app, ["search", "--help"], env={"COLUMNS": "200"})
    flat = " ".join(result.stdout.replace("│", " ").split())

    assert "--awards-only prints no table, so there a pick is reported as naming no row" in flat, (
        flat
    )
