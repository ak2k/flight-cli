# pyright: reportPrivateUsage=false
"""The search-page transport: `ds:1` extraction and refusal classification.

Google's `GetShoppingResults` RPC has been gated since 2026-08, so the gflight
backend GETs the public search page and reads the flight rows Google inlines in
its `AF_initDataCallback` `ds:1` blob. Every failure mode of that fetch — a
captcha interstitial, a consent wall, a re-shaped page — renders as zero rows,
so the load-bearing behavior under test is that none of them can reach the user
as "no flights on this route".

FIXTURE POLICY: **scrub secrets, not structure.** Fixtures are real captures
(2026-09-02) trimmed to three rows with the top-level session id at `[0][4]`
replaced; nothing else is nulled or reshaped. Most of these were additionally
slimmed by dropping the metadata blocks no code reads, which is safe for what
they pin but makes them useless for the misplaced-block scan — with indices
1-31 all `None`, `misplaced == ()` holds no matter what the scan does.
`ds1_metadata_blocks_kept.json` is the counterweight: a whole capture, all 31
indices intact including the nine blocks that are row-shaped by structure, at
39 KB. Slim a new fixture only if you know which invariant it is for.

Two page shapes are pinned because Google serves both: an initial JFK-LAX search
(a row block at `[2]` AND `[3]`) and a pinned return leg (`[2] = None`, the
whole board at `[3]`).
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
import threading
from typing import Any, ClassVar, cast

import pytest
from fli.search.exceptions import SearchHTTPError

from flight_cli import _gflight_ids as gfid
from flight_cli._gf_errors import GfConsentError, GfPageShapeError, GfThrottledError

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "gflight_page"
_FILTERS = cast("Any", None)  # a patched client never encodes the filter


def _ds1(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def _page(ds1_json: str) -> str:
    """The smallest page shaped like Google's: an AF_initDataCallback blob for
    an unrelated key, then the one we read."""
    return (
        "<!doctype html><html><body><script>"
        "AF_initDataCallback({key: 'ds:0', hash: '1', data:[[]], sideChannel: {}});"
        f"AF_initDataCallback({{key: 'ds:1', hash: '2', "
        f"data:{ds1_json}, sideChannel: {{}}}});"
        "</script></body></html>"
    )


_CONSENT_PAGE = (
    "<!doctype html><html><body>"
    '<form action="https://consent.google.com/save">Before you continue</form>'
    "</body></html>"
)
_SORRY_PAGE = "<!doctype html><html><body>Our systems have detected unusual traffic</body></html>"
_SHAPE_CHANGE_PAGE = (
    "<!doctype html><html><body><script>"
    "AF_initDataCallback({key: 'ds:4', hash: '9', data:[[]], sideChannel: {}});"
    "</script></body></html>"
)


class _NullCookies:
    """Enough of curl_cffi's cookie API for the seed/persist helpers."""

    jar: ClassVar[list[Any]] = []

    def set(self, *_a: object, **_kw: object) -> None:
        return None


class _FakeResponse:
    """The shape fli's client hands back — it has already called
    `raise_for_status()`, so a response reaching us is always 2xx."""

    def __init__(self, *, text: str, url: str = "") -> None:
        self.text = text
        self.url = url or "https://www.google.com/travel/flights?tfs=abc"


class _FakeClient:
    """Counts GETs so a test can assert the request budget, not just the value."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.gets: list[str] = []

    def get(self, url: str, **_kw: object) -> _FakeResponse:
        self.gets.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _reset_cookie_latches(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Point the cookie cache at a temp dir and re-arm BOTH latches.

    Seeding latches per thread, so a test that leaves it set silently disables
    seeding for every later test in the process."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"persisted": False})
    monkeypatch.setattr(gfid, "_seed_latch", threading.local())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    """Install a fake GF client and keep cookie seeding off the real cache."""
    _reset_cookie_latches(monkeypatch, tmp_path)

    def _stub_tfs(_filters: Any) -> bytes:
        return b"\x08\x1c"

    monkeypatch.setattr(gfid, "build_search_tfs", _stub_tfs)

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gfid.time, "sleep", _no_sleep)

    def install(response: _FakeResponse | Exception) -> _FakeClient:
        fake = _FakeClient(response)
        monkeypatch.setattr(gfid, "get_client", lambda: fake)
        return fake

    return install


# ─────────────────────────── ds:1 extraction ───────────────────────────


def test_extract_ds1_reads_the_flights_blob_past_other_keys() -> None:
    payload = gfid._extract_ds1(_page(_ds1("ds1_jfk_lax_3rows.json")))
    assert payload is not None
    rows, blocks_seen, misplaced = gfid._rows_from_ds1(payload)
    assert blocks_seen == 2
    assert misplaced == ()
    assert rows == payload[2][0] + payload[3][0]
    assert len(rows) == 3


def test_extract_ds1_returns_none_when_the_key_is_absent() -> None:
    assert gfid._extract_ds1(_SHAPE_CHANGE_PAGE) is None


def test_extract_ds1_returns_none_on_undecodable_data() -> None:
    assert gfid._extract_ds1(_page("[[,]]")) is None


def test_rows_keep_the_top_flights_block_first() -> None:
    """`ds:1[2]` is Google's own ranking and `[3]` the rest; concatenating in
    that order is the only way the page's ordering survives — we can't
    reproduce the blended rank locally."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    rows = gfid._rows_from_ds1(payload).rows
    assert rows[0] is payload[2][0][0]
    assert rows[1] is payload[3][0][0]


# ─────────────────────────── happy path ────────────────────────────────


def test_one_call_parses_ids_and_legroom_from_the_page(client: Any) -> None:
    fake = client(_FakeResponse(text=_page(_ds1("ds1_jfk_lax_3rows.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert all(a.legroom_class for g in out for a in g.amenities)
    assert len(fake.gets) == 1


# ─────────────────────── refusals are never "no results" ───────────────


def test_zero_row_page_is_an_authoritative_empty(client: Any) -> None:
    """A page that decodes with no rows is Google's answer, not a refusal —
    one GET, no retry, no raise."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_zero_rows.json"))))
    assert gfid._one_call_with_retry(_FILTERS) == []
    assert len(fake.gets) == 1


def test_sorry_redirect_raises_throttled(client: Any) -> None:
    client(
        _FakeResponse(
            text=_SORRY_PAGE,
            url="https://www.google.com/sorry/index?continue=https://www.google.com/travel",
        )
    )
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_http_429_raises_throttled(client: Any) -> None:
    """fli's own client calls `raise_for_status()` and wraps the failure, so a
    429 reaches us as `SearchHTTPError` — never as a response to inspect."""
    client(SearchHTTPError("rate limited", status_code=429))
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_other_http_errors_are_not_mistaken_for_throttling(client: Any) -> None:
    client(SearchHTTPError("server error", status_code=500))
    with pytest.raises(SearchHTTPError):
        gfid._one_call(_FILTERS)


def test_real_fli_client_turns_a_429_into_a_typed_throttle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The wiring the fake can't prove: fli's REAL `Client.get` in front of a
    stubbed session, so the `raise_for_status()` -> `SearchHTTPError` ->
    `GfThrottledError` chain is exercised end to end."""
    from curl_cffi.requests import exceptions as curl_exc
    from fli.search.client import Client

    _reset_cookie_latches(monkeypatch, tmp_path)

    def _stub(_filters: Any) -> bytes:
        return b"\x08\x1c"

    monkeypatch.setattr(gfid, "build_search_tfs", _stub)

    class _Resp:
        status_code = 429

        def raise_for_status(self) -> None:
            raise curl_exc.HTTPError("429 Too Many Requests", response=self)

    class _Session:
        cookies = _NullCookies()

        def get(self, _url: str, **_kw: object) -> _Resp:
            return _Resp()

    session = _Session()
    real = Client()
    monkeypatch.setattr(real, "_session", lambda: session)
    monkeypatch.setattr(gfid, "get_client", lambda: real)

    # fli wraps `get` in `@retry(stop_after_attempt(3), wait_exponential())`, so
    # the real backoff runs here. Tenacity's documented hook skips the waits;
    # the three attempts (and the request budget they cost) still happen.
    def _no_backoff(_seconds: float) -> None:
        return None

    # `Client.get` is decorated, so `.retry` is tenacity's controller — present
    # at runtime, invisible to a type checker looking at a plain function.
    retry_controller: Any = Client.get.retry  # pyright: ignore[reportFunctionMemberAccess]
    monkeypatch.setattr(retry_controller, "sleep", _no_backoff)

    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_sorry_body_at_the_original_url_raises_throttled(client: Any) -> None:
    """Google also serves the interstitial in place, with no redirect to key
    off — the body is the only tell, and without it this reads as a shape
    change."""
    client(_FakeResponse(text=_SORRY_PAGE))
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


def test_a_pinned_return_leg_page_serves_one_block(client: Any) -> None:
    """A served page may omit `[2]` and carry its whole board at `[3]`.

    Captured live 2026-09-02 from an HNL-MIA round-trip expansion. One row
    block is an ordinary served page, not a shape change."""
    payload = json.loads(_ds1("ds1_return_leg_pinned.json"))
    assert payload[2] is None, "fixture must keep the served shape"
    fake = client(_FakeResponse(text=_page(_ds1("ds1_return_leg_pinned.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert len(fake.gets) == 1


def test_a_flightless_board_is_an_authoritative_empty(client: Any) -> None:
    """MEASURED: an HNL-MIA nonstop-only search, where no nonstop exists, is
    served as an ordinary results page with no flight cards — `[2]` and `[3]`
    both `None`. Refusing that reports "the page shape changed" for a route
    that simply has no matching flights."""
    payload = json.loads(_ds1("ds1_flightless_board.json"))
    assert payload[2] is None and payload[3] is None
    client(_FakeResponse(text=_page(_ds1("ds1_flightless_board.json"))))
    assert gfid._one_call(_FILTERS) == []


@pytest.mark.parametrize("fixture", ["ds1_zero_rows.json", "ds1_single_block_empty.json"])
def test_synthetic_empty_block_shapes_are_authoritative_empties(client: Any, fixture: str) -> None:
    """SYNTHETIC shapes, hand-edited from the JFK-LAX capture — an empty row
    block at both indices, and at one. Neither has been seen in the wild (the
    measured flight-less board carries no block at all), but an empty block
    must never read as a refusal if Google starts sending one."""
    client(_FakeResponse(text=_page(_ds1(fixture))))
    assert gfid._one_call(_FILTERS) == []


def test_metadata_blocks_are_not_mistaken_for_relocated_rows() -> None:
    """`ds:1` carries other list-of-list-of-list structures on every page (1, 7,
    14, 17 among them). A nesting-depth test would call those relocated rows
    and refuse every ordinary page, so the probe parses instead."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    board = gfid._rows_from_ds1(payload)
    assert board.misplaced == ()
    assert board.blocks_seen == 2
    assert not gfid._holds_flight_rows(["not-a-row-block"])
    assert not gfid._holds_flight_rows([["metadata", "strings"]])


# The nine indices this capture serves that ARE row-shaped by structure — the
# whole reason the probe has to parse. Asserted first so a future re-trim that
# strips them fails loudly here instead of quietly making the test below pass
# for no reason.
_METADATA_DECOYS = (1, 6, 7, 11, 14, 17, 25, 26, 30)


def test_a_capture_with_every_metadata_block_intact_reports_no_relocation(client: Any) -> None:
    """The fixture the other ds1_*.json files can't be: a whole 31-index page,
    nothing nulled. On the slimmed fixtures `misplaced == ()` is true however
    the scan behaves, because there is nothing left to mistake for rows."""
    payload = json.loads(_ds1("ds1_metadata_blocks_kept.json"))
    decoys = tuple(
        i
        for i, block in enumerate(payload)
        if i not in gfid._DS_ROW_BLOCKS and gfid._looks_like_a_row_block(block)
    )
    assert decoys == _METADATA_DECOYS, "fixture was re-trimmed; it no longer pins anything"
    board = gfid._rows_from_ds1(payload)
    assert board.misplaced == ()
    assert board.blocks_seen == 2
    assert len(board.rows) == 3
    fake = client(_FakeResponse(text=_page(_ds1("ds1_metadata_blocks_kept.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert len(fake.gets) == 1


def test_a_block_that_is_not_a_row_list_is_a_shape_change(client: Any) -> None:
    """Junk at the indices we read is a value Google has never served. Reading
    it as an empty board would report "no flights on this route" for a page we
    simply can no longer parse."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = ["not-a-row-block"]
    payload[3] = ["nor-this"]
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"ds:1\[2\] holds list"):
        gfid._one_call(_FILTERS)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("[]", id="nothing-at-all"),
        pytest.param("[null]", id="one-entry"),
        pytest.param("[null,null,null]", id="one-short-of-3"),
        pytest.param('[null,null,null,["x"]]', id="list-of-strings-at-3"),
        pytest.param('[0,0,"junk",[[]]]', id="bare-string-at-2"),
        pytest.param('[0,0,{"a":1},[[]]]', id="object-at-2"),
        pytest.param("[0,0,7,[[]]]", id="int-at-2"),
    ],
)
def test_a_truncated_or_junk_payload_is_a_shape_change(client: Any, payload: str) -> None:
    """Iterating a list never visits an index that isn't there, so without an
    arity check the first three of these decode, skip the scan entirely and
    reach the user as an authoritative "no flights" at exit 0."""
    client(_FakeResponse(text=_page(payload)))
    with pytest.raises(GfPageShapeError):
        gfid._one_call(_FILTERS)


def test_the_shortest_payload_that_can_hold_a_board_is_read_not_refused(client: Any) -> None:
    """The boundary the arity check sits on: at arity 4 index [3] exists, so an
    absent board there is Google's answer rather than a truncation."""
    client(_FakeResponse(text=_page("[0,0,null,null]")))
    assert gfid._one_call(_FILTERS) == []


def test_an_absent_board_records_the_types_it_saw(client: Any, caplog: Any) -> None:
    """An absent board and a board we failed to recognise look identical from
    the outside, so the types at the two indices are the only thing a later
    reader has to tell them apart with."""
    client(_FakeResponse(text=_page(_ds1("ds1_flightless_board.json"))))
    with caplog.at_level(logging.DEBUG, logger="flight_cli._gflight_ids"):
        assert gfid._one_call(_FILTERS) == []
    assert "carried no row block at [2, 3] (types ['NoneType', 'NoneType'])" in caplog.text


def test_relocated_row_blocks_raise_page_shape(client: Any) -> None:
    """A payload that decodes but whose row blocks moved off [2]/[3] yields no
    rows — indistinguishable from an empty board without the block count."""
    client(_FakeResponse(text=_page(_ds1("ds1_blocks_relocated.json"))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


def test_a_bad_leading_row_does_not_hide_a_relocation(client: Any) -> None:
    """One unparseable row at the head of a moved block is exactly what a shape
    change looks like, so a probe that reads only the first row answers "not
    rows" on the very payloads it exists to catch."""
    payload = json.loads(_ds1("ds1_blocks_relocated.json"))
    for index in (4, 5):
        payload[index][0].insert(0, ["not-a-row"])
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"holds flight rows at \[4, 5\]"):
        gfid._one_call(_FILTERS)


def test_a_served_board_with_row_shaped_blocks_elsewhere_is_served_with_a_warning(
    client: Any, caplog: Any
) -> None:
    """Live pages carry 7-11 blocks that are row-shaped by structure, so
    refusing whenever one of them happens to parse would throw away a board we
    answered completely. The warning is what keeps it findable."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[5] = copy.deepcopy(payload[2])
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with caplog.at_level(logging.WARNING, logger="flight_cli._gflight_ids"):
        out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert "carried row-shaped blocks outside [2, 3] at [5]; served 3 rows" in caplog.text


@pytest.mark.parametrize(
    ("field", "value", "raised"),
    [
        pytest.param([0, 2, 0, 20], [10**100, 1, 1], OverflowError, id="year-past-a-c-long"),
        pytest.param([0, 2], None, TypeError, id="null-legs-field"),
    ],
)
def test_a_row_the_decoder_cannot_survive_is_typed_not_a_traceback(
    client: Any, field: list[int], value: Any, raised: type[Exception]
) -> None:
    """The row decoder reaches into untrusted remote data, and neither of these
    is an exception the original guard listed. A traceback here is the same
    outcome as a crash — the query dies and the user gets no typed refusal."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    for row in payload[2][0] + payload[3][0]:
        target = row
        for key in field[:-1]:
            target = target[key]
        target[field[-1]] = value
    with pytest.raises(raised):
        gfid._parse_flight_with_id(payload[2][0][0])  # the edit really does raise it
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match="none of 3 Google Flights rows parsed"):
        gfid._one_call(_FILTERS)


def test_the_probe_survives_the_same_rows_away_from_the_board(client: Any) -> None:
    """Same rows, parked at an index the probe scans rather than at [2]/[3].
    The probe feeds arbitrary metadata to the row decoder on every page, so it
    must classify a row it cannot decode, never propagate the failure."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[5] = copy.deepcopy(payload[2])
    payload[5][0][0][0][2][0][20] = [10**100, 1, 1]
    assert gfid._holds_flight_rows(payload[5]) is False
    client(_FakeResponse(text=_page(json.dumps(payload))))
    assert len(gfid._one_call(_FILTERS)) == 3


def test_brace_in_a_row_string_does_not_truncate_the_blob(client: Any) -> None:
    """`});` inside benign Google copy must not cut the capture short — the
    blob terminates on the `sideChannel` key for exactly this reason."""
    fake = client(_FakeResponse(text=_page(_ds1("ds1_brace_in_string.json"))))
    assert len(gfid._one_call(_FILTERS)) == 3
    assert len(fake.gets) == 1


def test_second_ds1_is_consulted_when_the_first_is_undecodable() -> None:
    html = _page("[[,]]") + _page(_ds1("ds1_jfk_lax_3rows.json"))
    payload = gfid._extract_ds1(html)
    assert payload is not None
    assert len(gfid._rows_from_ds1(payload).rows) == 3


def test_consent_page_raises_consent(client: Any) -> None:
    client(_FakeResponse(text=_CONSENT_PAGE))
    with pytest.raises(GfConsentError):
        gfid._one_call(_FILTERS)


def test_missing_ds1_raises_page_shape(client: Any) -> None:
    client(_FakeResponse(text=_SHAPE_CHANGE_PAGE))
    with pytest.raises(GfPageShapeError, match="page shape changed"):
        gfid._one_call(_FILTERS)


def test_zero_of_n_rows_parsing_raises_page_shape_with_reasons(client: Any) -> None:
    """Rows present and none parsed is a moved row layout, a different fact
    from an empty board — and the sampled reasons are what makes it fixable."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = [[["not-a-row"], ["nor-this"]]]
    payload[3] = [[]]
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match="none of 2 Google Flights rows parsed"):
        gfid._one_call(_FILTERS)


def test_a_real_results_page_is_not_read_as_consent(client: Any) -> None:
    """Google's own footer links to the consent domain, so the consent markers
    only mean anything once ds:1 has already come back missing."""
    page = _page(_ds1("ds1_jfk_lax_3rows.json")).replace(
        "</body>", '<a href="https://consent.google.com/">Privacy</a></body>'
    )
    client(_FakeResponse(text=page))
    assert len(gfid._one_call(_FILTERS)) == 3
