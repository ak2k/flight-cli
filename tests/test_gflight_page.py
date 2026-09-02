# pyright: reportPrivateUsage=false
"""The search-page transport: `ds:1` extraction and refusal classification.

Google's `GetShoppingResults` RPC has been gated since 2026-08, so the gflight
backend GETs the public search page and reads the flight rows Google inlines in
its `AF_initDataCallback` `ds:1` blob. Every failure mode of that fetch — a
captcha interstitial, a consent wall, a re-shaped page — renders as zero rows,
so the load-bearing behavior under test is that none of them can reach the user
as "no flights on this route".

Fixtures are real captures (2026-09-02) trimmed to three rows with the session
id scrubbed — the top-level arity and the row blocks are kept, and the ~3.5 MB
of UI copy and airport metadata no code reads is dropped. Two page shapes are
pinned because Google serves both: an initial JFK-LAX search (a row block at
`[2]` AND `[3]`) and a pinned return leg (`[2] = None`, the whole board at
`[3]`).
"""

from __future__ import annotations

import json
import pathlib
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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    """Install a fake GF client and keep cookie seeding off the real cache."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"seeded": False, "persisted": False})

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
    rows, blocks_seen = gfid._rows_from_ds1(payload)
    assert blocks_seen == 2
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
    rows, _ = gfid._rows_from_ds1(payload)
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

    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gfid, "_cookie_state", {"seeded": False, "persisted": False})

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
    """A leg pinned through tfs 3.4 legitimately carries `[2] = None` with the
    whole board at `[3]` — there is no top-flights ranking to show for a board
    answering an already-chosen outbound. Captured live 2026-09-02 from the
    HNL-MIA round-trip expansion; requiring both blocks refused it as a shape
    change and cost the user a real result."""
    payload = json.loads(_ds1("ds1_return_leg_pinned.json"))
    assert payload[2] is None, "fixture must keep the served shape"
    fake = client(_FakeResponse(text=_page(_ds1("ds1_return_leg_pinned.json"))))
    out = gfid._one_call(_FILTERS)
    assert len(out) == 3
    assert all(g.flight_id for g in out)
    assert len(fake.gets) == 1


def test_a_single_empty_block_is_an_authoritative_empty(client: Any) -> None:
    """The pinned shape with nothing in it: one block, zero rows. An empty
    block and a missing block are indistinguishable from the rows alone, so
    this has to be Google's answer rather than a refusal."""
    payload = json.loads(_ds1("ds1_single_block_empty.json"))
    assert payload[2] is None and payload[3] == [[]]
    client(_FakeResponse(text=_page(_ds1("ds1_single_block_empty.json"))))
    assert gfid._one_call(_FILTERS) == []


def test_a_block_that_is_not_a_row_list_does_not_count(client: Any) -> None:
    """A list at the right index whose [0] isn't a row list is not a row
    block — counting it would let a moved payload pass the guard."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    payload[2] = ["not-a-row-block"]
    payload[3] = ["nor-this"]
    client(_FakeResponse(text=_page(json.dumps(payload))))
    with pytest.raises(GfPageShapeError, match=r"no row block at \[2\] or \[3\]"):
        gfid._one_call(_FILTERS)


def test_relocated_row_blocks_raise_page_shape(client: Any) -> None:
    """A payload that decodes but whose row blocks moved off [2]/[3] yields no
    rows — indistinguishable from an empty board without the block count."""
    client(_FakeResponse(text=_page(_ds1("ds1_blocks_relocated.json"))))
    with pytest.raises(GfPageShapeError, match=r"no row block at \[2\] or \[3\]"):
        gfid._one_call(_FILTERS)


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
    assert len(gfid._rows_from_ds1(payload)[0]) == 3


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
