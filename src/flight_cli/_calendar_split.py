"""Query multi-airport calendars one airport pair at a time, then merge.

Matrix's calendar engine silently UNDER-REPORTS a multi-airport grid once the
query exceeds its per-query compute budget — and the failure is not all-or-
nothing. Cost grows roughly as (origins x destinations) x (departure days) x
(routing complexity); above the (load-dependent) ceiling Matrix returns *fewer*
solutions, degrading toward zero, with no error or warning. Measured for
MIA<->[...] +LH+ over a 30-day window: VIE=155 / PAR=27 / FCO=24 / MAD=17 alone,
but 2-dest=155, 3-dest=12, 4-dest=0 — and even the non-empty 4-dest=155 is short
of the true union (~223). So a combined multi-airport result can't be trusted
even when it isn't empty; the only query guaranteed to fully price is a single
(origin, destination). A metro code is such a combined query too: NYC-LON asked
as one query priced 11 of 14 days where its member pairs priced all 14.

We therefore always run a multi-airport calendar as one sub-search per airport
pair, metro codes split into their member airports, and merge.
`split_calendar_search` produces those sub-searches (mirrored onto the return
leg); `merge_calendar_results` re-assembles a single grid that is the
per-departure-day lowest fare across pairs — what the combined grid is
*supposed* to be — with each cell naming the pair whose query priced it, routed
back through `CalendarResult.from_api` so it renders through the normal path.

A mirrored pair cannot price a round trip that comes back into another airport
of the set (out of JFK, back into EWR), so a round trip also runs the user's own
combined query beside the pairs, as a per-day floor. Every query of a fan-out
over more than one origin asks one currency (`with_fanout_currency`): Matrix
otherwise prices each origin in its own (GBP from LHR, EUR from DUB), and the
merge compares fares by number. `is_empty_calendar` distinguishes a genuine
no-flights result (every sub-search empty) from a recovered one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._metro import expand_airports
from .models import CalendarResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .domain import CalendarSearch

# The (origin, destination) a sub-query asked, each side comma-joined.
type Pair = tuple[str, str]


def is_empty_calendar(res: CalendarResult) -> bool:
    """Matrix's compute-budget shed presents as a structurally-valid result with
    zero solutions / no priced days — not as an error."""
    return res.solution_count == 0 or not res.priced_days


def calendar_pair(search: CalendarSearch) -> Pair:
    """The outbound's origins and destinations, each comma-joined: the two
    arguments `flight detail` takes to ask the query that priced a cell again."""
    leg = search.legs[0]
    return ",".join(leg.origins), ",".join(leg.destinations)


def with_fanout_currency(search: CalendarSearch) -> CalendarSearch:
    """`search`, asking USD when more than one origin and no currency was asked.

    Matrix prices in the origin's currency when none is asked, so two origins
    come back in two currencies, and a merge comparing them by number picks the
    wrong fare. One origin leaves every query in the same default, and keeps it."""
    if search.options.currency is not None or len(expand_airports(search.legs[0].origins)) <= 1:
        return search
    options = search.options.model_copy(update={"currency": "USD"})
    return search.model_copy(update={"options": options})


def split_calendar_search(search: CalendarSearch, max_per_query: int = 1) -> list[CalendarSearch]:
    """One sub-search per (outbound origin, destination-group), metro codes
    expanded to their member airports first, grouping up to `max_per_query`
    destinations per query. The default of 1 (one airport pair per query) is
    the only size guaranteed complete — larger groups trade completeness for
    fewer requests, since Matrix may under-report a multi-destination grid.

    An airport is never both ends of a leg: an origin is dropped from its own
    destination group, and a group left empty is not asked. Routing/extension/
    time-of-day, the options and the window are preserved; the return leg is
    mirrored. Returns [] when a single query already covers the request (one
    pair left, or `max_per_query` ≥ the destination count with one origin)."""
    out = search.legs[0]
    ret = search.legs[1] if len(search.legs) > 1 else None
    dests = expand_airports(out.destinations)
    k = max(1, max_per_query)
    groups = [dests[i : i + k] for i in range(0, len(dests), k)]
    subs: list[CalendarSearch] = []
    for o in expand_airports(out.origins):
        for group in groups:
            g = tuple(d for d in group if d != o)
            if not g:
                continue
            out_leg = out.model_copy(update={"origins": (o,), "destinations": g})
            if ret is not None:
                ret_leg = ret.model_copy(update={"origins": g, "destinations": (o,)})
                legs = (out_leg, ret_leg)
            else:
                legs = (out_leg,)
            subs.append(search.model_copy(update={"legs": legs}))
    return subs if len(subs) > 1 else []


def _amount_start(s: str) -> int:
    return next((j for j, c in enumerate(s) if c.isdigit() or c == "."), len(s))


def _price_value(s: str | None) -> float | None:
    """Numeric value of a Matrix price string ('USD595.00' -> 595.0)."""
    if not s:
        return None
    try:
        return float(s[_amount_start(s) :])
    except ValueError:
        return None


def price_currencies(res: CalendarResult) -> tuple[str, ...]:
    """The currency of every price `res` carries — its cheapest notice, each
    priced day and each of that day's trip lengths — repeats dropped, in that
    order. A price with nothing before its number counts as '', so it never
    matches a currency code. Empty when `res` priced nothing: no currency at all."""
    prices = [res.cheapest_price]
    for d in res.priced_days:
        prices.append(d.min_price)
        prices.extend(o.min_price for o in d.options)
    return tuple(dict.fromkeys(p[: _amount_start(p)].strip() for p in prices if p))


@dataclass
class _Cell:
    """Per (month, day) aggregate while merging pair grids."""

    date: int
    min_price: str
    pv: float | None
    pair: Pair
    sols: int = 0
    floor_sols: int = 0
    durs: dict[int, tuple[str, float, Pair]] = field(
        default_factory=dict[int, tuple[str, float, Pair]]
    )


def _fold(
    cells: dict[tuple[int, int], _Cell], pair: Pair, res: CalendarResult, *, floor: bool
) -> None:
    """Fold one answer's priced days into `cells`, each kept only where strictly
    cheaper than what is there, so a tie stays with whichever came first."""
    for m in res.months:
        for d in m.days:
            if d.disabled or not d.min_price:
                continue
            key = (m.month or 0, d.date)
            pv = d.price_value
            cell = cells.get(key)
            if cell is None:
                cell = _Cell(date=d.date, min_price=d.min_price, pv=pv, pair=pair)
                cells[key] = cell
            elif pv is not None and (cell.pv is None or pv < cell.pv):
                cell.pv = pv
                cell.min_price = d.min_price
                cell.pair = pair
            if floor:
                cell.floor_sols = d.solution_count
            else:
                cell.sols += d.solution_count
            for o in d.options:
                ov = _price_value(o.min_price)
                if ov is None:
                    continue
                existing = cell.durs.get(o.trip_length)
                if existing is None or ov < existing[1]:
                    cell.durs[o.trip_length] = (o.min_price, ov, pair)


def merge_calendar_results(
    results: Sequence[tuple[Pair, CalendarResult]],
    floor: tuple[Pair, CalendarResult] | None = None,
) -> CalendarResult:
    """Merge per-pair calendar grids into one: per (month, day) the lowest fare
    across pairs, with each per-duration column also taken as the cross-pair
    minimum and `solution_count` summed. Every day and every duration names the
    pair whose query priced it; on a tie the earlier pair keeps it. The prices
    are compared by number, so every result must be in one currency. The result
    is built via `CalendarResult.from_api` so it renders identically to a
    native grid.

    `floor` is the user's own combined query, run beside a round trip's pairs
    because it is the only one that prices a return into another airport of the
    set. It takes a cell only when strictly cheaper than every pair. Its
    solutions overlap the pairs', so its counts are used only where the pairs
    counted none — a day no pair priced, or a grid whose pairs all came back
    empty — which keeps a grid only the floor priced from reading as empty."""
    cells: dict[tuple[int, int], _Cell] = {}
    cheapest_pv: float | None = None
    cheapest_notice: dict[str, Any] = {}
    # The floor last, so the strict comparisons leave every tie to a pair.
    sources = [*results, *([floor] if floor is not None else [])]
    for i, (pair, res) in enumerate(sources):
        cpv = _price_value(res.cheapest_price)
        if cpv is not None and (cheapest_pv is None or cpv < cheapest_pv):
            cheapest_pv = cpv
            cheapest_notice = (res.raw or {}).get("currencyNotice") or {}
        _fold(cells, pair, res, floor=i == len(results))
    total_sols = sum(res.solution_count for _, res in results)
    if total_sols == 0 and floor is not None:
        total_sols = floor[1].solution_count
    by_month: dict[int, list[dict[str, Any]]] = {}
    for (month, _date), cell in cells.items():
        day: dict[str, Any] = {
            "date": cell.date,
            "solutionCount": cell.sols or cell.floor_sols,
            "minPrice": cell.min_price,
            "origin": cell.pair[0],
            "destination": cell.pair[1],
            "tripDuration": {
                "options": [
                    {"tripLength": dur, "minPrice": price, "origin": o, "destination": d}
                    for dur, (price, _, (o, d)) in sorted(cell.durs.items())
                ]
            },
        }
        by_month.setdefault(month, []).append(day)
    months = [
        {"month": month, "weeks": [{"days": sorted(days, key=lambda x: x["date"])}]}
        for month, days in sorted(by_month.items())
    ]
    return CalendarResult.from_api(
        {
            "solutionCount": total_sols,
            "currencyNotice": cheapest_notice,
            "calendar": {"months": months},
        }
    )
