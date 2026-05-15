"""Seatmap URL helpers.

The Legrooms+ Chrome extension fetches `https://www.travelarrow.io/api/s`
with per-flight params and `window.open`s `https://seatmaps.com/<seatmap_url>`
with the returned path. We mirror that two-step:

- `seatmap_api_url(...)` constructs the deterministic API URL — no HTTP.
- `fetch_seatmap_url(...)` resolves it to the seatmaps.com URL via one GET.

Decoupled because most callers just want a clickable link they can paste;
the indirection through travelarrow.io is acceptable for the cheap path.
Eager batch fetches across a result set are opt-in.
"""

from __future__ import annotations

import datetime as _dt
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_SEATMAP_API = "https://www.travelarrow.io/api/s"
_SEATMAPS_BASE = "https://seatmaps.com"


def _format_date(d: _dt.date | str) -> str:
    """Legrooms+ extension sends `M/D/YYYY` (no zero-padding) — confirmed
    from load_flight_data.js: `date=${n}/${i}/${e}` where [e, n, i] = raw[20]
    is [year, month, day]. The travelarrow API tolerates ISO too, but we
    match the extension's wire shape exactly to minimize divergence risk."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d[:10])
    return f"{d.month}/{d.day}/{d.year}"


def seatmap_api_url(
    *,
    origin: str,
    dest: str,
    flight_number: str,
    carrier: str,
    date: _dt.date | str,
    aircraft: str | None = None,
) -> str:
    """Build the travelarrow.io/api/s URL for a single physical leg.

    `flight_number` may be either bare ("100") or IATA-prefixed ("AA100");
    we strip the prefix if it matches `carrier` to match what the extension
    sends (it passes `s.number` which is the bare number from data[0][2][i][22][1]).
    """
    bare = flight_number
    if bare[:2].upper() == carrier.upper():
        bare = bare[2:]
    params: list[tuple[str, str]] = [
        ("from", origin.upper()),
        ("to", dest.upper()),
        ("flightno", bare),
        ("carrier", carrier.upper()),
        ("date", _format_date(date)),
    ]
    if aircraft:
        params.append(("aircraft", aircraft.strip()))
    return f"{_SEATMAP_API}?{urllib.parse.urlencode(params)}"


def fetch_seatmap_url(
    *,
    origin: str,
    dest: str,
    flight_number: str,
    carrier: str,
    date: _dt.date | str,
    aircraft: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Resolve the travelarrow API to a seatmaps.com URL via one HTTP GET.

    Returns None if the API responds without a `seatmap_url` field (no
    seatmap on file for this aircraft/cabin combination). Network errors
    raise — callers that want graceful degradation should catch.
    """
    import httpx  # noqa: PLC0415

    url = seatmap_api_url(
        origin=origin,
        dest=dest,
        flight_number=flight_number,
        carrier=carrier,
        date=date,
        aircraft=aircraft,
    )
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    body: Mapping[str, object] = resp.json()
    path = body.get("seatmap_url")
    if not isinstance(path, str) or not path:
        return None
    return f"{_SEATMAPS_BASE}/{path.lstrip('/')}"
