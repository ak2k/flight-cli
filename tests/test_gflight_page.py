# pyright: reportPrivateUsage=false
"""The search-page transport: `ds:1` extraction and refusal classification.

Google's `GetShoppingResults` RPC has been gated since 2026-08, so the gflight
backend GETs the public search page and reads the flight rows Google inlines in
its `AF_initDataCallback` `ds:1` blob. Every failure mode of that fetch — a
captcha interstitial, a consent wall, a re-shaped page — renders as zero rows,
so the load-bearing behavior under test is that none of them can reach the user
as "no flights on this route".

Fixtures are a real JFK-LAX capture (2026-09-02) trimmed to three rows with the
session id scrubbed: the top-level arity and the two row blocks are kept, and
the ~3.5 MB of UI copy and airport metadata no code reads is dropped.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, cast

import pytest

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


class _FakeResponse:
    def __init__(self, *, text: str, status_code: int = 200, url: str = "") -> None:
        self.text = text
        self.status_code = status_code
        self.url = url or "https://www.google.com/travel/flights?tfs=abc"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class _FakeClient:
    """Counts GETs so a test can assert the request budget, not just the value."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.gets: list[str] = []

    def get(self, url: str, **_kw: object) -> _FakeResponse:
        self.gets.append(url)
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

    def install(response: _FakeResponse) -> _FakeClient:
        fake = _FakeClient(response)
        monkeypatch.setattr(gfid, "get_client", lambda: fake)
        return fake

    return install


# ─────────────────────────── ds:1 extraction ───────────────────────────


def test_extract_ds1_reads_the_flights_blob_past_other_keys() -> None:
    payload = gfid._extract_ds1(_page(_ds1("ds1_jfk_lax_3rows.json")))
    assert payload is not None
    assert len(gfid._rows_from_ds1(payload)) == 3


def test_extract_ds1_returns_none_when_the_key_is_absent() -> None:
    assert gfid._extract_ds1(_SHAPE_CHANGE_PAGE) is None


def test_extract_ds1_returns_none_on_undecodable_data() -> None:
    assert gfid._extract_ds1(_page("[[,]]")) is None


def test_rows_keep_the_top_flights_block_first() -> None:
    """`ds:1[2]` is Google's own ranking and `[3]` the rest; concatenating in
    that order is the only way the page's ordering survives — we can't
    reproduce the blended rank locally."""
    payload = json.loads(_ds1("ds1_jfk_lax_3rows.json"))
    rows = gfid._rows_from_ds1(payload)
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
    client(_FakeResponse(text="", status_code=429))
    with pytest.raises(GfThrottledError):
        gfid._one_call(_FILTERS)


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
