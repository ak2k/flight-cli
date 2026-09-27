"""The page-RPC envelope reader and wall check every captured page surface shares.

`fixtures/gf_rpc/error13.body` is an error row verbatim; the other bodies here
are built in the captured envelope's shape.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import (
    GfConsentError,
    GfThrottledError,
    GfUpstreamStatusError,
)
from flight_cli._gf_rpc_shared import GfPageRpcError, dig, refuse_a_wall, result_payloads

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_WHAT = "the test response"


def _envelope(*rows: list[object]) -> str:
    """A `batchexecute` body: one chunk per row, then the bookkeeping rows."""
    parts = [")]}'\n"]
    for row in (*rows, ["di", 70]):
        chunk = json.dumps([row])
        parts.append(f"\n{len(chunk) + 1}\n{chunk}")
    return "".join(parts) + "\n"


def test_every_result_row_is_decoded_in_order() -> None:
    body = _envelope(["wrb.fr", None, json.dumps([1])], ["wrb.fr", None, json.dumps([2])])
    assert result_payloads(body, what=_WHAT) == [[1], [2]]


def test_error_13_is_a_session_refusal_with_its_code_and_never_a_throttle() -> None:
    body = (FIXTURES / "gf_rpc" / "error13.body").read_text()
    with pytest.raises(GfPageRpcError) as caught:
        result_payloads(body, what=_WHAT)
    assert caught.value.code == 13
    assert not isinstance(caught.value, GfThrottledError)
    text = str(caught.value).lower()
    assert "error 13" in text
    assert "rate" not in text
    assert "thrott" not in text


def test_one_error_row_refuses_the_whole_stream() -> None:
    """A stream that failed part of the way is not an answer."""
    body = _envelope(
        ["wrb.fr", None, json.dumps([1])], ["wrb.fr", None, None, None, None, [13, None, []]]
    )
    with pytest.raises(GfPageRpcError) as caught:
        result_payloads(body, what=_WHAT)
    assert caught.value.code == 13


def test_an_empty_payload_without_a_code_is_still_a_refusal() -> None:
    with pytest.raises(GfPageRpcError, match="came back empty") as caught:
        result_payloads(_envelope(["wrb.fr", None, ""]), what=_WHAT)
    assert caught.value.code is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("<!doctype html><html></html>", id="html"),
        pytest.param(_envelope(["di", 1]), id="no-result-row"),
        pytest.param(')]}\'\n\n12\n[["wrb.fr", nul', id="truncated"),
        pytest.param(_envelope(["wrb.fr", None, "{not json"]), id="bad-payload"),
    ],
)
def test_a_body_with_nothing_readable_is_a_refusal_not_an_empty_answer(body: str) -> None:
    with pytest.raises(GfPageRpcError, match="could not be read"):
        result_payloads(body, what=_WHAT)


def test_dig_stops_where_the_path_runs_out() -> None:
    value = [[1, [2, 3]], None]
    assert dig(value, 0, 1, 1) == 3
    assert dig(value, 1, 0) is None
    assert dig(value, 5) is None
    assert dig("not a list", 0) is None


def _page(
    *,
    url: str = "https://www.google.com/travel/flights/booking?tfs=x",
    html: str = "",
    status: int = 200,
) -> PageFetch:
    return PageFetch(html=html, final_url=url, status_code=status)


def test_a_served_page_passes_the_wall_check() -> None:
    assert refuse_a_wall(_page(html="<html>app shell</html>")) is None


@pytest.mark.parametrize(
    "page",
    [
        pytest.param(
            _page(url="https://www.google.com/sorry/index?continue=x"), id="sorry-redirect"
        ),
        pytest.param(_page(status=429), id="429"),
    ],
)
def test_a_throttle_is_named_before_the_capture_waits(page: PageFetch) -> None:
    with pytest.raises(GfThrottledError):
        refuse_a_wall(page)


def test_a_consent_wall_is_named() -> None:
    with pytest.raises(GfConsentError):
        refuse_a_wall(_page(url="https://consent.google.com/ml?continue=x"))


def test_a_non_2xx_navigation_is_named() -> None:
    with pytest.raises(GfUpstreamStatusError):
        refuse_a_wall(_page(status=503))
