"""CLI: thin shells over the domain types. Each command parses args, builds
a Search variant, hands it to MatrixClient.execute(), and renders.

Five commands:
  flight fare      — specific-date search
  flight calendar  — lowest-fare grid
  flight detail    — phase-2 itineraries for a date picked from the grid
  flight gflight   — Google Flights handoff via fli
  flight airport   — IATA autocomplete
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast

import anyio
import typer
from rich.console import Console
from rich.table import Table

from .client import MatrixApiError, MatrixClient
from .domain import (
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
)
from .links import google_flights_url, matrix_deep_link
from .log import configure as configure_logging
from .pp.cli import LegQuery, auth_app, run_pp_for_search

if TYPE_CHECKING:
    from .models import CalendarResult, Location, SearchResult, Slice

# Tuple-length sentinels for `--slice` parser (`ORIGIN-DEST:DATE[:r=...:e=...]`).
_SLICE_MIN_PARTS = 2
_SLICE_MAX_PARTS = 3

# Matrix returns prices as 'USD877.00' (ISO-4217 prefix + decimal). We split
# the prefix off for rendering so tables can show the currency once in the
# title and keep cells uncluttered.
_PRICE_RE = re.compile(r"^([A-Z]{3})(.+)$")


def _split_price(s: str | None) -> tuple[str, str]:
    """Return (currency, amount). ('', s) if no recognizable prefix."""
    if not s:
        return "", s or ""
    m = _PRICE_RE.match(s)
    return (m.group(1), m.group(2)) if m else ("", s)


def _amount(s: str | None) -> str:
    """Strip the currency prefix; pass-through for placeholders like '—'."""
    return _split_price(s)[1] if s else "—"


app = typer.Typer(
    add_completion=False, rich_markup_mode="rich", help="CLI for ITA Matrix's Alkali backend."
)
app.add_typer(auth_app, name="auth")
console = Console()
err = Console(stderr=True)


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


# ─────────────────────────── argument parsers ──────────────────────────────


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        err.print(f"[red]bad date {s!r}; use YYYY-MM-DD[/]")
        raise typer.Exit(2) from e


def _parse_duration(s: str) -> tuple[int, int]:
    s = s.replace("..", "-").strip()
    if "-" in s:
        lo, hi = s.split("-", 1)
        return int(lo), int(hi)
    n = int(s)
    return n, n


def _parse_iata_list(s: str) -> tuple[str, ...]:
    return tuple(a.strip().upper() for a in s.split(",") if a.strip())


def _parse_times(s: str | None) -> tuple[TimeOfDay, ...]:
    if not s:
        return ()
    out: list[TimeOfDay] = []
    aliases = {
        "early": TimeOfDay.EARLY_MORNING,
        "early_morning": TimeOfDay.EARLY_MORNING,
        "morning": TimeOfDay.MORNING,
        "midday": TimeOfDay.MIDDAY,
        "noon": TimeOfDay.MIDDAY,
        "afternoon": TimeOfDay.AFTERNOON,
        "evening": TimeOfDay.EVENING,
        "night": TimeOfDay.NIGHT,
    }
    for raw in s.split(","):
        key = raw.strip().lower().replace("-", "_")
        if key not in aliases:
            err.print(
                f"[red]bad time-of-day {raw!r}; choose: "
                f"early,morning,midday,afternoon,evening,night[/]"
            )
            raise typer.Exit(2)
        out.append(aliases[key])
    return tuple(out)


def _resolve_cabin(name: str) -> Cabin:
    norm = name.lower().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {
        "coach": Cabin.COACH,
        "economy": Cabin.COACH,
        "y": Cabin.COACH,
        "premiumcoach": Cabin.PREMIUM_COACH,
        "premiumeconomy": Cabin.PREMIUM_COACH,
        "premium": Cabin.PREMIUM_COACH,
        "w": Cabin.PREMIUM_COACH,
        "business": Cabin.BUSINESS,
        "j": Cabin.BUSINESS,
        "first": Cabin.FIRST,
        "f": Cabin.FIRST,
    }
    if norm in aliases:
        return aliases[norm]
    err.print(f"[red]Unknown cabin {name!r}; choose: economy, premium, business, first[/]")
    raise typer.Exit(2)


def _build_options(
    *,
    cabin: str,
    adults: int,
    children: int,
    seniors: int,
    youth: int,
    infants_in_seat: int,
    infants_in_lap: int,
    stops: int | None,
    allow_airport_changes: bool,
    show_only_available: bool,
    page_size: int = 25,
) -> SearchOptions:
    return SearchOptions(
        cabin=_resolve_cabin(cabin),
        pax=Pax(
            adults=adults,
            children=children,
            seniors=seniors,
            youth=youth,
            infants_in_seat=infants_in_seat,
            infants_in_lap=infants_in_lap,
        ),
        max_extra_stops=stops,
        allow_airport_changes=allow_airport_changes,
        show_only_available=show_only_available,
        page_size=page_size,
    )


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


def _emit_urls(search: Search, *, matrix_url: bool, google_url: bool) -> None:
    if matrix_url:
        console.print()
        console.print("[dim]Matrix deep-link:[/]")
        console.print(f"  [link]{matrix_deep_link(search)}[/]")
    if google_url:
        # `google_flights_url` builds protobuf-encoded tfs= URLs via fast_flights.
        # That library has no documented exception surface — catch broadly so a
        # missing IATA or unsupported variant degrades the URL line, not the run.
        try:
            console.print("[dim]Google Flights (tfs= structured):[/]")
            console.print(f"  [link]{google_flights_url(search)}[/]")
        except Exception as e:  # noqa: BLE001 - third-party undocumented errors; non-fatal fallback
            console.print(f"[dim]Google Flights link: {e}[/]")


# ─────────────────────────── result renderers ──────────────────────────────


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

        def _fmt(s: Slice) -> str:
            dep = s.departure or ""
            arr = s.arrival or ""
            dur_min = s.duration or 0
            dur = f"{dur_min // 60}h{dur_min % 60:02d}m" if dur_min else ""
            o = (s.origin.code if s.origin else None) or "?"
            d = (s.destination.code if s.destination else None) or "?"
            return f"{o}→{d} {'/'.join(s.flights) or '?'} {dep[:16]}→{arr[:16]} {dur}"

        out = _fmt(slcs[0]) if slcs else "—"
        ret = _fmt(slcs[1]) if len(slcs) > 1 else "—"
        st.add_row(str(i), _amount(it.price), it_carriers or "?", out, ret)
    console.print(st)


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


# ─────────────────────────────── commands ──────────────────────────────────

# Common-args helpers — these reduce repetition across commands.
_RPS_OPT = typer.Option(1.0, help="Requests per second")
_IMPERSONATE_OPT = typer.Option("chrome", help="curl_cffi profile")


@app.command()
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
    rps: float = _RPS_OPT,
    impersonate: str = _IMPERSONATE_OPT,
    json_out: bool = typer.Option(False, "--json"),
    matrix_url: bool = typer.Option(True, "--matrix-url/--no-matrix-url"),
    google_url: bool = typer.Option(True, "--google-url/--no-google-url"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    pp: bool = typer.Option(
        False,
        "--pp",
        help="Augment results with PointsPath award prices (requires `auth pp login`).",
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
    """Specific-date search. One leg = one-way, two = round-trip, N = multi-city."""
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
    search = SpecificDateSearch(legs=legs, options=opts)
    # SpecificDateSearch → SearchResult by client._parse_response dispatch.
    res = cast("SearchResult", _run(search, rps, impersonate, no_cache))
    if json_out and not pp:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    if not pp_only:
        _render_search(res)
    if pp:
        # Build one PP query per Matrix leg. PP's airline-search is per-direction,
        # so a round-trip → 2 queries, multi-city → N queries. Each LegQuery
        # carries its slice_index so the matcher knows which Itinerary slice to
        # join against.
        pp_legs: list[LegQuery] = []
        for i, leg in enumerate(legs):
            if not leg.date or not leg.origins or not leg.destinations:
                continue  # shouldn't happen for SpecificDateSearch; defensive
            label_kind = (
                "outbound" if i == 0 and len(legs) > 1
                else "return" if i == 1 and len(legs) == 2
                else f"leg {i + 1}" if len(legs) > 2
                else "one-way"
            )
            pp_legs.append(LegQuery(
                origin=leg.origins[0],
                destination=leg.destinations[0],
                date=leg.date.isoformat(),
                slice_index=i,
                label=f"{label_kind} {leg.origins[0]}→{leg.destinations[0]} {leg.date.isoformat()}",
            ))
        run_pp_for_search(
            res,
            legs=pp_legs,
            num_passengers=adults + children + seniors + youth,
            airlines=pp_airlines,
            cabins=pp_cabin,
            pp_only=pp_only,
            json_out=json_out,
        )
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


def _parse_slice_spec(s: str) -> Leg:
    """Parse 'JFK-LHR:2026-08-15[:r=LH+:e=MAXCONNECT 2:00]'."""
    parts = s.split(":", 2)
    if len(parts) < _SLICE_MIN_PARTS:
        raise typer.BadParameter(f"slice {s!r} should be ORIGIN-DEST:DATE[:r=...:e=...]")
    od, dt = parts[0], parts[1]
    o, d = od.split("-", 1)
    routing = extension = None
    if len(parts) == _SLICE_MAX_PARTS:
        for chunk in re.split(r":(?=[re]=)", parts[2]):
            if chunk.startswith("r="):
                routing = chunk[2:]
            elif chunk.startswith("e="):
                extension = chunk[2:]
    return Leg.of(o, d, _parse_date(dt), route_language=routing, extension=extension)


@app.command()
def calendar(
    origin: Annotated[
        str,
        typer.Argument(help="Origin IATA (comma-list for multi-airport)"),
    ],
    destination: Annotated[str, typer.Argument(help="Destination IATA (comma-list)")],
    start: Annotated[str, typer.Option("--start", help="Window start YYYY-MM-DD")],
    end: Annotated[
        str | None,
        typer.Option("--end", help="Window end (default: start+30d)"),
    ] = None,
    duration: Annotated[
        str,
        typer.Option("--duration", "-d", help="Nights, '5' or '5-7'"),
    ] = "5-7",
    one_way: bool = typer.Option(False, "--one-way"),
    cabin: str = "economy",
    adults: int = 1,
    children: int = 0,
    seniors: int = 0,
    youth: int = 0,
    routing: str | None = typer.Option(None, "--routing"),
    extension: str | None = typer.Option(None, "--extension", "--ext"),
    routing_return: str | None = typer.Option(None, "--routing-ret"),
    extension_return: str | None = typer.Option(None, "--ext-ret"),
    depart_times: str | None = typer.Option(None, "--depart-times"),
    return_times: str | None = typer.Option(None, "--return-times"),
    stops: int | None = typer.Option(None, "--stops"),
    allow_airport_changes: bool = typer.Option(
        True, "--allow-airport-changes/--no-airport-changes"
    ),
    only_available: bool = typer.Option(True, "--only-available/--include-unavailable"),
    rps: float = _RPS_OPT,
    impersonate: str = _IMPERSONATE_OPT,
    json_out: bool = typer.Option(False, "--json"),
    matrix_url: bool = typer.Option(True, "--matrix-url/--no-matrix-url"),
    google_url: bool = typer.Option(False, "--google-url/--no-google-url"),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """Lowest-fare grid across a date window. Default round-trip; --one-way to flip."""
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
    # CalendarSearch → CalendarResult by client._parse_response dispatch.
    res = cast("CalendarResult", _run(search, rps, impersonate, no_cache))
    if json_out:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    _render_calendar(res, dmin=dmin, dmax=dmax, origin=origins, destination=dests, sd=sd, ed=ed)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


@app.command()
def detail(
    origin: Annotated[str, typer.Argument()],
    destination: Annotated[str, typer.Argument()],
    dep: Annotated[str, typer.Option("--dep", help="Departure YYYY-MM-DD")],
    ret: Annotated[str | None, typer.Option("--return", "-r")] = None,
    start: Annotated[
        str | None,
        typer.Option("--start", help="Original calendar window start (defaults to --dep)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="Original calendar window end (defaults to start+30d)"),
    ] = None,
    duration: Annotated[
        str, typer.Option("--duration", "-d", help="Original duration range")
    ] = "5-7",
    cabin: str = "economy",
    adults: int = 1,
    children: int = 0,
    seniors: int = 0,
    youth: int = 0,
    routing: str | None = typer.Option(None, "--routing"),
    extension: str | None = typer.Option(None, "--extension", "--ext"),
    routing_return: str | None = typer.Option(None, "--routing-ret"),
    extension_return: str | None = typer.Option(None, "--ext-ret"),
    stops: int | None = typer.Option(None, "--stops"),
    allow_airport_changes: bool = typer.Option(
        True, "--allow-airport-changes/--no-airport-changes"
    ),
    rps: float = _RPS_OPT,
    impersonate: str = _IMPERSONATE_OPT,
    json_out: bool = typer.Option(False, "--json"),
    matrix_url: bool = typer.Option(True, "--matrix-url/--no-matrix-url"),
    google_url: bool = typer.Option(True, "--google-url/--no-google-url"),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """Phase-2 of the calendar flow: full itineraries for a picked date."""
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
    res = cast("SearchResult", _run(search, rps, impersonate, no_cache))
    if json_out:
        sys.stdout.write(json.dumps(res.raw, indent=2))
        return
    _render_search(res)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


@app.command()
def gflight(
    origin: Annotated[str, typer.Argument()],
    destination: Annotated[str, typer.Argument()],
    dep: Annotated[str, typer.Option("--dep")],
    ret: Annotated[str | None, typer.Option("--return", "-r")] = None,
    cabin: str = "economy",
    adults: int = 1,
    children: int = 0,
    top_n: Annotated[int, typer.Option("--n", "-n")] = 5,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Query Google Flights for a city-pair you discovered with flight calendar."""

    # the CLI shouldn't pay that startup cost.
    from .fli_bridge import run_gflight_search  # noqa: PLC0415

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
    search = SpecificDateSearch(legs=legs, options=opts)

    # fli wraps selenium/selectolax and has no documented exception surface;
    # catching broadly here translates any underlying failure into a clean CLI
    # exit instead of an opaque traceback.
    try:
        # fli has no type stubs; treat its return as opaque at this boundary
        # and use duck-typed attribute access below.
        results: list[Any] = run_gflight_search(search, top_n=top_n)
    except Exception as e:
        err.print(f"[red]Google Flights query failed:[/] {e}")
        raise typer.Exit(1) from e

    if not results:
        console.print("[yellow]Google Flights returned no results.[/]")
        return

    # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType,
    #                 reportUnknownArgumentType, reportUnknownParameterType]
    # fli/fast_flights have no type stubs; results are duck-typed pydantic
    # models. Suppressing the noisy unknown-type chatter for this rendering
    # block keeps the boundary localized.
    if json_out:
        out: list[Any] = []
        for r in results:
            if isinstance(r, tuple):
                out.append([fr.model_dump(mode="json") for fr in r])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            else:
                out.append(r.model_dump(mode="json"))
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return

    t = Table(
        title=f"Google Flights · {origin.upper()}→{destination.upper()}"
        + (" + return" if ret else ""),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("price", justify="right")
    t.add_column("stops", justify="right")
    t.add_column("duration")
    t.add_column("legs")
    for i, r in enumerate(results[:top_n], 1):
        items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
        for j, fr in enumerate(items):
            label = f"{i}{'a' if j == 0 else 'b'}" if len(items) > 1 else str(i)
            legs_str = " → ".join(
                f"{getattr(leg.airline, 'name', leg.airline)} {getattr(leg, 'flight_number', '?')}"
                for leg in fr.legs
            )
            mins = fr.duration
            dur = f"{mins // 60}h{mins % 60:02d}m"
            t.add_row(label, f"{fr.currency or 'USD'}{fr.price:.2f}", str(fr.stops), dur, legs_str)
    console.print(t)


@app.command()
def airport(
    query: Annotated[str, typer.Argument(help="Partial name or IATA")],
    impersonate: str = _IMPERSONATE_OPT,
) -> None:
    """Look up airports by partial name or IATA code."""

    async def go() -> list[Location]:
        async with MatrixClient(impersonate=impersonate) as c:
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


if __name__ == "__main__":
    app()
