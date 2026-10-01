# pyright: reportPrivateUsage=false
"""`--exclude-basic` asks Google Flights for economy without basic fares.

The search page takes it as top-level field 25. Google repriced 78 of 93
JFK-LAX fares up under it, and served a basic fare on JFK-LHR anyway. No row
marks a basic fare, so no row can be checked for it, and every run says so.
Matrix is not asked at all, so the flag keeps a search on Google as `--bags`
does, and is never handed to Matrix afterwards."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from conftest import _answering, _ds1, _page
from flight_cli import cli
from flight_cli.domain import Leg, SearchOptions, SpecificDateSearch
from flight_cli.fli_bridge import to_fli_filter
from test_gf_full_board import _tfs
from test_links_search_tfs import _decode

if TYPE_CHECKING:
    from collections.abc import Callable

_SEARCH = ["search", "--cash-only", "--no-google-url", "--no-matrix-url"]
_EMPTY = "ds1_zero_rows.json"
_NOTE = (
    "Google Flights was asked to leave out basic economy, but its rows carry no "
    "fare-family mark to check, and it has served basic fares on JFK-LHR anyway."
)


def _dep() -> date:
    return date.today() + timedelta(days=45)


def _served(name: str = "ds1_jfk_lax_tfu.json") -> str:
    if name == _EMPTY:  # no row to re-date
        return _page(_ds1(name))
    return _page(_answering(_ds1(name), origin=None, destination=None, date=_dep().isoformat()))


def _search(*extra: str) -> Any:
    return CliRunner().invoke(
        cli.app,
        [*_SEARCH, "JFK", "LAX", "--dep", _dep().isoformat(), *extra],
        env={"COLUMNS": "400"},
    )


def _flat(text: str) -> str:
    return " ".join(text.replace("│", " ").split())


def _no_matrix(**_kw: object) -> None:
    pytest.fail("the search went to Matrix")


# ─────────────────────────── the page is asked ─────────────────────────────


def test_the_fli_filter_asks_for_it_only_when_asked() -> None:
    leg = Leg.of("JFK", "LAX", _dep())
    asked = SpecificDateSearch(legs=(leg,), options=SearchOptions(exclude_basic=True))
    assert to_fli_filter(asked).exclude_basic_economy is True
    assert to_fli_filter(SpecificDateSearch(legs=(leg,))).exclude_basic_economy is False


def test_the_page_is_asked_and_the_run_says_the_rows_cannot_be_checked(
    gf_session: Callable[..., Any],
) -> None:
    fake = gf_session(_served())
    result = _search("--exclude-basic", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert _decode(_tfs(fake.gets[0]))[25] == [1]
    assert json.loads(result.stdout), "Google's rows are the answer"
    assert _flat(result.stderr).count(_NOTE) == 1, result.stderr


def test_without_it_the_page_and_the_run_are_the_base_s(gf_session: Callable[..., Any]) -> None:
    fake = gf_session(_served())
    result = _search("--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert 25 not in _decode(_tfs(fake.gets[0]))
    assert "basic economy" not in result.stderr


def test_the_default_table_is_not_enriched_and_carries_the_note(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served())

    def _no_weave(**_kw: object) -> None:
        pytest.fail("the table was enriched against Matrix")

    monkeypatch.setattr(cli, "_run_enriched_path", _no_weave)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = _search("--exclude-basic")
    assert result.exit_code == 0, result.output
    printed = _flat(result.stderr)
    assert "No Matrix enrichment: Matrix is not asked to leave out basic economy." in printed
    assert printed.count(_NOTE) == 1, printed


# ───────────────────────── no Matrix after the fact ────────────────────────


def test_a_board_a_row_check_empties_is_answered_on_google(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served())
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = _search("--exclude-basic", "--max-price", "1", "--fast", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "Using Matrix" not in result.stderr


def test_a_failed_google_query_is_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fails(*_a: object, **_kw: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_gflight_results", _fails)
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = _search("--exclude-basic", "--format", "json")
    assert (result.exit_code, result.stdout) == (1, "")
    assert "Using Matrix" not in result.stderr


def test_an_empty_infant_board_names_the_flag_matrix_is_not_asked_for(
    gf_session: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gf_session(_served(_EMPTY))
    monkeypatch.setattr(cli, "_run_matrix_path", _no_matrix)
    result = _search("--exclude-basic", "--inf-lap", "1", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    printed = _flat(result.stderr)
    assert "Google Flights served no rows for a party with an infant" in printed, printed
    assert (
        "drop --exclude-basic to search Matrix, which is not asked to leave out basic economy"
        in printed
    )
    assert "Using Matrix" not in printed


# ─────────────────────────────── the refusals ──────────────────────────────


def _refused(*extra: str) -> str:
    result = _search(*extra)
    assert result.exit_code == 2, result.output
    return _flat(result.output)


def test_on_matrix_it_is_exit_2() -> None:
    printed = _refused("--exclude-basic", "--backend", "matrix")
    assert (
        "--exclude-basic needs Google Flights: Matrix is not asked to leave out basic economy"
        in printed
    )


def test_beside_a_matrix_reason_it_is_exit_2_naming_it() -> None:
    printed = _refused("--exclude-basic", "--routing", "BA AA")
    assert "--exclude-basic needs Google Flights, which can't serve routing 'BA AA'" in printed
    assert "Using Matrix" not in printed


@pytest.mark.parametrize("cabins", ["business", "premium", "economy,business"])
def test_any_cabin_but_economy_alone_is_exit_2(cabins: str) -> None:
    printed = _refused("--exclude-basic", "--cabin", cabins)
    assert "--exclude-basic takes --cabin economy alone" in printed


# ─────────────────────────────── the links ─────────────────────────────────


def test_every_link_says_it_is_not_asked_to_leave_out_basic_economy() -> None:
    leg = Leg.of("JFK", "LAX", _dep())
    asked = SpecificDateSearch(legs=(leg,), options=SearchOptions(exclude_basic=True))
    plain = SpecificDateSearch(legs=(leg,))
    for caveats in (
        cli._gflight_url_caveats,
        cli._pinned_gflight_url_caveats,
        cli._matrix_link_caveats,
    ):
        assert caveats(plain) == []
        notes = caveats(asked)
        assert len(notes) == 1
        assert "basic economy" in notes[0]
