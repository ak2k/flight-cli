"""CLI: thin shells over the domain types. Each command parses args, builds
a Search variant, hands it to MatrixClient.execute() (or fli for gflight),
and renders.

Commands:
  flight search    — specific-date search (auto-picks Matrix vs Google Flights)
  flight calendar  — lowest-fare grid (Matrix only)
  flight detail    — phase-2 itineraries for a date picked from the grid
  flight airport   — IATA autocomplete
  flight fare      — [deprecated] alias for `search --backend matrix`
  flight gflight   — [deprecated] alias for `search --backend gflight`
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast

import anyio
import anyio.to_thread
import typer
from rich.table import Table

from ._calendar_split import is_empty_calendar, merge_calendar_results, split_calendar_search
from ._console import console, err
from ._dispatch import BACKEND_AUTO, BACKEND_GFLIGHT, BACKEND_MATRIX, ProviderSelection
from ._dispatch import pick_backend as _pick_backend
from ._dispatch import resolve_providers as _resolve_providers
from ._dispatch import should_run_awards as _should_run_awards
from ._multi_cabin import MultiCabinRow, parse_price
from ._multi_cabin import merge as _merge_cabins
from ._parsing import (
    ROUND_TRIP_LEGS as _ROUND_TRIP_LEGS,
)
from ._parsing import (
    amount as _amount,
)
from ._parsing import (
    build_options as _build_options,
)
from ._parsing import (
    parse_date as _parse_date,
)
from ._parsing import (
    parse_duration as _parse_duration,
)
from ._parsing import (
    parse_iata_list as _parse_iata_list,
)
from ._parsing import (
    parse_slice_spec as _parse_slice_spec,
)
from ._parsing import (
    parse_times as _parse_times,
)
from ._parsing import (
    resolve_cabin as _resolve_cabin,
)
from ._parsing import (
    resolve_cabin_list as _resolve_cabin_list,
)
from ._parsing import (
    split_price as _split_price,
)
from ._runtime_opts import FORMAT_OPT as _FORMAT_OPT
from ._runtime_opts import IMPERSONATE_OPT as _IMPERSONATE_OPT
from ._runtime_opts import JSON_OPT as _JSON_OPT
from ._runtime_opts import NO_CACHE_OPT as _NO_CACHE_OPT
from ._runtime_opts import PROVIDER_OPT as _PROVIDER_OPT
from ._runtime_opts import RPS_OPT as _RPS_OPT
from ._runtime_opts import resolve_format as _resolve_format
from ._runtime_opts import resolve_impersonate as _resolve_impersonate
from ._runtime_opts import resolve_no_cache as _resolve_no_cache
from ._runtime_opts import resolve_rps as _resolve_rps
from ._urls import GOOGLE_URL_HELP as _GOOGLE_URL_HELP
from ._urls import MATRIX_URL_HELP as _MATRIX_URL_HELP
from ._urls import emit_urls as _emit_urls
from ._urls import pinned_solution_index as _pinned_solution_index
from ._urls import try_pinned_gflight_url as _try_pinned_gflight_url
from ._urls import try_pinned_matrix_url as _try_pinned_matrix_url
from .client import MatrixApiError, MatrixClient
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Search,
    SearchOptions,
    SpecificDateSearch,
)
from .log import configure as configure_logging
from .pp.auth import load_tokens
from .pp.cli import auth_app, run_pp_for_search
from .providers.base import LegQuery

if TYPE_CHECKING:
    from .models import CalendarResult, LegInfo, Location, SearchResult, Slice

# Names re-exported for the test suite: `cli.py` was decomposed into `_console`,
# `_parsing`, `_dispatch`, `_runtime_opts`, `_urls`, `_pp_glue`, and `_render`,
# but the tests still `from flight_cli.cli import <name>` and monkeypatch
# `cli.<name>`. Every moved definition is imported back above so it stays a
# `flight_cli.cli` attribute; the ones with no remaining in-module caller are
# listed here so ruff keeps the re-export instead of pruning it as dead.
__all__ = [
    "BACKEND_MATRIX",
    "_pinned_solution_index",
    "_try_pinned_gflight_url",
    "_try_pinned_matrix_url",
]


app = typer.Typer(
    add_completion=False, rich_markup_mode="rich", help="CLI for ITA Matrix's Alkali backend."
)
app.add_typer(auth_app, name="auth")


@app.callback()
def main(
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="Increase log verbosity (-v=INFO, -vv=DEBUG). Logs go to stderr.",
        ),
    ] = 0,
) -> None:
    # _http.py emits structlog warning/debug for cache hits/misses, retry
    # attempts, and rate-limit pauses. Configure the renderer to taste:
    # `-v` shows info-level diagnostics, `-vv` includes debug.
    level = ("warning", "info", "debug")[min(verbose, 2)]
    configure_logging(level)


# ─────────────────────────── backend dispatch ──────────────────────────────


def _should_run_pp(*, no_pp: bool, pp_only: bool) -> bool:  # pyright: ignore[reportUnusedFunction]
    """Decide whether PP augmentation runs.

    Tokens present → True (unless --no-pp). Both backends support PP overlay
    now: matrix consumes its own SearchResult directly; gflight wraps fli
    output via gflight_adapter into the same shape, so the matcher and
    renderer reuse end-to-end.

    --pp-only with missing tokens → hard error (user explicitly asked for PP).
    """
    if no_pp:
        if pp_only:
            err.print("[red]--pp-only and --no-pp are mutually exclusive.[/]")
            raise typer.Exit(2)
        return False
    tokens = load_tokens()
    if tokens is None:
        if pp_only:
            err.print(
                "[red]--pp-only set but no PointsPath tokens.[/] "
                "Run `flight auth pp login --tokens-file ...` first.",
            )
            raise typer.Exit(2)
        return False
    return True


# ─────────────────────────── shared execution ──────────────────────────────


def _run(
    search: Search,
    rps: float,
    impersonate: str,
    no_cache: bool,
) -> SearchResult | CalendarResult:
    async def go() -> SearchResult | CalendarResult:
        async with MatrixClient(rps=rps, impersonate=impersonate) as c:
            return await c.execute(search, cache=not no_cache)

    try:
        return anyio.run(go)
    except MatrixApiError as e:
        err.print(f"[red]Matrix returned an error ({e.kind}):[/] {e.message}")
        if e.request_id:
            err.print(f"[dim]request_id: {e.request_id}[/]")
        raise typer.Exit(1) from e


# Matrix silently UNDER-REPORTS multi-airport calendar grids under compute-budget
# pressure — even when the result is non-empty (a 3-destination query returned 12
# solutions where one destination alone returns 155). The only query guaranteed
# to fully price is a single (origin, destination), so a multi-airport calendar is
# always run as one sub-search per (origin, destination) pair, in parallel, and
# merged — the only way to get complete results.
#
# `split_calendar_search` returns the cartesian product of origins x destinations,
# so the fan-out is |origins| x |destinations| (just |destinations| in the common
# single-origin case). Matrix tolerates the concurrency (measured: ≥16 in flight,
# flat latency, no throttling); we hold a touch under that and let larger lists
# batch into multiple rounds. There is no hard cap — a large fan-out is the user's
# call; we warn loudly (and the concurrency limit keeps it a Ctrl-C-able drip).
_CALENDAR_FANOUT_CONCURRENCY = 12


async def _gather_calendar(
    c: MatrixClient, subs: list[CalendarSearch], *, cache: bool
) -> list[CalendarResult]:
    """Run the per-destination sub-searches concurrently on one client (its
    rate-limiter + semaphore bound the in-flight count). A sub-query that fails
    just drops its destination from the merge rather than sinking the whole run."""
    results: list[CalendarResult | None] = [None] * len(subs)

    async def one(i: int, s: CalendarSearch) -> None:
        try:
            results[i] = cast("CalendarResult", await c.execute(s, cache=cache))
        except Exception:  # noqa: BLE001 — a sub-query failure just drops that destination
            results[i] = None

    async with anyio.create_task_group() as tg:
        for i, s in enumerate(subs):
            tg.start_soon(one, i, s)
    return [r for r in results if r is not None]


def _run_calendar(
    search: CalendarSearch,
    *,
    rps: float,
    impersonate: str,
    no_cache: bool,
    max_per_query: int = 1,
    max_concurrency: int = _CALENDAR_FANOUT_CONCURRENCY,
) -> tuple[CalendarResult, int]:
    """Execute a calendar search. A multi-airport query is fanned out into
    sub-searches of up to `max_per_query` destinations each, run in parallel
    (≤ `max_concurrency` at a time), and merged — Matrix under-reports a combined
    multi-airport grid, so the default of one destination per query is the only
    size guaranteed complete.

    Returns `(result, n_queries)` where `n_queries > 1` means the fan-out path was
    used (for a one-line note). A single-airport calendar runs as one query and
    returns `n_queries == 0`.
    """
    subs = split_calendar_search(search, max_per_query)
    n = len(subs)
    multi = bool(subs)  # split returns [] when one query already covers the request
    conc = min(n, max(1, max_concurrency)) if multi else 3
    if multi and max_per_query > 1:
        err.print(
            "[yellow]--max-per-query > 1: Matrix may under-report a "
            "multi-destination request, so results could be incomplete.[/]"
        )
    if multi and n > conc:
        # No hard cap — a big fan-out is the user's call. The concurrency limit
        # keeps it a Ctrl-C-able drip rather than a burst; warn loudly so the
        # scale (and the wait) is visible before it runs.
        rounds = (n + conc - 1) // conc
        err.print(
            f"[yellow]Querying {n} origin/destination groups in ~{rounds} rounds "
            f"({conc} at a time); this may take a while — Ctrl-C to abort, or pass "
            f"--max-per-query to send fewer, larger requests.[/]"
        )

    async def go() -> tuple[CalendarResult, int]:
        async with MatrixClient(
            rps=max(rps, float(conc)), impersonate=impersonate, concurrency=conc
        ) as c:
            if not multi:
                return cast("CalendarResult", await c.execute(search, cache=not no_cache)), 0
            recovered = await _gather_calendar(c, subs, cache=not no_cache)
            merged = merge_calendar_results(recovered)
            return (merged, n) if not is_empty_calendar(merged) else (merged, 0)

    try:
        return anyio.run(go)
    except MatrixApiError as e:
        err.print(f"[red]Matrix returned an error ({e.kind}):[/] {e.message}")
        if e.request_id:
            err.print(f"[dim]request_id: {e.request_id}[/]")
        raise typer.Exit(1) from e


def _report_calendar_matrix_failure(state: dict[str, Any]) -> None:
    """Print the right stderr message for a Matrix calendar that returned no result:
    a known `MatrixApiError`, an unexpected non-MatrixApiError stashed by the weave's
    `_matrix` task, or a cancel/never-completed fall-through."""
    e = state.get("matrix_err")
    if e is not None:
        err.print(f"[red]Matrix returned an error ({e.kind}):[/] {e.message}")
        if e.request_id:
            err.print(f"[dim]request_id: {e.request_id}[/]")
    elif state.get("matrix_unexpected") is not None:
        err.print(f"[red]Matrix calendar failed:[/] {state['matrix_unexpected']}")
    else:
        err.print("[yellow]Matrix calendar did not complete.[/]")


def _run_calendar_enriched(
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    dmin: int,
    dmax: int,
    rps: float,
    impersonate: str,
    no_cache: bool,
    matrix_url: bool,
    google_url: bool,
) -> None:
    """GF-serveable calendar (one-way, single-airport), progressive: dispatch the
    Google Flights date-grid and the Matrix calendar CONCURRENTLY under one event
    loop, paint the GF grid immediately (~1s) while Matrix is in flight, then paint
    the authoritative Matrix calendar (~45s) — total ≈ max(GF, Matrix), not the sum.
    Mirrors `_run_enriched_path` (the search-path weave). `--fast` never reaches here
    (the command serves the grid alone for that). The `grid_can_serve` gate guarantees
    a single-airport query, so the Matrix side is one `execute` (no fan-out)."""
    from ._gf_dategrid import date_grid  # noqa: PLC0415
    from ._gflight_ids import GfThrottledError  # noqa: PLC0415

    # Single-airport calendar runs as one Matrix query; mirror `_run_calendar`'s
    # non-multi concurrency/rps so the request paces identically.
    conc = 3
    state: dict[str, Any] = {}

    async def _matrix(c: MatrixClient) -> None:
        try:
            state["matrix"] = await c.execute(search, cache=not no_cache)
        except MatrixApiError as e:
            state["matrix_err"] = e
        except Exception as e:  # noqa: BLE001
            # An unexpected Matrix failure (e.g. a raw httpx transport/status error that
            # execute() doesn't wrap) must NOT propagate out of this task and tear down
            # the group — that would cancel the still-pending grid paint and surface a
            # bare traceback. Stash it and report after the weave so the GF grid still
            # shows (per-backend isolation, mirroring the MatrixApiError path).
            state["matrix_unexpected"] = e

    async def _go() -> None:
        async with (
            MatrixClient(rps=max(rps, float(conc)), impersonate=impersonate, concurrency=conc) as c,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(_matrix, c)
            # The GF date-grid is sync (curl_cffi) — run it in a worker thread so the
            # Matrix calendar request progresses concurrently on the event loop.
            grid: dict[str, float] = {}
            try:
                grid = await anyio.to_thread.run_sync(date_grid, search)
            except GfThrottledError:
                state["gf_throttled"] = True
            except Exception as e:  # noqa: BLE001 — GF is the optional fast layer; Matrix still runs
                state["gf_err"] = e
            state["grid"] = grid
            # First paint, while Matrix is still in flight.
            if grid:
                _render_date_grid(grid, origin=origins, destination=dests, sd=sd, ed=ed)
                console.print("[dim]…refining with Matrix (full grid + durations)…[/]")
            elif state.get("gf_throttled"):
                console.print("[dim]Google Flights rate-limited — awaiting Matrix calendar…[/]")
            elif "gf_err" in state:
                err.print(f"[yellow]Google Flights date-grid failed:[/] {state['gf_err']}")
                console.print("[dim]…awaiting Matrix calendar…[/]")
            else:
                console.print("[dim]…awaiting Matrix calendar…[/]")

    anyio.run(_go)

    matrix_res = state.get("matrix")
    if matrix_res is None:
        # Matrix failed; the GF grid (if any) was already painted.
        _report_calendar_matrix_failure(state)
        if not state.get("grid"):
            raise typer.Exit(1)
        return
    res = cast("CalendarResult", matrix_res)
    _render_calendar(res, dmin=dmin, dmax=dmax, origin=origins, destination=dests, sd=sd, ed=ed)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


# ─────────────────────────── result renderers ──────────────────────────────


def _parse_iso(s: str) -> datetime | None:
    """Best-effort parse of a slice timestamp ("YYYY-MM-DDTHH:MM[:SS]")."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:16])
        except ValueError:
            return None


def _fmt_slice_times(dep: str, arr: str) -> str:
    """Compact, unambiguous departure→arrival for an itinerary cell.

    Shows the departure date once, the two clock times, and a `+Nd` marker
    when the arrival lands on a later calendar day. Without the marker an
    overnight return reads as "arrives before it departs" once the cell is
    squeezed (work-72syf). Falls back to raw ISO — which still carries both
    dates — when a timestamp can't be parsed.
    """
    d = _parse_iso(dep)
    a = _parse_iso(arr)
    if d is None or a is None:
        return f"{dep[:16]}→{arr[:16]}"
    day_off = (a.date() - d.date()).days
    suffix = f" +{day_off}d" if day_off > 0 else (f" {day_off}d" if day_off < 0 else "")
    return f"{d:%b%d %H:%M}→{a:%H:%M}{suffix}"


def _fmt_slice_route(s: Slice) -> str:
    """Origin→destination threading any intermediate connection airports, so a
    1-stop itinerary shows its connection city instead of hiding it."""
    o = (s.origin.code if s.origin else None) or "?"
    d = (s.destination.code if s.destination else None) or "?"
    vias = [e.code for e in s.stops if e and e.code]
    return "→".join([o, *vias, d])


def _fmt_slice_cell(s: Slice) -> str:
    """One itinerary slice as a table cell: route (with connection cities),
    flight numbers, compact unambiguous times, duration, then per-leg legroom
    lines. Shared by the single-cabin and multi-cabin itinerary tables."""
    dur_min = s.duration or 0
    dur = f"{dur_min // 60}h{dur_min % 60:02d}m" if dur_min else ""
    flights = "/".join(s.flights) or "?"
    times = _fmt_slice_times(s.departure or "", s.arrival or "")
    head = " ".join(p for p in (_fmt_slice_route(s), flights, times, dur) if p)
    tail = _fmt_legroom_lines(s)
    return f"{head}\n{tail}" if tail else head


def _render_search(res: SearchResult) -> None:
    if res.solution_count == 0:
        console.print("[yellow]No solutions returned.[/]")
        return
    ccy, cheapest = _split_price(res.cheapest_price)
    ccy_tag = f" ({ccy})" if ccy else ""
    console.print(
        f"[bold]{res.solution_count} solutions[/]  · "
        f"cheapest: [bold cyan]{cheapest or '—'}{ccy_tag}[/]"
    )

    cm = res.carrier_stop_matrix
    if cm and cm.columns and cm.rows:
        t = Table(
            title=f"Carrier x stops grid{ccy_tag}",
            show_header=True,
            header_style="bold magenta",
        )
        t.add_column("stops")
        for col in cm.columns:
            code = col.label.code if col.label else "?"
            sn = (col.label.short_name or "") if col.label else ""
            t.add_column(f"{code or '?'}\n{sn[:14]}")
        for row in cm.rows:
            cells = [str(row.label) if row.label is not None else "?"]
            for c in row.cells:
                p = _amount(c.min_price)
                mark = "★" if c.min_price_in_grid else ("·" if c.min_price_in_row else "")
                cells.append(f"{p} {mark}")
            t.add_row(*cells)
        console.print(t)

    st = Table(title=f"Itineraries{ccy_tag}", show_header=True, header_style="bold green")
    st.add_column("#", justify="right")
    st.add_column("price", justify="right")
    st.add_column("carriers")
    st.add_column("outbound")
    st.add_column("return")
    for i, it in enumerate(res.solutions[:10], 1):
        itn = it.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        it_carriers = ",".join((c.code or "?") for c in (itn.carriers if itn else []))

        out = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        st.add_row(str(i), _amount(it.price), it_carriers or "?", out, ret)
    console.print(st)


# ───────────────── legroom formatters (gflight-populated; Matrix slices noop) ──


def _fmt_legroom_one(flight_no: str, leg: LegInfo) -> str:
    """Per-leg summary. Returns '' when no legroom fields are populated
    (Matrix path — Matrix's response doesn't carry legroom). Uses the
    same color-not-text policy as `_fmt_gflight_legroom`."""
    parts: list[str] = []
    cabin_short = _CABIN_LETTER.get(leg.cabin or "", "")
    if cabin_short:
        parts.append(cabin_short)
    if leg.pitch_inches is not None:
        token = f'{leg.pitch_inches}"'
        color = _LEGROOM_AS_COLOR.get(leg.legroom_class or "")
        if color:
            token = f"[{color}]{token}[/]"
        parts.append(token)
    if leg.legroom_class and leg.legroom_class not in {"AVERAGE", "BELOW", "ABOVE"}:
        parts.append(leg.legroom_class)
    amenities: list[str] = []
    w = _WIFI_GLYPH.get(leg.wifi or "")
    if w:
        amenities.append(w)
    p = _POWER_GLYPH.get(leg.power or "")
    if p:
        amenities.append(p)
    v = _VIDEO_GLYPH.get(leg.video or "")
    if v:
        amenities.append(v)
    if amenities:
        parts.append("".join(amenities))
    if not parts:
        return ""
    return f"  {flight_no:<6} " + " ".join(parts)


def _fmt_legroom_lines(s: Slice) -> str:
    """Per-leg lines under a slice cell, one row per physical flight in the slice.
    Empty when no legroom data populated (Matrix path)."""
    if not s.legs:
        return ""
    rows = [_fmt_legroom_one(s.flights[i], leg) for i, leg in enumerate(s.legs)]
    return "\n".join(r for r in rows if r)


def _render_date_grid(
    grid: dict[str, float],
    *,
    origin: tuple[str, ...],
    destination: tuple[str, ...],
    sd: date,
    ed: date,
) -> None:
    """Render the GF native date-grid: cheapest fare per departure day (USD),
    sorted cheapest-first. One-way only (the grid's shape)."""
    if not grid:
        return
    console.print(
        f"[bold]{len(grid)} priced days[/]  · cheapest: "
        f"[bold cyan]{min(grid.values()):.0f} (USD)[/]  · "
        f"window {sd.isoformat()} → {ed.isoformat()}"
    )
    t = Table(
        title=f"{','.join(origin)} → {','.join(destination)}: "
        "lowest fare per departure day (Google Flights)",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("departure", justify="right")
    t.add_column("min (USD)", justify="right")
    for day, price in sorted(grid.items(), key=lambda kv: kv[1]):
        t.add_row(day, f"{price:.0f}")
    console.print(t)


def _render_calendar(
    res: CalendarResult,
    *,
    dmin: int,
    dmax: int,
    origin: tuple[str, ...],
    destination: tuple[str, ...],
    sd: date,
    ed: date,
) -> None:
    if res.solution_count == 0 or not res.priced_days:
        console.print(
            "[yellow]Calendar empty.[/] Matrix's calendar mode "
            "brownouts regularly; retry, or use [bold]flight fare[/] "
            "for a single date."
        )
        return
    ccy, cheapest = _split_price(res.cheapest_price)
    ccy_tag = f" ({ccy})" if ccy else ""
    console.print(
        f"[bold]{res.solution_count} solutions[/]  · "
        f"overall cheapest: [bold cyan]{cheapest or '—'}{ccy_tag}[/]  · "
        f"window {sd.isoformat()} → {ed.isoformat()}  · "
        f"duration {dmin}-{dmax} nights"
    )
    title = f"{','.join(origin)} → {','.join(destination)}: lowest fare per departure day{ccy_tag}"
    t = Table(title=title, show_header=True, header_style="bold green")
    t.add_column("departure", justify="right")
    t.add_column("min", justify="right")
    for dur in range(dmin, dmax + 1):
        t.add_column(f"{dur}n", justify="right")
    t.add_column("sols", justify="right")
    for d in sorted(res.priced_days, key=lambda x: x.price_value or 9e9):
        row = [str(d.date), _amount(d.min_price)]
        opts = {o.trip_length: o.min_price for o in d.options}
        for dur in range(dmin, dmax + 1):
            row.append(_amount(opts.get(dur)))
        row.append(str(d.solution_count))
        t.add_row(*row)
    console.print(t)


# ─────────────────────────── backend execution ─────────────────────────────


def _build_pp_legs(legs: tuple[Leg, ...]) -> list[LegQuery]:
    """One PP query per Matrix leg. slice_index lets the matcher join PP
    award results to the correct Itinerary slice in each Matrix solution."""
    out: list[LegQuery] = []
    for i, leg in enumerate(legs):
        if not leg.date or not leg.origins or not leg.destinations:
            continue
        n = len(legs)
        kind = (
            "outbound"
            if i == 0 and n > 1
            else "return"
            if i == 1 and n == _ROUND_TRIP_LEGS
            else f"leg {i + 1}"
            if n > _ROUND_TRIP_LEGS
            else "one-way"
        )
        iso = leg.date.isoformat()
        out.append(
            LegQuery(
                origin=leg.origins[0],
                destination=leg.destinations[0],
                date=iso,
                slice_index=i,
                label=f"{kind} {leg.origins[0]}→{leg.destinations[0]} {iso}",
            ),
        )
    return out


def _run_matrix_path(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    rps: float,
    impersonate: str,
    no_cache: bool,
    json_out: bool,
    matrix_url: bool,
    google_url: bool,
    run_pp: bool,
    sel: ProviderSelection,
    pick: int | None = None,
) -> None:
    """Matrix path: Alkali call → optional cash render → optional PP augmentation → URLs."""
    search = SpecificDateSearch(legs=legs, options=opts)
    # SpecificDateSearch → SearchResult by client._parse_response dispatch.
    res = cast(
        "SearchResult",
        _run(
            search,
            _resolve_rps(rps),
            _resolve_impersonate(impersonate),
            _resolve_no_cache(no_cache),
        ),
    )
    if json_out and not run_pp:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    if not sel.awards_only:
        _render_search(res)
    if run_pp:
        p = opts.pax
        run_pp_for_search(
            res,
            legs=_build_pp_legs(legs),
            num_passengers=p.adults + p.children + p.seniors + p.youth,
            airlines=sel.pp_airlines(),
            cabins=sel.pp_cabins(),
            pp_only=sel.awards_only,
            json_out=json_out,
            provider_filter=sel.provider_filter,
            seats_sources=sel.seats_sources(),
            cash_per_cabin=_cash_per_cabin_single(res, opts.cabin),
        )
    # `res` was cast to SearchResult at the top of this function; safe to pass through.
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url, result=res, pick=pick)


def _gflight_results(legs: tuple[Leg, ...], opts: SearchOptions, top_n: int) -> list[Any]:
    """Query Google Flights for `legs`, honoring routing/extension: Tier-1
    predicates narrow the fli query natively, the Tier-2 post-filter drops
    violating solutions. Returns the (filtered) raw fli result list.

    `search` applies the same routing/extension to every leg, so the first leg's
    constraints cover the trip for the native query; the post-filter is per slice.
    """
    from ._gf_postfilter import surviving_indices  # noqa: PLC0415
    from ._gflight_ids import search_with_ids  # noqa: PLC0415
    from .fli_bridge import apply_gf_native_filters, to_fli_filter  # noqa: PLC0415
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415
    from .routing_predicates import classify  # noqa: PLC0415

    fli_filter = to_fli_filter(SpecificDateSearch(legs=legs, options=opts))
    out_constraints = classify(legs[0].route_language, legs[0].extension) if legs else None
    if out_constraints and out_constraints.predicates:
        apply_gf_native_filters(fli_filter, out_constraints.predicates)
    results: list[Any] = search_with_ids(fli_filter, top_n=top_n) or []
    per_slice_preds = [list(classify(lg.route_language, lg.extension).predicates) for lg in legs]
    if results and any(per_slice_preds):
        keep = set(surviving_indices(fli_results_to_search_result(results), per_slice_preds))
        results = [r for i, r in enumerate(results) if i in keep]
    return results


_MERGE_SOURCE_TAG = {"both": "GF+MX", "matrix": "MX", "gf": "GF"}


def _render_merged(rows: list[Any], *, legs: tuple[Leg, ...], top_n: int) -> None:
    """Render the reconciled GF+Matrix view: one row per itinerary with the GF
    and Matrix prices attributed side-by-side and a source tag."""
    origin = legs[0].origins[0] if legs[0].origins else "?"
    destination = legs[0].destinations[0] if legs[0].destinations else "?"
    has_return = len(legs) >= _ROUND_TRIP_LEGS
    t = Table(
        title=f"Google Flights + Matrix · {origin}→{destination}"
        + (" + return" if has_return else ""),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("src")
    t.add_column("Matrix", justify="right")
    t.add_column("Google", justify="right")
    t.add_column("outbound")
    t.add_column("return")
    for i, row in enumerate(rows[:top_n], 1):
        itn = row.itinerary.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        out = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        t.add_row(
            str(i),
            _MERGE_SOURCE_TAG.get(row.source, row.source),
            _amount(row.matrix_price),
            _amount(row.gf_price),
            out,
            ret,
        )
    console.print(t)


def _run_gflight_path(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    json_out: bool,
    run_pp: bool = False,
    sel: ProviderSelection | None = None,
    matrix_url: bool = False,
    google_url: bool = False,
    pick: int | None = None,
) -> None:
    """Google Flights path: build fli filter → query → render. Single-leg or round-trip.

    When run_pp=True, fli's results are adapted into a SearchResult shape so
    the existing PP matcher + renderer reuse cleanly. PP runs on the same
    (origin, dest, date) per leg as the matrix path.
    """
    from ._gflight_ids import GfThrottledError  # noqa: PLC0415
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    try:
        results = _gflight_results(legs, opts, top_n)
    except GfThrottledError as e:
        err.print(
            "[yellow]Google Flights is rate-limiting this IP.[/] Wait a moment and "
            "retry, or use [bold]--backend matrix[/]."
        )
        raise typer.Exit(1) from e
    except Exception as e:
        err.print(f"[red]Google Flights query failed:[/] {e}")
        raise typer.Exit(1) from e

    if not results:
        console.print("[yellow]Google Flights: no results (or none matched the routing).[/]")
        return

    # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType,
    #                 reportUnknownArgumentType, reportUnknownParameterType]
    # fli/fast_flights have no type stubs; results are duck-typed pydantic
    # models. Suppressing the noisy unknown-type chatter for this rendering
    # block keeps the boundary localized.
    if json_out and not run_pp:
        out: list[Any] = []
        for r in results:
            items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
            dumped = [{**g.flight.model_dump(mode="json"), "flight_id": g.flight_id} for g in items]
            out.append(dumped if isinstance(r, tuple) else dumped[0])
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return

    awards_only = sel.awards_only if sel is not None else False
    if not awards_only:
        _render_gflight_table(results, legs=legs, top_n=top_n, match_carriers=_match_carriers(legs))

    # Always adapt to SearchResult shape so the URL emission has segment
    # info for the pinned link (cheap: just shuffles existing fields).
    sr = fli_results_to_search_result(results)

    if run_pp:
        p = opts.pax
        run_pp_for_search(
            sr,
            legs=_build_pp_legs(legs),
            num_passengers=p.adults + p.children + p.seniors + p.youth,
            airlines=sel.pp_airlines() if sel is not None else None,
            cabins=sel.pp_cabins() if sel is not None else None,
            pp_only=awards_only,
            json_out=json_out,
            provider_filter=sel.provider_filter if sel is not None else None,
            seats_sources=sel.seats_sources() if sel is not None else None,
            cash_per_cabin=_cash_per_cabin_single(sr, opts.cabin),
        )

    _emit_urls(
        SpecificDateSearch(legs=legs, options=opts),
        matrix_url=matrix_url,
        google_url=google_url,
        result=sr,
        pick=pick,
    )


def _run_enriched_path(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    run_pp: bool,
    sel: ProviderSelection | None,
    matrix_url: bool,
    google_url: bool,
    pick: int | None,
    rps: float,
    impersonate: str,
    no_cache: bool,
) -> None:
    """GF-serveable query, progressive: dispatch Google Flights + Matrix
    concurrently under one event loop, paint GF immediately (~1s), then repaint a
    reconciled GF+Matrix table once Matrix lands (~45s). PP/awards + URLs run on
    the Matrix (authoritative) result. `--fast` skips this for GF-only speed."""
    from ._enrich import merge_results  # noqa: PLC0415
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    matrix_search = SpecificDateSearch(legs=legs, options=opts)
    awards_only = sel.awards_only if sel is not None else False
    state: dict[str, Any] = {}

    async def _matrix(c: MatrixClient) -> None:
        try:
            state["matrix"] = await c.execute(matrix_search, cache=not no_cache)
        except MatrixApiError as e:
            state["matrix_err"] = e

    async def _go() -> None:
        async with (
            MatrixClient(rps=rps, impersonate=impersonate) as c,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(_matrix, c)
            # Google Flights is sync (curl_cffi) — run it in a worker thread so the
            # Matrix request progresses concurrently on the event loop.
            try:
                gf = await anyio.to_thread.run_sync(_gflight_results, legs, opts, top_n)
            except Exception as e:  # noqa: BLE001 - reported below; Matrix may still succeed
                state["gf_err"] = e
                gf = []
            state["gf"] = gf
            # First paint, while Matrix is still in flight.
            if gf and not awards_only:
                _render_gflight_table(
                    gf, legs=legs, top_n=top_n, match_carriers=_match_carriers(legs)
                )
                console.print("[dim]…refining with Matrix (authoritative fares)…[/]")
            elif not gf and "gf_err" not in state:
                console.print("[yellow]Google Flights: no results; awaiting Matrix…[/]")

    anyio.run(_go)

    gf: list[Any] = state.get("gf") or []
    if "gf_err" in state:
        from ._gflight_ids import GfThrottledError  # noqa: PLC0415

        e = state["gf_err"]
        if isinstance(e, GfThrottledError):
            console.print("[dim]Google Flights rate-limited — showing Matrix only.[/]")
        else:
            err.print(f"[yellow]Google Flights query failed:[/] {e}")
    matrix_res = state.get("matrix")
    if matrix_res is None:
        # Matrix failed; the GF table (if any) was already painted.
        e = state.get("matrix_err")
        if e is not None:
            err.print(f"[red]Matrix returned an error ({e.kind}):[/] {e.message}")
        if not gf:
            raise typer.Exit(1)
        return
    matrix_res = cast("SearchResult", matrix_res)

    # Repaint: reconciled GF + Matrix, prices attributed.
    if not awards_only:
        merged = merge_results(fli_results_to_search_result(gf), matrix_res)
        _render_merged(merged, legs=legs, top_n=top_n)

    if run_pp:
        p = opts.pax
        run_pp_for_search(
            matrix_res,
            legs=_build_pp_legs(legs),
            num_passengers=p.adults + p.children + p.seniors + p.youth,
            airlines=sel.pp_airlines() if sel is not None else None,
            cabins=sel.pp_cabins() if sel is not None else None,
            pp_only=awards_only,
            json_out=False,
            provider_filter=sel.provider_filter if sel is not None else None,
            seats_sources=sel.seats_sources() if sel is not None else None,
            cash_per_cabin=_cash_per_cabin_single(matrix_res, opts.cabin),
        )

    _emit_urls(
        matrix_search, matrix_url=matrix_url, google_url=google_url, result=matrix_res, pick=pick
    )


# ─────────────────────────── multi-cabin orchestration ─────────────────────

# When --cabin selects multiple cabins, each cabin's per-query top-N is bumped
# so the client-side merge has overlap to work with. Cheapest economy and
# cheapest business on a given route are often different carriers entirely
# (e.g. JFK-LHR: VS in economy, FI in business) — a top-5 query per cabin
# almost never overlaps, leaving the J column rendered as all "—".
# Bumping per-cabin queries to ~25-50 itineraries lets the join surface
# matching itineraries that exist in both cabins' results.
#
# Capped to bound response size (each itinerary costs bytes + parse time);
# Matrix and gflight both tolerate page sizes in this range comfortably.
_MULTI_CABIN_QUERY_BUMP_FACTOR = 5
_MULTI_CABIN_QUERY_BUMP_CAP = 100


def _bumped_query_top_n(top_n: int, cabin_count: int) -> int:
    """Per-cabin query page size for a multi-cabin search.

    Single-cabin invocations get `top_n` unchanged. Multi-cabin gets
    `top_n * factor` capped at the bump ceiling. The visible row count
    after merge is still `top_n` (renderer trims by sort cabin) — the
    bump only widens the search space the join can draw from.
    """
    if cabin_count <= 1:
        return top_n
    return min(top_n * _MULTI_CABIN_QUERY_BUMP_FACTOR, _MULTI_CABIN_QUERY_BUMP_CAP)


def _derive_pp_cabins(cash_cabins: tuple[Cabin, ...]) -> tuple[str, ...]:
    """Map cash cabin list → PP cabin list for the PP overlay.

    Adds First when Business is requested but First isn't: award seekers
    treat business/first as a paired premium tier, and First is rare enough
    that surfacing it costs almost nothing while filling a real research
    gap. The reverse promotion (First → +Business) isn't applied — asking
    for First means the user has already made that call.
    """
    out: list[str] = [_CABIN_TO_PP_NAME[c] for c in cash_cabins]
    if Cabin.BUSINESS in cash_cabins and Cabin.FIRST not in cash_cabins:
        out.append(_CABIN_TO_PP_NAME[Cabin.FIRST])
    return tuple(out)


def _pp_cabins_for_multi(sel: ProviderSelection, cabins: tuple[Cabin, ...]) -> str | None:
    """PP cabins for a multi-cabin search. User's `--provider-opt pp.cabins=`
    wins; otherwise derive from `--cabin` with the business→+first rule."""
    user_set = sel.pp_cabins()
    if user_set is not None:
        return user_set
    return ",".join(_derive_pp_cabins(cabins))


def _cash_per_cabin_single(res: SearchResult, query_cabin: Cabin) -> dict[int, dict[str, float]]:
    """Build the per-itinerary cash map for a single-cabin invocation.

    The PP renderer needs to know which PP cabin name the cash field on each
    itinerary corresponds to — otherwise it can't compute ¢/mi against the
    right cash basis. For single-cabin runs the answer is the queried cabin
    applied uniformly.
    """
    name = _CABIN_TO_PP_NAME[query_cabin]
    out: dict[int, dict[str, float]] = {}
    for it in res.solutions:
        cash = parse_price(it.price)
        if cash is not None:
            out[id(it)] = {name: cash}
    return out


def _cash_per_cabin_multi(rows: list[MultiCabinRow]) -> dict[int, dict[str, float]]:
    """Build the per-itinerary cash map for a multi-cabin merged result.

    `rows` carries each itinerary alongside the prices observed in each cabin.
    Object identity is preserved through the merge (and through PP's matcher
    and de-dup), so `id(row.itinerary)` is a stable lookup key.
    """
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        prices: dict[str, float] = {}
        for cab, price in r.prices.items():
            cash = parse_price(price)
            if cash is not None:
                prices[_CABIN_TO_PP_NAME[cab]] = cash
        if prices:
            out[id(r.itinerary)] = prices
    return out


def _run_matrix_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    rps: float,
    impersonate: str,
    no_cache: bool,
) -> dict[Cabin, SearchResult]:
    """Fan out N parallel Matrix queries (one per cabin), one shared client.

    Per-cabin failures are soft: log + omit from the result dict (renderer
    shows that column as all '—'). Connection-level / auth errors still
    propagate so the user sees real outages.
    """
    results: dict[Cabin, SearchResult] = {}

    async def query_cabin(client: MatrixClient, cab: Cabin) -> None:
        cabin_opts = opts.model_copy(update={"cabin": cab})
        search = SpecificDateSearch(legs=legs, options=cabin_opts)
        try:
            res = await client.execute(search, cache=not no_cache)
        except MatrixApiError as e:
            err.print(f"[yellow]Matrix {cab.value} query failed ({e.kind}): {e.message}[/]")
            return
        results[cab] = cast("SearchResult", res)

    async def go() -> None:
        async with (
            MatrixClient(rps=rps, impersonate=impersonate) as client,
            anyio.create_task_group() as tg,
        ):
            for cab in cabins:
                tg.start_soon(query_cabin, client, cab)

    try:
        anyio.run(go)
    except MatrixApiError as e:
        err.print(f"[red]Matrix returned an error ({e.kind}):[/] {e.message}")
        if e.request_id:
            err.print(f"[dim]request_id: {e.request_id}[/]")
        raise typer.Exit(1) from e
    return results


def _run_gflight_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    top_n: int,
) -> dict[Cabin, list[Any]]:
    """Fan out N parallel gflight queries (one per cabin). fli is sync, so
    each query runs in a worker thread via `anyio.to_thread.run_sync`."""
    from ._gflight_ids import search_with_ids  # noqa: PLC0415
    from .fli_bridge import to_fli_filter  # noqa: PLC0415

    results: dict[Cabin, list[Any]] = {}

    def query_sync(cab: Cabin) -> list[Any]:
        cabin_opts = opts.model_copy(update={"cabin": cab})
        search = SpecificDateSearch(legs=legs, options=cabin_opts)
        return search_with_ids(to_fli_filter(search), top_n=top_n) or []

    async def query_cabin(cab: Cabin) -> None:
        try:
            results[cab] = await anyio.to_thread.run_sync(query_sync, cab)
        except Exception as e:  # noqa: BLE001 — fli has no documented exception surface
            err.print(f"[yellow]Google Flights {cab.value} query failed: {e}[/]")

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            for cab in cabins:
                tg.start_soon(query_cabin, cab)

    anyio.run(go)
    return results


def _gflight_to_search_result_per_cabin(
    results_by_cabin: dict[Cabin, list[Any]],
) -> dict[Cabin, SearchResult]:
    """Adapt gflight's duck-typed fli results into the SearchResult shape so
    the merge/render path is backend-agnostic. Reuses pp.gflight_adapter."""
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    return {cab: fli_results_to_search_result(res) for cab, res in results_by_cabin.items()}


def _render_multi_cabin_search(
    rows: list[MultiCabinRow],
    *,
    cabins: tuple[Cabin, ...],
    sort_by: Cabin,
    title_prefix: str = "Itineraries",
) -> None:
    """Render multi-cabin merged rows. One row per itinerary, one $ column
    per requested cabin, '—' for missing."""
    if not rows:
        console.print("[yellow]No itineraries.[/]")
        return
    # Use the first present price to surface a currency tag in the title.
    ccy = ""
    for row in rows:
        for p in row.prices.values():
            ccy_candidate, _ = _split_price(p)
            if ccy_candidate:
                ccy = ccy_candidate
                break
        if ccy:
            break
    ccy_tag = f" ({ccy})" if ccy else ""
    cabin_labels = "+".join(_CABIN_TO_LETTER[c] for c in cabins)

    t = Table(
        title=f"{title_prefix} · {cabin_labels} (sorted by {_CABIN_TO_LETTER[sort_by]}){ccy_tag}",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("carriers")
    t.add_column("outbound")
    t.add_column("return")
    for cab in cabins:
        t.add_column(f"{_CABIN_TO_LETTER[cab]} $", justify="right")

    for i, row in enumerate(rows, 1):
        itn = row.itinerary.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        carriers = ",".join((c.code or "?") for c in (itn.carriers if itn else []))

        out_cell = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret_cell = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        price_cells = [_amount(row.prices.get(cab)) for cab in cabins]
        t.add_row(str(i), carriers or "?", out_cell, ret_cell, *price_cells)
    console.print(t)


def _validate_sort_cabin(sort_by: Cabin, cabins: tuple[Cabin, ...]) -> None:
    if sort_by not in cabins:
        names = ", ".join(c.value for c in cabins)
        err.print(f"[red]--sort {sort_by.value!r} must be one of --cabin: {names}[/]")
        raise typer.Exit(2)


def _run_matrix_path_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    sort_by: Cabin,
    top_n: int,
    rps: float,
    impersonate: str,
    no_cache: bool,
    json_out: bool,
    matrix_url: bool,
    google_url: bool,
    run_pp: bool,
    sel: ProviderSelection,
) -> None:
    """Matrix multi-cabin: N parallel cabin queries → client-side join → render."""
    # Widen each per-cabin query so the join has overlap to render — top_n
    # rows visible after merge, but each cabin's underlying query pulls
    # `_bumped_query_top_n` candidates. See _bumped_query_top_n docstring.
    query_opts = opts.model_copy(update={"page_size": _bumped_query_top_n(top_n, len(cabins))})
    results_by_cabin = _run_matrix_multi(
        legs=legs,
        opts=query_opts,
        cabins=cabins,
        rps=_resolve_rps(rps),
        impersonate=_resolve_impersonate(impersonate),
        no_cache=_resolve_no_cache(no_cache),
    )
    if not results_by_cabin:
        err.print("[red]All cabin queries failed.[/]")
        raise typer.Exit(1)

    if json_out and not run_pp:
        # JSON shape: {cabin: raw} so consumers can re-merge if they want.
        sys.stdout.write(
            json.dumps({c.value: r.raw for c, r in results_by_cabin.items()}, indent=2)
        )
        return

    rows = _merge_cabins(results_by_cabin, sort_by=sort_by, top_n=top_n)
    if not sel.awards_only:
        _render_multi_cabin_search(rows, cabins=cabins, sort_by=sort_by)

    if run_pp:
        # PP runs once against the merged result so award flights match against
        # the full itinerary set we just rendered. Pick any one of the cabin
        # results to source slices for cash_hints / matched-id lookups —
        # itineraries that survived the merge are the union of all.
        merged = _merge_results_into_one(results_by_cabin, rows)
        p = opts.pax
        run_pp_for_search(
            merged,
            legs=_build_pp_legs(legs),
            num_passengers=p.adults + p.children + p.seniors + p.youth,
            airlines=sel.pp_airlines(),
            cabins=_pp_cabins_for_multi(sel, cabins),
            pp_only=sel.awards_only,
            json_out=json_out,
            provider_filter=sel.provider_filter,
            seats_sources=sel.seats_sources(),
            cash_per_cabin=_cash_per_cabin_multi(rows),
        )

    # Deep links are cabin-specific (Matrix's URL encodes one cabin). Emit the
    # link for the sort cabin — it's the "primary" surface in the rendered
    # table and the one a user is most likely to click through to.
    sort_opts = opts.model_copy(update={"cabin": sort_by})
    _emit_urls(
        SpecificDateSearch(legs=legs, options=sort_opts),
        matrix_url=matrix_url,
        google_url=google_url,
    )


def _run_gflight_path_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    sort_by: Cabin,
    top_n: int,
    json_out: bool,
    run_pp: bool,
    sel: ProviderSelection,
) -> None:
    """Google Flights multi-cabin: N parallel cabin queries (threadpool) → join → render."""
    # Widen per-cabin queries so the join has overlap; see _bumped_query_top_n.
    query_top_n = _bumped_query_top_n(top_n, len(cabins))
    fli_by_cabin = _run_gflight_multi(legs=legs, opts=opts, cabins=cabins, top_n=query_top_n)
    if not fli_by_cabin:
        err.print("[red]All Google Flights cabin queries failed.[/]")
        raise typer.Exit(1)

    if json_out and not run_pp:
        out: dict[str, Any] = {}
        for cab, fli_results in fli_by_cabin.items():
            cab_dumped: list[Any] = []
            for r in fli_results:
                items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
                dumped = [
                    {**g.flight.model_dump(mode="json"), "flight_id": g.flight_id} for g in items
                ]
                cab_dumped.append(dumped if isinstance(r, tuple) else dumped[0])
            out[cab.value] = cab_dumped
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return

    results_by_cabin = _gflight_to_search_result_per_cabin(fli_by_cabin)
    rows = _merge_cabins(results_by_cabin, sort_by=sort_by, top_n=top_n)
    if not sel.awards_only:
        _render_multi_cabin_search(
            rows, cabins=cabins, sort_by=sort_by, title_prefix="Google Flights"
        )

    if run_pp:
        merged = _merge_results_into_one(results_by_cabin, rows)
        p = opts.pax
        run_pp_for_search(
            merged,
            legs=_build_pp_legs(legs),
            num_passengers=p.adults + p.children + p.seniors + p.youth,
            airlines=sel.pp_airlines(),
            cabins=_pp_cabins_for_multi(sel, cabins),
            pp_only=sel.awards_only,
            json_out=json_out,
            provider_filter=sel.provider_filter,
            seats_sources=sel.seats_sources(),
            cash_per_cabin=_cash_per_cabin_multi(rows),
        )


def _merge_results_into_one(
    results_by_cabin: dict[Cabin, SearchResult],
    rows: list[MultiCabinRow],
) -> SearchResult:
    """Build a single SearchResult whose `solutions` are the merged itineraries
    in render order. Used as input to `run_pp_for_search` — PP matches by
    flight#+date, so any per-cabin price difference doesn't affect the match
    (PP attaches awards to flights, not fares)."""
    # SearchResult is TYPE_CHECKING-only at module top — need a runtime import.
    from .models import SearchResult  # noqa: PLC0415

    # Pick any one of the per-cabin results to seed the carrier_stop_matrix /
    # currency_notice — PP only reads `.solutions`, so the rest doesn't matter.
    seed = next(iter(results_by_cabin.values()))
    return SearchResult(
        solutionCount=len(rows),
        solutions=[r.itinerary for r in rows],
        carrierStopMatrix=seed.carrier_stop_matrix,
        currencyNotice=seed.currency_notice,
        session=seed.session,
        solutionSet=seed.solution_set,
        raw=seed.raw,
    )


def _match_carriers(legs: tuple[Leg, ...]) -> frozenset[str]:
    """Marketing carrier codes the user filtered on (for codeshare-aware display).
    Empty when there's no marketing-carrier include filter — operating (`O:`) and
    exclude filters don't trigger codeshare relabeling."""
    from .routing_predicates import CarrierPred, classify  # noqa: PLC0415

    codes: set[str] = set()
    for lg in legs:
        for p in classify(lg.route_language, lg.extension).predicates:
            if isinstance(p, CarrierPred) and not p.operating and not p.exclude:
                codes |= p.codes
    return frozenset(codes)


def _leg_display(leg: Any, amenity: Any, match_carriers: frozenset[str]) -> str:
    """Per-leg label '<carrier> <num>'. If the booking carrier isn't in the user's
    carrier filter but the leg is sold under a codeshare that IS (e.g. UA58 sold as
    LH9407 under `--routing LH+`), show the matched identity: 'LH9407 (op UA58)'."""
    code = getattr(leg.airline, "name", "") or ""
    number = getattr(leg, "flight_number", "?")
    booking = f"{code} {number}"
    if not match_carriers or code in match_carriers:
        return booking
    raw_mf = getattr(amenity, "marketing_flights", ()) if amenity else ()
    mflights: tuple[str, ...] = tuple(raw_mf or ())
    for mf in mflights:
        if mf[:2].upper() in match_carriers:
            return f"{mf} (op {code}{number})"
    return booking


def _render_gflight_table(
    results: list[Any],
    *,
    legs: tuple[Leg, ...],
    top_n: int,
    match_carriers: frozenset[str] = frozenset(),
) -> None:
    """Render fli results as a rich table. Duck-typed: fli has no type stubs.

    Accepts our `GFlightWithId` wrappers — `.flight` is fli's FlightResult,
    `.amenities` is per-leg legroom data parsed from Google's response.
    `match_carriers` enables codeshare-aware leg labels (see `_leg_display`)."""
    origin = legs[0].origins[0] if legs[0].origins else "?"
    destination = legs[0].destinations[0] if legs[0].destinations else "?"
    has_return = len(legs) >= _ROUND_TRIP_LEGS
    t = Table(
        title=f"Google Flights · {origin}→{destination}" + (" + return" if has_return else ""),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("price", justify="right")
    t.add_column("stops", justify="right")
    t.add_column("duration")
    t.add_column("legs")
    t.add_column("legroom")
    any_legroom = False
    for i, r in enumerate(results[:top_n], 1):
        items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
        for j, g in enumerate(items):
            fr = g.flight  # unwrap GFlightWithId → fli FlightResult
            amenities = getattr(g, "amenities", []) or []
            label = f"{i}{'a' if j == 0 else 'b'}" if len(items) > 1 else str(i)
            legs_str = " → ".join(
                _leg_display(leg, amenities[k] if k < len(amenities) else None, match_carriers)
                for k, leg in enumerate(fr.legs)
            )
            mins = fr.duration
            dur = f"{mins // 60}h{mins % 60:02d}m"
            legroom_str = _fmt_gflight_legroom(fr.legs, amenities)
            if legroom_str:
                any_legroom = True
            t.add_row(
                label,
                f"{fr.currency or 'USD'}{fr.price:.2f}",
                str(fr.stops),
                dur,
                legs_str,
                legroom_str,
            )
    console.print(t)
    if any_legroom:
        console.print(_LEGROOM_KEY)


# AVERAGE/BELOW/ABOVE are pitch-relative judgments — collapse them to color on
# the inches token so the eye picks out squeeze rows without text noise. The
# named premium-cabin enums describe seat construction (Lie Flat vs Suite vs
# Angled Flat aren't comparable on pitch alone) so those stay as text.
_LEGROOM_AS_COLOR = {"BELOW": "red", "ABOVE": "green"}
_CABIN_LETTER = {"ECONOMY": "Y", "PREMIUM": "W", "BUSINESS": "J", "FIRST": "F"}
# Domain Cabin enum → human label and PP API cabin string. Used for
# multi-cabin column headers and PP cabin derivation.
_CABIN_TO_LETTER: dict[Cabin, str] = {
    Cabin.COACH: "Y",
    Cabin.PREMIUM_COACH: "W",
    Cabin.BUSINESS: "J",
    Cabin.FIRST: "F",
}
_CABIN_TO_PP_NAME: dict[Cabin, str] = {
    Cabin.COACH: "Economy",
    Cabin.PREMIUM_COACH: "Premium economy",
    Cabin.BUSINESS: "Business",
    Cabin.FIRST: "First",
}
# 📶 for wifi is the only emoji (2-col) — wifi is the highest-value binary signal
# and 📶 is universally read at-a-glance where ≋ is not. Power and video keep
# 1-col Unicode pairs so the plug-vs-USB and stream-vs-ondemand distinctions
# don't bloat the column. See `_LEGROOM_KEY` for the rendered legend.
_WIFI_GLYPH = {"free": "📶", "paid": "[yellow]📶$[/]"}
# ↯ is more lightning-y (= plug power); ⌁ reads more like a connector (= USB).
_POWER_GLYPH = {"plug": "↯", "usb": "⌁"}
# ◰ (quadrant square) evokes a phone screen — stands in for BYOD streaming.
_VIDEO_GLYPH = {"stream": "▶", "ondemand": "▷", "byod": "◰"}
_LEGROOM_KEY = (
    "[dim]Legroom glyphs: "
    f"{_WIFI_GLYPH['free']} free wifi · "
    f"{_WIFI_GLYPH['paid']}[dim] paid wifi · "
    f"{_POWER_GLYPH['plug']} in-seat plug · "
    f"{_POWER_GLYPH['usb']} USB only · "
    f"{_VIDEO_GLYPH['stream']} live TV · "
    f"{_VIDEO_GLYPH['ondemand']} on-demand · "
    f"{_VIDEO_GLYPH['byod']} stream-to-device · "
    "[red]red[/dim] = BELOW · [green]green[/] = ABOVE"
    "[/]"
)


def _fmt_gflight_legroom(fli_legs: list[Any], amenities: list[Any]) -> str:
    """One line per physical leg: `<cabin> <pitch>" [seat-type] <amenities>`.

    `amenities[i]` is a LegAmenities instance from _gflight_ids; misaligned
    or empty inputs render as ''."""
    lines: list[str] = []
    for i, leg in enumerate(fli_legs):
        a = amenities[i] if i < len(amenities) else None
        if a is None:
            continue
        parts: list[str] = []
        cabin = _CABIN_LETTER.get(getattr(a, "cabin", None) or "", "")
        if cabin:
            parts.append(cabin)
        pitch = getattr(a, "pitch_inches", None)
        cls = getattr(a, "legroom_class", None)
        if pitch is not None:
            tok = f'{pitch}"'
            color = _LEGROOM_AS_COLOR.get(cls or "")
            if color:
                tok = f"[{color}]{tok}[/]"
            parts.append(tok)
        if cls and cls not in {"AVERAGE", "BELOW", "ABOVE"}:
            parts.append(cls)
        glyphs: list[str] = []
        wifi_g = _WIFI_GLYPH.get(getattr(a, "wifi", None) or "")
        if wifi_g:
            glyphs.append(wifi_g)
        power_g = _POWER_GLYPH.get(getattr(a, "power", None) or "")
        if power_g:
            glyphs.append(power_g)
        video_g = _VIDEO_GLYPH.get(getattr(a, "video", None) or "")
        if video_g:
            glyphs.append(video_g)
        if glyphs:
            parts.append("".join(glyphs))
        if not parts:
            continue
        leg_label = (
            f"{getattr(leg.airline, 'name', leg.airline)}{getattr(leg, 'flight_number', '?')}"
        )
        lines.append(f"{leg_label:<6} " + " ".join(parts))
    return "\n".join(lines)


# ─────────────────────────────── commands ──────────────────────────────────

# rich_help_panel groups for grouped `--help` output. Same names used across
# search/calendar/detail/fare/gflight so the user gets a consistent mental
# map for where each kind of flag lives.
_GROUP_ITINERARY = "Itinerary"
_GROUP_FILTERING = "Filtering"
_GROUP_OUTPUT = "Output"
_GROUP_BACKEND = "Backend & providers"


@app.command()
def search(
    origin: Annotated[
        str | None,
        typer.Argument(help="Origin IATA (comma-list ok for multi-airport)"),
    ] = None,
    destination: Annotated[
        str | None,
        typer.Argument(help="Destination IATA (comma-list ok)"),
    ] = None,
    dep: Annotated[
        str | None,
        typer.Option("--dep", help="YYYY-MM-DD", rich_help_panel=_GROUP_ITINERARY),
    ] = None,
    ret: Annotated[
        str | None,
        typer.Option(
            "--return",
            "-r",
            help="YYYY-MM-DD; omit for one-way",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    slice_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--slice",
            "-s",
            help="Multi-city: 'ORIG-DEST:DATE[:r=ROUTING:e=EXT]'. Repeat. (Matrix only)",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=(
                "auto|matrix|gflight. auto picks gflight for plain searches and "
                "matrix when Matrix-only flags are set (routing/extension/slice/"
                "time-of-day/extra pax types/PP config)."
            ),
            rich_help_panel=_GROUP_BACKEND,
        ),
    ] = BACKEND_AUTO,
    cabin: str = typer.Option(
        "economy",
        "--cabin",
        help=(
            "Cabin, or comma list for multi-cabin compare ('economy,business'). "
            "Multi-cabin renders one $ column per cabin; '—' means the itinerary "
            "wasn't in that cabin's top-N (cabin unavailable OR priced out). "
            "Bump -n for broader overlap across cabins."
        ),
        rich_help_panel=_GROUP_ITINERARY,
    ),
    sort_cabin: Annotated[
        str | None,
        typer.Option(
            "--sort",
            help="Cabin to sort multi-cabin results by. Default: first in --cabin.",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    adults: int = typer.Option(1, "--adults", rich_help_panel=_GROUP_ITINERARY),
    children: int = typer.Option(0, "--children", rich_help_panel=_GROUP_ITINERARY),
    seniors: int = typer.Option(0, "--seniors", rich_help_panel=_GROUP_ITINERARY),
    youth: int = typer.Option(0, "--youth", rich_help_panel=_GROUP_ITINERARY),
    inf_seat: Annotated[
        int,
        typer.Option("--inf-seat", rich_help_panel=_GROUP_ITINERARY),
    ] = 0,
    inf_lap: Annotated[
        int,
        typer.Option("--inf-lap", rich_help_panel=_GROUP_ITINERARY),
    ] = 0,
    routing: Annotated[
        str | None,
        typer.Option(
            "--routing",
            help="Routing language ('LH+', 'BA AA', '[F* X F*]'). Matrix only.",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    extension: Annotated[
        str | None,
        typer.Option(
            "--extension",
            "--ext",
            help="Extension codes ('MAXCONNECT 2:00', 'MAXSTOPS 1'). Matrix only.",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    depart_times: Annotated[
        str | None,
        typer.Option(
            "--depart-times",
            help="Preferred outbound times-of-day (comma list: morning,evening). Matrix only.",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    return_times: Annotated[
        str | None,
        typer.Option(
            "--return-times",
            help="Preferred return times-of-day. Matrix only.",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    stops: Annotated[
        int | None,
        typer.Option(
            "--stops",
            help="Max extra stops beyond nonstop (0=nonstop only, 1=up to 1 stop, ...)",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    allow_airport_changes: bool = typer.Option(
        True,
        "--allow-airport-changes/--no-airport-changes",
        rich_help_panel=_GROUP_FILTERING,
    ),
    only_available: bool = typer.Option(
        True,
        "--only-available/--include-unavailable",
        rich_help_panel=_GROUP_FILTERING,
    ),
    page_size: int = typer.Option(
        10,
        "--n",
        "-n",
        help="Result count (matrix: page size; gflight: top_n).",
        rich_help_panel=_GROUP_OUTPUT,
    ),
    rps: float | None = _RPS_OPT,
    impersonate: str | None = _IMPERSONATE_OPT,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
    matrix_url: bool = typer.Option(
        True,
        "--matrix-url/--no-matrix-url",
        help=_MATRIX_URL_HELP,
        rich_help_panel=_GROUP_OUTPUT,
    ),
    google_url: bool = typer.Option(
        True,
        "--google-url/--no-google-url",
        help=_GOOGLE_URL_HELP,
        rich_help_panel=_GROUP_OUTPUT,
    ),
    pick: int | None = typer.Option(
        None,
        "--pick",
        help="Pin itinerary #N (1-based, as shown in the table) in the "
        "--matrix-url/--google-url deep links. Default: cheapest.",
        rich_help_panel=_GROUP_OUTPUT,
    ),
    no_cache: bool = _NO_CACHE_OPT,
    fast: bool = typer.Option(
        False,
        "--fast/--enrich",
        "--no-enrich/--no-fast",
        help="Skip Matrix enrichment: show only the fast Google Flights result "
        "(~1s) instead of also reconciling against Matrix. Default: enrich when "
        "Google Flights can serve the query.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    providers: str | None = typer.Option(
        None,
        "--providers",
        help=("CSV of award providers to use (e.g. 'pp'). Default: all configured providers."),
        rich_help_panel=_GROUP_BACKEND,
    ),
    cash_only: bool = typer.Option(
        False,
        "--cash-only",
        help="Skip all award providers; just show the cash table.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    awards_only: bool = typer.Option(
        False,
        "--awards-only",
        help="Skip the cash table; show only the award provider output.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    provider_opt: list[str] | None = _PROVIDER_OPT,
    no_pp: bool = typer.Option(
        False,
        "--no-pp",
        hidden=True,
        help="[deprecated] Use --cash-only.",
    ),
    pp_only: bool = typer.Option(
        False,
        "--pp-only",
        hidden=True,
        help="[deprecated] Use --awards-only.",
    ),
    pp_airlines: str | None = typer.Option(
        None,
        "--pp-airlines",
        hidden=True,
        help="[deprecated] Use --provider-opt pp.airlines=A,B.",
    ),
    pp_cabin: str | None = typer.Option(
        None,
        "--pp-cabin",
        hidden=True,
        help="[deprecated] Use --provider-opt pp.cabins=Economy,Business.",
    ),
) -> None:
    """Specific-date flight search across Matrix and Google Flights backends.

    auto-picks the backend: gflight for plain cash searches; matrix when a
    Matrix-only flag is set (routing/extension/multi-city slice/time-of-day/
    extra pax types/PP config). Force with --backend matrix|gflight.
    """
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    # Deprecated-flag warning surfaces at runtime since hidden=True hides the
    # banner from --help.
    if no_pp or pp_only or pp_airlines or pp_cabin:
        err.print(
            "[yellow]--no-pp/--pp-only/--pp-airlines/--pp-cabin are deprecated; "
            "use --cash-only / --awards-only / --provider-opt instead.[/]",
        )
    sel = _resolve_providers(
        providers=providers,
        cash_only=cash_only,
        awards_only=awards_only,
        provider_opt=tuple(provider_opt or ()),
        legacy_no_pp=no_pp,
        legacy_pp_only=pp_only,
        legacy_pp_airlines=pp_airlines,
        legacy_pp_cabin=pp_cabin,
    )
    resolved = _pick_backend(
        backend=backend,
        routing=routing,
        extension=extension,
        slice_specs=slice_specs,
        depart_times=depart_times,
        return_times=return_times,
        seniors=seniors,
        youth=youth,
        inf_seat=inf_seat,
        inf_lap=inf_lap,
    )
    if slice_specs:
        legs = tuple(_parse_slice_spec(s) for s in slice_specs)
    elif origin and destination and dep:
        out_times = _parse_times(depart_times)
        ret_times = _parse_times(return_times)
        legs = (
            Leg.of(
                _parse_iata_list(origin),
                _parse_iata_list(destination),
                _parse_date(dep),
                route_language=routing,
                extension=extension,
                time_ranges=out_times,
            ),
        )
        if ret:
            legs += (
                Leg.of(
                    _parse_iata_list(destination),
                    _parse_iata_list(origin),
                    _parse_date(ret),
                    route_language=routing,
                    extension=extension,
                    time_ranges=ret_times,
                ),
            )
    else:
        err.print("[red]Specify --slice ... or origin destination --dep[/]")
        raise typer.Exit(2)

    cabins_tuple = _resolve_cabin_list(cabin)
    # _resolve_cabin_list raises typer.Exit on empty input, so cabins_tuple is
    # never empty here. Bind `first_cabin` before any len-narrowing branches so
    # basedpyright keeps the `tuple[Cabin, ...]` → Cabin inference.
    first_cabin = cabins_tuple[0]
    sort_by = _resolve_cabin(sort_cabin) if sort_cabin else first_cabin
    if len(cabins_tuple) > 1:
        _validate_sort_cabin(sort_by, cabins_tuple)

    # `_build_options` seeds with the first cabin in the list; multi-cabin
    # orchestrators clone opts per cabin via `model_copy(update={"cabin": ...})`.
    opts = _build_options(
        cabin=first_cabin.value,
        adults=adults,
        children=children,
        seniors=seniors,
        youth=youth,
        infants_in_seat=inf_seat,
        infants_in_lap=inf_lap,
        stops=stops,
        allow_airport_changes=allow_airport_changes,
        show_only_available=only_available,
        page_size=page_size,
    )

    run_awards = _should_run_awards(sel)

    if len(cabins_tuple) > 1:
        # The multi-cabin gflight path doesn't apply the routing/extension
        # filters yet, so a constrained multi-cabin search goes to Matrix to
        # honor the routing correctly (the single-cabin gflight path filters).
        if resolved == BACKEND_GFLIGHT and not (routing or extension):
            _run_gflight_path_multi(
                legs=legs,
                opts=opts,
                cabins=cabins_tuple,
                sort_by=sort_by,
                top_n=page_size,
                json_out=json_out,
                run_pp=run_awards,
                sel=sel,
            )
            return
        _run_matrix_path_multi(
            legs=legs,
            opts=opts,
            cabins=cabins_tuple,
            sort_by=sort_by,
            top_n=page_size,
            rps=_resolve_rps(rps),
            impersonate=_resolve_impersonate(impersonate),
            no_cache=_resolve_no_cache(no_cache),
            json_out=json_out,
            matrix_url=matrix_url,
            google_url=google_url,
            run_pp=run_awards,
            sel=sel,
        )
        return

    if resolved == BACKEND_GFLIGHT:
        # GF can serve this query — paint it fast (~1s), then enrich against
        # Matrix (authoritative) and repaint a merged table. `--fast` (or JSON
        # output, which wants a single stable shape) takes the GF-only path.
        if not fast and not json_out:
            _run_enriched_path(
                legs=legs,
                opts=opts,
                top_n=page_size,
                run_pp=run_awards,
                sel=sel,
                matrix_url=matrix_url,
                google_url=google_url,
                pick=pick,
                rps=_resolve_rps(rps),
                impersonate=_resolve_impersonate(impersonate),
                no_cache=_resolve_no_cache(no_cache),
            )
            return
        _run_gflight_path(
            legs=legs,
            opts=opts,
            top_n=page_size,
            json_out=json_out,
            run_pp=run_awards,
            sel=sel,
            matrix_url=matrix_url,
            google_url=google_url,
            pick=pick,
        )
        return

    _run_matrix_path(
        legs=legs,
        opts=opts,
        rps=_resolve_rps(rps),
        impersonate=_resolve_impersonate(impersonate),
        no_cache=_resolve_no_cache(no_cache),
        json_out=json_out,
        matrix_url=matrix_url,
        google_url=google_url,
        run_pp=run_awards,
        sel=sel,
        pick=pick,
    )


@app.command(deprecated=True)
def fare(
    origin: Annotated[
        str | None,
        typer.Argument(help="Origin IATA (comma-list ok)"),
    ] = None,
    destination: Annotated[
        str | None,
        typer.Argument(help="Destination IATA (comma-list ok)"),
    ] = None,
    dep: Annotated[str | None, typer.Option("--dep", help="YYYY-MM-DD")] = None,
    ret: Annotated[
        str | None,
        typer.Option("--return", "-r", help="YYYY-MM-DD; omit for one-way"),
    ] = None,
    slice_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--slice", "-s", help="Multi-city: 'ORIG-DEST:DATE[:r=ROUTING:e=EXT]'. Repeat."
        ),
    ] = None,
    cabin: str = "economy",
    adults: int = 1,
    children: int = 0,
    seniors: int = 0,
    youth: int = 0,
    inf_seat: Annotated[int, typer.Option("--inf-seat")] = 0,
    inf_lap: Annotated[int, typer.Option("--inf-lap")] = 0,
    routing: Annotated[
        str | None, typer.Option("--routing", help="Routing language ('LH+', 'BA AA', '[F* X F*]')")
    ] = None,
    extension: Annotated[
        str | None,
        typer.Option(
            "--extension", "--ext", help="Extension codes ('MAXCONNECT 2:00', 'MAXSTOPS 1')"
        ),
    ] = None,
    depart_times: Annotated[
        str | None,
        typer.Option(
            "--depart-times", help="Preferred outbound times-of-day (comma list: morning,evening)"
        ),
    ] = None,
    return_times: Annotated[
        str | None, typer.Option("--return-times", help="Preferred return times-of-day")
    ] = None,
    stops: Annotated[
        int | None,
        typer.Option(
            "--stops", help="Max extra stops beyond nonstop (0=nonstop only, 1=up to 1 stop, ...)"
        ),
    ] = None,
    allow_airport_changes: bool = typer.Option(
        True, "--allow-airport-changes/--no-airport-changes"
    ),
    only_available: bool = typer.Option(True, "--only-available/--include-unavailable"),
    page_size: int = typer.Option(10, "--n", "-n"),
    rps: float | None = _RPS_OPT,
    impersonate: str | None = _IMPERSONATE_OPT,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
    matrix_url: bool = typer.Option(True, "--matrix-url/--no-matrix-url"),
    google_url: bool = typer.Option(True, "--google-url/--no-google-url"),
    no_cache: bool = _NO_CACHE_OPT,
    pp: bool = typer.Option(
        False,
        "--pp",
        help="[deprecated] No-op; PP is implicit on the Matrix backend now.",
    ),
    no_pp: bool = typer.Option(
        False,
        "--no-pp",
        help="Skip PointsPath award augmentation even if tokens are present.",
    ),
    pp_only: bool = typer.Option(
        False,
        "--pp-only",
        help="Show only PointsPath award availability; skip Matrix table render.",
    ),
    pp_airlines: str | None = typer.Option(
        None,
        "--pp-airlines",
        help=(
            "CSV of PointsPath airline names (e.g. United,Delta). "
            "Default: discovered from your account's enabled airline set "
            "via /api/extension-config + /api/pricing-info."
        ),
    ),
    pp_cabin: str | None = typer.Option(
        None,
        "--pp-cabin",
        help="CSV of cabins to query (Economy,Business,First). Default: Economy,Business.",
    ),
) -> None:
    """[deprecated] Use `flight search` (or `flight search --backend matrix`)."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    err.print(
        "[yellow]`flight fare` is deprecated; use `flight search` "
        "(it auto-picks Matrix when Matrix-only flags are set).[/]",
    )
    if pp:
        err.print("[dim]Note: `--pp` is now a no-op (PP is implicit on Matrix backend).[/]")
    if slice_specs:
        legs = tuple(_parse_slice_spec(s) for s in slice_specs)
    elif origin and destination and dep:
        out_times = _parse_times(depart_times)
        ret_times = _parse_times(return_times)
        legs = (
            Leg.of(
                _parse_iata_list(origin),
                _parse_iata_list(destination),
                _parse_date(dep),
                route_language=routing,
                extension=extension,
                time_ranges=out_times,
            ),
        )
        if ret:
            legs += (
                Leg.of(
                    _parse_iata_list(destination),
                    _parse_iata_list(origin),
                    _parse_date(ret),
                    route_language=routing,
                    extension=extension,
                    time_ranges=ret_times,
                ),
            )
    else:
        err.print("[red]Specify --slice ... or origin destination --dep[/]")
        raise typer.Exit(2)

    opts = _build_options(
        cabin=cabin,
        adults=adults,
        children=children,
        seniors=seniors,
        youth=youth,
        infants_in_seat=inf_seat,
        infants_in_lap=inf_lap,
        stops=stops,
        allow_airport_changes=allow_airport_changes,
        show_only_available=only_available,
        page_size=page_size,
    )
    sel = _resolve_providers(
        providers=None,
        cash_only=False,
        awards_only=False,
        provider_opt=(),
        legacy_no_pp=no_pp,
        legacy_pp_only=pp_only,
        legacy_pp_airlines=pp_airlines,
        legacy_pp_cabin=pp_cabin,
    )
    run_pp = _should_run_awards(sel)
    _run_matrix_path(
        legs=legs,
        opts=opts,
        rps=_resolve_rps(rps),
        impersonate=_resolve_impersonate(impersonate),
        no_cache=_resolve_no_cache(no_cache),
        json_out=json_out,
        matrix_url=matrix_url,
        google_url=google_url,
        run_pp=run_pp,
        sel=sel,
    )


@app.command()
def calendar(
    origin: Annotated[
        str,
        typer.Argument(help="Origin IATA (comma-list for multi-airport)"),
    ],
    destination: Annotated[str, typer.Argument(help="Destination IATA (comma-list)")],
    start: Annotated[
        str,
        typer.Option("--start", help="Window start YYYY-MM-DD", rich_help_panel=_GROUP_ITINERARY),
    ],
    end: Annotated[
        str | None,
        typer.Option(
            "--end", help="Window end (default: start+30d)", rich_help_panel=_GROUP_ITINERARY
        ),
    ] = None,
    duration: Annotated[
        str,
        typer.Option(
            "--duration", "-d", help="Nights, '5' or '5-7'", rich_help_panel=_GROUP_ITINERARY
        ),
    ] = "5-7",
    one_way: bool = typer.Option(False, "--one-way", rich_help_panel=_GROUP_ITINERARY),
    cabin: str = typer.Option("economy", "--cabin", rich_help_panel=_GROUP_ITINERARY),
    adults: int = typer.Option(1, "--adults", rich_help_panel=_GROUP_ITINERARY),
    children: int = typer.Option(0, "--children", rich_help_panel=_GROUP_ITINERARY),
    seniors: int = typer.Option(0, "--seniors", rich_help_panel=_GROUP_ITINERARY),
    youth: int = typer.Option(0, "--youth", rich_help_panel=_GROUP_ITINERARY),
    routing: str | None = typer.Option(None, "--routing", rich_help_panel=_GROUP_FILTERING),
    extension: str | None = typer.Option(
        None, "--extension", "--ext", rich_help_panel=_GROUP_FILTERING
    ),
    routing_return: str | None = typer.Option(
        None, "--routing-ret", rich_help_panel=_GROUP_FILTERING
    ),
    extension_return: str | None = typer.Option(
        None, "--ext-ret", rich_help_panel=_GROUP_FILTERING
    ),
    depart_times: str | None = typer.Option(
        None, "--depart-times", rich_help_panel=_GROUP_FILTERING
    ),
    return_times: str | None = typer.Option(
        None, "--return-times", rich_help_panel=_GROUP_FILTERING
    ),
    stops: int | None = typer.Option(None, "--stops", rich_help_panel=_GROUP_ITINERARY),
    allow_airport_changes: bool = typer.Option(
        True,
        "--allow-airport-changes/--no-airport-changes",
        rich_help_panel=_GROUP_FILTERING,
    ),
    only_available: bool = typer.Option(
        True,
        "--only-available/--include-unavailable",
        rich_help_panel=_GROUP_FILTERING,
    ),
    rps: float | None = _RPS_OPT,
    impersonate: str | None = _IMPERSONATE_OPT,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
    matrix_url: bool = typer.Option(
        True,
        "--matrix-url/--no-matrix-url",
        help=_MATRIX_URL_HELP,
        rich_help_panel=_GROUP_OUTPUT,
    ),
    google_url: bool = typer.Option(
        False,
        "--google-url/--no-google-url",
        help=_GOOGLE_URL_HELP + " (calendar mode emits the search URL only.)",
        rich_help_panel=_GROUP_OUTPUT,
    ),
    no_cache: bool = _NO_CACHE_OPT,
    fast: bool = typer.Option(
        False,
        "--fast/--enrich",
        "--no-enrich/--no-fast",
        help="Skip the Matrix enrichment: show only the fast Google Flights "
        "date-grid (one-way, single-airport, Tier-1 filters) instead of also "
        "running the authoritative Matrix calendar.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    max_per_query: int = typer.Option(
        1,
        "--max-per-query",
        help=(
            "Multi-airport calendar: max destinations per Matrix request. 1 "
            "(default) queries each destination separately for complete results; "
            "higher is fewer/faster requests but Matrix may under-report (incomplete)."
        ),
        rich_help_panel=_GROUP_BACKEND,
    ),
    max_concurrency: int = typer.Option(
        12,
        "--max-concurrency",
        help="Max concurrent Matrix requests in the multi-airport calendar fan-out.",
        rich_help_panel=_GROUP_BACKEND,
    ),
) -> None:
    """Lowest-fare grid across a date window. Default round-trip; --one-way to flip."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    origins = _parse_iata_list(origin)
    dests = _parse_iata_list(destination)
    sd = _parse_date(start)
    ed = _parse_date(end) if end else sd + timedelta(days=30)
    dmin, dmax = _parse_duration(duration)
    out_times = _parse_times(depart_times)
    ret_times = _parse_times(return_times)

    out_leg = Leg.of(
        origins, dests, route_language=routing, extension=extension, time_ranges=out_times
    )
    legs = (out_leg,)
    if not one_way:
        legs += (
            Leg.of(
                dests,
                origins,
                route_language=routing_return or routing,
                extension=extension_return or extension,
                time_ranges=ret_times,
            ),
        )

    opts = _build_options(
        cabin=cabin,
        adults=adults,
        children=children,
        seniors=seniors,
        youth=youth,
        infants_in_seat=0,
        infants_in_lap=0,
        stops=stops,
        allow_airport_changes=allow_airport_changes,
        show_only_available=only_available,
    )
    window = CalendarWindow(start=sd, end=ed, duration_min=dmin, duration_max=dmax)
    search = CalendarSearch(legs=legs, options=opts, window=window)

    # Fast layer: the GF native date-grid (~1s, throttle-friendly, dodges Matrix's
    # compute-budget under-reporting) for one-way / single-airport / Tier-1-only
    # windows. Paint it first, then enrich with the authoritative Matrix calendar
    # (full per-duration grid). `--fast` stops after the grid. The cheap pre-check
    # avoids importing the fli-heavy module for the Matrix-only cases.
    if not json_out and one_way and len(origins) == 1 and len(dests) == 1:
        from ._gf_dategrid import grid_can_serve  # noqa: PLC0415

        if grid_can_serve(search):
            if not fast:
                # Progressive weave: dispatch the GF date-grid and the Matrix
                # calendar concurrently, paint the grid first (~1s), then the
                # authoritative Matrix calendar — total ≈ Matrix alone.
                _run_calendar_enriched(
                    search,
                    origins=origins,
                    dests=dests,
                    sd=sd,
                    ed=ed,
                    dmin=dmin,
                    dmax=dmax,
                    rps=_resolve_rps(rps),
                    impersonate=_resolve_impersonate(impersonate),
                    no_cache=_resolve_no_cache(no_cache),
                    matrix_url=matrix_url,
                    google_url=google_url,
                )
                return
            # --fast: Google Flights date-grid only (no Matrix).
            from ._gf_dategrid import date_grid  # noqa: PLC0415
            from ._gflight_ids import GfThrottledError  # noqa: PLC0415

            grid: dict[str, float] = {}
            try:
                grid = date_grid(search)
            except GfThrottledError:
                console.print("[dim]Google Flights rate-limited — Matrix only.[/]")
            except Exception as e:  # noqa: BLE001 — GF is the optional fast layer; Matrix still runs
                err.print(f"[yellow]Google Flights date-grid failed:[/] {e}")
            if grid:
                _render_date_grid(grid, origin=origins, destination=dests, sd=sd, ed=ed)
                _emit_urls(search, matrix_url=matrix_url, google_url=google_url)
            else:
                console.print("[yellow]No Google Flights grid; drop --fast for Matrix.[/]")
            return

    # Matrix (authoritative; also the only path for round-trip, multi-airport,
    # Tier-2/3 routing, or when the grid was empty/throttled).
    # CalendarSearch → CalendarResult by client._parse_response dispatch.
    # On a multi-airport brownout, _run_calendar splits per-destination + merges.
    res, n_split = _run_calendar(
        search,
        rps=_resolve_rps(rps),
        impersonate=_resolve_impersonate(impersonate),
        no_cache=_resolve_no_cache(no_cache),
        max_per_query=max_per_query,
        max_concurrency=max_concurrency,
    )
    if json_out:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    if n_split:
        console.print(
            f"[dim]Queried {n_split} destinations separately and merged — Matrix "
            f"under-reports the combined multi-airport calendar grid.[/]"
        )
    _render_calendar(res, dmin=dmin, dmax=dmax, origin=origins, destination=dests, sd=sd, ed=ed)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


@app.command()
def detail(
    origin: Annotated[str, typer.Argument()],
    destination: Annotated[str, typer.Argument()],
    dep: Annotated[
        str,
        typer.Option("--dep", help="Departure YYYY-MM-DD", rich_help_panel=_GROUP_ITINERARY),
    ],
    ret: Annotated[
        str | None,
        typer.Option("--return", "-r", rich_help_panel=_GROUP_ITINERARY),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="Original calendar window start (defaults to --dep)",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(
            "--end",
            help="Original calendar window end (defaults to start+30d)",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    duration: Annotated[
        str,
        typer.Option(
            "--duration",
            "-d",
            help="Original duration range",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = "5-7",
    cabin: str = typer.Option("economy", "--cabin", rich_help_panel=_GROUP_ITINERARY),
    adults: int = typer.Option(1, "--adults", rich_help_panel=_GROUP_ITINERARY),
    children: int = typer.Option(0, "--children", rich_help_panel=_GROUP_ITINERARY),
    seniors: int = typer.Option(0, "--seniors", rich_help_panel=_GROUP_ITINERARY),
    youth: int = typer.Option(0, "--youth", rich_help_panel=_GROUP_ITINERARY),
    routing: str | None = typer.Option(None, "--routing", rich_help_panel=_GROUP_FILTERING),
    extension: str | None = typer.Option(
        None, "--extension", "--ext", rich_help_panel=_GROUP_FILTERING
    ),
    routing_return: str | None = typer.Option(
        None, "--routing-ret", rich_help_panel=_GROUP_FILTERING
    ),
    extension_return: str | None = typer.Option(
        None, "--ext-ret", rich_help_panel=_GROUP_FILTERING
    ),
    stops: int | None = typer.Option(None, "--stops", rich_help_panel=_GROUP_ITINERARY),
    allow_airport_changes: bool = typer.Option(
        True,
        "--allow-airport-changes/--no-airport-changes",
        rich_help_panel=_GROUP_FILTERING,
    ),
    rps: float | None = _RPS_OPT,
    impersonate: str | None = _IMPERSONATE_OPT,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
    matrix_url: bool = typer.Option(
        True,
        "--matrix-url/--no-matrix-url",
        help=_MATRIX_URL_HELP,
        rich_help_panel=_GROUP_OUTPUT,
    ),
    google_url: bool = typer.Option(
        True,
        "--google-url/--no-google-url",
        help=_GOOGLE_URL_HELP,
        rich_help_panel=_GROUP_OUTPUT,
    ),
    no_cache: bool = _NO_CACHE_OPT,
) -> None:
    """Phase-2 of the calendar flow: full itineraries for a picked date."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    origins = _parse_iata_list(origin)
    dests = _parse_iata_list(destination)
    dep_d = _parse_date(dep)
    ret_d = _parse_date(ret) if ret else None
    sd = _parse_date(start) if start else dep_d
    ed = _parse_date(end) if end else sd + timedelta(days=30)
    dmin, dmax = _parse_duration(duration)

    legs = (Leg.of(origins, dests, dep_d, route_language=routing, extension=extension),)
    if ret_d:
        legs += (
            Leg.of(
                dests,
                origins,
                ret_d,
                route_language=routing_return or routing,
                extension=extension_return or extension,
            ),
        )

    opts = _build_options(
        cabin=cabin,
        adults=adults,
        children=children,
        seniors=seniors,
        youth=youth,
        infants_in_seat=0,
        infants_in_lap=0,
        stops=stops,
        allow_airport_changes=allow_airport_changes,
        show_only_available=True,
    )
    window = CalendarWindow(start=sd, end=ed, duration_min=dmin, duration_max=dmax)
    search = CalendarFollowup(legs=legs, options=opts, window=window)
    # CalendarFollowup → SearchResult by client._parse_response dispatch.
    res = cast(
        "SearchResult",
        _run(
            search,
            _resolve_rps(rps),
            _resolve_impersonate(impersonate),
            _resolve_no_cache(no_cache),
        ),
    )
    if json_out:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    _render_search(res)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url, result=res)


@app.command(deprecated=True)
def gflight(
    origin: Annotated[str, typer.Argument()],
    destination: Annotated[str, typer.Argument()],
    dep: Annotated[str, typer.Option("--dep")],
    ret: Annotated[str | None, typer.Option("--return", "-r")] = None,
    cabin: str = "economy",
    adults: int = 1,
    children: int = 0,
    top_n: Annotated[int, typer.Option("--n", "-n")] = 5,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
) -> None:
    """[deprecated] Use `flight search --backend gflight` (or just `flight search`)."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    err.print(
        "[yellow]`flight gflight` is deprecated; use `flight search` "
        "(or `flight search --backend gflight` to force).[/]",
    )
    legs = (Leg.of(origin, destination, _parse_date(dep)),)
    if ret:
        legs += (Leg.of(destination, origin, _parse_date(ret)),)
    opts = _build_options(
        cabin=cabin,
        adults=adults,
        children=children,
        seniors=0,
        youth=0,
        infants_in_seat=0,
        infants_in_lap=0,
        stops=None,
        allow_airport_changes=True,
        show_only_available=True,
    )
    _run_gflight_path(legs=legs, opts=opts, top_n=top_n, json_out=json_out)


@app.command()
def airport(
    query: Annotated[str, typer.Argument(help="Partial name or IATA")],
    impersonate: str | None = _IMPERSONATE_OPT,
) -> None:
    """Look up airports by partial name or IATA code."""
    resolved_impersonate = _resolve_impersonate(impersonate)

    async def go() -> list[Location]:
        async with MatrixClient(impersonate=resolved_impersonate) as c:
            return await c.airports(query)

    locs = anyio.run(go)
    if not locs:
        console.print("[yellow]No matches.[/]")
        return
    t = Table(title=f"Airport lookup: {query!r}", show_header=True, header_style="bold blue")
    t.add_column("code")
    t.add_column("name")
    t.add_column("city")
    t.add_column("tz")
    for loc in locs:
        t.add_row(loc.code, loc.display_name or "", loc.city_name or "", loc.timezone or "")
    console.print(t)


@app.command()
def seatmap(
    origin: Annotated[str, typer.Argument(help="Origin IATA")],
    destination: Annotated[str, typer.Argument(help="Destination IATA")],
    flight: Annotated[
        str,
        typer.Argument(help="Flight number, bare or IATA-prefixed (e.g. AA100)"),
    ],
    date: Annotated[str, typer.Option("--date", help="Flight date YYYY-MM-DD")],
    aircraft: Annotated[
        str | None,
        typer.Option("--aircraft", help="Aircraft type (e.g. 'Airbus A330') — improves match"),
    ] = None,
    carrier: Annotated[
        str | None,
        typer.Option(
            "--carrier",
            help="Carrier IATA. Inferred from flight# if it starts with letters.",
        ),
    ] = None,
    fetch: Annotated[
        bool,
        typer.Option(
            "--fetch/--no-fetch",
            help="Resolve to seatmaps.com URL via one HTTP GET (default).",
        ),
    ] = True,
) -> None:
    """Get a seatmaps.com URL for a specific flight.

    Mirrors what the Legrooms+ Chrome extension does on click. Without
    --no-fetch, makes one GET to travelarrow.io/api/s and prints the
    seatmaps.com URL it resolves to. With --no-fetch, just prints the
    travelarrow URL itself (cheap, but you'll get JSON not a seatmap).
    """
    from .seatmap import fetch_seatmap_url, seatmap_api_url  # noqa: PLC0415

    flight = flight.upper()
    if carrier is None:
        prefix = "".join(c for c in flight[:3] if c.isalpha())
        if not prefix:
            err.print("[red]Cannot infer carrier from flight number — pass --carrier IATA.[/]")
            raise typer.Exit(2)
        carrier = prefix

    parsed = _parse_date(date)
    api_url = seatmap_api_url(
        origin=origin,
        dest=destination,
        flight_number=flight,
        carrier=carrier,
        date=parsed,
        aircraft=aircraft,
    )
    if not fetch:
        console.print(api_url)
        return
    try:
        url = fetch_seatmap_url(
            origin=origin,
            dest=destination,
            flight_number=flight,
            carrier=carrier,
            date=parsed,
            aircraft=aircraft,
        )
    except Exception as e:
        err.print(f"[red]Seatmap lookup failed:[/] {e}")
        console.print(f"[dim]API URL:[/] {api_url}")
        raise typer.Exit(1) from e
    if url is None:
        err.print("[yellow]No seatmap on file for this flight/aircraft.[/]")
        console.print(f"[dim]API URL:[/] {api_url}")
        raise typer.Exit(1)
    console.print(url)


if __name__ == "__main__":
    app()
