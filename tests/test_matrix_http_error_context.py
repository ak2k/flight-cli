"""A `MatrixHttpError` holds no handled exception: httpx's own error text quotes
the request URL, and Matrix's URL carries the API key. A refused key
(`ApiKeyResolutionError`) is raised with `from` and keeps its chain."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
import stamina

from flight_cli import client
from flight_cli.client import ApiKeyResolutionError, MatrixClient, MatrixHttpError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_KEY = "AIzaSyTestKeyNotReal0123456789"
_URL = "https://example.invalid/v1/search"

type _Call = Callable[[MatrixClient], Awaitable[object]]
_CALLS: dict[str, _Call] = {
    "_post": lambda c: c._post(_URL, {}, cache=False),  # pyright: ignore[reportPrivateUsage]
    "airports": lambda c: c.airports("x"),
    "airport": lambda c: c.airport("JFK"),
    "currencies": lambda c: c.currencies(),
}


@pytest.fixture(autouse=True)
def _stub_key_state(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse pytest fixture
    """A 403 invalidates the cached key and resolves a new one: neither may
    reach the operator's key cache."""

    def resolved(**_kw: object) -> str:
        return _KEY

    monkeypatch.setattr(client, "invalidate_cache", lambda: None)
    monkeypatch.setattr(client, "resolve_api_key", resolved)


def _run(tmp_path: Path, answers: list[int], call: _Call, *, rebootstrap: bool = False) -> None:
    """Run `call` on a client whose transport answers each request with the
    next status in `answers`."""
    pending = iter(answers)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(pending), text="no")

    async def go() -> None:
        c = MatrixClient(api_key=_KEY, cache_dir=str(tmp_path), rebootstrap=rebootstrap)
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
        async with c:
            await call(c)

    with stamina.set_testing(True, attempts=1):
        anyio.run(go)


@pytest.mark.parametrize("caller", [*_CALLS, "_post after a rebootstrap"])
def test_an_http_error_holds_no_url_in_its_cause_or_context(tmp_path: Path, caller: str) -> None:
    after_403 = caller == "_post after a rebootstrap"
    call = _CALLS["_post" if after_403 else caller]

    with pytest.raises(MatrixHttpError) as caught:
        _run(tmp_path, [403, 502] if after_403 else [502], call, rebootstrap=after_403)

    e = caught.value
    assert str(e) == "Matrix answered HTTP 502 Bad Gateway", str(e)
    assert e.__cause__ is None and e.__suppress_context__
    assert e.__context__ is None, repr(e.__context__)


@pytest.mark.parametrize(("answers", "rebootstrap"), [([403], False), ([403, 403], True)])
def test_a_refused_key_keeps_its_chain(
    tmp_path: Path, answers: list[int], rebootstrap: bool
) -> None:
    with pytest.raises(ApiKeyResolutionError) as caught:
        _run(tmp_path, answers, _CALLS["_post"], rebootstrap=rebootstrap)

    cause = caught.value.__cause__
    assert isinstance(cause, httpx.HTTPStatusError), repr(cause)
    assert cause.response.status_code == 403
