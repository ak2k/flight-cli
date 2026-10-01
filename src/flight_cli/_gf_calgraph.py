"""Google Flights' price graph, read from the search page's own request.

`GetCalendarGraph` answers a client that cannot sign its request with an empty
payload (`_gf_dategrid`). The search page signs its own, so this path lets the
page ask: Chrome opens the filtered search page on the window's first date,
clicks "Price graph", and `GfBrowserSession.capture` hands back the response the
page received. Nothing here writes a request or runs script in the page.

The graph carries the cheapest fare per departure date and no itineraries, so
nothing can be filtered afterwards. Every constraint on the search has to be on
the page URL exactly, and Google has to have been seen applying it there, or the
grid answers a wider question and prints that as the answer; `graph_blocker`
names the constraints that fail either test.

fli is heavy, so `cli` imports this module only when a browser grid runs.
"""

from __future__ import annotations

import contextlib
import json
import re
import urllib.parse
from datetime import date, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from . import _gf_browser
from ._gf_browser import Control
from ._gf_dategrid import grid_routing_blocker, unwritten_constraint
from ._gf_errors import GfBackendError, GfBrowserUnavailableError, GfPageShapeError
from ._gflight_ids import (
    _rows_from_page_html,  # pyright: ignore[reportPrivateUsage]
    search_page_url,
)
from .domain import covers_one_window, time_bounds
from .fli_bridge import (
    _fli_max_stops,  # pyright: ignore[reportPrivateUsage]
    apply_gf_native_filters,
    to_fli_filter,
)
from .routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectTimePred,
    MaxDurationPred,
    StopsPred,
    Tier,
    classify,
    page_can_encode,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from ._gf_common import PageFetch
    from .domain import CalendarSearch, SearchOptions, TimeOfDay
    from .routing_predicates import Predicate

_PRICE_GRAPH = Control("button", "Price graph")
_GRAPH_RPC = "/GetCalendarGraph"
# One load prices about five weeks from the date it opens on (measured: seven
# days before it to thirty after; another page gave sixty days), so eight loads
# cover most of a year. A window needing more is refused, never paged unbounded.
_MAX_PAGES = 8
# What one load is counted as covering when a window is admitted, from the date
# it opens on. Only an estimate: the loads themselves page by the span each
# response reports.
_PAGE_DAYS = 31
# Google reads a latest departure hour to its last minute, so whole hours bound
# a window exactly only when it ends on the day's last minute.
_DAY_END = 23 * 60 + 59
_XSSI_GUARD = ")]}'"
# A batchexecute `rt=c` body is chunked: a length line, then one JSON array of
# rows. The stated length does not count what `len(str)` counts, so each chunk
# is read by decoding one JSON value instead.
_CHUNK_HEAD = re.compile(r"\s*\d+\s*\n")
_RESULT_ROW = "wrb.fr"


class GfPriceGraphError(GfBackendError):
    """The page's price graph came back with nothing that can be shown.

    An error row, a graph with no priced date in the window, and a body this
    parser cannot read are all this: each would otherwise reach the user as an
    empty grid, which reads as "no fares" when nothing was priced. `code` is the
    error row's code when there was one.

    An error row is not a throttle. Error 13 on the page's own request stays
    with the browser session across retries, and "rate-limited" would send the
    user to wait out a limit that is not there."""

    def __init__(self, reason: str, *, code: int | None = None) -> None:
        """Say what came back; `code` is the error row's, if the graph had one."""
        self.code = code
        super().__init__(reason)


class GfGraphBudgetError(GfPriceGraphError):
    """The graph's page loads ran out before its window did, with loads spent
    on something other than the window: a page loaded again because it drew no
    graph, or the trip lengths before it. `loads` is every load it spent.

    Its own type because "narrow --start/--end" would send the user to shorten
    a window that fits in the budget on its own."""

    def __init__(self, *, loads: int, reloaded: bool) -> None:
        """Name the budget, and a page loaded again when one was."""
        self.loads = loads
        again = " (a page that drew no graph was loaded again)" if reloaded else ""
        super().__init__(
            f"no price-graph load of the {_MAX_PAGES:d} was left for the rest of the window{again}"
        )


class GfGraphStalledError(GfBrowserUnavailableError):
    """A page that passed the wall check drew no price graph in time, and its
    second load did not either, or had no load left to try. `loads` is every
    load the graph spent, these included.

    Its own type because it is the one failure that says nothing about the next
    trip length's page, which a range goes on to ask."""

    def __init__(self, cause: GfBrowserUnavailableError, *, loads: int) -> None:
        """Keep the browser's words and remedy; count what the graph spent."""
        self.loads = loads
        super().__init__(cause.reason, remedy=cause.remedy)


class GraphCell(NamedTuple):
    """One priced departure date. `return_date` is set on a round trip only."""

    departure: date
    return_date: date | None
    price: float


class PriceGraph(NamedTuple):
    """The priced dates inside the window, by departure. `trip_length` is the
    nights between outbound and return, None for one-way; `loads` is how many
    page loads it took."""

    trip_length: int | None
    cells: tuple[GraphCell, ...]
    loads: int = 1


class LostLength(NamedTuple):
    """A trip length whose graph is not shown, and why."""

    nights: int | None
    cause: GfBackendError


class GraphRange(NamedTuple):
    """The graphs a calendar priced, by trip length in order, and the lengths it
    lost."""

    graphs: list[PriceGraph]
    lost: list[LostLength]


class _GraphPage(NamedTuple):
    """What one load's graph says: the last departure date it covers, priced or
    not, and the dates it priced."""

    last: date
    cells: list[GraphCell]


def page_blocker(search: CalendarSearch) -> str | None:
    """The `--fast --gf-transport http` gate's last check: a time window, a
    non-adult passenger, an availability switch, any predicate but a stop limit
    of two or fewer, or round-trip legs that differ, or None.

    The phrase completes "this is …" in the `--fast` refusal. Shape (one leg or
    two, Tier-1 routing only) and the airports are checked before this by the
    caller. The price graph asks `graph_blocker` instead, which admits the
    constraints Google was measured applying from the page URL; this narrower
    set keeps the RPC grid's admission apart from what was measured on the
    page."""
    for leg, which in zip(search.legs, ("departure", "return"), strict=False):
        if leg.time_ranges:
            return f"a {which}-time window"
    options = search.options
    if (switch := _option_blocker(options)) is not None:
        return switch
    per_leg = [classify(leg.route_language, leg.extension).predicates for leg in search.legs]
    stops = [] if options.max_extra_stops is None else [StopsPred(options.max_extra_stops)]
    _, reasons = page_can_encode([*stops, *(p for predicates in per_leg for p in predicates)])
    if reasons:
        return "; ".join(dict.fromkeys(reasons))
    if len(per_leg) > 1 and set(per_leg[0]) != set(per_leg[1]):
        return "different routing or extension codes on the outbound and the return"
    return None


def graph_blocker(search: CalendarSearch) -> str | None:  # noqa: PLR0911 — one return per named reason, in order
    """Why the price graph cannot be asked for this calendar, or None when it can.

    The phrase completes "this is …". The caller has checked the currency, the
    shape and the airports. What is left is every constraint the graph cannot
    both find on the page URL exactly and trust Google to apply there, since it
    has no rows to check afterwards. Admitted, per leg: a stop
    ceiling of two or fewer, one marketing-carrier or alliance include, a
    maximum duration, a minimum layover, a maximum layover, and a departure-time
    window the page's whole hours bound exactly; each was measured narrowing the
    graph (one-way LGA-LAX, and a carrier include on a round trip). A round
    trip's time window drew a graph with no priced date, so it is refused.

    Refusals are tried in the order the narrower gate tries them, so a calendar
    that neither admits is named as that gate names it: routing the grids refuse
    (`grid_routing_blocker`), then a code or bound the request would leave out,
    then a time window, the options, a predicate the page cannot carry, a
    combination the URL would write wider than asked, and legs that differ."""
    per_leg = [classify(leg.route_language, leg.extension).predicates for leg in search.legs]
    if any(p.tier is not Tier.GF_NATIVE and not _graph_takes(p) for ps in per_leg for p in ps):
        return grid_routing_blocker(search) or "a constraint the price grid can't honor"
    for i, predicates in enumerate(per_leg):
        if (phrase := unwritten_constraint(predicates)) is not None:
            return f"{phrase} on the return leg" if i else phrase
    stops = search.options.max_extra_stops
    if stops is not None and (phrase := unwritten_constraint([StopsPred(stops)])) is not None:
        return phrase
    round_trip = len(search.legs) > 1
    for leg, which in zip(search.legs, ("departure", "return"), strict=False):
        if leg.time_ranges and (round_trip or not _hours_bound_exactly(leg.time_ranges)):
            return f"a {which}-time window"
    if (switch := _option_blocker(search.options)) is not None:
        return switch
    _, reasons = page_can_encode(p for ps in per_leg for p in ps if not _graph_takes(p))
    if reasons:
        return "; ".join(dict.fromkeys(reasons))
    for i, predicates in enumerate(per_leg):
        if (wider := _wider_url(set(predicates))) is not None:
            return f"{wider} on the return leg" if i else wider
    if len(per_leg) > 1 and set(per_leg[0]) != set(per_leg[1]):
        return "different routing or extension codes on the outbound and the return"
    return None


def _graph_takes(p: Predicate) -> bool:
    """Whether the page URL writes `p` on its own exactly: `graph_blocker`
    refuses a zero bound, a stop ceiling fli cannot write and an unmapped code
    before asking this."""
    match p:
        case CarrierPred(exclude=False, operating=False):
            return True
        case StopsPred() | AlliancePred() | MaxDurationPred() | ConnectTimePred():
            return True
        case _:
            return False


def _hours_bound_exactly(buckets: Sequence[TimeOfDay]) -> bool:
    """Whether the page's whole departure hours ask for exactly these buckets.

    The page writes one earliest and one latest hour for the leg, so the
    buckets have to adjoin, and every bucket starts on the hour, so only the
    end can be widened: Google reads 11 as up to 11:59."""
    return covers_one_window(buckets) and max(time_bounds(b)[1] for b in buckets) == _DAY_END


def _wider_url(predicates: Collection[Predicate]) -> str | None:
    """A combination on one leg that the URL would write wider than asked.

    3.6 is one include list, which Google reads as any of its codes, where
    Matrix requires every include. The bridge keeps the last maximum duration
    and the last maximum layover it meets, and drops a minimum layover above
    the maximum."""
    includes = [p for p in predicates if isinstance(p, CarrierPred | AlliancePred)]
    if len(includes) > 1:
        if any(isinstance(p, AlliancePred) for p in includes):
            return "an alliance filter combined with another carrier or alliance filter"
        return "a carrier filter combined with another carrier filter"
    durations = sorted({p.minutes for p in predicates if isinstance(p, MaxDurationPred)})
    if len(durations) > 1:
        return f"more than one maximum trip duration ({', '.join(f'{m} min' for m in durations)})"
    layovers = [p for p in predicates if isinstance(p, ConnectTimePred)]
    maxima = sorted({p.max_minutes for p in layovers if p.max_minutes is not None})
    if len(maxima) > 1:
        return f"more than one maximum layover ({', '.join(f'{m} min' for m in maxima)})"
    minima = [p.min_minutes for p in layovers if p.min_minutes is not None]
    if maxima and minima and max(minima) > maxima[0]:
        return f"a minimum layover ({max(minima)} min) above the maximum ({maxima[0]} min)"
    return None


def _option_blocker(options: SearchOptions) -> str | None:
    """The search-wide options the page has no field for, for both gates."""
    pax = options.pax
    if pax.children or pax.seniors or pax.youth or pax.infants_in_seat or pax.infants_in_lap:
        return "a passenger type other than adults"
    if not options.allow_airport_changes:
        return "an airport-change exclusion (--no-airport-changes)"
    if not options.show_only_available:
        return "a request for unavailable fares (--include-unavailable)"
    return None


def page_url(search: CalendarSearch, departure: date) -> str:
    """The filtered search page whose price graph opens on `departure`.

    `to_fli_filter` first, so the page asks what a search would, with the
    return at `departure` plus the trip length. Then the legs' own predicates,
    then ONE stop limit: the lowest any source sets. The bridge reads `--stops`
    alone, and `apply_gf_native_filters` lets the last `StopsPred` it meets
    overwrite it, so either order on its own can widen a nonstop request.

    The bridge writes one predicate set onto every slice, and the gate admits a
    round trip only when both legs carry the same set, so the outbound's is
    written once: both legs' together would list each carrier twice."""
    window = search.window
    moved = window.model_copy(update={"start": departure, "end": max(departure, window.end)})
    filters = to_fli_filter(search.model_copy(update={"window": moved}))
    leg = search.legs[0]
    predicates = list(dict.fromkeys(classify(leg.route_language, leg.extension).predicates))
    apply_gf_native_filters(filters, predicates)
    limits = [p.max_stops for p in predicates if isinstance(p, StopsPred)]
    if search.options.max_extra_stops is not None:
        limits.append(search.options.max_extra_stops)
    if limits:
        filters.stops = _fli_max_stops(min(limits))
    return search_page_url(filters)


def _is_graph_rpc(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.endswith(_GRAPH_RPC)


def _refuse_a_wall(page: PageFetch) -> None:
    """Name a throttle, consent or error page before the click would time out
    on it, with the verdicts the search path reaches on the same page.

    A page whose rows moved can still draw the graph, so the one refusal that
    is about the rows is not this path's to raise."""
    with contextlib.suppress(GfPageShapeError):
        _rows_from_page_html(page)


class _WallCheck:
    """`_refuse_a_wall`, remembering whether the page got past it."""

    def __init__(self) -> None:
        self.passed = False

    def __call__(self, page: PageFetch) -> None:
        _refuse_a_wall(page)
        self.passed = True


def _envelope_rows(body: str) -> list[Any]:
    rows: list[Any] = []
    text = body.removeprefix(_XSSI_GUARD)
    decoder = json.JSONDecoder()
    at = 0
    while (head := _CHUNK_HEAD.match(text, at)) is not None:
        chunk, at = decoder.raw_decode(text, head.end())
        if isinstance(chunk, list):
            rows.extend(chunk)  # pyright: ignore[reportUnknownArgumentType] — decoded JSON
    return rows


def _result_row(body: str) -> list[Any]:
    """The one result row of the graph's answer; the others are bookkeeping."""
    unreadable = "Google Flights' price graph response could not be read; its shape changed"
    try:
        rows = _envelope_rows(body)
    except ValueError as e:
        raise GfPriceGraphError(unreadable) from e
    for row in rows:
        if isinstance(row, list) and cast("list[Any]", row)[:1] == [_RESULT_ROW]:
            return cast("list[Any]", row)
    raise GfPriceGraphError(unreadable)


def _price(cell: list[Any]) -> float | None:
    """The fare in a graph cell, `[dep, ret, [[_, price], token], _]`."""
    try:
        price = cell[2][0][1]
    except (IndexError, TypeError):
        return None
    if isinstance(price, bool) or not isinstance(price, int | float) or price <= 0:
        return None
    return float(price)


def parse_graph(body: str, *, trip_length: int | None) -> _GraphPage:
    """Read one `GetCalendarGraph` response the page received.

    Refuses rather than returning nothing: an error row, a body with no result
    row, a result with no dated cell, and anything that does not decode are all
    `GfPriceGraphError`. A dated cell with no fare still counts toward what the
    graph covers; only a priced one is returned.

    On a round trip every cell's return date has to sit `trip_length` nights
    after its departure. The page sets the trip length from its own dates, and a
    graph of some other length would otherwise be printed as this one."""
    row = _result_row(body)
    # `[tag, rpc id, payload, …]`; an error row leaves the payload empty.
    payload = next(iter(row[2:3]), None)
    if not payload:
        code = _error_code(row)
        raise GfPriceGraphError(
            f"Google Flights answered the price graph with error {code}, a refusal of this "
            "browser session rather than a rate limit"
            if code is not None
            else "Google Flights answered the price graph with an empty result",
            code=code,
        )
    dated: list[date] = []
    priced: list[GraphCell] = []
    try:
        for cell in json.loads(payload)[1]:
            departure = date.fromisoformat(cell[0])
            returning = date.fromisoformat(cell[1]) if trip_length is not None else None
            if returning is not None and (returning - departure).days != trip_length:
                raise GfPriceGraphError(
                    f"Google Flights' price graph priced a {(returning - departure).days}-night "
                    f"trip on {departure}, not the {trip_length} nights asked for"
                )
            dated.append(departure)
            if (price := _price(cell)) is not None:
                priced.append(GraphCell(departure, returning, price))
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise GfPriceGraphError(
            "Google Flights' price graph carried a cell this parser cannot read; its shape changed"
        ) from e
    if not dated:
        raise GfPriceGraphError("Google Flights' price graph carried no dates")
    return _GraphPage(max(dated), priced)


def _error_code(row: list[Any]) -> int | None:
    """An error row's code: `row[5] = [code, None, [details]]`."""
    try:
        code = row[5][0]
    except (IndexError, TypeError):
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def price_graph(search: CalendarSearch, *, headed: bool, pages: int = _MAX_PAGES) -> PriceGraph:
    """The cheapest fare per departure date across the window.

    One page load when the graph's span covers the window, and more when it
    does not: each load after the first opens on the first date the last one
    did not cover. The span is read from each response rather than assumed,
    because it differs by page. Paging stops on a graph that covers nothing
    from its opening date on, and after `pages` loads.

    A page that passed the wall check and then drew no graph in time is loaded
    once more, from the same budget: Google serves such a page now and then, and
    loading it again is not asking a wall again. A wall, a failed navigation and
    any answer the graph gave are raised as they are.

    Running out of loads blames the window only when the window alone spent all
    `_MAX_PAGES`; a reload, or a smaller budget left by other trip lengths, is
    `GfGraphBudgetError`.

    The caller arms `interrupt_guard` and holds `session_scope` around this."""
    window = search.window
    trip_length = window.duration_min if len(search.legs) > 1 else None
    session = _gf_browser.session(headed=headed)
    found: dict[date, GraphCell] = {}
    cursor = window.start
    loads = 0
    missed = reloaded = False
    while True:
        if loads >= pages:
            if reloaded or pages != _MAX_PAGES:
                raise GfGraphBudgetError(loads=loads, reloaded=reloaded)
            raise GfPriceGraphError(
                f"the window needs more than {pages} price-graph pages; narrow --start/--end"
            )
        loads += 1
        check = _WallCheck()
        try:
            captured = session.capture(
                page_url(search, cursor), _is_graph_rpc, click=_PRICE_GRAPH, check_page=check
            )
        except GfBrowserUnavailableError as e:
            if not check.passed:
                raise
            if missed or loads >= pages:
                raise GfGraphStalledError(e, loads=loads) from e
            missed = reloaded = True
            continue
        missed = False
        if not HTTPStatus.OK <= captured.status < HTTPStatus.MULTIPLE_CHOICES:
            raise GfPriceGraphError(
                f"Google Flights' price graph request returned HTTP {captured.status}"
            )
        page = parse_graph(captured.body, trip_length=trip_length)
        if page.last < cursor:
            raise GfPriceGraphError(
                f"Google Flights' price graph ends at {page.last}, before {cursor}, so it "
                "prices none of the window from there; narrow --end"
            )
        for cell in page.cells:
            if window.start <= cell.departure <= window.end:
                found.setdefault(cell.departure, cell)
        if page.last >= window.end:
            break
        cursor = page.last + timedelta(days=1)
    if not found:
        raise GfPriceGraphError("Google Flights' price graph priced no date in the window")
    return PriceGraph(trip_length, tuple(found[d] for d in sorted(found)), loads)


def graph_lengths(search: CalendarSearch) -> tuple[int | None, ...]:
    """The trip lengths one graph each answers: every length in a round trip's
    range, and a one-way's single graph, which has no trip length."""
    if len(search.legs) == 1:
        return (None,)
    window = search.window
    return tuple(range(window.duration_min, window.duration_max + 1))


def page_budget_blocker(search: CalendarSearch) -> str | None:
    """Why this calendar's graphs would not fit in `_MAX_PAGES` loads, or None.

    Counted before any load, at `_PAGE_DAYS` a load for every trip length. The
    phrase completes "this is …"."""
    window = search.window
    days = (window.end - window.start).days + 1
    graphs = len(graph_lengths(search))
    loads = graphs * -(-days // _PAGE_DAYS)
    if loads > _MAX_PAGES:
        what = "a window" if graphs == 1 else "a window and trip-length range"
        return f"{what} needing {loads:d} price-graph loads (at most {_MAX_PAGES:d})"
    return None


def price_graphs(search: CalendarSearch, *, headed: bool) -> GraphRange:
    """One graph per trip length, in order, within `_MAX_PAGES` loads in all.

    Each length is asked with the loads still left, since a page's span can fall
    short of the estimate that admitted the window. A length that fails, or that
    no load is left for, is lost with its cause, and the lengths that priced
    stand without it: kept as an empty column, it would print as dates nobody
    priced.

    Only a page that drew no graph, or a length that ran out of loads, lets the
    next length be asked; the loads either spent are counted, so after a length
    that ran out the rest are lost for want of a load. A wall, an error row or a
    Chrome that cannot load the page would meet the next length's page too, and
    a wall would take another load to say so; no other failure is told apart
    from those, so after any of them the lengths still to come are lost with it.

    When no length priced, the first failure is raised as it came, and the
    graph's own error is named with its trip length.

    The caller arms `interrupt_guard` and holds `session_scope` around this."""
    lengths = graph_lengths(search)
    graphs: list[PriceGraph] = []
    lost: list[LostLength] = []
    used = 0
    for i, nights in enumerate(lengths):
        left = _MAX_PAGES - used
        if left <= 0:
            lost.append(
                LostLength(
                    nights, GfPriceGraphError(f"no price-graph load of the {_MAX_PAGES:d} was left")
                )
            )
            continue
        one = search
        if nights is not None:
            window = search.window.model_copy(
                update={"duration_min": nights, "duration_max": nights}
            )
            one = search.model_copy(update={"window": window})
        try:
            graph = price_graph(one, headed=headed, pages=left)
        except (GfGraphStalledError, GfGraphBudgetError) as e:
            used += e.loads
            lost.append(LostLength(nights, e))
            continue
        except GfBackendError as e:
            lost.append(LostLength(nights, e))
            after = GfPriceGraphError(f"not asked after {nights}-night trips failed")
            lost.extend(LostLength(n, after) for n in lengths[i + 1 :])
            break
        used += graph.loads
        graphs.append(graph)
    if not graphs:
        nights, cause = lost[0]
        if isinstance(cause, GfPriceGraphError) and len(lengths) > 1:
            raise GfPriceGraphError(f"{nights}-night trips: {cause}", code=cause.code) from cause
        raise cause
    return GraphRange(graphs, lost)


def document(
    graph: PriceGraph, *, origin: str, destination: str, currency: str = "USD"
) -> dict[str, Any]:
    """The `--format json` document: one row per priced date, by departure."""
    rows: list[dict[str, Any]] = []
    for cell in graph.cells:
        row: dict[str, Any] = {"departure": cell.departure.isoformat()}
        if cell.return_date is not None:
            row["return"] = cell.return_date.isoformat()
        row["price"] = int(cell.price) if cell.price.is_integer() else cell.price
        rows.append(row)
    return {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "trip_length": graph.trip_length,
        "grid": rows,
    }
