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
import re
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast

import anyio
import typer
from rich.console import Console
from rich.table import Table

from . import _config
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
from .pp.auth import load_tokens
from .pp.cli import auth_app, run_pp_for_search
from .providers.base import LegQuery

if TYPE_CHECKING:
    from .models import CalendarResult, LegInfo, Location, SearchResult, Slice

# Tuple-length sentinels for `--slice` parser (`ORIGIN-DEST:DATE[:r=...:e=...]`).
_SLICE_MIN_PARTS = 2
_SLICE_MAX_PARTS = 3
_ROUND_TRIP_LEGS = 2  # 2 legs = round-trip; 1 = one-way; >2 = multi-city

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


# ─────────────────────────── backend dispatch ──────────────────────────────

BACKEND_AUTO = "auto"
BACKEND_MATRIX = "matrix"
BACKEND_GFLIGHT = "gflight"
_VALID_BACKENDS = (BACKEND_AUTO, BACKEND_MATRIX, BACKEND_GFLIGHT)


def _pick_backend(
    *,
    backend: str,
    routing: str | None,
    extension: str | None,
    slice_specs: list[str] | None,
    depart_times: str | None,
    return_times: str | None,
    seniors: int,
    youth: int,
    inf_seat: int,
    inf_lap: int,
) -> str:
    """Resolve --backend to a concrete backend.

    auto: matrix iff a Matrix-only flag is set, else gflight.
    Matrix-only set: --routing/--extension/--slice/--depart-times/--return-times,
    any pax type beyond adults+children. PP overlay rides both backends now —
    plain `--pp-only` stays on gflight for speed + ULCC inventory.

    Explicit --backend matrix: matrix. --backend gflight: gflight, unless a
    Matrix-only flag is also set (error — the request is inexpressible on fli)."""
    matrix_only = bool(routing or extension or slice_specs or depart_times or return_times) or (
        seniors > 0 or youth > 0 or inf_seat > 0 or inf_lap > 0
    )
    if backend == BACKEND_AUTO:
        return BACKEND_MATRIX if matrix_only else BACKEND_GFLIGHT
    if backend == BACKEND_GFLIGHT and matrix_only:
        raise typer.BadParameter(
            "--backend gflight is incompatible with Matrix-only flags "
            "(--routing/--extension/--slice/--depart-times/--return-times/"
            "extra pax types). Drop them, or use --backend matrix.",
        )
    if backend not in _VALID_BACKENDS:
        raise typer.BadParameter(f"--backend must be one of {_VALID_BACKENDS}; got {backend!r}")
    return backend


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


class ProviderSelection:
    """Resolved provider selection: what runs, which subset, with what options.

    `provider_filter` is a tuple of provider names to use (None = all enabled).
    `cash_only` skips every award provider. `awards_only` suppresses the cash
    table render. `provider_opts` is `{provider_name: {key: value}}` — e.g.
    `{"pp": {"airlines": ["United", "Delta"], "cabins": ["Economy"]}}`.
    """

    __slots__ = ("awards_only", "cash_only", "provider_filter", "provider_opts")

    def __init__(
        self,
        *,
        provider_filter: tuple[str, ...] | None,
        cash_only: bool,
        awards_only: bool,
        provider_opts: dict[str, dict[str, Any]],
    ) -> None:
        self.provider_filter = provider_filter
        self.cash_only = cash_only
        self.awards_only = awards_only
        self.provider_opts = provider_opts

    def pp_airlines(self) -> str | None:
        """Backward-compat shim: PP's `airlines` as CSV (None = use default)."""
        v: Any = self.provider_opts.get("pp", {}).get("airlines")
        if v is None:
            return None
        if isinstance(v, list):
            return ",".join(str(x) for x in cast("list[Any]", v))
        return str(v)

    def pp_cabins(self) -> str | None:
        """Backward-compat shim: PP's `cabins` as CSV (None = use default)."""
        v: Any = self.provider_opts.get("pp", {}).get("cabins")
        if v is None:
            return None
        if isinstance(v, list):
            return ",".join(str(x) for x in cast("list[Any]", v))
        return str(v)


def _resolve_providers(  # noqa: PLR0912 — single-purpose validator + merge; splitting hurts readability
    *,
    providers: str | None,
    cash_only: bool,
    awards_only: bool,
    provider_opt: tuple[str, ...],
    # deprecated aliases — forwarded into the new shape:
    legacy_no_pp: bool = False,
    legacy_pp_only: bool = False,
    legacy_pp_airlines: str | None = None,
    legacy_pp_cabin: str | None = None,
) -> ProviderSelection:
    """Resolve the new + deprecated provider flags into one ProviderSelection.

    Precedence for per-provider options: config.toml < --provider-opt CLI.
    Deprecated --pp-airlines / --pp-cabin map onto the --provider-opt path
    so legacy invocations land in the same downstream shape.
    """
    # Conflict checks across the new surface.
    if cash_only and awards_only:
        err.print("[red]--cash-only and --awards-only are mutually exclusive.[/]")
        raise typer.Exit(2)
    # Conflict checks across the old + new surface.
    new_surface_set = cash_only or awards_only or providers is not None
    if legacy_no_pp and new_surface_set:
        err.print(
            "[red]--no-pp conflicts with --cash-only/--awards-only/--providers; use one.[/]",
        )
        raise typer.Exit(2)
    if legacy_pp_only and new_surface_set:
        err.print(
            "[red]--pp-only conflicts with --cash-only/--awards-only/--providers; use one.[/]",
        )
        raise typer.Exit(2)
    if legacy_no_pp and legacy_pp_only:
        err.print("[red]--no-pp and --pp-only are mutually exclusive.[/]")
        raise typer.Exit(2)

    # Forward legacy intent.
    if legacy_no_pp:
        cash_only = True
    if legacy_pp_only:
        awards_only = True

    # --providers parsing.
    provider_filter: tuple[str, ...] | None = None
    if providers is not None:
        provider_filter = tuple(p.strip() for p in providers.split(",") if p.strip())
        if not provider_filter:
            err.print("[red]--providers cannot be empty.[/]")
            raise typer.Exit(2)

    # Build provider_opts: config.toml < --provider-opt CLI.
    try:
        config = _config.load()
    except (OSError, ValueError) as e:
        err.print(f"[red]Failed to load ~/.config/flight-cli/config.toml: {e}[/]")
        raise typer.Exit(2) from e
    base_opts: dict[str, dict[str, Any]] = {}
    providers_section: Any = config.get("providers", {})
    if isinstance(providers_section, dict):
        for name, opts in cast("dict[str, Any]", providers_section).items():
            if isinstance(opts, dict):
                base_opts[name] = dict(cast("dict[str, Any]", opts))

    try:
        cli_opts = _config.parse_provider_opt_overrides(list(provider_opt))
    except ValueError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from e
    merged_opts = _config.merge_provider_options(base_opts, cli_opts)

    # Forward legacy --pp-airlines / --pp-cabin into the merged opts.
    # CLI --provider-opt still wins (no-op if user set both, since merged_opts
    # already has the override from cli_opts).
    pp_section: dict[str, Any] = dict(merged_opts.get("pp", {}))
    if legacy_pp_airlines is not None and "airlines" not in pp_section:
        pp_section["airlines"] = [v.strip() for v in legacy_pp_airlines.split(",") if v.strip()]
    if legacy_pp_cabin is not None and "cabins" not in pp_section:
        pp_section["cabins"] = [v.strip() for v in legacy_pp_cabin.split(",") if v.strip()]
    if pp_section:
        merged_opts["pp"] = pp_section

    return ProviderSelection(
        provider_filter=provider_filter,
        cash_only=cash_only,
        awards_only=awards_only,
        provider_opts=merged_opts,
    )


def _should_run_awards(sel: ProviderSelection) -> bool:
    """Award providers run iff (a) not --cash-only, (b) at least one is
    configured, and (c) the filter (if any) names at least one configured
    provider. Mirrors the old _should_run_pp semantics for the PP-only era;
    generalizes naturally once more providers exist.
    """
    if sel.cash_only:
        return False
    # Today we only know about PP. has_any_configured() is provider-blind by
    # construction (see providers/registry.py) so this stays correct when
    # work-2eoa adds seats.aero.
    from .providers.registry import has_any_configured  # noqa: PLC0415

    if not has_any_configured():
        if sel.awards_only:
            err.print(
                "[red]--awards-only set but no award provider is configured.[/] "
                "Run `flight auth pp login --tokens-file ...` first.",
            )
            raise typer.Exit(2)
        return False
    if sel.provider_filter is not None and "pp" not in sel.provider_filter:
        # Filter excludes PP; nothing else is configured yet.
        if sel.awards_only:
            err.print(
                f"[red]--awards-only set but --providers={sel.provider_filter} "
                "matches no configured provider.[/]",
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
            head = f"{o}→{d} {'/'.join(s.flights) or '?'} {dep[:16]}→{arr[:16]} {dur}"
            tail = _fmt_legroom_lines(s)
            return f"{head}\n{tail}" if tail else head

        out = _fmt(slcs[0]) if slcs else "—"
        ret = _fmt(slcs[1]) if len(slcs) > 1 else "—"
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
        )
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


def _run_gflight_path(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    json_out: bool,
    run_pp: bool = False,
    sel: ProviderSelection | None = None,
) -> None:
    """Google Flights path: build fli filter → query → render. Single-leg or round-trip.

    When run_pp=True, fli's results are adapted into a SearchResult shape so
    the existing PP matcher + renderer reuse cleanly. PP runs on the same
    (origin, dest, date) per leg as the matrix path.
    """
    # fli is heavy (selenium/selectolax); lazy-import so the rest of flight_cli
    # doesn't pay the startup cost when not used. `search_with_ids` wraps fli's
    # encoder + client but parses the response ourselves to capture the opaque
    # per-flight ID (data[0][17]) — that's what PP's enableGoogleFlightMatching
    # joins against to produce matchedGoogleFlightId in its response.
    from ._gflight_ids import search_with_ids  # noqa: PLC0415
    from .fli_bridge import to_fli_filter  # noqa: PLC0415

    search = SpecificDateSearch(legs=legs, options=opts)
    try:
        # Returns GFlightWithId | tuple[GFlightWithId, ...]. The .flight attribute
        # exposes fli's FlightResult; .flight_id is Google's opaque ID.
        results: list[Any] = search_with_ids(to_fli_filter(search), top_n=top_n) or []
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
        _render_gflight_table(results, legs=legs, top_n=top_n)

    if run_pp:
        from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

        sr = fli_results_to_search_result(results)
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
        )


def _render_gflight_table(results: list[Any], *, legs: tuple[Leg, ...], top_n: int) -> None:
    """Render fli results as a rich table. Duck-typed: fli has no type stubs.

    Accepts our `GFlightWithId` wrappers — `.flight` is fli's FlightResult,
    `.amenities` is per-leg legroom data parsed from Google's response."""
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
                f"{getattr(leg.airline, 'name', leg.airline)} {getattr(leg, 'flight_number', '?')}"
                for leg in fr.legs
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

# Common-args helpers — these reduce repetition across commands.
# These flags are hidden because almost nobody touches them in normal use;
# defaults live in config.toml ([http] section) and can be overridden via
# FLIGHT_RPS / FLIGHT_IMPERSONATE env vars. The CLI flag still works for
# one-off overrides — it's just no longer in --help. None sentinel means
# "fall back to config/env"; explicit value overrides everything.
_RPS_OPT = typer.Option(
    None,
    "--rps",
    hidden=True,
    help="Requests per second (default: 1.0; FLIGHT_RPS / config.toml).",
)
_IMPERSONATE_OPT = typer.Option(
    None,
    "--impersonate",
    hidden=True,
    help="curl_cffi profile (default: chrome; FLIGHT_IMPERSONATE / config.toml).",
)
_NO_CACHE_OPT = typer.Option(
    False,
    "--no-cache",
    hidden=True,
    help="Bypass the on-disk response cache (or set FLIGHT_NO_CACHE=1).",
)
_PROVIDER_OPT = typer.Option(
    None,
    "--provider-opt",
    help=(
        "Per-provider override, repeatable: 'pp.airlines=United,Delta'. "
        "Overrides ~/.config/flight-cli/config.toml [providers.<name>]."
    ),
    rich_help_panel="Backend & providers",
)


def _resolve_rps(flag: float | None) -> float:
    """CLI flag wins; otherwise fall back to env / config / default."""
    if flag is not None:
        return flag
    try:
        return _config.http_rps()
    except ValueError as e:
        err.print(f"[red]Bad rps configuration: {e}[/]")
        raise typer.Exit(2) from e


def _resolve_impersonate(flag: str | None) -> str:
    if flag is not None:
        return flag
    return _config.http_impersonate()


def _resolve_no_cache(flag: bool) -> bool:
    """The CLI flag is a one-way toggle: passing --no-cache forces True.
    Without it, env/config decide."""
    if flag:
        return True
    return _config.cache_disabled()


# ─────────────────────────── --format / --json ─────────────────────────────

# Output formats currently implemented end-to-end. csv/tsv/yaml were in the
# original work-4uls plan but deferred to a follow-up: the cash-itinerary
# shape isn't naturally tabular without a flattening pass that deserves its
# own design. Today's surface is the front door; emitters layer on later.
_VALID_FORMATS = ("table", "json")

_FORMAT_OPT = typer.Option(
    "table",
    "--format",
    help=f"Output format: one of {'/'.join(_VALID_FORMATS)}.",
    rich_help_panel=_GROUP_OUTPUT,
)
_JSON_OPT = typer.Option(
    False,
    "--json",
    hidden=True,
    help="[deprecated] Use --format json.",
)


def _resolve_format(*, fmt: str, json_flag: bool) -> str:
    """Collapse --format + deprecated --json into a single format string.

    `--json` forwards to `--format json` with a deprecation warning. Setting
    both (--json --format X for X != json) is a hard error: ambiguous intent.
    """
    if json_flag:
        err.print("[yellow]--json is deprecated; use --format json.[/]")
        if fmt not in ("table", "json"):
            err.print(f"[red]--json conflicts with --format {fmt!r}; pick one.[/]")
            raise typer.Exit(2)
        return "json"
    if fmt not in _VALID_FORMATS:
        err.print(f"[red]--format must be one of {'/'.join(_VALID_FORMATS)}; got {fmt!r}[/]")
        raise typer.Exit(2)
    return fmt


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
    cabin: str = typer.Option("economy", "--cabin", rich_help_panel=_GROUP_ITINERARY),
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
        True, "--matrix-url/--no-matrix-url", rich_help_panel=_GROUP_OUTPUT
    ),
    google_url: bool = typer.Option(
        True, "--google-url/--no-google-url", rich_help_panel=_GROUP_OUTPUT
    ),
    no_cache: bool = _NO_CACHE_OPT,
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

    run_awards = _should_run_awards(sel)
    if resolved == BACKEND_GFLIGHT:
        _run_gflight_path(
            legs=legs,
            opts=opts,
            top_n=page_size,
            json_out=json_out,
            run_pp=run_awards,
            sel=sel,
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


def _parse_slice_spec(s: str) -> Leg:
    """Parse 'JFK-LHR:2026-08-15[:r=LH+:e=MAXCONNECT 2:00]'.

    Error paths surface the specific failure (missing colon, malformed
    origin-dest, unknown key prefix, bad date) instead of the generic
    "should be ORIGIN-DEST:DATE[:r=...:e=...]" — that message is fine
    for missing date but useless when the user typed `r-LH+` instead
    of `r=LH+` (which the lookahead split otherwise silently ignores).
    """
    parts = s.split(":", 2)
    if len(parts) < _SLICE_MIN_PARTS:
        raise typer.BadParameter(
            f"slice {s!r}: missing date — expected ORIGIN-DEST:DATE[:r=...:e=...]"
        )
    od, dt = parts[0], parts[1]
    if "-" not in od:
        raise typer.BadParameter(
            f"slice {s!r}: missing '-' between origin and destination "
            f"(got {od!r}; expected e.g. JFK-LHR)"
        )
    o, d = od.split("-", 1)
    if not o or not d:
        raise typer.BadParameter(
            f"slice {s!r}: origin and destination must both be non-empty (got {od!r})"
        )
    # Inline date parse (don't route through _parse_date) so we control the
    # error envelope. _parse_date raises typer.Exit with a separate err.print
    # which would surface as a double-message in slice-specific BadParameter.
    try:
        parsed_date = datetime.strptime(dt, "%Y-%m-%d").date()
    except ValueError as e:
        raise typer.BadParameter(f"slice {s!r}: invalid date {dt!r} (expected YYYY-MM-DD)") from e
    routing = extension = None
    if len(parts) == _SLICE_MAX_PARTS:
        # Chunks come in as r=... and e=... separated by ':' followed by the
        # key prefix. Anything that doesn't start with r= or e= is a typo
        # (the most common is r-VALUE instead of r=VALUE).
        for chunk in re.split(r":(?=[re]=)", parts[2]):
            if chunk.startswith("r="):
                routing = chunk[2:]
            elif chunk.startswith("e="):
                extension = chunk[2:]
            else:
                raise typer.BadParameter(
                    f"slice {s!r}: unknown key prefix in {chunk!r}; "
                    f"valid keys are r=ROUTING and e=EXTENSION (note the '=')"
                )
    return Leg.of(o, d, parsed_date, route_language=routing, extension=extension)


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
        True, "--matrix-url/--no-matrix-url", rich_help_panel=_GROUP_OUTPUT
    ),
    google_url: bool = typer.Option(
        False, "--google-url/--no-google-url", rich_help_panel=_GROUP_OUTPUT
    ),
    no_cache: bool = _NO_CACHE_OPT,
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
    # CalendarSearch → CalendarResult by client._parse_response dispatch.
    res = cast(
        "CalendarResult",
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
        True, "--matrix-url/--no-matrix-url", rich_help_panel=_GROUP_OUTPUT
    ),
    google_url: bool = typer.Option(
        True, "--google-url/--no-google-url", rich_help_panel=_GROUP_OUTPUT
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
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url)


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
