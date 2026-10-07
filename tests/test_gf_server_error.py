# pyright: reportPrivateUsage=false
"""Google's server error on a search page is named, not read as a re-shaped page.

Google sometimes answers a search page with HTTP 200 and a `ds:1` that holds an
RPC status, `[13, null, [ErrorResponse]]`, ending `errorHasStatus: true` where a
board's blob ends on `sideChannel`. The two fixtures are live NYC-LON pages of
that shape, trimmed to their scripts: in one the `ds:1` blob is the page's last,
in the other it comes before `ds:4`.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Any

import pytest

from conftest import GFLIGHT_PAGE_DIR, _ds1, _page
from flight_cli import _doctor, _gf_calgraph, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfBackendError, GfPageShapeError, GfSearchServerError
from test_gf_full_board import _URL
from test_gf_rung_parity import _NO_BOARD

_ERROR_PAGES = [
    "page_ds1_error_status13.html.gz",
    "page_ds1_error_status13_before_ds4.html.gz",
]
_BOARDLESS = "Google Flights' search page carried no readable ds:1 payload; the page shape changed"
_TOO_SHORT = (
    "ds:1 decoded to 2 top-level entries, too few to hold a board at [2, 3]; "
    "the payload layout changed"
)


def _error_page(name: str = _ERROR_PAGES[0]) -> str:
    return gzip.decompress((GFLIGHT_PAGE_DIR / name).read_bytes()).decode()


def _read(html: str) -> gfid.Board[gfid.GFlightWithId]:
    return gfid._rows_from_page_html(PageFetch(html, _URL, 200))


def _outcome(html: str) -> object:
    """A page's rows and what the board says beside them, or its refusal."""
    try:
        board = _read(html)
    except GfBackendError as e:
        return type(e), str(e)
    return list(board), vars(board)


def _committed_pages() -> list[Any]:
    plain = sorted(GFLIGHT_PAGE_DIR.glob("*.json"))
    zipped = sorted((GFLIGHT_PAGE_DIR / "rung_parity").glob("*.json.gz"))
    return [
        *(pytest.param(_page(p.read_text()), id=p.name) for p in plain),
        *(pytest.param(_page(gzip.decompress(p.read_bytes()).decode()), id=p.name) for p in zipped),
    ]


def _no_status(_html: str) -> None:
    return None


def _none_parsed() -> str:
    """The three-row JFK-LAX capture with every row read and none parsed."""
    payload: list[Any] = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    for row in gfid._rows_from_ds1(payload).rows:
        row[0][2][0][20] = None
    return json.dumps(payload)


# ───────────────────────── the page is named ─────────────────────────


@pytest.mark.parametrize("name", _ERROR_PAGES)
def test_a_ds1_holding_a_server_error_is_that_error(name: str) -> None:
    """Red at the base, which raised `_BoardlessPageError`: "carried no readable
    ds:1 payload; the page shape changed"."""
    with pytest.raises(GfSearchServerError) as caught:
        _read(_error_page(name))
    assert caught.value.code == 13
    assert not isinstance(caught.value, GfPageShapeError)


@pytest.mark.parametrize("html", _committed_pages())
def test_no_committed_page_reads_as_a_server_error(
    html: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard, green at the base by behavior: every committed page reads as
    it did before the detector, rows or refusal. Red at the base only on the
    missing name."""
    assert gfid._ds1_error_status(html) is None
    read = _outcome(html)
    monkeypatch.setattr(gfid, "_ds1_error_status", _no_status)
    assert _outcome(html) == read


@pytest.mark.parametrize(
    ("html", "refusal", "message"),
    [
        pytest.param(_NO_BOARD, gfid._BoardlessPageError, re.escape(_BOARDLESS), id="no-ds1"),
        pytest.param(
            _error_page().replace("({key: 'ds:1'", "({key: 'ds:4'"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="error-keyed-ds4",
        ),
        pytest.param(
            _error_page().replace("data:[13,null,", "data:[13,null,,"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="undecodable-status",
        ),
        pytest.param(
            _error_page().replace("data:[13,", "data:[true,"),
            gfid._BoardlessPageError,
            re.escape(_BOARDLESS),
            id="bool-code",
        ),
        pytest.param(
            _page("[null, null]"), GfPageShapeError, re.escape(_TOO_SHORT), id="too-short"
        ),
        pytest.param(
            _page(_ds1("ds1_blocks_relocated.json")),
            GfPageShapeError,
            r"ds:1 holds flight rows at \[[\d, ]+\], not at \[2, 3\] \(found 0 there\); "
            r"the payload layout changed",
            id="rows-elsewhere",
        ),
        pytest.param(
            _page(_none_parsed()),
            gfid._PageUnreadError,
            r"none of 3 Google Flights rows parsed; the row shape changed",
            id="none-parsed",
        ),
    ],
)
def test_a_real_shape_change_keeps_its_type_and_message(
    html: str, refusal: type[GfBackendError], message: str
) -> None:
    """A guard, green at the base by behavior: a page with no server error in
    its `ds:1` is refused as the base refused it."""
    with pytest.raises(GfBackendError) as caught:
        _read(html)
    assert type(caught.value) is refusal
    assert re.match(message, str(caught.value))


@pytest.mark.parametrize(
    ("bags", "remedy"),
    [
        (False, "use [bold]--backend matrix[/]"),
        (True, "drop [bold]--bags[/] to search Matrix, which prices no bags"),
    ],
    ids=["plain", "bags"],
)
def test_the_refusal_names_the_server_error_and_its_code(bags: bool, remedy: str) -> None:
    """Red at the base, which had no type to render; its page rendered as
    "Google Flights' page shape changed"."""
    refusal = cli._gf_refusal(GfSearchServerError(13), bags=bags)
    assert refusal.note == "Google Flights answered with a server error (status 13)"
    assert refusal.message == (
        "[yellow]Google Flights answered with a server error (status 13).[/] "
        f"Retry later, or {remedy}."
    )


def test_doctor_reads_the_server_error_as_upstream() -> None:
    """Red at the base, where the page read as "shape", a cause that does not
    lift on its own."""
    assert _doctor._classify(GfSearchServerError(13)) == (
        "upstream",
        "Google Flights answered the search with a server error (status 13)",
    )


def test_the_price_graph_does_not_refuse_on_a_server_error() -> None:
    """A guard, green at the base, which suppressed the page as a shape error:
    the graph is its own request."""
    _gf_calgraph._refuse_a_wall(PageFetch(_error_page(), _URL, 200))
