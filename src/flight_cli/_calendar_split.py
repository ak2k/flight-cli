"""Recover Matrix calendar "brownouts" on multi-airport queries by splitting.

Matrix's calendar engine silently returns 0 solutions (HTTP 200, no error or
warning) once a query exceeds its per-query compute budget — cost grows roughly
as (origins x destinations) x (departure days) x (routing complexity). A
multi-destination + routing query over a wide window crosses it even though each
single-destination sub-query prices fine. Measured: MIA<->[VIE,PAR,FCO,MAD] +LH+
over a 30-day window = 0, but VIE=155 / PAR=27 / FCO=24 / MAD=17 alone, 3-dest=12,
2-dest=155; narrowing the window 30->7 days recovers (0->4). The shed is
deterministic, so retrying the same query never helps — splitting the airport set
into cheaper per-(origin, destination) sub-searches and merging their grids does.

`split_calendar_search` produces one sub-search per (outbound origin, outbound
destination) pair, mirrored onto the return leg. `merge_calendar_results`
re-assembles a single grid that is the per-departure-day lowest fare across
destinations — exactly what the combined grid represents — and routes it back
through `CalendarResult.from_api` so it renders through the normal path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import CalendarResult

if TYPE_CHECKING:
    from .domain import CalendarSearch


def is_empty_calendar(res: CalendarResult) -> bool:
    """Matrix's compute-budget shed presents as a structurally-valid result with
    zero solutions / no priced days — not as an error."""
    return res.solution_count == 0 or not res.priced_days


def split_calendar_search(search: CalendarSearch) -> list[CalendarSearch]:
    """One sub-search per (outbound origin, outbound destination) pair, mirrored
    onto the return leg. Routing/extension/time-of-day and the window are
    preserved. Returns [] when there is nothing to split (single origin and
    destination each way)."""
    out = search.legs[0]
    ret = search.legs[1] if len(search.legs) > 1 else None
    pairs = [(o, d) for o in out.origins for d in out.destinations]
    if len(pairs) <= 1:
        return []
    subs: list[CalendarSearch] = []
    for o, d in pairs:
        out_leg = out.model_copy(update={"origins": (o,), "destinations": (d,)})
        if ret is not None:
            ret_leg = ret.model_copy(update={"origins": (d,), "destinations": (o,)})
            legs = (out_leg, ret_leg)
        else:
            legs = (out_leg,)
        subs.append(search.model_copy(update={"legs": legs}))
    return subs


def _price_value(s: str | None) -> float | None:
    """Numeric value of a Matrix price string ('USD595.00' -> 595.0)."""
    if not s:
        return None
    i = next((j for j, c in enumerate(s) if c.isdigit() or c == "."), len(s))
    try:
        return float(s[i:])
    except ValueError:
        return None


@dataclass
class _Cell:
    """Per (month, day) aggregate while merging destination grids."""

    date: int
    min_price: str
    pv: float | None
    sols: int = 0
    durs: dict[int, tuple[str, float]] = field(default_factory=dict[int, tuple[str, float]])


def merge_calendar_results(results: list[CalendarResult]) -> CalendarResult:
    """Merge per-destination calendar grids into one: per (month, day) the lowest
    fare across destinations, with each per-duration column also taken as the
    cross-destination minimum and `solution_count` summed. The result is built
    via `CalendarResult.from_api` so it renders identically to a native grid."""
    cells: dict[tuple[int, int], _Cell] = {}
    total_sols = 0
    cheapest_pv: float | None = None
    cheapest_notice: dict[str, Any] = {}
    for res in results:
        total_sols += res.solution_count
        cpv = _price_value(res.cheapest_price)
        if cpv is not None and (cheapest_pv is None or cpv < cheapest_pv):
            cheapest_pv = cpv
            cheapest_notice = (res.raw or {}).get("currencyNotice") or {}
        for m in res.months:
            for d in m.days:
                if d.disabled or not d.min_price:
                    continue
                key = (m.month or 0, d.date)
                pv = d.price_value
                cell = cells.get(key)
                if cell is None:
                    cell = _Cell(date=d.date, min_price=d.min_price, pv=pv)
                    cells[key] = cell
                if pv is not None and (cell.pv is None or pv < cell.pv):
                    cell.pv = pv
                    cell.min_price = d.min_price
                cell.sols += d.solution_count
                for o in d.options:
                    ov = _price_value(o.min_price)
                    if ov is None:
                        continue
                    existing = cell.durs.get(o.trip_length)
                    if existing is None or ov < existing[1]:
                        cell.durs[o.trip_length] = (o.min_price, ov)
    by_month: dict[int, list[dict[str, Any]]] = {}
    for (month, _date), cell in cells.items():
        day: dict[str, Any] = {
            "date": cell.date,
            "solutionCount": cell.sols,
            "minPrice": cell.min_price,
            "tripDuration": {
                "options": [
                    {"tripLength": dur, "minPrice": price}
                    for dur, (price, _) in sorted(cell.durs.items())
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
