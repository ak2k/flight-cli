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
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, assert_never, cast

import anyio
import anyio.to_thread
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import _config
from ._calendar_split import is_empty_calendar, merge_calendar_results, split_calendar_search
from ._gf_errors import (
    GfBackendError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfTfsUnsupportedError,
    GfThrottledError,
    GfTransportError,
)
from ._multi_cabin import MultiCabinRow, parse_price
from ._multi_cabin import merge as _merge_cabins
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
from .links import (
    extract_pin_segments_from_slice,
    google_flights_pinned_url,
    google_flights_url,
    matrix_deep_link,
    matrix_itinerary_url,
)
from .log import configure as configure_logging
from .pp.auth import load_tokens
from .pp.cli import auth_app, run_pp_for_search
from .providers.base import LegQuery

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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


# Characters that drive a terminal rather than appear in it, hide inside what does
# appear, or cannot be written out at all. `escape` neutralises `[` and nothing
# else, so an ESC or CSI inside remote text still clears the screen, repositions
# the cursor, or repaints what came before it — and a redirected stderr keeps
# every byte for whatever reads the file next.
_CTRL = {
    **{c: None for c in range(0x20) if c not in (0x09, 0x0A)},  # C0, keeping tab and newline
    0x7F: None,  # DEL
    **{c: None for c in range(0x80, 0xA0)},  # C1, including the 8-bit CSI
    # `str.splitlines` breaks on these two as it does on `\n`, so one message
    # carrying one arrives at a log reader or a `readlines` caller as two records.
    0x2028: None,  # LINE SEPARATOR
    0x2029: None,  # PARAGRAPH SEPARATOR
    # Bidi. The marks reorder the run they sit in and the embeddings, overrides
    # and isolates reorder everything up to their terminator, so any of them can
    # make a sentence read back as something it does not say.
    0x061C: None,  # ARABIC LETTER MARK
    0x200E: None,  # LEFT-TO-RIGHT MARK
    0x200F: None,  # RIGHT-TO-LEFT MARK
    **{c: None for c in range(0x202A, 0x202F)},  # embeddings and overrides
    **{c: None for c in range(0x2066, 0x206A)},  # isolates
    # Invisible and not whitespace, so they survive `strip()` and `split()` and
    # sit unseen inside a carrier code or a price: two values that read as equal
    # compare unequal, and nothing on the screen says why.
    0x00AD: None,  # SOFT HYPHEN
    **{c: None for c in range(0x200B, 0x200E)},  # zero-width space, non-joiner, joiner
    0x2060: None,  # WORD JOINER
    0xFEFF: None,  # ZERO WIDTH NO-BREAK SPACE
    **{c: None for c in range(0xE0000, 0xE0080)},  # tag block
    # A lone surrogate has no utf-8 encoding at all, so one in a Matrix price
    # reaches a real stdout as UnicodeEncodeError: the render of a query that
    # succeeded dies on the way out, where a console file object hides it.
    **{c: None for c in range(0xD800, 0xE000)},
}


def _safe_text(value: object) -> str:
    """Remote sentence-shaped text, ready for a console: control characters
    dropped, then markup escaped.

    For text we did not write and the user did not type — a Matrix error message,
    an exception's `str()`. Neither quoted nor truncated, unlike `_quote`: this is
    a sentence someone needs to read whole, and the part that explains the failure
    is as often at the end as the start.

    Strip before escape, never after. `escape` only sees a tag where `[` is
    followed by `[a-z#/@]`, so a control character between the brackets hides the
    tag from it, and stripping afterwards uncovers a live one: `"[\x00red]x"`
    comes out of the other order as `"[red]x"`, styled."""
    text = escape(str(value).translate(_CTRL))
    if not text.strip() and isinstance(value, BaseException):
        # `httpx.ConnectTimeout("")` stringifies to nothing, which would leave a
        # reporter saying "Matrix calendar failed:" and stopping. The class name is
        # the only thing such an exception carries, and it takes the same two steps
        # as the message would: a class built from a remote payload can be named
        # anything. A blank from anywhere else is a value someone chose, and stays
        # blank.
        return escape(type(value).__name__.translate(_CTRL))
    return text


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


def _require_airports(origin: str, destination: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Both airport lists, parsed — or exit 2 rather than build an empty leg.

    `_parse_iata_list` drops blank entries, so `""` and `","` arrive as an empty
    tuple while the argument itself is still truthy and passes a plain `if
    origin`. `Leg.of` accepts a leg with no airports at all out of that, and the
    query only fails much later, inside a backend, as an index error."""
    origins, destinations = _parse_iata_list(origin), _parse_iata_list(destination)
    if not origins or not destinations:
        err.print("[red]origin and destination are required.[/]")
        raise typer.Exit(2)
    return origins, destinations


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


def _resolve_cabin_list(csv: str) -> tuple[Cabin, ...]:
    """Parse a comma-separated cabin list. Single-cabin invocations still
    take this path — they emit a 1-tuple and the dispatcher routes them
    back through the single-cabin code path unchanged."""
    tokens = [t.strip() for t in csv.split(",") if t.strip()]
    if not tokens:
        err.print("[red]--cabin must name at least one cabin.[/]")
        raise typer.Exit(2)
    seen: dict[Cabin, None] = {}
    for t in tokens:
        seen.setdefault(_resolve_cabin(t), None)
    return tuple(seen)


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
    stops: int | None,
    children: int,
    seniors: int,
    youth: int,
    inf_seat: int,
    inf_lap: int,
    origin: str | None,
    destination: str | None,
    allow_airport_changes: bool,
    show_only_available: bool,
) -> str:
    """Resolve --backend to a concrete backend.

    auto: matrix iff the request needs it, else gflight (~1s vs Matrix's ~45s).
    `--routing`/`--extension` don't force Matrix on their own — they're parsed
    and classified, and Google Flights serves them when the search page's tfs=
    parameter can encode every one (`page_can_encode`). A constraint it can't
    carry goes to Matrix WITH ITS REASON PRINTED, rather than being post-
    filtered out of Google's fixed ~30-row board, which would answer a
    constrained search with a plausible-looking "no results".

    Hard-Matrix flags always force Matrix: `--slice` (multi-city),
    `--depart-times`/`--return-times`, a `--stops` ceiling above two (fli maps
    it to "any", so the tfs field would be omitted and the constraint lost),
    any pax type beyond adults (the page's
    passenger field has kind codes for children and infants that we have never
    verified against a live priced search), a multi-airport
    `--origin`/`--destination` set (the GF bridge flattens those to the first
    code, so serving them on GF would silently drop the rest), and
    `--no-airport-changes` / `--include-unavailable`, which the search page's
    `tfs=` parameter has no field for at all.

    A constraint the page cannot carry has to be a reason here and nowhere
    else. Left out, `auto` serves it on Google with the constraint silently
    dropped and `--backend gflight` accepts it without a word, while the deep
    link printed underneath still carries it — three surfaces disagreeing about
    what was asked.

    Whatever the cause, `auto` names it on stderr. Silently taking the 45x
    slower backend leaves the user with no way to tell a constraint they could
    drop from one they can't.

    Explicit --backend matrix: matrix. --backend gflight: gflight, unless the
    request is inexpressible on GF (error)."""
    from .routing_predicates import (  # noqa: PLC0415
        MAX_ENCODABLE_STOPS,
        classify,
        page_can_encode,
    )

    reasons: list[str] = []
    if slice_specs:
        reasons.append("a multi-city itinerary")
    if depart_times or return_times:
        reasons.append("a departure/arrival time window")
    if children or seniors or youth or inf_seat or inf_lap:
        reasons.append("a passenger type beyond adults")
    if not allow_airport_changes:
        # Both of these reach the Matrix REQUEST and the Matrix deep link and
        # nothing else: `fli_bridge`, which the search page's `tfs=` is encoded
        # from, has no field for either. Served on Google the constraint is
        # simply absent, and the board that comes back is the unconstrained one
        # — the shape this picker exists to keep off the fast backend.
        reasons.append("a ban on changing airports")
    if not show_only_available:
        reasons.append("unavailable itineraries included")
    if len(_parse_iata_list(origin or "")) > 1 or len(_parse_iata_list(destination or "")) > 1:
        reasons.append("a multi-airport origin/destination")
    if stops is not None and stops > MAX_ENCODABLE_STOPS:
        # Same ceiling as the routing-language spelling below, and the same
        # wording: fli's MaxStops maps anything higher to ANY, which omits the
        # tfs field, so `--stops 3` would encode byte-identically to no --stops.
        reasons.append(f"a stop ceiling above {MAX_ENCODABLE_STOPS} ({stops})")
    if routing or extension:
        reasons.extend(page_can_encode(classify(routing, extension).predicates)[1])

    # The same reasons go out two ways, and only one of them is markup. A
    # reason quotes the user's --routing string verbatim, so one square bracket
    # decides between a MarkupError traceback and a backslash the user can see.
    if backend == BACKEND_AUTO:
        if not reasons:
            return BACKEND_GFLIGHT
        err.print(
            f"[dim]Using Matrix: Google Flights can't serve {escape(_join_reasons(reasons))}.[/]"
        )
        return BACKEND_MATRIX
    if backend == BACKEND_GFLIGHT and reasons:
        # typer renders a BadParameter as plain Text, never markup — escaping
        # here would print the backslashes instead of hiding them.
        raise typer.BadParameter(
            f"--backend gflight can't serve this request: {_join_reasons(reasons)}. "
            "Drop it, or use --backend matrix.",
        )
    if backend not in _VALID_BACKENDS:
        raise typer.BadParameter(f"--backend must be one of {_VALID_BACKENDS}; got {backend!r}")
    return backend


def _join_reasons(items: list[str]) -> str:
    """Human list join: 'a', 'a and b', 'a, b and c'."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


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

    def seats_sources(self) -> tuple[str, ...] | None:
        """Seats.aero mileage-program filter (`--provider-opt seats-aero.sources=...`).

        Maps to the API's `sources=` query param. None = no filter (return all
        programs the route is monitored on). The canonical provider key is
        `seats-aero`; user-facing aliases (`sa`, `seatsaero`, `seats.aero`)
        are normalized at parse time so we only need to read one key here."""
        v: Any = self.provider_opts.get("seats-aero", {}).get("sources")
        if v is None:
            return None
        if isinstance(v, list):
            return tuple(str(x).strip() for x in cast("list[Any]", v))
        return (str(v).strip(),)


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

    # --providers parsing. Normalize each entry through the alias map so
    # `--providers sa,pointspath` ends up the same as `--providers seats-aero,pp`.
    provider_filter: tuple[str, ...] | None = None
    if providers is not None:
        provider_filter = tuple(
            _config.canonical_provider(p) for p in providers.split(",") if p.strip()
        )
        if not provider_filter:
            err.print("[red]--providers cannot be empty.[/]")
            raise typer.Exit(2)

    # Build provider_opts: config.toml < --provider-opt CLI. Section names in
    # the config are normalized through the alias map so user-facing spellings
    # (`[providers.sa]`) land at the canonical key the registry expects.
    try:
        config = _config.load()
    except (OSError, ValueError) as e:
        err.print(f"[red]Failed to load ~/.config/flight-cli/config.toml: {escape(str(e))}[/]")
        raise typer.Exit(2) from e
    base_opts: dict[str, dict[str, Any]] = {}
    providers_section: Any = config.get("providers", {})
    if isinstance(providers_section, dict):
        for name, opts in cast("dict[str, Any]", providers_section).items():
            if isinstance(opts, dict):
                base_opts[_config.canonical_provider(name)] = dict(cast("dict[str, Any]", opts))

    try:
        cli_opts = _config.parse_provider_opt_overrides(list(provider_opt))
    except ValueError as e:
        err.print(f"[red]{escape(str(e))}[/]")
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
    configured globally, and (c) the filter (if any) names at least one
    configured provider.

    Provider-blind by construction: every known provider has an
    is_configured() check imported below; adding a new provider is one
    import + one entry in the `known` map.
    """
    if sel.cash_only:
        return False
    # Lazy-imported to avoid the registry/CLI import cycle and to keep PP's
    # token-load (which touches disk) out of the cli module top-level.
    from .providers.pointspath.provider import is_configured as pp_is_configured  # noqa: PLC0415
    from .providers.registry import has_any_configured  # noqa: PLC0415
    from .providers.seats_aero.auth import is_configured as seats_is_configured  # noqa: PLC0415

    known: dict[str, bool] = {
        "pp": pp_is_configured(),
        "seats-aero": seats_is_configured(),
    }

    if not has_any_configured():
        if sel.awards_only:
            err.print(
                "[red]--awards-only set but no award provider is configured.[/] "
                "Run `flight auth pp login` or `flight auth seats-aero key <KEY>` first.",
            )
            raise typer.Exit(2)
        return False
    # Filter matches at least one configured provider? Values are already
    # canonical (normalized by _resolve_providers), so a direct membership
    # check is enough here. None filter ⇒ "all enabled", short-circuits to True.
    if sel.provider_filter is not None and not any(
        known.get(name, False) for name in sel.provider_filter
    ):
        if sel.awards_only:
            err.print(
                f"[red]--awards-only set but --providers={escape(str(sel.provider_filter))} "
                "matches no configured provider.[/]",
            )
            raise typer.Exit(2)
        return False
    return True


# ─────────────────────────── shared execution ──────────────────────────────


def _orderly_exit(e: BaseException) -> typer.Exit | typer.Abort | None:
    """The first orderly exit anywhere inside `e`, or None.

    `typer.Exit` and `typer.Abort` subclass `RuntimeError` on the installed click,
    and a task group wraps EVERYTHING that leaves it in an `ExceptionGroup` — the
    host body's own exception included. Between them, a broad arm outside a group
    catches a deliberate stop wearing the shape of a backend failure and answers it
    with a backend's name and the wrong exit code. An exit beside other failures
    still wins: it is the one outcome somebody asked for. "First" is first in
    member order, which is task-start order — not the first to raise, and not the
    most severe."""
    if isinstance(e, (typer.Exit, typer.Abort)):
        return e
    if isinstance(e, BaseExceptionGroup):
        # `isinstance` narrows to the unparameterised generic, which leaves every
        # member unknown; anyio builds these and they hold whatever the tasks raised.
        for member in cast("BaseExceptionGroup[BaseException]", e).exceptions:
            found = _orderly_exit(member)
            if found is not None:
                return found
    return None


def _failures_inside(e: BaseException) -> list[BaseException]:
    """Every failure `e` is carrying, with the orderly exits left out.

    A task group hands its caller one object holding whatever its tasks raised,
    so the thing caught outside a group is a container: what failed is inside
    it, possibly several deep, possibly beside a deliberate stop that is not a
    failure at all."""
    if isinstance(e, BaseExceptionGroup):
        # Same narrowing as `_orderly_exit`: the members are whatever the tasks
        # raised, which the unparameterised generic cannot describe.
        members = cast("BaseExceptionGroup[BaseException]", e).exceptions
        return [leaf for member in members for leaf in _failures_inside(member)]
    if isinstance(e, typer.Exit | typer.Abort):
        return []
    return [e]


def _failure_text(cause: object) -> str:
    """What to call `cause` on a typed line.

    Never the group: "unhandled errors in a TaskGroup" is plumbing, printed to
    someone whose search failed for a reason that is sitting inside it. One
    failure is named as itself. Several are all named and counted, because
    picking one would report half an outage as the whole of it — and a user who
    sees one cause fixes one thing and runs the same command again.

    `object` rather than an exception, because a caller reporting what it holds
    cannot always promise it holds an exception — a stash read back, a value off
    a task's result. Anything that is not one is a value somebody chose and is
    named as it stands. Every path out of here ends in `_safe_text`, which is
    the property that lets a printer treat this function as already escaped."""
    if not isinstance(cause, BaseException):
        return _safe_text(cause)
    failures = _failures_inside(cause)
    if not failures:
        # No leaves at all. A group holding nothing but orderly exits reaches
        # this too, and naming the group there would be the plumbing string this
        # docstring refuses — but that group is the caller's to have raised
        # already, which `_reraise_if_orderly` does before any of these print.
        return _safe_text(cause)
    if len(failures) == 1:
        return _safe_text(failures[0])
    named = "; ".join(_safe_text(f) for f in failures)
    return f"{len(failures):d} concurrent failures: {named}"


def _reraise_if_orderly(e: Exception, *, said: str) -> None:
    """Re-raise a deliberate stop that arrived inside `e`, and never let it
    hide what failed beside it.

    An exit anywhere in the group is the outcome somebody asked for, so it keeps
    its own code. But a group can carry an exit AND a real failure — one task
    stopping the command while another one broke — and re-raising the exit alone
    reports success, or a chosen code, with both streams empty. The failures are
    named first, on the stream every other failure here uses, and the exit code
    is left exactly as it was asked for.

    The count is said out loud rather than left to be inferred from a list: one
    failure beside a stop reads as the outcome unless the sentence says it stood
    beside one, and it is the same sentence a calendar failure prints for the
    same shape.

    A `MatrixApiError` then goes to the reporter that knows it. Rendered
    as text it is its message alone — `kind` and `request_id` are what tell a
    user whether to fix their query or wait out a brownout, and losing them here
    would make this the one Matrix line on the branch that drops them.

    `said` is escaped like any other value this file prints. Every caller passes
    a literal today, which is exactly why the guard belongs here: a banner is the
    kind of parameter that later gets built from something remote."""
    orderly = _orderly_exit(e)
    if orderly is None:
        return
    beside = _failures_inside(e)
    if beside:
        plural = "" if len(beside) == 1 else "s"
        err.print(
            f"[red]{_safe_text(said)}:[/] {len(beside):d} failure{plural} "
            f"beside a deliberate stop: {_failure_text(e)}"
        )
        for f in beside:
            if isinstance(f, MatrixApiError):
                _print_matrix_error(f)
    raise orderly


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
        _print_matrix_error(e)
        raise typer.Exit(1) from e
    except (typer.Exit, typer.Abort):
        # An orderly exit is not a failure. Redundant with the
        # `_reraise_if_orderly` below on the installed click, and the only guard
        # left if `typer.Exit` ever stops subclassing `Exception`.
        raise
    except Exception as e:
        # `execute()` wraps what Matrix answered; it does not wrap a DNS failure
        # or a refused connection. Untyped, those leave here as a rich traceback
        # with the cause hundreds of lines down, on the most ordinary command
        # there is — and this package's rule is that a third-party transport
        # error never reaches a caller untyped.
        _reraise_if_orderly(e, said="Matrix search failed")
        err.print(f"[red]Matrix search failed:[/] {_failure_text(e)}")
        raise typer.Exit(1) from e


def _print_matrix_error(e: MatrixApiError) -> None:
    """Report a Matrix error to stderr: control characters dropped, markup escaped.

    Matrix echoes the routing string back inside `message` ("Illegal COMMAND-LINE
    prefix: BA[/weird]AA"), so all three fields carry remote text onto a markup
    console. Every Matrix reporter that FAILS a command reports through here — the
    calendar sites, `_run` (which serves `detail` and the search path), the search
    weave and the group-level multi-cabin arm — so one Matrix error reads the same
    whichever command asked for it. The per-cabin fan-out is the one exception and
    is deliberate: its failure is soft, one cabin of several, so it prints a yellow
    line naming that cabin and wraps the two fields itself rather than reporting a
    red failure for a command that is still going to answer."""
    err.print(f"[red]Matrix returned an error ({_safe_text(e.kind)}):[/] {_safe_text(e.message)}")
    if e.request_id:
        err.print(f"[dim]request_id: {_safe_text(e.request_id)}[/]")


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


def _pinned_solution_index(result: SearchResult | None, pick: int | None) -> int | None:
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


def _try_pinned_matrix_url(search: Search, result: SearchResult | None, idx: int) -> str | None:
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


def _try_pinned_gflight_url(search: Search, result: SearchResult | None, idx: int) -> str | None:
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
    if len(itn.slices) >= _ROUND_TRIP_LEGS:
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


def _emit_urls(
    search: Search,
    *,
    matrix_url: bool,
    google_url: bool,
    result: SearchResult | None = None,
    pick: int | None = None,
) -> None:
    idx = _pinned_solution_index(result, pick)
    # Only claim "#N" when we actually honored the user's pick; an out-of-range
    # pick falls back to idx 0 and must not mislabel the cheapest as "#N".
    pinned_label = (
        f"itinerary #{pick}"
        if (idx is not None and pick is not None and idx == pick - 1)
        else "cheapest itinerary"
    )
    if matrix_url:
        console.print()
        pinned_m = _try_pinned_matrix_url(search, result, idx) if idx is not None else None
        if pinned_m is not None:
            console.print(f"[dim]Matrix ({pinned_label} pinned):[/]")
            console.print(f"  [link]{_safe_text(pinned_m)}[/]")
        else:
            console.print("[dim]Matrix deep-link:[/]")
            console.print(f"  [link]{_safe_text(matrix_deep_link(search))}[/]")
    if google_url:
        # `google_flights_url` builds protobuf-encoded tfs= URLs via fast_flights.
        # That library has no documented exception surface — catch broadly so a
        # missing IATA or unsupported variant degrades the URL line, not the run.
        try:
            pinned = _try_pinned_gflight_url(search, result, idx) if idx is not None else None
            if pinned is not None:
                console.print(f"[dim]Google Flights ({pinned_label} pinned):[/]")
                console.print(f"  [link]{_safe_text(pinned)}[/]")
            else:
                console.print("[dim]Google Flights (tfs= structured):[/]")
                console.print(f"  [link]{_safe_text(google_flights_url(search))}[/]")
        except Exception as e:  # noqa: BLE001 - third-party undocumented errors; non-fatal fallback
            console.print(f"[dim]Google Flights link: {_safe_text(e)}[/]")


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
    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
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
    if not json_out:
        # Same contract as every other path: the range a pick is measured
        # against is the VISIBLE count, and the reporter that says so is the one
        # whose second clause knows whether a link follows. Clamping here is
        # also what keeps one out-of-range fact from being stated two ways —
        # `_pinned_solution_index` would otherwise answer it on stdout, in a
        # `--format json` sibling's stream and with a different fallback.
        #
        # An empty result is numbered nowhere, so it gets no sentence at all
        # rather than an empty `(1-0)` interval and a pin claim nothing honours.
        pick = (
            _pick_in_range(pick, len(res.solutions), links_follow=matrix_url or google_url)
            if res.solutions
            else None
        )
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


def _gflight_json_row(g: Any) -> dict[str, Any]:
    """One `--format json` itinerary: fli's FlightResult plus the two things
    only this backend knows — Google's opaque `flight_id` and the per-leg
    legroom/amenity extract, which the human table shows and `model_dump()`
    alone doesn't carry."""
    row: dict[str, Any] = {**g.flight.model_dump(mode="json"), "flight_id": g.flight_id}
    legs: list[Any] = row.get("legs") or []
    amenities: list[Any] = list(g.amenities)
    # A misaligned extract leaves the surplus legs as fli dumped them.
    for leg, a in zip(legs, amenities, strict=False):
        leg["legroom_class"] = a.legroom_class
        # Same key as fli's own `FlightLeg.amenities`, a different schema. Safe
        # only because `_flight_leg` never populates fli's — if it ever does,
        # this write silently replaces it and needs its own key.
        leg["amenities"] = asdict(a)
    return row


def _terminal_fare_key(r: Any) -> tuple[int, float]:
    """Sort key for one round-trip combination: its terminal member's fare,
    with a row Google did not price ordered last.

    Google surfaces no shopping-list price for some rows — premium-cabin round
    trips with several passengers are the routine case — and a row it did not
    price is still a row the board served. There is no number to rank it on, so
    it goes last rather than being dropped or read as a zero fare; the leading
    term is what carries that, and it leaves the priced rows compared on the
    fare alone.

    Reads `.flight.price` and no other attribute, so the key holds for anything
    shaped like a result row rather than only for fli's own model."""
    price: float | None = list(r)[-1].flight.price
    return (1, 0.0) if price is None else (0, price)


def _price_ordered(results: list[Any]) -> list[Any]:
    """Round-trip combinations in price order. A one-way board is returned as
    it came.

    Two sets, ordered by two different things, and a combination priced from
    its terminal member; the argument for both is in the memo's `-n` section.

    The sort is stable, so combinations sharing a total stay in the order the
    pins were fetched."""
    if any(not isinstance(r, tuple) for r in results):
        return results
    return sorted(results, key=_terminal_fare_key)


def _pick_in_range(pick: int | None, rows: int, *, links_follow: bool) -> int | None:
    """`pick` when it names one of the `rows` the user was shown, else None
    with the reason on stderr.

    The range a pick is measured against is the visible count, which is decided
    at the trim and nowhere else, so the check belongs beside it. Answering
    None rather than an index is what makes the fallback single-sourced: the
    pin machinery already treats "no pick" as "the cheapest row", labels the
    link that way, and a caller that clamped to an index instead would pin the
    right row under a label claiming the user's number.

    Two clauses, and neither is unconditional. A number the user typed that
    names no row on screen is worth a line, and `rows` is what the line
    measures it against. What happens NEXT is a separate question: a run that
    emits no link pins nothing, so `links_follow` is what keeps the second
    clause from describing something that did not happen — the defect this
    whole reporter exists to avoid, one sentence in.

    A board with NO rows is the case the callers keep away from here rather
    than one this reports, and for the same reason the second clause exists:
    `1-0` is an empty interval, so it cannot say what a valid pick would be,
    and nothing is pinned for a fallback clause to name. Every caller skips
    this on an empty list and prints nothing there.

    The fallback names ROW ONE rather than "the cheapest", because that is what
    every caller of this does with the None: they pin the first row of the list
    the table numbered. Only a round trip's rows are in price order, so on a
    one-way board — which keeps Google's ranking — "the cheapest" describes a
    different row from the one the link opens.

    stderr, because a `--format json` document on stdout stays a document —
    the same rule every other note on this path follows."""
    if pick is None or 1 <= pick <= rows:
        return pick
    fallback = "; pinning itinerary #1 instead." if links_follow else "."
    err.print(f"[yellow]--pick {pick} is out of range (1-{rows}){fallback}[/]")
    return None


class _GfRefusal(NamedTuple):
    """How one Google Flights refusal reads: `note` where Matrix still answers
    and the refusal is a footnote, `message` where it is the whole outcome.

    The two fields are not interchangeable, and the difference is markup.
    `message` is PRE-RENDERED rich markup — it carries its own tags and any
    exception text in it is already escaped, so print it as-is and never escape
    it again. `note` is PLAIN text with no tags, so a caller embedding it in
    markup of its own must escape it there."""

    note: str
    message: str


_GF_DECLINED = "Google Flights declined the request"


def _gf_refusal(e: GfBackendError) -> _GfRefusal:  # noqa: PLR0911 - one arm per wall is the point
    """User-facing text for a typed Google Flights refusal.

    Each wall gets its own wording and its own next move — collapsing them into
    one message ("Google Flights failed") is how a page-shape regression gets
    mistaken for a route with no service.

    One dispatch, so the two renderings of a refusal AGREE. That is the whole
    of the guarantee: a type with no arm of its own still renders, from the base
    case, in both places — identically and without naming the wall.

    The test that walks the subclasses is what keeps a new type from landing
    there. `assert_never` only closes the door this function cannot be given: a
    value that is not a `GfBackendError` at all. With the base case present,
    deleting a subclass arm type-checks clean."""
    match e:
        case GfThrottledError():
            return _GfRefusal(
                "Google Flights rate-limited",
                "[yellow]Google Flights is rate-limiting this IP.[/] Wait a moment and "
                "retry, or use [bold]--backend matrix[/].",
            )
        case GfConsentError():
            return _GfRefusal(
                "Google Flights served its consent page",
                "[yellow]Google served its consent page instead of flight results.[/] "
                "Use [bold]--backend matrix[/].",
            )
        case GfPageShapeError():
            return _GfRefusal(
                "Google Flights' page shape changed",
                "[red]Google Flights' page shape changed[/] — no rows could be read. "
                f"Use [bold]--backend matrix[/]. ({_safe_text(e)})",
            )
        case GfTfsUnsupportedError():
            # Generic note: `page_can_encode` keeps these queries off Google
            # Flights, so the enrich path never has one to render.
            return _GfRefusal(
                _GF_DECLINED,
                f"[red]Google Flights can't express this search:[/] {escape(e.reason)}. "
                "Use [bold]--backend matrix[/].",
            )
        case GfTransportError():
            # "unreachable", the same word the pin loop uses for a spent
            # transport ladder. Two sites naming one fact two ways leaves the
            # user deciding which of them to believe, and only one is right:
            # nothing here says the query was declined.
            #
            # The exception's own `str()` already opens with "could not be
            # reached", so the sentence starts there rather than saying it
            # twice — and the cause it carries is the part worth reading.
            return _GfRefusal(
                "Google Flights was unreachable",
                f"[yellow]{_safe_text(e)}[/] — the connection failed, not the "
                "query. Use [bold]--backend matrix[/].",
            )
        case GfPinIgnoredError():
            # The page loaded, the rows parsed, and they describe a segment
            # nobody asked for — so the sentence names the leg that was served
            # against the one that was wanted, which `_safe_text(e)` carries.
            # Reporting a shape change here would deny its own evidence: the
            # served endpoints are known only because the rows WERE read.
            return _GfRefusal(
                "Google Flights answered the wrong leg",
                f"[red]Google Flights served a board for a leg nobody asked for[/] — "
                f"{_safe_text(e)}. Use [bold]--backend matrix[/].",
            )
        case GfBackendError():
            # The base type is raised directly — a 5xx from the search page is
            # the reachable one — so this is a case, not a fallback. It names no
            # wall because it knows none.
            return _GfRefusal(_GF_DECLINED, f"[red]{_GF_DECLINED}:[/] {_safe_text(e)}")
        case _:
            assert_never(e)


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

    This is where `top_n` becomes the answer's size. The query cannot ask for a
    count, so everything below the trim — the table, the JSON document, the
    pinned link and the awards — is drawn from the same `top_n` rows, and
    everything above it reads the whole board.
    """
    _pin_cap_note(legs=legs, top_n=top_n)
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    try:
        results = _gflight_results(legs, opts, top_n)
    except GfBackendError as e:
        err.print(_gf_refusal(e).message)
        raise typer.Exit(1) from e
    except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
        raise
    except Exception as e:
        err.print(f"[red]Google Flights query failed:[/] {_safe_text(e)}")
        raise typer.Exit(1) from e

    if not results:
        if json_out:
            # No rows is a value, and a document is what was asked for. The
            # sentence below is for a person; to a consumer it is a parse error
            # where an empty answer belongs, and the two are indistinguishable
            # from the exit code.
            sys.stdout.write(json.dumps([], indent=2))
            return
        console.print("[yellow]Google Flights: no results (or none matched the routing).[/]")
        return

    # `-n` is one number for everything the user can act on. Google's page
    # serves its whole board (~30 rows) whatever count is asked of it, so the
    # count is a trim rather than a query parameter, and everything below this
    # line is drawn from the same rows: the table, the JSON document, the range
    # `--pick` accepts, the itineraries the award matcher is fanned out over.
    # The trim is HERE rather than in the query because the wide board is what
    # the Tier-2 post-filter above and the multi-cabin join elsewhere are drawn
    # from — narrowing the query would answer a filtered search with fewer rows
    # than exist, which is the failure this backend is most prone to.
    results = _price_ordered(results)[:top_n]
    # A link follows only where one is asked for and the format has room for it:
    # `--format json` emits none at all, and neither does a run with both URL
    # flags off. The range is still reported; the fallback is not claimed.
    pick = _pick_in_range(
        pick, len(results), links_follow=not json_out and (matrix_url or google_url)
    )

    # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType,
    #                 reportUnknownArgumentType, reportUnknownParameterType]
    # fli/fast_flights have no type stubs; results are duck-typed pydantic
    # models. Suppressing the noisy unknown-type chatter for this rendering
    # block keeps the boundary localized.
    if json_out and not run_pp:
        out: list[Any] = []
        for r in results:
            items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
            dumped = [_gflight_json_row(g) for g in items]
            out.append(dumped if isinstance(r, tuple) else dumped[0])
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return

    awards_only = sel.awards_only if sel is not None else False
    # `not json_out` as well as `not awards_only`: the early return above fires
    # only with awards OFF, so with them on the document is written further
    # down by the award renderer and every human surface between here and it
    # would land in the same stream. A caller asking for a document gets a
    # document — one, and nothing else — whatever else was asked for beside it.
    if not awards_only and not json_out:
        try:
            _render_gflight_table(
                results, legs=legs, top_n=top_n, match_carriers=_match_carriers(legs)
            )
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            raise
        except Exception as e:
            # The table IS the output on this path — there is no Matrix half to
            # fall back on — so a renderer meeting a drifted row shape decides
            # the command. Typed and non-zero, because the alternative is a
            # traceback with nothing on either stream that a user could act on.
            _report_paint_failure(e)
            raise typer.Exit(1) from e

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

    # The URL lines are prose on stdout, and `_emit_urls` is shared text that
    # cannot know which format asked for it, so the guard belongs here.
    if not json_out:
        _emit_urls(
            SpecificDateSearch(legs=legs, options=opts),
            matrix_url=matrix_url,
            google_url=google_url,
            result=sr,
            # The pin LABEL names the row it pins. `sr` is built from the rows
            # the table numbered, so row 1 is the default pin — but a one-way
            # board keeps Google's own ranking, where row 1 need not be the
            # cheapest, and the label "cheapest itinerary" over it is simply
            # false. `1` makes the label say what the link does on every board.
            pick=pick or 1,
        )


def _paint_first_gf_table(
    state: dict[str, Any],
    gf: list[Any],
    *,
    legs: tuple[Leg, ...],
    top_n: int,
    awards_only: bool,
) -> None:
    """The Google Flights table, painted while Matrix is still in flight.

    Guarded for the same reason the Matrix task beside it is: this runs INSIDE
    the weave's task group, so a renderer raising on a drifted row shape would
    cancel Matrix and end the command as a bare ExceptionGroup — a traceback in
    place of the answer the other backend was about to give. The failure is
    stashed and reported after the weave, like every other one here.

    The note on the empty branch is true when it prints: it is gated on there
    being no Google refusal stashed, so Matrix really is the only half still
    running. A Google half that FAILED is a different sentence, and
    `_report_enriched_gf_failure` is where the difference is made.

    BOTH sentences go to stderr, and for one reason: each names the MATRIX half
    from inside the weave, before that half has resolved. Neither can be true
    when it prints, because whether Matrix answers is not yet known. On the run
    where it does not, a promise on stdout is a sentence saying a table follows
    on a stream where nothing else ever arrives — and the command exits 0. The
    rule is that no stdout sentence promises a half that never came, which is
    narrower than "owed zero bytes": the run below paints a real table first and
    still may not keep this promise."""
    if gf and not awards_only:
        try:
            _render_gflight_table(gf, legs=legs, top_n=top_n, match_carriers=_match_carriers(legs))
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            raise
        except Exception as e:  # noqa: BLE001 — see the docstring
            state["paint_err"] = e
        else:
            err.print("[dim]…refining with Matrix (authoritative fares)…[/]")
    elif not gf and "gf_err" not in state:
        err.print("[yellow]Google Flights: no results; awaiting Matrix…[/]")


async def _matrix_into(
    state: dict[str, Any], search: Search, rps: float, impersonate: str, cache: bool
) -> None:
    """The Matrix half of a weave: run the search, stash every way it can fail.

    The client is BUILT in here, inside the task, because building it resolves
    the API key — the disk cache first, then the network. Built beside the task
    group instead, a key that will not resolve ends the command before the
    Google Flights thread is ever started, and the user gets nothing on a query
    `--fast` answers with a table. In here it is one half of a weave failing,
    which is what the other half is for.

    Nothing leaves this task. Anything `execute()` does not wrap — a connect
    timeout, a TLS failure, a key that will not resolve, a transport that will
    not close — would cancel the still-pending Google Flights paint and end the
    command as a bare ExceptionGroup. Every stash here is read after the weave.

    A result and a `MatrixApiError` cannot be stashed together: this package
    builds that error at exactly one site, under `execute()`, and closing the
    client awaits the transport and nothing that raises one — so the arm below
    reaches `state["matrix"]` only through the value `execute()` returned, and
    reaching it at all means nothing raised. The single-origin half is asserted
    rather than described, in `tests/test_refusal_markup.py`.
    """
    try:
        async with MatrixClient(rps=rps, impersonate=impersonate) as c:
            state["matrix"] = await c.execute(search, cache=cache)
    except MatrixApiError as e:
        state["matrix_err"] = e
    except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
        raise
    except Exception as e:  # noqa: BLE001 — see the docstring: this task must not tear the group down
        state["matrix_unexpected"] = e


def _run_the_weave(go: Callable[[], Coroutine[Any, Any, None]], state: dict[str, Any]) -> None:
    """Run the weave and stash anything that escapes it, so the reporters below
    it decide the outcome.

    Each task guards its own body, so what reaches here is what the weave
    itself does: opening the loop, starting the group, and the group's own
    unwinding. Untyped, any of that is a traceback with both streams empty on
    the most ordinary command there is — and the rows the other backend already
    has go with it. A stash is not an outcome: every path out of its caller
    reads it, including the one whose other half succeeded."""
    try:
        anyio.run(go)
    except (typer.Exit, typer.Abort):
        # An orderly exit is a decision, not a failure. `typer.Exit` subclasses
        # `RuntimeError` on the installed click, so the arm below would catch it
        # and report the exit CODE as a Matrix error message. This arm is for
        # an exit raised by the weave itself; one raised inside the task group
        # arrives wrapped, which is what the next line is for — and on the
        # installed click that next line covers this one too, leaving this the
        # guard that still works if `typer.Exit` stops subclassing `Exception`.
        raise
    except Exception as e:  # noqa: BLE001 — reported by _report_search_matrix_failure
        # Backend-neutral, both times. This group spans BOTH halves, so what it
        # hands over names whatever any of its tasks left in it — a broken pipe
        # out of the Google paint among them. "Matrix search failed" in front of
        # that sends the user to the backend that did not fail. `_run` and
        # `_run_matrix_multi` keep the Matrix banner: their groups hold nothing
        # else.
        _reraise_if_orderly(e, said="Search failed")
        # Its own key. The Matrix task stashes what IT could not do; this is
        # what the weave itself could not do, and one key for both means
        # whichever lands second is the only one anybody reads.
        state["weave_err"] = e


def _report_paint_failure(e: object) -> None:
    """The one sentence for a Google Flights table that could not be drawn.

    Both paths that draw one say it: the weave, where Matrix may still answer,
    and the Google-only path, where it is the whole outcome. What differs is
    what happens next, not what the user is told."""
    err.print(f"[yellow]Google Flights results could not be rendered:[/] {_safe_text(e)}")


def _report_weave_aftermath(state: dict[str, Any]) -> None:
    """What the weave left behind on a run that ANSWERED.

    A Google Flights table that could not be drawn, a Matrix half that failed
    after producing its result — a transport that would not close, a console
    write that failed — and the weave's own unwinding. None of them is the
    outcome, and none of them may be silence: a value stashed on one path and
    read only on another is a failure the command hid.

    Every stash, not the first: `state["matrix"]` is written by the last
    statement inside its `async with`, so a Matrix half can leave a result AND
    a failure behind, and a run that answered can still have lost the table
    beside it."""
    if state.get("paint_err") is not None:
        _report_paint_failure(state["paint_err"])
    if state.get("matrix_unexpected") is not None:
        err.print(
            f"[yellow]Matrix answered, then failed:[/] {_failure_text(state['matrix_unexpected'])}"
        )
    if state.get("weave_err") is not None:
        err.print(
            f"[yellow]The search answered, then failed:[/] {_failure_text(state['weave_err'])}"
        )


def _report_enriched_gf_failure(e: Exception, *, matrix_answered: bool, awards_only: bool) -> None:
    """Say why the Google Flights half of the weave produced nothing.

    Where Matrix answered it is still authoritative, so a typed refusal is a
    note beside its table rather than the outcome — but it stays named, or the
    merged table just looks like Google had nothing cheaper.

    Where Matrix did not answer there is no table for the note to sit beside,
    and "showing Matrix only" promises a half that never arrives — on the most
    likely both-halves-fail shape there is, a stale key with no route to either
    backend. The same refusal then reads as what it is: half of the outcome,
    on the stream the other half is about to be named on, leaving stdout the
    zero bytes a run that answered nothing owes a caller.

    `matrix_answered` is necessary for that sentence and not sufficient, and
    `awards_only` is the rest of it. It is read AFTER the weave, so the Matrix
    half is in hand — but under `awards_only` the merged table is never
    rendered, so "showing Matrix only" names a surface this run does not print
    even though the half behind it answered. Worse, the award renderer has two
    arms that return without writing a byte to stdout, so on those the promise
    is the ENTIRE document at exit 0 with its retraction on stderr. Only the
    both-true arm keeps the sentence, and only there is stdout certain to carry
    a Matrix table under it; the awards arm says the same news on `err`, where
    a run that ends up printing nothing owes nothing.

    That is also why this sentence can sit on stdout at all while its sibling in
    `_paint_first_gf_table` cannot: the sibling is painted from inside the weave
    and can only guess."""
    if not isinstance(e, GfBackendError):
        err.print(f"[yellow]Google Flights query failed:[/] {_safe_text(e)}")
    elif matrix_answered and not awards_only:
        console.print(f"[dim]{_safe_text(_gf_refusal(e).note)} — showing Matrix only.[/]")
    elif matrix_answered:
        err.print(
            f"[yellow]{_safe_text(_gf_refusal(e).note)}[/] — awards only; "
            f"no fare table is printed on this arm."
        )
    else:
        err.print(_gf_refusal(e).message)


def _report_search_matrix_failure(state: dict[str, Any]) -> None:
    """Print the stderr message for a search that produced no Matrix result: a
    known `MatrixApiError` through the shared reporter, an unexpected one out
    of the Matrix task, the weave's own unwinding, or a task that never
    finished.

    EVERY stash held, not the first of them. The three can coexist — the task
    stashes what Matrix could not do while the weave stashes what the group
    could not do — and reporting one of two is the same defect as reporting
    the group instead of its leaves: the user fixes one thing and runs the
    same command again. It is the rule `_failure_text` already applies to
    several failures inside one group, applied to several stashes beside it.

    The last arm is not decoration. A cancellation is a `BaseException`, so
    nothing stashes it, and without a fall-through the command exits non-zero
    with both streams empty — which is the one outcome every reporter here
    exists to prevent."""
    e = state.get("matrix_err")
    said = False
    if e is not None:
        _print_matrix_error(e)
        said = True
    if state.get("matrix_unexpected") is not None:
        err.print(f"[red]Matrix search failed:[/] {_failure_text(state['matrix_unexpected'])}")
        said = True
    if state.get("weave_err") is not None:
        # Neutral, because this group spans both backends; see `_run_the_weave`.
        err.print(f"[red]Search failed:[/] {_failure_text(state['weave_err'])}")
        said = True
    if not said:
        err.print("[yellow]Matrix search did not complete.[/]")


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
    # Imported here rather than deeper in: every enriched run executes these two
    # lines, so a packaging fault in either module fails the same way on every
    # run instead of only on the runs where Matrix happens to land.
    from ._enrich import merge_results  # noqa: PLC0415
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    _pin_cap_note(legs=legs, top_n=top_n)
    matrix_search = SpecificDateSearch(legs=legs, options=opts)
    awards_only = sel.awards_only if sel is not None else False
    state: dict[str, Any] = {}

    async def _go() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_matrix_into, state, matrix_search, rps, impersonate, not no_cache)
            # Google Flights is sync (curl_cffi) — run it in a worker thread so the
            # Matrix request progresses concurrently on the event loop.
            try:
                gf = await anyio.to_thread.run_sync(_gflight_results, legs, opts, top_n)
            except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
                raise
            except Exception as e:  # noqa: BLE001 - reported below; Matrix may still succeed
                state["gf_err"] = e
                gf = []
            state["gf"] = gf
            _paint_first_gf_table(state, gf, legs=legs, top_n=top_n, awards_only=awards_only)

    _run_the_weave(_go, state)

    gf: list[Any] = state.get("gf") or []
    # What reached the USER, which is not the same question as what was
    # fetched. The paint is gated on `not awards_only` and on the renderer not
    # failing, so a non-empty `gf` can still mean a byte-empty stdout — and an
    # exit code that reports success on one is the command lying about it. One
    # expression for both, because two that must agree eventually will not.
    painted = bool(gf) and not awards_only and state.get("paint_err") is None
    # The Google half's report needs both of the things that decide whether a
    # refusal is a footnote or the outcome: whether Matrix answered, and whether
    # a Matrix table is printed at all. "Showing Matrix only" promises a half
    # that never arrives without the first, and a surface this run never renders
    # without the second. Passing the two flags is what carries that; where the
    # read sits relative to the block below is not, since the reporter never
    # touches `state`.
    matrix_res = state.get("matrix")
    if "gf_err" in state:
        _report_enriched_gf_failure(
            state["gf_err"], matrix_answered=matrix_res is not None, awards_only=awards_only
        )
    if matrix_res is None:
        # Every stash is read here: the paint failure is part of why nothing
        # reached the user, and the Matrix one IS the outcome.
        if state.get("paint_err") is not None:
            _report_paint_failure(state["paint_err"])
        _report_search_matrix_failure(state)
        if not painted:
            raise typer.Exit(1)
        return
    matrix_res = cast("SearchResult", matrix_res)
    _report_weave_aftermath(state)

    # Repaint: reconciled GF + Matrix, prices attributed.
    #
    # `shown` is the list the user saw numbered, which is what `--pick N` names
    # and what the pin label claims. The merged rows are price-sorted and can
    # include Google-only itineraries, so their order and their length both
    # differ from `matrix_res.solutions`: indexing those instead pins a row the
    # table numbered differently, under the number read off the screen.
    pinnable: SearchResult | None = None
    if not awards_only:
        merged = merge_results(fli_results_to_search_result(gf), matrix_res)
        _render_merged(merged, legs=legs, top_n=top_n)
        shown = [r.itinerary for r in merged[:top_n]]
        # The same contract `_run_gflight_path` has: the range a pick is
        # measured against is the VISIBLE count. Clamping here also means the
        # duplicate warning inside `_emit_urls` is never reached from this path.
        #
        # An empty board is numbered nowhere, so it gets no clamp sentence at
        # all: `(1-0)` is an empty interval that cannot say what a valid pick
        # would be, and the fallback clause beside it would name a pin that does
        # not happen — this arm renders a header-only table and carries on where
        # the sibling has already returned.
        pick = (
            _pick_in_range(pick, len(shown), links_follow=matrix_url or google_url)
            if shown
            else None
        )
        pinnable = matrix_res.model_copy(update={"solutions": shown})
    else:
        # Nothing this arm prints carries a row number: the award renderer is
        # the only surface it has and its columns hold no `#`. So a pick names
        # no row here — not one out of range, one that does not exist — and it
        # is refused rather than clamped. The links stay unpinned for the same
        # reason: with no numbered list, neither `itinerary #N` nor `cheapest
        # itinerary` is a label the user could check against anything.
        if pick is not None:
            err.print(
                f"[yellow]--pick {pick} names a row in the results table, and this mode "
                f"prints none; the links below are unpinned.[/]"
            )
        pick = None

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

    # A result built from the rows the table numbered, so `_emit_urls`' label
    # expression is true by construction. A Google-only row carries no
    # `Itinerary.id`, so the Matrix line falls back to the plain deep link while
    # the Google line still pins from that row's slices. None where no table was
    # numbered, which is what makes both lines fall back to the unpinned form.
    _emit_urls(
        matrix_search,
        matrix_url=matrix_url,
        google_url=google_url,
        result=pinnable,
        pick=pick or 1,
    )


# ─────────────────────────── multi-cabin orchestration ─────────────────────

# When --cabin selects multiple cabins, each cabin's per-query top-N is bumped
# so the client-side merge has overlap to work with. Cheapest economy and
# cheapest business on a given route are often different carriers entirely
# (e.g. JFK-LHR: VS in economy, FI in business) — a top-5 query per cabin
# almost never overlaps, leaving the J column rendered as all "—".
#
# What the bump widens is how many LEG-1 rows each cabin keeps. It does NOT
# widen a round trip's pinned fan-out, which `_gflight_ids._PINNED_FANOUT_CAP`
# clamps whatever this returns — see that constant for the budget and its cost.
#
# Capped to bound response size (each itinerary costs bytes + parse time);
# Matrix and gflight both tolerate page sizes in this range comfortably.
_MULTI_CABIN_QUERY_BUMP_FACTOR = 5
_MULTI_CABIN_QUERY_BUMP_CAP = 100


def _pin_cap_note(*, legs: tuple[Leg, ...], top_n: int) -> None:
    """Say so when a round trip will search fewer outbounds than were asked for.

    A round trip prices returns against the outbounds Google ranks first, and
    the number of those is capped however large `-n` is. Without a word the user
    reads a short table as the market rather than as the budget, so every
    round-trip path says it: the enriched one, `--fast`, `--format json` and
    multi-cabin alike.

    Ranked first, not cheapest: the pin loop slices the board in the order the
    page served it. A note claiming otherwise is checkably false on the
    repository's own capture, whose lowest fare sits in the second block and is
    never pinned at all below `-n 3`.

    "Up to", because the cap bounds the count and the board may hold fewer. The
    exact number is knowable only inside the pin loop, and carrying it back out
    means a new return type on a recursive function to replace a true sentence
    with a truer one.

    stderr, so a `--format json` document on stdout stays a document."""
    from ._gflight_ids import pinned_fanout  # noqa: PLC0415

    pins = pinned_fanout(top_n)
    if len(legs) >= _ROUND_TRIP_LEGS and pins < top_n:
        err.print(
            f"[dim]Google Flights combines returns against up to {pins} first-ranked outbounds.[/]"
        )


def _multi_cabin_join_note(pins: int) -> str:
    """Why a cabin cell can be empty on a multi-cabin round trip.

    The count comes from the pin budget rather than a literal, because the
    sentence is only true while they agree: the cap is what decides how many
    outbounds the join can see, and `-n` below it lowers the number further.
    Every part is ours, so there is nothing here to escape."""
    return (
        f"Google Flights joins cabins on up to {pins} of each cabin's first-ranked "
        "outbounds; '—' means no shared itinerary, not no fare."
    )


def _bumped_query_top_n(top_n: int, cabin_count: int) -> int:
    """Per-cabin query page size for a multi-cabin search.

    Single-cabin invocations get `top_n` unchanged. Multi-cabin gets
    `top_n * factor` capped at the bump ceiling. The visible row count
    after merge is still `top_n` (renderer trims by sort cabin) — the
    bump only widens the search space the join can draw from.

    On the page transport a round trip's pinned fan-out is capped on its own
    budget, so raising this does not widen the outbounds such a join sees.
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
            err.print(
                f"[yellow]Matrix {cab.value} query failed "
                f"({_safe_text(e.kind)}): {_safe_text(e.message)}[/]"
            )
            return
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            raise
        except Exception as e:  # noqa: BLE001 — see below: one cabin is not the group
            # Soft here for the same reason the arm above it is soft, and for
            # one more: an exception leaving this task cancels its siblings and
            # surfaces as an ExceptionGroup, so one cabin's unreachable network
            # would take the cabins that answered with it.
            err.print(f"[yellow]Matrix {cab.value} query failed: {_safe_text(e)}[/]")
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
    except (typer.Exit, typer.Abort):
        # An orderly exit is not a failure. Redundant with the
        # `_reraise_if_orderly` below on the installed click, and the only guard
        # left if `typer.Exit` ever stops subclassing `Exception`.
        raise
    except Exception as e:
        # No cabin FAILURE reaches here — those are caught per cabin — so this
        # is the shared client failing to open or close at all. A deliberate
        # stop does reach it: `typer.Exit` subclasses `RuntimeError`, the
        # per-cabin arm re-raises it on purpose, and `_reraise_if_orderly`
        # below unwraps it from the group anyio put it in. A `BaseException`
        # leaf is outside both arms whichever way it arrives — anyio hands a
        # generic one back inside a `BaseExceptionGroup` and the runner
        # re-raises a `KeyboardInterrupt` or a `SystemExit` bare — and neither
        # form is an `Exception`, so it leaves by the door a deliberate stop
        # should leave by. Widening either arm to catch it would report that
        # stop as a backend failure. Typed
        # rather than a traceback, and worded like every other Matrix failure.
        # One arm and not two: a `MatrixApiError` cannot arrive here either.
        # `execute()` is the only thing that raises one and every call to it is
        # inside a task, from which anything escaping arrives wrapped in a
        # group that `except MatrixApiError` cannot catch.
        _reraise_if_orderly(e, said="Matrix search failed")
        err.print(f"[red]Matrix search failed:[/] {_failure_text(e)}")
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
    each query runs in a worker thread via `anyio.to_thread.run_sync`.

    Each cabin runs the SAME query builder as the single-cabin path, so the
    native filters and the Tier-2 post-filter cannot drift apart. They also
    share ONE throttle ladder: Google's wall is per-IP, so a cabin per thread
    laddering against it separately spends the cabin count times the requests to
    be told the same thing."""
    from ._gflight_ids import shared_throttle_ladder  # noqa: PLC0415

    results: dict[Cabin, list[Any]] = {}

    def query_sync(cab: Cabin) -> list[Any]:
        return _gflight_results(legs, opts.model_copy(update={"cabin": cab}), top_n)

    async def query_cabin(cab: Cabin) -> None:
        try:
            results[cab] = await anyio.to_thread.run_sync(query_sync, cab)
        except GfBackendError as e:
            # A typed refusal is why this cabin's column will be missing; the
            # bare handler below would print it as an unexplained failure.
            err.print(f"[yellow]Google Flights {cab.value}: {_safe_text(_gf_refusal(e).note)}.[/]")
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            # `typer.Exit` subclasses `RuntimeError` on the installed click, so
            # the arm below would swallow the stop and print the exit CODE as
            # this cabin's error message.
            raise
        except Exception as e:  # noqa: BLE001 — fli has no documented exception surface
            err.print(f"[yellow]Google Flights {cab.value} query failed: {_safe_text(e)}[/]")

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            for cab in cabins:
                tg.start_soon(query_cabin, cab)

    with shared_throttle_ladder():
        try:
            anyio.run(go)
        except (typer.Exit, typer.Abort):
            # An orderly exit is a decision, not a failure. Redundant with the
            # `_reraise_if_orderly` below on the installed click, and the only
            # guard left if `typer.Exit` ever stops subclassing `Exception`.
            raise
        except Exception as e:
            # No cabin FAILURE reaches here — those are caught per cabin — so
            # this is the fan-out itself: opening the loop, starting the group,
            # or the group's own unwinding. A deliberate stop does reach it:
            # `typer.Exit` subclasses `RuntimeError`, the per-cabin arm
            # re-raises it on purpose, and `_reraise_if_orderly` below unwraps
            # it from its group. A `BaseException` leaf passes both arms
            # instead — whether anyio hands it back wrapped or the runner
            # re-raises it bare, it is not an `Exception`; that is the door a
            # deliberate stop leaves by, and widening this to catch it would
            # answer one with a backend's name. Untyped, what this DOES
            # receive is a bare traceback with both streams empty, which is the
            # one outcome every reporter on this path exists to prevent.
            _reraise_if_orderly(e, said="Google Flights search failed")
            err.print(f"[red]Google Flights search failed:[/] {_failure_text(e)}")
            raise typer.Exit(1) from e
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
        err.print(
            f"[red]--sort {escape(repr(sort_by.value))} must be one of --cabin: {escape(names)}[/]"
        )
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
    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
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
    if not json_out:
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
    # The user's count, not the bumped one. The bump widens the pool each cabin
    # keeps so the join has overlap; it is not what anyone asked for, and
    # quoting it tells someone who asked for a handful of rows that returns are
    # combined against the whole pin cap — more than they wanted, from a note
    # whose whole job is to say when they will get fewer.
    _pin_cap_note(legs=legs, top_n=top_n)
    if len(legs) >= _ROUND_TRIP_LEGS and len(cabins) > 1:
        from ._gflight_ids import pinned_fanout  # noqa: PLC0415

        err.print(f"[dim]{_multi_cabin_join_note(pinned_fanout(query_top_n))}[/]")
    fli_by_cabin = _run_gflight_multi(legs=legs, opts=opts, cabins=cabins, top_n=query_top_n)
    if not fli_by_cabin:
        err.print("[red]All Google Flights cabin queries failed.[/]")
        raise typer.Exit(1)

    if json_out and not run_pp:
        out: dict[str, Any] = {}
        for cab, fli_results in fli_by_cabin.items():
            cab_dumped: list[Any] = []
            # The user's count per cabin, not the bumped one the cabins were
            # queried at: the bump exists to give the join overlap to work
            # with, and quoting it back answers a small `-n` with a whole
            # bumped page. The table path gets the same number through
            # `_merge_cabins`.
            for r in _price_ordered(fli_results)[:top_n]:
                items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
                dumped = [_gflight_json_row(g) for g in items]
                cab_dumped.append(dumped if isinstance(r, tuple) else dumped[0])
            out[cab.value] = cab_dumped
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return

    results_by_cabin = _gflight_to_search_result_per_cabin(fli_by_cabin)
    rows = _merge_cabins(results_by_cabin, sort_by=sort_by, top_n=top_n)
    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
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
    `match_carriers` enables codeshare-aware leg labels (see `_leg_display`).

    A round-trip combination therefore prints TWO different prices, on its `Na`
    and `Nb` rows, and that reads as a bug until you know what each is: the `a`
    row carries the outbound board's own quote — the cheapest total reachable
    from that outbound — while the `b` row carries THIS combination's total,
    from the return board fetched with that outbound pinned. Printing each
    member's own number is deliberate, because both are true of the row they
    sit on and the pair is what says which combination costs what. The
    itinerary fare downstream is the terminal member's; the argument and the
    measurements are in the round-trip-pricing paragraph of
    docs/memories/gf_routing_and_carriers.md and in
    `tests/pp/test_gflight_adapter.py`."""
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
    for i, r in enumerate(_price_ordered(results)[:top_n], 1):
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
            # A row Google did not price is SHOWN, with the placeholder every
            # other absent amount in this CLI uses. Dropping it would shorten a
            # board the user asked `-n` rows of and make the count a lie, and a
            # currency prefix over nothing would read as a fare of zero.
            price_cell = "—" if fr.price is None else f"{fr.currency or 'USD'}{fr.price:.2f}"
            t.add_row(
                label,
                price_cell,
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
        err.print(f"[red]Bad rps configuration: {escape(str(e))}[/]")
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

# URL emission flags shared by `search` / `calendar` / `detail`.
#
# Both URLs encode the search criteria. The Google-Flights URL ALSO pins one
# matched itinerary — the row `--pick` names, defaulting to the first row the
# FINAL table printed — when an itinerary row is available; Matrix's URL only
# encodes the search (Matrix's SPA doesn't surface a per-itinerary URL state).
# The enriched path prints two numbered tables and the pin names a row of the
# second, so "the first row printed" would name a different itinerary from the
# one the link opens.
#
# Neither line is printed under `--format json`, and the suppression is at the
# call sites rather than in `_emit_urls`, which cannot know which format asked
# for it. Both help strings say so, because a flag whose text promises output
# it does not produce is the same defect on either of them.
_MATRIX_URL_HELP = (
    "Print the Matrix ITA search URL (pre-fills the search; Matrix's SPA "
    "doesn't expose per-itinerary URL state, so this is the deepest link "
    "available). No link line is printed under --format json."
)
_GOOGLE_URL_HELP = (
    "Print the Google Flights URL. When an itinerary can be resolved from the "
    "results the URL deep-links to the row --pick names, or to the first row of "
    "the final table; the label says which. Otherwise it pre-fills the search. "
    "No link line is printed under --format json."
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
        help=(
            "Allow an itinerary to change airports within a city. "
            "Matrix only: --no-airport-changes routes the search to Matrix."
        ),
        rich_help_panel=_GROUP_FILTERING,
    ),
    only_available: bool = typer.Option(
        True,
        "--only-available/--include-unavailable",
        help=(
            "Show only itineraries with seats available for sale. "
            "Matrix only: --include-unavailable routes the search to Matrix."
        ),
        rich_help_panel=_GROUP_FILTERING,
    ),
    page_size: int = typer.Option(
        10,
        "--n",
        "-n",
        min=1,
        help=(
            "Result count (matrix: page size; gflight: top_n). On Google Flights it "
            "keeps the board in Google's ranking and round-trip combinations in "
            "price order."
        ),
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
        "--matrix-url/--google-url deep links. Default: the first row of the "
        "final table. Ignored under --format json, which emits no link lines "
        "at all.",
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
        stops=stops,
        children=children,
        seniors=seniors,
        youth=youth,
        inf_seat=inf_seat,
        inf_lap=inf_lap,
        origin=origin,
        destination=destination,
        allow_airport_changes=allow_airport_changes,
        show_only_available=only_available,
    )
    if slice_specs:
        legs = tuple(_parse_slice_spec(s) for s in slice_specs)
    elif origin and destination and dep:
        origins, destinations = _require_airports(origin, destination)
        out_times = _parse_times(depart_times)
        ret_times = _parse_times(return_times)
        legs = (
            Leg.of(
                origins,
                destinations,
                _parse_date(dep),
                route_language=routing,
                extension=extension,
                time_ranges=out_times,
            ),
        )
        if ret:
            legs += (
                Leg.of(
                    destinations,
                    origins,
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
        # `_pick_backend` already refused anything the page can't encode, so a
        # constraint that survived to here is one the fan-out honours natively.
        # Re-testing `routing or extension` here would drop it to Matrix with no
        # reason printed.
        if resolved == BACKEND_GFLIGHT:
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
    page_size: int = typer.Option(10, "--n", "-n", min=1),
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
    # This block is `search`'s, near-duplicated. Deliberately not shared: `fare`
    # is deprecated and prints so on every run, and a helper spanning a command
    # on its way out ties the survivor's leg building to the leaving one.
    if slice_specs:
        legs = tuple(_parse_slice_spec(s) for s in slice_specs)
    elif origin and destination and dep:
        origins, destinations = _require_airports(origin, destination)
        out_times = _parse_times(depart_times)
        ret_times = _parse_times(return_times)
        legs = (
            Leg.of(
                origins,
                destinations,
                _parse_date(dep),
                route_language=routing,
                extension=extension,
                time_ranges=out_times,
            ),
        )
        if ret:
            legs += (
                Leg.of(
                    destinations,
                    origins,
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
    top_n: Annotated[int, typer.Option("--n", "-n", min=1)] = 5,
    fmt: str = _FORMAT_OPT,
    json_out: bool = _JSON_OPT,
) -> None:
    """[deprecated] Use `flight search --backend gflight` (or just `flight search`)."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    err.print(
        "[yellow]`flight gflight` is deprecated; use `flight search` "
        "(or `flight search --backend gflight` to force).[/]",
    )
    # Airports split the way `search` splits them, so `JFK,LAX` is a
    # multi-airport query the picker routes to Matrix rather than a string
    # `Leg.of` rejects as one bad IATA code.
    #
    # Legs first: `_pick_backend` announces the backend it chose, and a genuinely
    # bad airport must not be reported after a line claiming the query is already
    # on its way.
    origins, destinations = _require_airports(origin, destination)
    legs = (Leg.of(origins, destinations, _parse_date(dep)),)
    if ret:
        legs += (Leg.of(destinations, origins, _parse_date(ret)),)
    # This alias has no --backend flag, so it resolves like `search` on auto
    # rather than forcing Google Flights: `--children N` can't be priced on the
    # page transport, and taking the backend that can price it beats erroring on
    # a query the alias accepts. `_pick_backend` prints the reason either way.
    resolved = _pick_backend(
        backend=BACKEND_AUTO,
        routing=None,
        extension=None,
        slice_specs=None,
        depart_times=None,
        return_times=None,
        children=children,
        seniors=0,
        youth=0,
        inf_seat=0,
        inf_lap=0,
        origin=origin,
        destination=destination,
        stops=None,
        # The neutral values `_build_options` below hardcodes for this alias:
        # it has no flag for either, so neither can be a reason here.
        allow_airport_changes=True,
        show_only_available=True,
    )
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
        page_size=top_n,
    )
    if resolved == BACKEND_MATRIX:
        _run_matrix_path(
            legs=legs,
            opts=opts,
            rps=_resolve_rps(None),
            impersonate=_resolve_impersonate(None),
            no_cache=_resolve_no_cache(False),
            json_out=json_out,
            matrix_url=False,
            google_url=False,
            run_pp=False,
            # This alias predates the provider flags: cash only, no awards.
            sel=_resolve_providers(
                providers=None, cash_only=True, awards_only=False, provider_opt=()
            ),
            pick=None,
        )
        return
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
        # Escaped, not bare: this URL is printed to be copied, and rich would
        # read a bracketed segment as markup and drop it from what the user
        # pastes — a wrong URL is worse than a loud failure.
        console.print(_safe_text(api_url))
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
        err.print(f"[red]Seatmap lookup failed:[/] {_safe_text(e)}")
        console.print(f"[dim]API URL:[/] {_safe_text(api_url)}")
        raise typer.Exit(1) from e
    if url is None:
        err.print("[yellow]No seatmap on file for this flight/aircraft.[/]")
        console.print(f"[dim]API URL:[/] {_safe_text(api_url)}")
        raise typer.Exit(1)
    console.print(_safe_text(url))


if __name__ == "__main__":
    app()
