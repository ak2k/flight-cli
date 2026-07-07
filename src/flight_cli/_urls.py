"""Deep-link emission for Matrix and Google Flights, with itinerary pinning.

`emit_urls` prints the Matrix search URL and the Google Flights URL for a search,
deep-linking to a specific itinerary when the result carries the server-generated
identifiers (Matrix) or reducible segment lists (Google Flights) needed to pin
it. The `pinned_solution_index` / `try_pinned_*` helpers resolve the pinned row.
Imported back into `cli` (under private-name aliases) so paths and commands
reference them as module globals; `emit_urls` is a monkeypatch point in tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._console import console
from ._parsing import ROUND_TRIP_LEGS
from .links import (
    extract_pin_segments_from_slice,
    google_flights_pinned_url,
    google_flights_url,
    matrix_deep_link,
    matrix_itinerary_url,
)

if TYPE_CHECKING:
    from .domain import Search
    from .models import SearchResult

# URL emission flags shared by `search` / `calendar` / `detail`.
#
# Both URLs encode the search criteria. The Google-Flights URL ALSO pins
# the cheapest matched itinerary (deep link to that specific selection)
# when an itinerary row is available; Matrix's URL only encodes the
# search (Matrix's SPA doesn't surface a per-itinerary URL state).
MATRIX_URL_HELP = (
    "Print the Matrix ITA search URL (pre-fills the search; Matrix's SPA "
    "doesn't expose per-itinerary URL state, so this is the deepest link "
    "available)."
)
GOOGLE_URL_HELP = (
    "Print the Google Flights URL. When a cheapest itinerary is resolved "
    "from the results, the URL deep-links to that specific itinerary "
    "(pins selected flights). Otherwise it pre-fills the search."
)


def pinned_solution_index(result: SearchResult | None, pick: int | None) -> int | None:
    """0-based index into `result.solutions` of the itinerary to pin in a deep
    link. `pick` is the 1-based itinerary number the user saw in the table;
    None pins the cheapest (row 1). Out-of-range picks warn and fall back to
    the cheapest rather than emit a wrong or broken link. None when there's
    nothing to pin."""
    if result is None or not result.solutions:
        return None
    if pick is None:
        return 0
    if pick < 1 or pick > len(result.solutions):
        console.print(
            f"[yellow]--pick {pick} is out of range (1-{len(result.solutions)}); "
            f"pinning the cheapest itinerary instead.[/]"
        )
        return 0
    return pick - 1


def try_pinned_matrix_url(search: Search, result: SearchResult | None, idx: int) -> str | None:
    """Build a Matrix `/itinerary` URL pinning solution `idx`, if the result
    carries all three server-generated identifiers (session, solutionSet, and
    the solution's own id). Returns None when any are missing, the search shape
    doesn't support pinning, or the search isn't a specific-date variant.
    """
    if result is None or idx >= len(result.solutions):
        return None
    sol = result.solutions[idx]
    if not sol.id or not result.session or not result.solution_set:
        return None
    try:
        return matrix_itinerary_url(
            search,
            solution_id=sol.id,
            session=result.session,
            solution_set=result.solution_set,
        )
    except TypeError:
        return None


def try_pinned_gflight_url(search: Search, result: SearchResult | None, idx: int) -> str | None:
    """Build a Google Flights URL that pre-selects itinerary `idx` in `result`,
    if the data supports it. Returns None when the result is empty, the search
    shape doesn't support pinning (calendar-grid mode), or any slice can't be
    reduced to a segment list (see `extract_pin_segments_from_slice` for the
    bail-out cases).
    """
    if result is None or idx >= len(result.solutions):
        return None
    itn = result.solutions[idx].itinerary
    if itn is None or not itn.slices:
        return None
    out_segments = extract_pin_segments_from_slice(itn.slices[0])
    if out_segments is None:
        return None
    ret_segments = None
    if len(itn.slices) >= ROUND_TRIP_LEGS:
        ret_segments = extract_pin_segments_from_slice(itn.slices[1])
        if ret_segments is None:
            return None
    try:
        return google_flights_pinned_url(
            search,
            outbound_segments=out_segments,
            return_segments=ret_segments,
        )
    except (TypeError, AssertionError):
        # `google_flights_pinned_url` rejects calendar-mode searches and
        # legs missing dates — both are expected non-pin cases, fall back.
        return None


def emit_urls(
    search: Search,
    *,
    matrix_url: bool,
    google_url: bool,
    result: SearchResult | None = None,
    pick: int | None = None,
) -> None:
    idx = pinned_solution_index(result, pick)
    # Only claim "#N" when we actually honored the user's pick; an out-of-range
    # pick falls back to idx 0 and must not mislabel the cheapest as "#N".
    pinned_label = (
        f"itinerary #{pick}"
        if (idx is not None and pick is not None and idx == pick - 1)
        else "cheapest itinerary"
    )
    if matrix_url:
        console.print()
        pinned_m = try_pinned_matrix_url(search, result, idx) if idx is not None else None
        if pinned_m is not None:
            console.print(f"[dim]Matrix ({pinned_label} pinned):[/]")
            console.print(f"  [link]{pinned_m}[/]")
        else:
            console.print("[dim]Matrix deep-link:[/]")
            console.print(f"  [link]{matrix_deep_link(search)}[/]")
    if google_url:
        # `google_flights_url` builds protobuf-encoded tfs= URLs via fast_flights.
        # That library has no documented exception surface — catch broadly so a
        # missing IATA or unsupported variant degrades the URL line, not the run.
        try:
            pinned = try_pinned_gflight_url(search, result, idx) if idx is not None else None
            if pinned is not None:
                console.print(f"[dim]Google Flights ({pinned_label} pinned):[/]")
                console.print(f"  [link]{pinned}[/]")
            else:
                console.print("[dim]Google Flights (tfs= structured):[/]")
                console.print(f"  [link]{google_flights_url(search)}[/]")
        except Exception as e:  # noqa: BLE001 - third-party undocumented errors; non-fatal fallback
            console.print(f"[dim]Google Flights link: {e}[/]")
