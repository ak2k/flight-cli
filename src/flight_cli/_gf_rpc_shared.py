"""Reading a response Google Flights' own page received, shared by every path
that captures one through `GfBrowserSession.capture`.

Such a response is a `batchexecute` body: the `)]}'` guard, then chunks of a
length line and one JSON array of rows. A result row is
`["wrb.fr", rpc id, payload JSON, …]`; an error row leaves the payload empty and
carries `[code, …]` at index 5. One reader and one refusal type, so every page
surface reads that envelope and names its failures the same way.

Error 13 is the one error row seen in practice. It stays with the browser
session that received it, so it is never typed or worded as a throttle: that
would send the user to wait out a limit that is not there, and a retry in the
same session gets the same answer.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from ._gf_errors import GfBackendError, GfConsentError, GfThrottledError, GfUpstreamStatusError
from ._gflight_ids import (
    _is_consent_page,  # pyright: ignore[reportPrivateUsage]
    _is_page_throttled,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from ._gf_common import PageFetch

_XSSI_GUARD = ")]}'"
# The stated length does not count what `len(str)` counts, so each chunk is read
# by decoding one JSON value rather than by slicing.
_CHUNK_HEAD = re.compile(r"\s*\d+\s*\n")
_RESULT_ROW = "wrb.fr"
# `[tag, rpc id, payload, …]`
_PAYLOAD_AT = 2


class GfPageRpcError(GfBackendError):
    """A response the page received carries nothing that can be shown: an
    error row, no result row, a body this reader cannot decode, or a payload
    the caller found empty or answering another question.

    Each of those would otherwise reach the user as an empty list, which reads
    as "Google has nothing" when nothing was read. `code` is the error row's
    code when there was one."""

    def __init__(self, reason: str, *, code: int | None = None) -> None:
        """Say what came back; `code` is the error row's, if there was one."""
        self.code = code
        super().__init__(reason)


def result_payloads(body: str, *, what: str) -> list[Any]:
    """The decoded payload of every result row in `body`, in order.

    `what` names the response in a refusal. Any error row refuses the whole
    body: a stream that failed part of the way is not an answer."""
    unreadable = f"{what} could not be read; its shape changed"
    text = body.removeprefix(_XSSI_GUARD)
    decoder = json.JSONDecoder()
    payloads: list[Any] = []
    at = 0
    try:
        while (head := _CHUNK_HEAD.match(text, at)) is not None:
            chunk, at = decoder.raw_decode(text, head.end())
            for row in cast("list[Any]", chunk) if isinstance(chunk, list) else []:
                if not isinstance(row, list) or cast("list[Any]", row)[:1] != [_RESULT_ROW]:
                    continue
                payloads.append(_payload(cast("list[Any]", row), what=what))
    except ValueError as e:
        raise GfPageRpcError(unreadable) from e
    if not payloads:
        raise GfPageRpcError(unreadable)
    return payloads


def dig(value: Any, *path: int) -> Any:
    """`value[p0][p1]…`, or None where the path runs out or meets a non-list."""
    for i in path:
        if not isinstance(value, list):
            return None
        items = cast("list[Any]", value)
        if i >= len(items):
            return None
        value = items[i]
    return value


def _payload(row: list[Any], *, what: str) -> Any:
    raw = row[_PAYLOAD_AT] if len(row) > _PAYLOAD_AT else None
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    code = _error_code(row)
    if code is None:
        raise GfPageRpcError(f"{what} came back empty")
    raise GfPageRpcError(
        f"{what} came back with error {code:d}: Google refused this browser session's request",
        code=code,
    )


def _error_code(row: list[Any]) -> int | None:
    """An error row's code: `row[5] = [code, None, [details]]`."""
    try:
        code = row[5][0]
    except (IndexError, TypeError):
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def url_currency(url: str) -> str:
    """The currency a page prices in: its URL's `curr=`. No response names
    one, and the page prices in whatever its URL asks for."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["curr"][0]


def refuse_a_wall(page: PageFetch) -> None:
    """A `check_page` for a navigation whose own HTML holds no data: name a
    throttle, a consent interstitial or a non-2xx status before the capture
    waits out its deadline for a request such a page never makes."""
    if page.status_code == HTTPStatus.TOO_MANY_REQUESTS or _is_page_throttled(
        final_url=page.final_url, html=page.html
    ):
        raise GfThrottledError("Google Flights rate-limited the browser; wait a while and retry")
    if _is_consent_page(final_url=page.final_url, html=page.html):
        raise GfConsentError("Google served its consent page instead of Google Flights")
    if not HTTPStatus.OK <= page.status_code < HTTPStatus.MULTIPLE_CHOICES:
        raise GfUpstreamStatusError(page.status_code)
