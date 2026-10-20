# pyright: reportPrivateUsage=false
"""The `gf_session` fake answers by the kind of page a GET asks for."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from flight_cli.links import google_flights_search_page_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import _FakeClient


def test_a_cheapest_get_arriving_first_still_gets_the_cheapest_body(
    gf_session: Callable[..., _FakeClient],
) -> None:
    fake = gf_session("best page", "cheapest page")
    session = fake._session()
    best_url = google_flights_search_page_url(b"tfs")
    cheapest_url = google_flights_search_page_url(b"tfs", cheapest=True)

    first = session.get(cheapest_url)
    second = session.get(best_url)

    assert [first.text, second.text] == ["cheapest page", "best page"]
    assert fake.gets == [cheapest_url, best_url]
    # The counters sit under a lock, so two threads can GET at once: it is free
    # once each GET has answered, and a GET waits while it is held.
    assert session._lock.acquire(blocking=False)
    waiting = threading.Thread(target=session.get, args=(best_url,))
    try:
        waiting.start()
        waiting.join(timeout=0.5)
        assert waiting.is_alive()
    finally:
        session._lock.release()
    waiting.join()
    assert fake.gets == [cheapest_url, best_url, best_url]
