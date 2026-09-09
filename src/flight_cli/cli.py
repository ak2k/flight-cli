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

# The `--gf-transport` vocabulary, from the leaf that costs nothing to import.
# `_gflight_ids` owns the ladder but costs fli (~95 ms), and EVERY search
# validates this flag — including the Matrix-only ones that never reach a rung.
# One definition, so the CLI's accepted set cannot drift from the ladder's type.
from ._gf_common import TRANSPORT_BROWSER, TRANSPORT_HTTP, VALID_TRANSPORT_MODES, GfTransportMode
from ._gf_errors import (
    GfBackendError,
    GfBrowserUnavailableError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfTfsUnsupportedError,
    GfThrottledError,
    GfTransportError,
    GfUpstreamStatusError,
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
_DURATION_BOUNDS = 2  # a nights range is min and max, never a third bound
# `--duration` default for `calendar` and `detail`. Shared so both can tell an
# explicit value from an unset one when the trip is one-way and the value is moot.
_DEFAULT_CALENDAR_DURATION = "5-7"

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
    """The amount with its currency prefix stripped, ready for a markup console;
    '—' where there is no price.

    Sanitized here rather than at each print site: Matrix chooses the whole string
    and every caller drops it into a Rich table cell or a summary line, both of
    which parse markup — an unbalanced `[/x]` there raises `MarkupError` and loses
    a query that succeeded."""
    return _safe_text(_split_price(s)[1]) if s else "—"


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


_MAX_ECHOED_VALUE = 60  # characters of a rejected value worth showing back


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


def _elide(value: str) -> str:
    """A value cut to `_MAX_ECHOED_VALUE` code points, with an ellipsis if cut.

    Separate from `_quote` because the cap governs the value the user typed, not
    the message around it: `repr` can double the length of a backslash-heavy
    string, so a bound on the finished message would say nothing about the input
    it is supposed to limit."""
    if len(value) <= _MAX_ECHOED_VALUE:
        return value
    return value[:_MAX_ECHOED_VALUE] + "…"


def _quote(value: str) -> str:
    """A rejected user value, ready to interpolate into a markup console message.

    The message exists to show WHICH value was rejected, so an oversized one is
    cut: a 4301-digit `--duration` echoed whole buries its own point, and the
    parsers accept any string a shell can pass.

    Two orderings matter. `_elide` before `repr`, so the cap counts characters the
    user typed rather than the quotes and escapes `repr` adds. `repr` before
    `escape`, because `repr` doubles the backslash `escape` prepends and hands the
    tag straight back to the markup parser."""
    return escape(repr(_elide(value)))


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        err.print(f"[red]bad date {_quote(s)}; use YYYY-MM-DD[/]")
        raise typer.Exit(2) from e


# A nights bound: 1-9 digits, optionally signed. Narrower than `int()` on both
# axes. Class: `int()` swallows every Unicode space, so `5-\xa07` would parse as a
# range while reading as one token (U+001C..1F are `isspace()`-true but `int()`
# rejects those, so the class is real and that pair is not it). Length: `int()`
# REFUSES a string of 4300+ digits (CPython's int/str conversion cap), so an
# unbounded match hands `_canonical_bound` a traceback instead of a usage error.
# Nine digits is what the PARSE needs bounded and all it bounds: it keeps `int()`
# inside its own conversion cap. How large a nights range may be is a separate
# question, and this regex answers none of it — `_MAX_NIGHTS` does, after the parse.
# `\Z` not `$`, which admits one trailing newline — `--duration '5-7\n'` out of a
# pipeline would then parse as a range instead of being told its shape is wrong.
_RE_DURATION_BOUND = re.compile(r"\A[+-]?\d{1,9}\Z")
# The same bound without the width, to tell "not a number" from "too many digits":
# "use nights as '5' or '5-7'" describes the SHAPE, and `1000000000` is already in
# that shape, so answering it with the shape hands back what the user just typed.
_RE_NUMERIC_BOUND = re.compile(r"\A[+-]?\d+\Z")
# The widest range that is still a nights range. `_render_calendar` gives every
# night between the bounds its own column and every priced day a cell in it, so
# the number typed here multiplies the render: a nine-digit bound is hours of
# work and terabytes of table, spent AFTER Matrix has already answered. A year is
# where the domain runs out — past it the value is a typo, not a trip.
_MAX_NIGHTS = 365


def _canonical_bound(part: str) -> str:
    """One spelling per number, so `05`, `+5` and `5` compare equal. A part that
    is not a number is returned as-is for `_parse_duration` to reject: this
    function decides sameness, never validity."""
    part = part.strip(" \t")
    return str(int(part)) if _RE_DURATION_BOUND.match(part) else part


def _normalize_duration(s: str) -> str:
    """One form for the spellings of a nights range that mean the same thing: `..`
    for `-`, blanks around either bound, and the zero-padded or signed writings of
    a number. Shared with `_resolve_duration`, which decides whether a value
    differs from the default without parsing it, so the parser and that comparison
    agree on which spellings are one range.

    Blanks go per bound and only spaces and tabs, so `5 7` stays the parse error it
    is and a control character stays visible rather than being quietly stripped."""
    return "-".join(_canonical_bound(part) for part in s.replace("..", "-").strip(" \t").split("-"))


def _parse_duration(s: str) -> tuple[int, int]:
    """Nights as '5', '5-7' or '5..7'. Every failure is a typed CLI error: the
    pair feeds `CalendarWindow`, whose validator rejects a reversed range with a
    pydantic ValidationError, and a stack trace is not an answer to a mistyped
    flag.

    Split on the separator rather than parsed bound-first, so an empty bound is
    caught while it is still visible: `5-- 7` is a malformed range, and reading it
    as a max of -7 would answer a typo with a number the user never wrote."""
    parts = _normalize_duration(s).split("-")
    if len(parts) == 1:
        parts *= 2  # a bare '5' is the degenerate range 5-5
    if len(parts) != _DURATION_BOUNDS or not all(_RE_DURATION_BOUND.match(p) for p in parts):
        if len(parts) == _DURATION_BOUNDS and all(_RE_NUMERIC_BOUND.match(p) for p in parts):
            err.print(f"[red]bad duration {_quote(s)}: each bound is at most 9 digits[/]")
        else:
            err.print(f"[red]bad duration {_quote(s)}; use nights as '5' or '5-7'[/]")
        raise typer.Exit(2)
    lo, hi = int(parts[0]), int(parts[1])
    if hi < lo:
        err.print(f"[red]bad duration {_quote(s)}: max ({hi}) is below min ({lo})[/]")
        raise typer.Exit(2)
    if hi > _MAX_NIGHTS:
        # After the ordering check, so `hi` is the larger bound and the message
        # names the one that is out of range.
        err.print(
            f"[red]bad duration {_quote(s)}: {hi} nights is past the {_MAX_NIGHTS}-night maximum[/]"
        )
        raise typer.Exit(2)
    return lo, hi


def _resolve_duration(duration: str, *, round_trip: bool) -> tuple[int, int]:
    """Trip length for the calendar window, resolved against the trip shape.

    A one-way has no length to bound, and nothing reads the number: the wire body
    (`_set_trip_length`) and the SPA URL (`_spa_calendar_leg`) both attach it only
    when there is a return leg. So the shape is resolved BEFORE the value is
    parsed — telling someone their `--duration 9-3` is backwards, and then
    ignoring it, is two contradictory answers to one flag.

    The note goes to stderr under every `--format`: it is a remark about the
    command line, and stdout under `--format json` carries a document or nothing.
    A value spelling the default is indistinguishable from the default and passes
    unremarked — also the one case where nothing looks different. That comparison
    runs on `_normalize_duration`, not on parsed ints: parsing here would fail on
    a bad range and hand a one-way the very error this function exists to avoid."""
    if round_trip:
        return _parse_duration(duration)
    if _normalize_duration(duration) != _DEFAULT_CALENDAR_DURATION:
        err.print("[dim]--duration is ignored for a one-way trip.[/]")
    return _parse_duration(_DEFAULT_CALENDAR_DURATION)


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
                f"[red]bad time-of-day {_quote(raw)}; choose: "
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
    err.print(f"[red]Unknown cabin {_quote(name)}; choose: economy, premium, business, first[/]")
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

# Metro codes fli's airport table DOES have a member for, pointing somewhere
# else: QSF is Ain Arnat in Algeria rather than the Bay Area, SAO is Campo de
# Marte rather than São Paulo's airline airports. A membership test alone reads
# these as serveable, so they are named.
_GF_METRO_COLLISIONS = frozenset({"QSF", "SAO"})


def _gf_unserveable_reasons(backend: str, origin: str | None, destination: str | None) -> list[str]:
    """Reasons a city code keeps this request off Google Flights.

    `docs/memories/airport_groups.md` tells the user to prefer a metro code over
    a comma-list where one exists, and Matrix takes them, but the Google Flights
    bridge resolves an origin by name against fli's airport table: 16 of the 24
    metro codes that memo documents have no member there and two resolve to a
    different city's airport. So the code fails one of two ways — an
    `AttributeError` out of the bridge before any request, or a query silently
    run against the wrong airport — and neither is an answer to what was asked.

    Checked with the same attribute lookup the bridge performs, so this cannot
    drift from what the bridge will accept, and only where Google Flights is
    still in the running: a Matrix run pays neither the import nor the check."""
    if backend == BACKEND_MATRIX:
        return []
    # PLC0415: paid only when Google Flights would otherwise serve the request;
    # fli's package import is slow enough that a Matrix run should not carry it.
    # reportMissingTypeStubs: fli ships none, as at every other seam onto it.
    from fli.models.airport import (  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]
        Airport as FliAirport,
    )

    toks = (*_parse_iata_list(origin or ""), *_parse_iata_list(destination or ""))
    bad = [t for t in toks if not hasattr(FliAirport, t) or t in _GF_METRO_COLLISIONS]
    return [f"a city code rather than an airport ({', '.join(bad)})"] if bad else []


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
    reasons.extend(_gf_unserveable_reasons(backend, origin, destination))
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
            f"[dim]Using Matrix: Google Flights can't serve "
            f"{_safe_text(_join_reasons(reasons))}.[/]"
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
        err.print(f"[red]Failed to load {_quote(str(_config.config_path()))}: {_safe_text(e)}[/]")
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
        # `parse_provider_opt_overrides` builds this message around the user's raw
        # `--provider-opt` token, so the sentence carries whatever was typed.
        err.print(f"[red]{_safe_text(e)}[/]")
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
                f"[red]--awards-only set but "
                f"--providers={_quote(','.join(sel.provider_filter))} "
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
        # `str()` of a Matrix error is its message alone, so a group hands this
        # line the one field of three that a reader cannot act on by itself.
        # Each one inside `e` goes on to the reporter that keeps `kind` and
        # `request_id` — the pair that says whether to fix the query or wait out
        # a brownout.
        for f in _failures_inside(e):
            if isinstance(f, MatrixApiError):
                _print_matrix_error(f)
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
# `split_calendar_search` returns the cartesian product of origins x destination
# GROUPS, so the fan-out is |origins| x ceil(|destinations| / --max-per-query) —
# which is |destinations| only at the default of one per query, with one origin.
# Matrix tolerates the concurrency (measured: ≥16 in flight, flat latency, no
# throttling); we hold a touch under that and let larger lists batch into multiple
# rounds. There is no hard cap — a large fan-out is the user's call; we warn
# loudly (and the concurrency limit keeps it a Ctrl-C-able drip).
_CALENDAR_FANOUT_CONCURRENCY = 12


class _CalendarFanout(NamedTuple):
    """What a fanned-out calendar came back with, and what it lost on the way.

    The losses travel with the results because they change what the results MEAN:
    a merged grid holding no priced day renders as "Calendar empty", which reads as
    "Matrix priced this window and found nothing" — true when the sub-queries
    answered, and the one thing the data cannot support when they did not."""

    results: list[CalendarResult]
    # Every failure, in SUB-QUERY order rather than the order they raised:
    # sub-queries are indexed origins-outermost over the (origin, destination-group)
    # product and finish in whatever order the network gives them, so the same
    # outage names the same groups in the same sequence on every run. All of them,
    # because three sub-queries failing for three different reasons is three things
    # to fix and a reader told only the lowest-index one never learns the others
    # were different.
    failures: list[Exception]
    # The route each of those failures dropped, same order and same length. A
    # Matrix error names the fare it could not price, never the group we asked
    # for, so the cause alone leaves the reader to guess which destination is
    # missing from a grid whose whole point is comparing them.
    lost: list[str]

    @property
    def failed(self) -> int:
        """How many origin/destination groups dropped out of the merge."""
        return len(self.failures)


async def _gather_calendar(
    c: MatrixClient, subs: list[CalendarSearch], *, cache: bool
) -> _CalendarFanout:
    """Run the sub-searches concurrently on one client (its rate-limiter +
    semaphore bound the in-flight count). Each covers one (origin, destination
    group); a sub-query that fails just drops its own group from the merge rather
    than sinking the whole run, and is counted so the caller can say so."""
    results: list[CalendarResult | None] = [None] * len(subs)
    errors: list[Exception | None] = [None] * len(subs)

    async def one(i: int, s: CalendarSearch) -> None:
        try:
            results[i] = cast("CalendarResult", await c.execute(s, cache=cache))
        except (typer.Exit, typer.Abort):
            # Both subclass `RuntimeError` on the installed click, so the broad arm
            # below would read an orderly exit as one more dropped group.
            raise
        except Exception as e:  # noqa: BLE001 — this group drops; the caller counts it
            errors[i] = e

    async with anyio.create_task_group() as tg:
        for i, s in enumerate(subs):
            tg.start_soon(one, i, s)
    return _CalendarFanout(
        [r for r in results if r is not None],
        [e for e in errors if e is not None],
        [_calendar_route_label(s) for s, e in zip(subs, errors, strict=True) if e is not None],
    )


def _exception_leaves(e: BaseException) -> list[BaseException]:
    """Every non-group exception inside `e`, flattened, in member order.

    A group's members sit in task-start order rather than the order they raised,
    so this is the fan-out's own order and names the same failure on every run."""
    if not isinstance(e, BaseExceptionGroup):
        return [e]
    return [
        leaf
        for member in cast("BaseExceptionGroup[BaseException]", e).exceptions
        for leaf in _exception_leaves(member)
    ]


def _print_calendar_failure(
    cause: object, lost: str = "", *, backend: str = "Matrix calendar"
) -> None:
    """The one line a calendar failure prints, wherever in the calendar it failed.

    Both outer guards, both fan-out refusals and the weave's stashed cause end
    here, so one failure reads the same whether it arrived alone, beside a
    deliberate stop, or as one of several sub-queries — and a caller has one
    prefix to match on. `lost` is the count sentence the caller built from its own
    numbers, and `backend` names the half of the command that failed, so a run
    serving the Google Flights grid alone does not report its own renderer under
    Matrix's name; both are wrapped rather than allowlisted because a parameter's
    value belongs to callers this module's markup guard never reads.

    A `MatrixApiError` finishes through `_print_matrix_error`, under the count
    line rather than inside it, so one backend error reads the same whether one
    query asked or twelve did — `str()` of that exception is the message alone,
    and the kind and request id a reader needs to report it would be dropped by
    the sub-query path and kept by the single-query one. A group gets the same
    treatment leaf by leaf, below the line that names them all: a backend error
    inside one is the commonest thing in a group, and `_failure_text` has only
    `str()` of it."""
    if isinstance(cause, MatrixApiError):
        if lost:
            err.print(f"[red]{_safe_text(backend)} failed:[/] {_safe_text(lost)}")
        else:
            # A full stop rather than a colon, because `lost` is the count sentence
            # and a single query has none: a colon there promises a clause that
            # never comes. Printed all the same, so the prefix a caller matches on
            # is on the commonest calendar failure of all and not only the rare ones.
            err.print(f"[red]{_safe_text(backend)} failed.[/]")
        _print_matrix_error(cause)
        return
    err.print(f"[red]{_safe_text(backend)} failed:[/] {_safe_text(lost)}{_failure_text(cause)}")
    if isinstance(cause, BaseExceptionGroup):
        # `isinstance` narrows to the unparameterised generic, which leaves every
        # member unknown; anyio builds these and they hold whatever the tasks raised.
        for leaf in _exception_leaves(cast("BaseExceptionGroup[BaseException]", cause)):
            if isinstance(leaf, MatrixApiError):
                _print_matrix_error(leaf)


def _calendar_cause(e: Exception) -> Exception:
    """The exception worth naming, unwrapped from the group anyio put round it.

    A lone member IS the cause and the group is plumbing. A group of several comes
    back whole, because picking one of them would hide the rest — `_failure_text`
    is what then names each of them, since the group's own `str` is a count. A
    lone member that is not an `Exception` comes back whole too: the callers' arms
    are typed to `Exception`, so unwrapping a cancellation out of its group would
    hand them something they are written not to catch."""
    while isinstance(e, BaseExceptionGroup):
        members = cast("BaseExceptionGroup[BaseException]", e).exceptions
        if len(members) != 1 or not isinstance(members[0], Exception):
            return e
        e = members[0]
    return e


def _calendar_failure(e: Exception) -> Exception:
    """Honour an orderly exit inside `e`, or hand back the failure worth naming.

    Both calendar guards catch whatever leaves `anyio.run`, and both have to tell
    the same three things apart: a deliberate stop, which keeps its own exit code
    and is raised from here; the failures standing beside that stop, which nothing
    downstream would ever say, because the exit ends the command where it is; and
    an ordinary failure, which the caller then prints or stashes. Telling them
    apart once is what keeps the two paths from answering the same exception two
    ways — and the exit leaves with `__context__` intact, so a debugger still
    reaches the group it came out of."""
    orderly = _orderly_exit(e)
    if orderly is not None:
        hidden = [x for x in _exception_leaves(e) if not isinstance(x, (typer.Exit, typer.Abort))]
        if hidden:
            plural = "" if len(hidden) == 1 else "s"
            _print_calendar_failure(
                hidden[0],
                f"{len(hidden):d} failure{plural} beside a deliberate stop; first cause: ",
            )
            rest = hidden[1:]
            if rest:
                # The count above says how many there were, and this is where the
                # rest of them get said. Through the same printer, so each keeps
                # the dispatch a backend error needs: the exit ends the command
                # here, so nothing downstream will ever mention these again.
                _print_calendar_failure(
                    rest[0] if len(rest) == 1 else BaseExceptionGroup("beside a stop", rest),
                    "and beside it: ",
                )
        raise orderly
    return _calendar_cause(e)


def _deliver_calendar(
    write_answer: Callable[[], None], *, backend: str = "Matrix calendar"
) -> None:
    """Write the calendar's answer inside the guard that reports a failure.

    The renderer and the URL emitter are the calls that put the document on stdout,
    and a raise in either is as much a calendar failure as a backend that never
    answered — a malformed price the table cannot format, a reader that closed the
    pipe. Outside a guard it is a rich traceback on the one path whose whole
    contract is that a failure is a typed line and exit 1, and on the weave it
    reports a calendar that was already delivered as a crash.

    An orderly exit passes through: a stop is not a delivery failure, and both
    `typer.Exit` and `typer.Abort` subclass `RuntimeError` on the installed click,
    so the broad arm below would answer one with a backend's name.

    `backend` is the name the failure is reported under, because the caller is the
    only one that knows which half of the command built the document: `--fast`
    serves the Google Flights grid with no Matrix behind it, and its renderer
    reported as a Matrix outage sends a reader after a backend that was never
    asked."""
    try:
        write_answer()
    except (typer.Exit, typer.Abort):
        raise
    except Exception as e:
        _print_calendar_failure(e, backend=backend)
        raise typer.Exit(1) from e


def _report_calendar_fanout(fan: _CalendarFanout, total: int, *, merged_empty: bool) -> None:
    """Say what the fan-out lost, and refuse when nothing is left to show.

    Judged on the MERGED grid, not on the fraction that failed. Rows still in it
    are worth reading even short a group, so that is a note beside them. No
    rows at all is a different claim whatever fraction failed: the table arm prints
    "Calendar empty" and the brownout advice, `--format json` writes
    `solutionCount: 0`, and both say Matrix priced this window and found nothing —
    which is exactly what a sub-query that never answered cannot support. So the
    note only ever prints beside a grid, and its "below" is always true."""
    if fan.failed == 0:
        return
    # Every group that dropped, not the lowest-index one alone: three
    # sub-queries refused for three different reasons is three things to fix, and
    # the count in the sentence is the only true half of a report that names one.
    # A lone failure goes as itself, because a group of one is plumbing.
    causes = fan.failures[0] if fan.failed == 1 else BaseExceptionGroup("sub-queries", fan.failures)
    if fan.failed >= total:
        _print_calendar_failure(causes, f"all {total:d} sub-queries failed; ")
        raise typer.Exit(1)
    if merged_empty:
        _print_calendar_failure(
            causes,
            f"{fan.failed:d} of {total:d} sub-queries failed and nothing that answered "
            "priced a day; ",
        )
        raise typer.Exit(1)
    err.print(
        f"[yellow]{fan.failed:d} of {total:d} sub-queries failed; those origin/destination "
        f"groups are missing from the grid below: {_safe_text(', '.join(fan.lost))}.[/]"
    )


def _calendar_route_label(s: CalendarSearch) -> str:
    """`JFK,EWR→LHR` for a sub-query, for failure messages."""
    if not s.legs:
        return "?"
    leg = s.legs[0]
    return f"{','.join(leg.origins)}→{','.join(leg.destinations)}"


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

    answer: tuple[CalendarResult, int] | None = None

    async def go() -> tuple[CalendarResult, int]:
        nonlocal answer
        async with MatrixClient(
            rps=max(rps, float(conc)), impersonate=impersonate, concurrency=conc
        ) as c:
            if not multi:
                answer = (cast("CalendarResult", await c.execute(search, cache=not no_cache)), 0)
            else:
                fan = await _gather_calendar(c, subs, cache=not no_cache)
                # Merge BEFORE reporting: whether what survived says anything is
                # what decides between a note and a refusal, and only the merge
                # knows.
                merged = merge_calendar_results(fan.results)
                empty = is_empty_calendar(merged)
                _report_calendar_fanout(fan, n, merged_empty=empty)
                answer = (merged, 0) if empty else (merged, n)
            # Recorded inside the `async with`, because the client's own teardown is
            # one of the things that can fail after Matrix has answered, and the
            # guard below has no other way to tell a query that never ran from one
            # that ran and was thrown away on the way out.
            return answer

    # One arm for every way this can fail. The client is built inside `go`, so an
    # unresolvable API key, a refused connection or a DNS failure raises there
    # rather than in `execute` — a `MatrixApiError` arm alone leaves those as a
    # traceback, which is the one outcome a caller reading exit codes cannot act on.
    try:
        return anyio.run(go)
    except Exception as e:  # noqa: BLE001 — every cause leaves as one typed line and exit 1
        # `Exception`, not `BaseException`: a real Ctrl-C is a bare
        # `KeyboardInterrupt` and leaves through here untouched, which is the one
        # interruption that must not be dressed up as a backend failure.
        cause = _calendar_failure(e)
        _print_calendar_failure(cause)  # which sends a MatrixApiError to its own reporter
        if answer is not None:
            # A calendar that arrived and a run that then failed on the way out are
            # both true, and the exit code follows what the reader got: the answer
            # still stands, so the failure is the line above and nothing more.
            return answer
        raise typer.Exit(1) from cause
    except BaseExceptionGroup as group:
        # BELOW the arm above, and it has to stay there: a group whose members are
        # all `Exception`s IS an `Exception` and belongs to the classifier. What
        # reaches here is a sub-query that ended on some other `BaseException`, and
        # the fan-out's own arm catches `Exception`, so it leaves the task group
        # wrapped — where the arm above cannot see it and neither can click.
        # `SystemExit` and `KeyboardInterrupt` are the two that never arrive as a
        # group: nothing here writes to a console from inside the task group, so a
        # closed pipe raises in the caller rather than in a child, and a child's
        # `SystemExit` is re-raised bare. Unwrapped, the process ends the way it
        # would have with no group round it.
        leaves = _exception_leaves(group)
        if len(leaves) == 1:
            raise leaves[0] from None
        raise


def _run_calendar_weave(go: Callable[[], Coroutine[Any, Any, None]], state: dict[str, Any]) -> None:
    """Run the calendar weave, stashing a Matrix failure where its own tasks do.

    The client is built in the weave's `async with` header, so an unresolvable API
    key, a refused connection or a DNS failure raises BEFORE the task group opens —
    outside the `_matrix` task whose handlers would have caught it, and outside the
    typed line every other Matrix path prints. Stashing rather than reporting keeps
    one reporter below: it reads everything this appends, on the branch where Matrix
    answered as well as the one where it did not, and a grid painted before the
    failure still decides the exit code."""
    try:
        anyio.run(go)
    except Exception as e:  # noqa: BLE001 — the reporter below turns any cause into one line
        # Appended, never assigned. The weave's own Matrix task writes to this same
        # list, and a session that refuses to close after a query Matrix already
        # refused is two failures: an assignment keeps whichever was written last,
        # which is the teardown, and drops the reason there is no calendar.
        state.setdefault("matrix_failures", []).append(_calendar_failure(e))
    except BaseExceptionGroup as group:
        # BELOW the arm above, and it has to stay there: a group whose members are
        # all `Exception`s IS an `Exception` and belongs to the classifier. What
        # reaches here holds something that is not — rich answers a reader that hung
        # up with `SystemExit` — and a task group wraps whatever leaves it, so the
        # arm above cannot see it and neither can click. This is the only calendar
        # arm that writes to a console from inside a task group, so it is the only
        # one where a closed pipe turns into a group at all. Unwrapped, the process
        # ends the way it would have with no group round it.
        leaves = _exception_leaves(group)
        if len(leaves) == 1:
            raise leaves[0] from None
        raise


def _report_calendar_failures(state: dict[str, Any], *, answered: bool = False) -> None:
    """Print the stderr line for every failure the weave stashed, on either half of
    it, or a cancel/never-completed fall-through when Matrix stashed nothing at all.

    Every failure is reported, not the first of them and not one per class: the
    weave and its own tasks APPEND, because a Matrix outage and a client that then
    refused to close are two things that happened and one slot keeps only the
    second. Each is named under the backend it came from — the Matrix stash through
    the shared calendar printer, which prints the prefix and then dispatches a
    `MatrixApiError` to the Matrix reporter under it, so the kind and the request id
    a reader quotes arrive below a line a caller can match on; and a first paint
    that raised under Google Flights, because a display failure reported as a Matrix
    one sends an operator after an outage that never happened.

    `answered` says a calendar arrived, which is the only thing that makes an
    empty stash unremarkable. It is what lets the caller on that branch hand the
    whole stash here rather than testing a key itself: the weave stashes after the
    answer as readily as instead of it, and a branch that reads one key is how a
    failure goes silent."""
    matrix = cast("list[BaseException]", state.get("matrix_failures", []))
    for cause in matrix:
        _print_calendar_failure(cause)
    for cause in cast("list[BaseException]", state.get("gf_failures", [])):
        err.print(f"[yellow]{_safe_text(_GF_GRID_NAME)} could not be shown:[/] {_safe_text(cause)}")
    if not matrix and not answered:
        err.print("[yellow]Matrix calendar did not complete.[/]")


# Says "no grid" rather than staying silent, which a reader would take for "no
# cheap fares". Two constants because the weave prints while Matrix is still in
# flight — it can promise to wait, not to deliver — and `--fast` has no Matrix
# coming at all.
_GF_GRID_UNAVAILABLE_NOTE = (
    "Google Flights price grid unavailable: the calendar RPC currently returns "
    "no data to this client (tracked in work-h70kv.5)."
)
_GF_GRID_UNAVAILABLE_WEAVE_NOTE = f"{_GF_GRID_UNAVAILABLE_NOTE} …awaiting Matrix calendar…"

# The grid's name as a reader sees it, in one place because it is the prefix a
# caller matches a failure on: two spellings across the arms of one command means
# a matcher has to know both, and which one it gets depends on where the run
# broke. Every remaining "date-grid" in this file is in a docstring or a comment,
# where it is English about the thing rather than the name a reader is shown,
# which is why none of them is built from here.
_GF_GRID_NAME = "Google Flights date grid"


def _paint_calendar_first(
    grid: dict[str, float],
    state: dict[str, Any],
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
) -> None:
    """What the weave shows while the Matrix calendar is still in flight.

    Every branch ends by naming what the reader is waiting for, because the grid is
    the fast half and Matrix is ~45s behind it: an unexplained pause reads as "no
    cheap fares", and by the time the real grid lands the impression is formed.

    Only the grid itself goes to stdout, and every branch's status line goes to
    stderr — the one beside a painted grid included. Four of the five have no
    document to show and are saying so, Matrix may yet fail behind any of them,
    which is exit 1, and a status line on stdout is prose in the stream a caller
    reads for the answer; `_run_fast_calendar_grid` puts every equivalent line on
    stderr for the same reason."""
    if grid:
        _render_date_grid(grid, origin=origins, destination=dests, sd=sd, ed=ed)
        err.print("[dim]…refining with Matrix (full grid + durations)…[/]")
    elif state.get("gf_throttled"):
        err.print("[dim]Google Flights rate-limited — awaiting Matrix calendar…[/]")
    elif state.get("gf_unavailable"):
        err.print(f"[dim]{_GF_GRID_UNAVAILABLE_WEAVE_NOTE}[/]")
    elif "gf_err" in state:
        err.print(f"[yellow]{_safe_text(_GF_GRID_NAME)} failed:[/] {_safe_text(state['gf_err'])}")
        err.print("[dim]…awaiting Matrix calendar…[/]")
    else:
        err.print("[dim]…awaiting Matrix calendar…[/]")


def _run_fast_calendar_grid(
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    matrix_url: bool,
    google_url: bool,
) -> None:
    """`--fast`: the Google Flights date-grid alone, or a refusal. No Matrix."""
    from ._gf_dategrid import GfGridUnavailableError, date_grid  # noqa: PLC0415
    from ._gflight_ids import GfThrottledError  # noqa: PLC0415

    grid: dict[str, float] = {}
    try:
        grid = date_grid(search)
    # Each handler only says WHY there is no grid; the single exit below says THAT
    # there is none. Under `--fast` there is no Matrix to fall back to, so every
    # no-grid outcome — gate, throttle, an empty grid, or a bad airport or date
    # landing in the broad except — has to leave the same way, or a wrapper doing
    # `--fast || fallback` reads success where it should read failure.
    #
    # All of it on stderr, like the up-front refusals in `_grid_branch_blocker`: a
    # `--fast` run leaves stdout carrying a grid or nothing at all, so a caller can
    # read the stream without first parsing it to find out whether this was an
    # answer or an explanation.
    except GfThrottledError:
        err.print("[dim]Google Flights rate-limited; no grid to show.[/]")
    except GfGridUnavailableError:
        # Ahead of the broad except, as in the weave.
        err.print(f"[dim]{_GF_GRID_UNAVAILABLE_NOTE}[/]")
    except (typer.Exit, typer.Abort):
        raise  # an orderly exit is not a grid failure; see the weave's arm
    except Exception as e:  # noqa: BLE001 — any other cause is still just "no grid"
        err.print(f"[yellow]{_safe_text(_GF_GRID_NAME)} failed:[/] {_safe_text(e)}")
    if grid:

        def _write_answer() -> None:
            _render_date_grid(grid, origin=origins, destination=dests, sd=sd, ed=ed)
            _emit_urls(search, matrix_url=matrix_url, google_url=google_url)

        _deliver_calendar(_write_answer, backend=_GF_GRID_NAME)
    else:
        # `--fast` means the GF grid alone in ~1s; quietly running the ~45s Matrix
        # calendar instead would change what the flag means.
        err.print("[yellow]No Google Flights grid; drop --fast for Matrix.[/]")
        raise typer.Exit(1)


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
    Mirrors the SHAPE of `_run_enriched_path` (the search-path weave) and not its
    stream discipline: every status line here goes to stderr, while that one still
    writes some of its own to stdout. `--fast` never reaches here
    (the command serves the grid alone for that). The `grid_can_serve` gate guarantees
    a single-airport query, so the Matrix side is one `execute` (no fan-out).

    While the GF grid RPC is gated (`GfGridUnavailableError`), the grid arm raises
    before it opens a client, so the first-paint branch below is runtime-dead and
    what a user sees is the unavailable note plus the Matrix calendar."""
    from ._gf_dategrid import GfGridUnavailableError, date_grid  # noqa: PLC0415
    from ._gflight_ids import GfThrottledError  # noqa: PLC0415

    # Single-airport calendar runs as one Matrix query; mirror `_run_calendar`'s
    # non-multi concurrency/rps so the request paces identically.
    conc = 3
    state: dict[str, Any] = {}

    async def _matrix(c: MatrixClient) -> None:
        try:
            state["matrix"] = await c.execute(search, cache=not no_cache)
        except (typer.Exit, typer.Abort):
            # `RuntimeError` subclasses on the installed click: the broad arm below
            # would stash an orderly exit and report it as a Matrix failure.
            raise
        except Exception as e:  # noqa: BLE001
            # Every Matrix failure lands here, typed or not. A raw httpx transport or
            # status error that execute() doesn't wrap must NOT propagate out of this
            # task and tear down the group — that would cancel the still-pending grid
            # paint and surface a bare traceback. Stashed and reported after the weave
            # so the GF grid still shows, and appended rather than assigned because
            # the guard outside the loop writes to this same list. The reporter
            # dispatches on the class, so a `MatrixApiError` keeps its kind and id.
            state.setdefault("matrix_failures", []).append(e)

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
            except GfGridUnavailableError:
                # Ahead of the broad except, which would report a standing gate as
                # "date grid failed: …". Matrix still prices the window.
                state["gf_unavailable"] = True
            except (typer.Exit, typer.Abort):
                raise  # an orderly exit is not a grid failure; see `_matrix` above
            except Exception as e:  # noqa: BLE001 — GF is the optional fast layer; Matrix still runs
                state["gf_err"] = e
            try:
                _paint_calendar_first(grid, state, origins=origins, dests=dests, sd=sd, ed=ed)
            except (typer.Exit, typer.Abort):
                raise  # an orderly exit is not a paint failure; see `_matrix` above
            except Exception as e:  # noqa: BLE001 — the display half fails on its own terms
                # The first paint is the Google Flights half's own output, so a raise
                # here belongs to that backend: on the Matrix list it would print
                # "Matrix calendar failed" for a renderer, and the still-pending
                # Matrix calendar would be cancelled by a failure that is not its own.
                state.setdefault("gf_failures", []).append(e)
            else:
                # After the paint, and a boolean rather than the grid itself: the exit
                # gate below asks whether the reader was given something, and a grid
                # that was fetched and then died in the renderer is not that.
                state["painted"] = bool(grid)

    _run_calendar_weave(_go, state)

    matrix_res = state.get("matrix")
    if matrix_res is None:
        # Matrix failed; the GF grid (if any) was already painted.
        _report_calendar_failures(state)
        if not state.get("painted"):
            raise typer.Exit(1)
        return
    # Matrix answered, and the run may still have failed after it — a client
    # teardown, a renderer, a closed pipe. The same reporter, because the stash is
    # the same stash: reading one of its keys here is how a failure that happened
    # after the answer stayed silent. Said BEFORE the render, since whatever broke
    # may break that too, and said as a line rather than an exit code, because the
    # answer below still stands.
    _report_calendar_failures(state, answered=True)
    res = cast("CalendarResult", matrix_res)

    def _write_answer() -> None:
        _render_calendar(
            res,
            dmin=dmin,
            dmax=dmax,
            origin=origins,
            destination=dests,
            sd=sd,
            ed=ed,
            round_trip=len(search.legs) == _ROUND_TRIP_LEGS,
        )
        _emit_urls(search, matrix_url=matrix_url, google_url=google_url)

    _deliver_calendar(_write_answer)


def _pinned_solution_index(
    result: SearchResult | None,
    pick: int | None,
    rendered: int | None = None,
) -> int | None:
    """0-based index into `result.solutions` of the itinerary to pin in a deep
    link. `pick` is the 1-based itinerary number the user saw in the table;
    None pins the cheapest (row 1). Out-of-range picks warn and fall back to
    the cheapest rather than emit a wrong or broken link. None when there's
    nothing to pin."""
    if result is None or not result.solutions:
        return None
    if pick is None:
        return 0
    # Bound by what the user could actually SEE. Validating against
    # `len(result.solutions)` accepted a `--pick` beyond the rendered table and
    # then labelled the link "itinerary #N pinned" for a row never displayed.
    upper = len(result.solutions) if rendered is None else min(rendered, len(result.solutions))
    if pick < 1 or pick > upper:
        console.print(
            f"[yellow]--pick {pick:d} is out of range (1-{upper:d}); "
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


def _overlay_awards(
    matrix_res: SearchResult,
    *,
    legs: tuple[Leg, ...],
    opts: Any,
    sel: Any,
    awards_only: bool,
) -> None:
    """Render the award overlay for an already-fetched Matrix result."""
    p = opts.pax
    run_pp_for_search(
        matrix_res,
        legs=_build_pp_legs(legs),
        num_passengers=_seated_pax(p),
        airlines=sel.pp_airlines() if sel is not None else None,
        cabins=sel.pp_cabins() if sel is not None else None,
        pp_only=awards_only,
        json_out=False,
        provider_filter=sel.provider_filter if sel is not None else None,
        seats_sources=sel.seats_sources() if sel is not None else None,
        cash_per_cabin=_cash_per_cabin_single(matrix_res, opts.cabin),
    )


def _emit_urls(
    search: Search,
    *,
    matrix_url: bool,
    google_url: bool,
    result: SearchResult | None = None,
    pick: int | None = None,
    rendered: int | None = None,
) -> None:
    idx = _pinned_solution_index(result, pick, rendered)
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
                for note in _gflight_url_caveats(search):
                    console.print(f"  [yellow]note: {_safe_text(note)}[/]")
        except Exception as e:  # noqa: BLE001 - third-party undocumented errors; non-fatal fallback
            console.print(f"[dim]Google Flights link: {_safe_text(e)}[/]")


# ─────────────────────────── result renderers ──────────────────────────────


def _gflight_url_caveats(search: Search) -> list[str]:
    """Ways the emitted Google link is NARROWER than the search it came from.

    `fast_flights`' tfs= encoder takes exactly one origin and one destination
    per leg and has no routing-language field, so a multi-airport or routed
    search silently degrades: rows flying EWR->LGW under `--routing AA+` sat
    beside a link that searched JFK->LHR unconstrained, and nothing said so.
    The Matrix link on the same output IS faithful, which made the two
    disagree with no explanation.

    We still emit the link — it is a useful starting point, and booking hands
    off to Google — but the ways it differs are now stated.
    """
    legs: tuple[Leg, ...] = getattr(search, "legs", ()) or ()
    notes: list[str] = []
    if legs and any(len(lg.origins) > 1 or len(lg.destinations) > 1 for lg in legs):
        first = legs[0]
        notes.append(
            f"multi-airport search narrowed to {first.origins[0]}→{first.destinations[0]} "
            "(Google's link format takes one airport pair)"
        )
    if any(lg.route_language or lg.extension for lg in legs):
        notes.append("routing/extension codes are not expressible in a Google link")
    return notes


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
        # The fallback is the branch where this function could NOT read Matrix's
        # two strings, so what it puts in the cell is whatever Matrix sent. The
        # parsed branch below formats two datetimes and carries none of it.
        return f"{_safe_text(dep[:16])}→{_safe_text(arr[:16])}"
    day_off = (a.date() - d.date()).days
    suffix = f" +{day_off}d" if day_off > 0 else (f" {day_off}d" if day_off < 0 else "")
    return f"{d:%b%d %H:%M}→{a:%H:%M}{suffix}"


def _fmt_slice_route(s: Slice) -> str:
    """Origin→destination threading any intermediate connection airports, so a
    1-stop itinerary shows its connection city instead of hiding it.

    Matrix chooses all three codes and the result is a table cell, which parses
    markup: wrapped per code rather than around the join, so the cell composed
    from this can still carry the tags `_fmt_legroom_one` writes on purpose."""
    o = _safe_text((s.origin.code if s.origin else None) or "?")
    d = _safe_text((s.destination.code if s.destination else None) or "?")
    vias = [_safe_text(e.code) for e in s.stops if e and e.code]
    return "→".join([o, *vias, d])


def _fmt_slice_cell(s: Slice) -> str:
    """One itinerary slice as a table cell: route (with connection cities),
    flight numbers, compact unambiguous times, duration, then per-leg legroom
    lines. Shared by the single-cabin and multi-cabin itinerary tables.

    Every remote leaf is wrapped where it is read — here, in `_fmt_slice_route`,
    `_fmt_slice_times` and `_fmt_legroom_one` — and the composed cell is not
    wrapped again: `_fmt_legroom_one` emits `[red]` on purpose, and one wrap
    around the whole cell would show that tag instead of colouring the pitch."""
    dur_min = s.duration or 0
    dur = f"{dur_min // 60}h{dur_min % 60:02d}m" if dur_min else ""
    flights = _safe_text("/".join(s.flights)) or "?"
    times = _fmt_slice_times(s.departure or "", s.arrival or "")
    head = " ".join(p for p in (_fmt_slice_route(s), flights, times, dur) if p)
    tail = _fmt_legroom_lines(s)
    return f"{head}\n{tail}" if tail else head


def _seated_pax(p: Pax) -> int:
    """Occupants needing their own seat.

    An infant IN SEAT buys a seat, so it counts; only a LAP infant does not.
    Omitting it made the award query ask for fewer seats than the cash query
    on the same run, so an award with too little availability rendered as
    bookable for the party.
    """
    return p.adults + p.children + p.seniors + p.youth + p.infants_in_seat


_DEFAULT_RENDER_LIMIT = 10  # matches the `-n/--page-size` default


def _render_search(res: SearchResult, limit: int = _DEFAULT_RENDER_LIMIT) -> None:
    """Render the itinerary table, showing at most `limit` rows.

    `limit` MUST be the same bound `--pick` is validated against. It was
    hardcoded to 10 while `--pick` checked against `len(res.solutions)`, so
    `-n 15 --pick 15` printed 10 rows and then emitted a booking link labelled
    "itinerary #15 pinned" for a row the user never saw — with no out-of-range
    warning, because 15 was in range for the unrendered list.
    """
    if res.solution_count == 0:
        console.print("[yellow]No solutions returned.[/]")
        return
    # Matrix chooses the price, the currency, the carrier codes and short names,
    # the stop labels, and — through `_fmt_slice_cell` — the airport codes, flight
    # numbers and timestamps in the itinerary cells. The summary line, the two
    # table titles and every header and cell parse markup, so each of those values
    # is wrapped where it is read and a hostile field cannot lose the render of a
    # query that succeeded.
    ccy, cheapest = _split_price(res.cheapest_price)
    ccy_tag = f" ({_safe_text(ccy)})" if ccy else ""
    console.print(
        f"[bold]{res.solution_count} solutions[/]  · "
        f"cheapest: [bold cyan]{_safe_text(cheapest or '—')}{ccy_tag}[/]"
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
            t.add_column(f"{_safe_text(code or '?')}\n{_safe_text(sn[:14])}")
        for row in cm.rows:
            cells = [_safe_text(row.label) if row.label is not None else "?"]
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
    for i, it in enumerate(res.solutions[:limit], 1):
        itn = it.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        it_carriers = ",".join(_safe_text(c.code or "?") for c in (itn.carriers if itn else []))

        out = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        st.add_row(f"{i:d}", _amount(it.price), it_carriers or "?", out, ret)
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
        # Not one of the three judgments above, so it is a seat-type name the
        # backend chose ("Lie Flat", "Suite") and reaches the cell as it came.
        parts.append(_safe_text(leg.legroom_class))
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
    # The colour tag on the pitch token is ours and stays live. The flight number
    # is the backend's, and escaping LENGTHENS it — one backslash per markup-shaped
    # bracket — so padding the escaped value would count a character the reader
    # never sees and drift the column. Strip, pad, then escape: the width is
    # measured on what renders. The exception is the tab `_CTRL` deliberately
    # keeps for sentence-shaped text — inside a fixed pad it counts as one
    # character and renders as eight.
    shown = str(flight_no).translate(_CTRL)
    return f"  {escape(f'{shown:<6}')} " + " ".join(parts)


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
    priced_days = len(grid)
    cheapest = min(grid.values())
    console.print(
        f"[bold]{priced_days} priced days[/]  · cheapest: "
        f"[bold cyan]{cheapest:.0f} (USD)[/]  · "
        f"window {_safe_text(sd.isoformat())} → {_safe_text(ed.isoformat())}"
    )
    t = Table(
        title=f"{_safe_text(','.join(origin))} → {_safe_text(','.join(destination))}: "
        "lowest fare per departure day (Google Flights)",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("departure", justify="right")
    t.add_column("min (USD)", justify="right")
    for day, price in sorted(grid.items(), key=lambda kv: kv[1]):
        # The day is a key off the Google Flights grid, not a date this module built.
        t.add_row(_safe_text(day), f"{price:.0f}")
    console.print(t)


def _grid_branch_blocker(
    search: CalendarSearch,
    *,
    json_out: bool,
    one_way: bool,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
) -> str | None:
    """Why the GF date-grid can't serve this calendar, or None if it can.

    The string is user-facing: it completes "this is …" in the `--fast` refusal, so
    every branch returns a noun phrase. The first three name the SHAPE, which is
    the whole story for them; the routing branch names the flag and the tier
    instead, because a constraint the grid can't honor is not visible in the shape
    of the command line. Ordered cheapest-first so the fli-heavy `_gf_dategrid`
    import is still skipped for the shapes that never need it.
    """
    if json_out:
        return "JSON output"
    if not one_way:
        return "a round-trip window"
    if len(origins) > 1 or len(dests) > 1:
        return "a multi-airport route"
    from ._gf_dategrid import grid_can_serve, grid_routing_blocker  # noqa: PLC0415

    if not grid_can_serve(search):
        # `grid_can_serve` is False for Tier-2 AND Tier-3, so ask which: the grid
        # returns no itineraries (Tier-2's problem) and cannot reach fare
        # construction at all (Tier-3's), and only one of those is a routing tier
        # the reader can do anything about. The fallback covers a future gate
        # condition that routing does not explain.
        return grid_routing_blocker(search) or "a constraint the price grid can't honor"
    return None


def _render_calendar(
    res: CalendarResult,
    *,
    dmin: int,
    dmax: int,
    origin: tuple[str, ...],
    destination: tuple[str, ...],
    sd: date,
    ed: date,
    round_trip: bool,
) -> None:
    """Render the lowest-fare grid. `round_trip` decides whether the trip-LENGTH
    dimension exists at all: `wire._set_trip_length` attaches `layover` only when
    there is a return leg, so a one-way request never asked for per-night prices
    and Matrix never sent any. Showing the columns anyway prints a wall of '—'
    under a duration range the backend never saw."""
    if res.solution_count == 0 or not res.priced_days:
        console.print(
            "[yellow]Calendar empty.[/] Matrix's calendar mode "
            "brownouts regularly; retry, or use [bold]flight fare[/] "
            "for a single date."
        )
        return
    # Matrix chose the whole price string. This line parses markup and so does
    # the table title below, which carries the currency half a second time.
    ccy, cheapest = _split_price(res.cheapest_price)
    ccy_tag = f" ({_safe_text(ccy)})" if ccy else ""
    duration_note = f"  · duration {dmin}-{dmax} nights" if round_trip else ""
    console.print(
        f"[bold]{res.solution_count} solutions[/]  · "
        f"overall cheapest: [bold cyan]{_safe_text(cheapest or '—')}{ccy_tag}[/]  · "
        f"window {_safe_text(sd.isoformat())} → {_safe_text(ed.isoformat())}"
        f"{duration_note}"
    )
    t = Table(
        title=f"{_safe_text(','.join(origin))} → {_safe_text(','.join(destination))}: "
        f"lowest fare per departure day{ccy_tag}",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("departure", justify="right")
    t.add_column("min", justify="right")
    if round_trip:
        for dur in range(dmin, dmax + 1):
            t.add_column(f"{dur:d}n", justify="right")
    t.add_column("sols", justify="right")
    for d in sorted(res.priced_days, key=lambda x: x.price_value or 9e9):
        row = [f"{d.date:d}", _amount(d.min_price)]
        if round_trip:
            opts = {o.trip_length: o.min_price for o in d.options}
            for dur in range(dmin, dmax + 1):
                row.append(_amount(opts.get(dur)))
        row.append(f"{d.solution_count:d}")
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
        _render_search(res, opts.page_size)
    if run_pp:
        p = opts.pax
        run_pp_for_search(
            res,
            legs=_build_pp_legs(legs),
            num_passengers=_seated_pax(p),
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


# How much deeper to fetch when a Tier-2 routing post-filter will discard rows.
# Bounded rather than unlimited: Google's own result depth is finite and each
# extra page costs a round trip.
_POSTFILTER_OVERFETCH = 5


def _gflight_results(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
) -> list[Any]:
    """Query Google Flights for `legs`, honoring routing/extension: Tier-1
    predicates narrow the fli query natively, the Tier-2 post-filter drops
    violating solutions. Returns the (filtered) raw fli result list.

    `search` applies the same routing/extension to every leg, so the first leg's
    constraints cover the trip for the native query; the post-filter is per slice.

    This is also where a rung-2 browser session dies. It is created lazily on
    whichever thread runs this call — the enrich path runs it inside
    `anyio.to_thread.run_sync` — and a playwright object may only be closed by
    the thread that made it, so the `finally` here is the guarantee. A SIGINT
    landing while that thread sits in `page.goto` escapes it, which is why the
    profile-lock refusal names the interrupted-run case.
    """
    # Every import in this block is deferred for one reason: the Google Flights
    # backend must not load on a Matrix-only search, and this function is the
    # first point that has committed to Google Flights.
    from ._gf_postfilter import surviving_indices  # noqa: PLC0415 — GF-only; see above
    from ._gflight_ids import GfTransport, search_with_ids  # noqa: PLC0415 — fli, ~95 ms
    from .fli_bridge import apply_gf_native_filters, to_fli_filter  # noqa: PLC0415 — fli
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415 — GF-only
    from .routing_predicates import classify  # noqa: PLC0415 — pulled in by the two above

    # Built here rather than at the CLI seam: this is the first point that has
    # already paid for `_gflight_ids`.
    # Always built, never a sentinel. `GfTransport(mode="http")` IS
    # `search_with_ids`' own default, so the old `None` branch distinguished two
    # equal values and made every reader check which one this was.
    #
    # No cast: `gf_mode` is already a `GfTransportMode`. `_resolve_gf_transport`
    # narrowed typer's plain `str` once, at the CLI seam, and every path here
    # runs through it.
    transport = GfTransport(mode=gf_mode, headed=gf_headed)
    fli_filter = to_fli_filter(SpecificDateSearch(legs=legs, options=opts))
    out_constraints = classify(legs[0].route_language, legs[0].extension) if legs else None
    if out_constraints and out_constraints.predicates:
        apply_gf_native_filters(fli_filter, out_constraints.predicates)
    per_slice_preds = [list(classify(lg.route_language, lg.extension).predicates) for lg in legs]

    # Fetch deeper than we need when a Tier-2 post-filter will run, because it
    # drops rows AFTER truncation: `-n 1 --routing AA+` fetched exactly one
    # itinerary, discarded it for violating the routing, and reported "no
    # results" while a qualifying one sat at rank 2. Over-fetching lets the
    # filter choose from a real candidate pool; the final slice below still
    # honours the user's `top_n`.
    fetch_n = top_n * _POSTFILTER_OVERFETCH if any(per_slice_preds) else top_n
    try:
        results: list[Any] = search_with_ids(fli_filter, top_n=fetch_n, transport=transport) or []
    finally:
        # Named positively, because only rung 2 opens anything to close. The
        # call would be a no-op on the others, but reaching for it would read
        # as though an http search might hold a Chrome — the one thing that
        # transport promises it never does, and `auto` is that transport under
        # another name until the escalation rung lands.
        if transport.mode == TRANSPORT_BROWSER:
            from ._gf_browser import close_thread_session  # noqa: PLC0415 — GF-only; see above

            close_thread_session()
    if results and any(per_slice_preds):
        keep = set(surviving_indices(fli_results_to_search_result(results), per_slice_preds))
        results = [r for i, r in enumerate(results) if i in keep]
    # Untrimmed on purpose: a round trip's combinations are built pin-major, so
    # the first `top_n` of them are one outbound's returns and nothing else.
    # Every caller trims what it renders, in the order that surface ranks by.
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
    err.print(f"[yellow]--pick {pick:d} is out of range (1-{rows:d}){fallback}[/]")
    return None


class _GfRefusal(NamedTuple):
    """How one Google Flights refusal reads: `note` where Matrix still answers
    and the refusal is a footnote, `message` where it is the whole outcome.

    The two fields are not interchangeable, and the difference is markup.
    `message` is PRE-RENDERED rich markup — it carries its own tags and any
    exception text in it is already escaped, so print it as-is and never escape
    it again. `note` carries no tags of its own, but the exception text in it
    has been through `_safe_text` as well, so it is equally console-ready: a
    caller drops it straight into markup of its own. Escaping it there a second
    time puts a visible backslash in front of every bracket the remote text
    carried, on the default search path."""

    note: str
    message: str


_GF_DECLINED = "Google Flights declined the request"


def _gf_refusal(  # noqa: PLR0911 — one return per refusal type; see the docstring
    e: GfBackendError,
    *,
    transport: GfTransportMode = TRANSPORT_HTTP,
) -> _GfRefusal:
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
    deleting a subclass arm type-checks clean.

    **Every interpolated string arrives escaped, and both fields leave here
    console-ready.** The app runs rich in markup mode, so an unescaped
    `[browser]` in a remedy, or a `[0m` in patchright's driver text, is either
    deleted from the output or raises `MarkupError` from `print` — the second
    one turning a typed refusal into a crash. Anything read off an exception
    that carries REMOTE text goes through `_safe_text`, which drops the control
    characters before it escapes. The one arm that only escapes reads a reason
    this package wrote itself, so it has no control character to drop; literal
    markup in these templates is ours and stays unescaped. A caller prints
    `note` as it stands, and escaping it a second time is not free: it puts a
    visible backslash in front of every bracket the remote text carried, on the
    default search path.

    `transport` only changes the throttle wording. The browser rung runs no
    retry ladder, so "wait a moment and retry" would describe a recovery the
    caller does not have."""
    match e:
        case GfThrottledError() if transport == TRANSPORT_BROWSER:
            return _GfRefusal(
                "Google Flights rate-limited the browser rung",
                "[yellow]Google Flights rate-limited the browser rung.[/] It does not "
                "retry, so use [bold]--backend matrix[/].",
            )
        case GfThrottledError():
            # Not "this IP": the budget is per client context, which is why the
            # browser rung keeps working from an IP that is throttling this one.
            return _GfRefusal(
                "Google Flights rate-limited",
                "[yellow]Google Flights rate-limited the request.[/] Wait a moment and "
                "retry, use [bold]--gf-transport browser[/], or use [bold]--backend matrix[/].",
            )
        case GfConsentError():
            return _GfRefusal(
                "Google Flights served its consent page",
                "[yellow]Google served its consent page instead of flight results.[/] "
                "Use [bold]--backend matrix[/].",
            )
        case GfBrowserUnavailableError():
            # The note carries the reason AND the remedy. It is not the short
            # form of the message here: the enrich path is the DEFAULT search
            # and prints only the note, so a note built from the remedy alone
            # collapses "no Chrome", "nav timed out", "no response" and "body
            # unreadable" into one line that says "Retry".
            return _GfRefusal(
                f"Google Flights' browser rung is unavailable — "
                f"{_safe_text(e.reason)} {_safe_text(e.remedy)}",
                f"[yellow]{_safe_text(e)}[/]",
            )
        case GfUpstreamStatusError():
            # Through `_safe_text` like any other value read off an exception.
            # The annotation says `int`, and the constructor is reached with
            # whatever a caller passes it.
            status = _safe_text(e.status_code)
            return _GfRefusal(
                f"Google Flights returned HTTP {status}",
                f"[yellow]Google Flights returned HTTP {status}.[/] Use [bold]--backend matrix[/], "
                "or fetch the page the other way with [bold]--gf-transport http[/] or "
                "[bold]browser[/].",
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
        title=f"Google Flights + Matrix · {_safe_text(origin)}→{_safe_text(destination)}"
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
            f"{i:d}",
            # `rows` is duck-typed, and the lookup falls back to the tag it was
            # handed when it is not one of the three this module writes.
            _safe_text(_MERGE_SOURCE_TAG.get(row.source, row.source)),
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
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
) -> None:
    """Google Flights path: build fli filter → query → render. Single-leg or round-trip.

    When run_pp=True, fli's results are adapted into a SearchResult shape so
    the existing PP matcher + renderer reuse cleanly. PP runs on the same
    (origin, dest, date) per leg as the matrix path.

    This is where `top_n` becomes the answer's size. The query cannot ask for a
    count, so everything below the trim — the table, the JSON document, the
    pinned link and the awards — is drawn from the same `top_n` rows, and
    everything above it reads the whole board.

    `gf_mode` defaults to rung 1 — the deprecated `gflight` command has no
    transport flag, so it never asks for another.
    """
    _pin_cap_note(legs=legs, top_n=top_n)
    # Deferred like the adapter below: this arm reaches rung 2 only when the
    # transport says so, and the module pulls in nothing patchright at import.
    from ._gf_browser import interrupt_guard  # noqa: PLC0415 — GF-only; see above
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    try:
        # Armed around the whole search, not around the browser: a Ctrl-C is only
        # answerable while the process still holds the driver, and on this arm
        # the navigation runs on the thread the signal is delivered to.
        with interrupt_guard():
            results = _gflight_results(legs, opts, top_n, gf_mode, gf_headed)
    except GfBackendError as e:
        refusal = _gf_refusal(e, transport=gf_mode)
        err.print(refusal.message)
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
            num_passengers=_seated_pax(p),
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


def _run_the_weave(
    go: Callable[[], Coroutine[Any, Any, None]],
    state: dict[str, Any],
    gf_mode: GfTransportMode,
) -> None:
    """Run the weave and stash anything that escapes it, so the reporters below
    it decide the outcome.

    Each task guards its own body, so what reaches here is what the weave
    itself does: opening the loop, starting the group, and the group's own
    unwinding. Untyped, any of that is a traceback with both streams empty on
    the most ordinary command there is — and the rows the other backend already
    has go with it. A stash is not an outcome: every path out of its caller
    reads it, including the one whose other half succeeded."""
    from ._gf_browser import interrupt_guard  # noqa: PLC0415 — patchright off the import path

    try:
        try:
            # OUTSIDE `anyio.run`, and it may not move inward. `asyncio.Runner`
            # installs a SIGINT handler of its own only when the disposition is
            # still the default (`asyncio/runners.py:102-104`) and restores that
            # default on the way out (`:125-129`), so a guard armed from inside a
            # coroutine or a worker would be replaced or reset. Armed here, the
            # Runner installs nothing. `_run_enriched_path` is the only caller,
            # so this is the enriched search path and nothing else.
            #
            # Armed only for the transport that can open a browser. This half
            # runs on a worker no interrupt reaches, so on the transports that
            # hold no driver the first Ctrl-C has nothing to free and an ignored
            # second one takes away the only thing that could end the process.
            with interrupt_guard(armed=gf_mode == TRANSPORT_BROWSER):
                anyio.run(go)
        except* KeyboardInterrupt:
            # With no Runner handler installed, the interrupt lands wherever the
            # main thread stands — which includes the task group's own host
            # frame, and the group appends it and re-raises it wrapped. Every
            # handler from here to the exit matches the BARE class: typer turns a
            # `KeyboardInterrupt` into exit 130 and a group of them into a
            # traceback and exit 1, so unwrapped the user's stop reads as a crash.
            # `except*` because a nested group unwraps the same way for free.
            #
            # A group carrying an interrupt AND a non-`Exception` leaf still
            # propagates as a group, unchanged. Widening this arm to catch that
            # would swallow the other leaf; it needs two of them in the same
            # instant, and the shape below is the one that has a caller.
            raise KeyboardInterrupt from None
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


def _report_enriched_gf_failure(
    e: Exception,
    *,
    matrix_answered: bool,
    awards_only: bool,
    transport: GfTransportMode = TRANSPORT_HTTP,
) -> None:
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
    and can only guess.

    `transport` is the rung the search actually ran on. Every wording below is
    dispatched with it, or the browser rung's throttle — which has no retry
    ladder to wait for — reaches the default search path telling the user to
    wait a moment and try again."""
    if not isinstance(e, GfBackendError):
        err.print(f"[yellow]Google Flights query failed:[/] {_safe_text(e)}")
    else:
        refusal = _gf_refusal(e, transport=transport)
        if matrix_answered and not awards_only:
            console.print(f"[dim]{refusal.note} — showing Matrix only.[/]")
        elif matrix_answered:
            err.print(
                f"[yellow]{refusal.note}[/] — awards only; no fare table is printed on this arm."
            )
        else:
            err.print(refusal.message)


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
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
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
                gf = await anyio.to_thread.run_sync(
                    _gflight_results, legs, opts, top_n, gf_mode, gf_headed
                )
            except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
                raise
            except Exception as e:  # noqa: BLE001 - reported below; Matrix may still succeed
                state["gf_err"] = e
                gf = []
            state["gf"] = gf
            _paint_first_gf_table(state, gf, legs=legs, top_n=top_n, awards_only=awards_only)

    _run_the_weave(_go, state, gf_mode)

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
            state["gf_err"],
            matrix_answered=matrix_res is not None,
            awards_only=awards_only,
            transport=gf_mode,
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
        #
        # That second clause is conditional for the reason `_pick_in_range`'s
        # own is: `--no-matrix-url --no-google-url` leaves this arm printing no
        # link at all, and a sentence describing how links below are labelled
        # is then describing something that does not happen.
        if pick is not None:
            unpinned = "; the links below are unpinned." if (matrix_url or google_url) else "."
            err.print(
                f"[yellow]--pick {pick:d} names a row in the results table, and this mode "
                f"prints none{unpinned}[/]"
            )
        pick = None

    if run_pp:
        _overlay_awards(matrix_res, legs=legs, opts=opts, sel=sel, awards_only=awards_only)

    # A result built from the rows the table numbered, so `_emit_urls`' label
    # expression is true by construction. A Google-only row carries no
    # `Itinerary.id`, so the Matrix line falls back to the plain deep link while
    # the Google line still pins from that row's slices. Unpinned where no
    # table was numbered, which is what makes both lines fall back to the label
    # that claims nothing.
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
            f"[dim]Google Flights combines returns against up to {pins:d} "
            f"first-ranked outbounds.[/]"
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
        # One arm and not two: a `MatrixApiError` arrives here either bare, from
        # the client's own open or close, or wrapped in the group a task raised
        # it inside — and `except MatrixApiError` catches only the first.
        _reraise_if_orderly(e, said="Matrix search failed")
        err.print(f"[red]Matrix search failed:[/] {_failure_text(e)}")
        # `str()` of a Matrix error is its message alone, so a group hands this
        # line the one field of three that a reader cannot act on by itself.
        # Each one inside `e` goes on to the reporter that keeps `kind` and
        # `request_id` — the pair that says whether to fix the query or wait out
        # a brownout.
        for f in _failures_inside(e):
            if isinstance(f, MatrixApiError):
                _print_matrix_error(f)
        raise typer.Exit(1) from e
    return results


def _gflight_cabins_in_series(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    top_n: int,
    gf_headed: bool,
) -> dict[Cabin, list[Any]] | None:
    """Rung 2's multi-cabin shape: one Chrome, one cabin at a time, on this thread.

    The parallel fan-out below cannot run rung 2. A thread per cabin is a
    session per cabin, and Chromium single-instances the profile directory, so
    the second cabin fails on the first one's lock. Serialising here is what
    lets ONE session serve every cabin: the launch is paid once, and every
    navigation runs on the thread that made the session.

    No task group and no worker thread on this arm, so the loop runs on the
    thread the interrupt is delivered to and a Ctrl-C is honoured where it
    lands rather than after the cabin in flight finishes.

    ONE guard around the whole loop, never one per cabin. A guard clears the
    interrupt latch on its way in, so a second cabin's guard would erase the
    stop the first one recorded and re-arm a SIGINT the first one had set to be
    ignored.

    `None` says rung 2 never opened at all, and the caller then runs the whole
    fan-out on rung 1. Only before the first cabin is served: once a cabin has
    rows, re-running the fan-out would discard them, and a table whose columns
    came from two different rungs is not one answer.
    """
    from ._gf_browser import interrupt_guard, session_scope  # noqa: PLC0415 — GF-only
    from ._gflight_ids import shared_throttle_ladder  # noqa: PLC0415 — fli, ~95 ms

    def note_missing_column(cab: Cabin, e: GfBackendError) -> None:
        """Why this cabin's column will be missing. Left to the bare arm below,
        a typed refusal reads as an unexplained failure."""
        # `removesuffix`, because a browser refusal's note already ends in the
        # full stop its remedy carries and every other refusal's does not.
        note = _gf_refusal(e, transport=TRANSPORT_BROWSER).note.removesuffix(".")
        err.print(f"[yellow]Google Flights {cab.value}: {note}.[/]")

    results: dict[Cabin, list[Any]] = {}
    with shared_throttle_ladder(), interrupt_guard(), session_scope():
        for cab in cabins:
            try:
                results[cab] = _gflight_results(
                    legs,
                    opts.model_copy(update={"cabin": cab}),
                    top_n,
                    TRANSPORT_BROWSER,
                    gf_headed,
                )
            except GfBrowserUnavailableError as e:
                # Ahead of the `GfBackendError` arm below, which is its base
                # class and would otherwise report a rung that never opened as
                # one cabin's missing column.
                if not results:
                    # The phrase leads the line so that no console width can
                    # break it. The remedy follows the reason because the other
                    # half of it — install Chrome, point the binary — is what a
                    # user whose http rung is also refused has left to try.
                    err.print(
                        f"[dim]multi-cabin is using http: "
                        f"{_safe_text(e.reason)} {_safe_text(e.remedy)}[/]"
                    )
                    return None
                note_missing_column(cab, e)
            except GfBackendError as e:
                note_missing_column(cab, e)
            except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
                raise
            except Exception as e:  # noqa: BLE001 — fli has no documented exception surface
                err.print(f"[yellow]Google Flights {cab.value} query failed: {_safe_text(e)}[/]")
    return results


def _run_gflight_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    top_n: int,
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
) -> dict[Cabin, list[Any]]:
    """Fan out N parallel gflight queries (one per cabin). fli is sync, so
    each query runs in a worker thread via `anyio.to_thread.run_sync`.

    Each cabin runs the SAME query builder as the single-cabin path, so the
    native filters and the Tier-2 post-filter cannot drift apart. They also
    share ONE throttle ladder: Google's wall is per-IP, so a cabin per thread
    laddering against it separately spends the cabin count times the requests to
    be told the same thing."""
    from ._gflight_ids import shared_throttle_ladder  # noqa: PLC0415

    if gf_mode == TRANSPORT_BROWSER:
        served = _gflight_cabins_in_series(
            legs=legs, opts=opts, cabins=cabins, top_n=top_n, gf_headed=gf_headed
        )
        if served is not None:
            return served

    # Rung 1 for every cabin below: either the caller asked for it, or rung 2
    # could not open at all and said so. A browser mode reaching the fan-out
    # would open a session per worker thread, which is the profile-lock
    # collision the series runner exists to avoid.
    fanout_mode = TRANSPORT_HTTP if gf_mode == TRANSPORT_BROWSER else gf_mode
    results: dict[Cabin, list[Any]] = {}

    def query_sync(cab: Cabin) -> list[Any]:
        return _gflight_results(
            legs, opts.model_copy(update={"cabin": cab}), top_n, fanout_mode, gf_headed
        )

    async def query_cabin(cab: Cabin) -> None:
        try:
            results[cab] = await anyio.to_thread.run_sync(query_sync, cab)
        except GfBackendError as e:
            # A typed refusal is why this cabin's column will be missing; the
            # bare handler below would print it as an unexplained failure.
            refusal = _gf_refusal(e)
            err.print(f"[yellow]Google Flights {cab.value}: {refusal.note}.[/]")
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
    ccy_tag = f" ({_safe_text(ccy)})" if ccy else ""
    cabin_labels = "+".join(_CABIN_TO_LETTER[c] for c in cabins)
    sort_label = _CABIN_TO_LETTER[sort_by]

    t = Table(
        # `title_prefix` is a parameter: its value is chosen by whoever calls, and
        # a claim about every present and future caller is not one this function
        # can keep. The two callers pass a literal, so the wrap costs nothing.
        title=f"{_safe_text(title_prefix)} · {cabin_labels} (sorted by {sort_label}){ccy_tag}",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("carriers")
    t.add_column("outbound")
    t.add_column("return")
    for letter in (_CABIN_TO_LETTER[c] for c in cabins):
        t.add_column(f"{letter} $", justify="right")

    for i, row in enumerate(rows, 1):
        itn = row.itinerary.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        # Wrapped per code, as `_render_search` does with the same field.
        carriers = ",".join(_safe_text(c.code or "?") for c in (itn.carriers if itn else []))

        out_cell = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret_cell = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        price_cells = [_amount(row.prices.get(cab)) for cab in cabins]
        t.add_row(f"{i:d}", carriers or "?", out_cell, ret_cell, *price_cells)
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
            num_passengers=_seated_pax(p),
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
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
) -> None:
    """Google Flights multi-cabin: N cabin queries → join → render.

    Parallel on rung 1 and serial on rung 2; `_run_gflight_multi` chooses."""
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

        join_note = _multi_cabin_join_note(pinned_fanout(query_top_n))
        err.print(f"[dim]{join_note}[/]")
    fli_by_cabin = _run_gflight_multi(
        legs=legs,
        opts=opts,
        cabins=cabins,
        top_n=query_top_n,
        gf_mode=gf_mode,
        gf_headed=gf_headed,
    )
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
            num_passengers=_seated_pax(p),
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
    # The filter is compared against the code Google Flights sent; escaping first
    # would test a string the user's `--routing` could never have named.
    raw_code = getattr(leg.airline, "name", "") or ""
    code = _safe_text(raw_code)
    number = _safe_text(getattr(leg, "flight_number", "?"))
    booking = f"{code} {number}"
    if not match_carriers or raw_code in match_carriers:
        return booking
    raw_mf = getattr(amenity, "marketing_flights", ()) if amenity else ()
    mflights: tuple[str, ...] = tuple(raw_mf or ())
    for mf in mflights:
        if mf[:2].upper() in match_carriers:
            return f"{_safe_text(mf)} (op {code}{number})"
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

    A round-trip combination can print two DIFFERENT prices, on its `Na` and
    `Nb` rows, and that reads as a bug until you know what each is: the `a` row
    carries the outbound board's own quote — the cheapest total reachable from
    that outbound — while the `b` row carries THIS combination's total, from
    the return board fetched with that outbound pinned. They agree only where
    this combination IS the cheapest one reachable from that outbound, which is
    the total the `a` row was quoting; on the committed capture two of the nine
    combinations read that way. Printing each member's own number is
    deliberate, because both are true of the row they sit on and the pair is
    what says which combination costs what. The itinerary fare downstream is
    the terminal member's; the argument and the measurements are under
    "What a round-trip row's price means." in
    docs/memories/gf_routing_and_carriers.md and in
    `tests/pp/test_gflight_adapter.py`."""
    origin = legs[0].origins[0] if legs[0].origins else "?"
    destination = legs[0].destinations[0] if legs[0].destinations else "?"
    has_return = len(legs) >= _ROUND_TRIP_LEGS
    t = Table(
        title=f"Google Flights · {_safe_text(origin)}→{_safe_text(destination)}"
        + (" + return" if has_return else ""),
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
            t.add_row(
                label,
                ("—" if fr.price is None else f"{_safe_text(fr.currency or 'USD')}{fr.price:.2f}"),
                _safe_text(fr.stops),
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
            # Both fields are `getattr` off a duck-typed Google Flights object,
            # so neither has a type this module checked. The colour around the
            # token is ours and goes on after the value is escaped.
            tok = f'{_safe_text(pitch)}"'
            color = _LEGROOM_AS_COLOR.get(cls or "")
            if color:
                tok = f"[{color}]{tok}[/]"
            parts.append(tok)
        if cls and cls not in {"AVERAGE", "BELOW", "ABOVE"}:
            parts.append(_safe_text(cls))
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
        # Google Flights chose both leaves and this cell parses markup, exactly as
        # `_fmt_legroom_one`'s does; padded before escaping for the same reason,
        # and carrying the same tab exception.
        leg_label = (
            f"{getattr(leg.airline, 'name', leg.airline)}{getattr(leg, 'flight_number', '?')}"
        )
        shown = leg_label.translate(_CTRL)
        lines.append(f"{escape(f'{shown:<6}')} " + " ".join(parts))
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
        # The file this process reads, not the default: `FLIGHT_CLI_CONFIG_DIR`
        # moves it, and this string tells the reader where to put the option.
        # `rich_markup_mode="rich"` renders this through the same markup parser
        # `console.print` uses, so the path takes the wrapper any remote value
        # takes — `escape` alone would leave an ESC in a directory name to reach
        # Typer's from-ANSI branch, which drops the path it was meant to show.
        # The section name is escaped so it RENDERS: unescaped, the parser reads
        # `[providers.<name>]` as a style tag and eats the one token the sentence
        # exists to give the reader.
        f"Overrides {_safe_text(_config.config_path())} \\[providers.<name>]."
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
        err.print(f"[red]Bad rps configuration: {_safe_text(e)}[/]")
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


def _resolve_gf_transport(mode: str) -> GfTransportMode:
    """Validate `--gf-transport`, returning the mode as the ladder's own type.

    The ONE narrowing seam. typer hands over a plain `str`, every search passes
    through here, and everything downstream — `_gflight_results` and both render
    paths — takes a `GfTransportMode`, so no call site needs a `cast` and none
    can be reached with a mode that was never checked.

    Returning the matched member is what narrows: `mode in VALID_TRANSPORT_MODES`
    tells the type checker nothing about a `str`. The accepted set is derived
    from the `Literal` in `_gf_common`, so a rung added to the type is offered
    here the same day it is added.

    A mode string, not a `GfTransport`, because every search validates this while
    only a Google Flights search should pay for `_gflight_ids` — building the
    value here would put fli's import on the Matrix path too, measured at ~95 ms
    on top of an already-loaded `cli`. `_gflight_results` builds it instead; that
    is the first point which has already paid."""
    for known in VALID_TRANSPORT_MODES:
        if mode == known:
            return known
    raise typer.BadParameter(
        f"unknown transport {mode!r}; expected one of {'/'.join(VALID_TRANSPORT_MODES)}",
        param_hint="--gf-transport",
    )


# ─────────────────────────── --format / --json ─────────────────────────────

# Output formats currently implemented end-to-end. csv/tsv/yaml were in the
# original work-4uls plan but deferred to a follow-up: the cash-itinerary
# shape isn't naturally tabular without a flattening pass that deserves its
# own design. Today's surface is the front door; emitters layer on later.
_VALID_FORMATS = ("table", "json")
# Rendered once, so the message and the help string cannot drift, and so the two
# print sites interpolate a name rather than an expression.
_FORMAT_CHOICES = "/".join(_VALID_FORMATS)

_FORMAT_OPT = typer.Option(
    "table",
    "--format",
    help=f"Output format: one of {_FORMAT_CHOICES}.",
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
            err.print(f"[red]--json conflicts with --format {_quote(fmt)}; pick one.[/]")
            raise typer.Exit(2)
        return "json"
    if fmt not in _VALID_FORMATS:
        err.print(f"[red]--format must be one of {_FORMAT_CHOICES}; got {_quote(fmt)}[/]")
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
    gf_transport: str = typer.Option(
        TRANSPORT_HTTP,
        "--gf-transport",
        help=(
            "How the Google Flights backend fetches its search page: "
            "[bold]http[/] (default) is one curl_cffi GET; [bold]browser[/] launches a real "
            "Chrome (headless unless [bold]--gf-headed[/]) against the same URL, a few "
            "seconds per search, and survives the rate limit that blocks http; "
            "[bold]auto[/] is identical to http today (escalate-on-throttle lands "
            "separately). A multi-cabin [bold]browser[/] search runs its cabins one at "
            "a time through a single Chrome (~10s for two cabins, ~14s for three, against "
            "~1.3s over http), and falls back to http if Chrome cannot open. Needs "
            # Escaped: rich reads `[browser]` as a style tag and deletes it, which
            # printed an install command that silently omits the extra.
            "[bold]uv pip install 'flight-cli\\[browser]'[/] for browser."
        ),
        rich_help_panel=_GROUP_BACKEND,
    ),
    gf_headed: bool = typer.Option(
        False,
        "--gf-headed",
        help="Show the Chrome window --gf-transport browser opens. Default: headless.",
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
    # Validated for every backend, so a typo is caught whether or not this
    # particular query happens to reach Google Flights. Bound to a new name
    # rather than reassigned: the parameter is declared `str` for typer's sake,
    # and reassigning it would throw away the narrowing this call just did.
    gf_mode = _resolve_gf_transport(gf_transport)

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
                gf_mode=gf_mode,
                gf_headed=gf_headed,
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
                gf_mode=gf_mode,
                gf_headed=gf_headed,
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
            gf_mode=gf_mode,
            gf_headed=gf_headed,
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
            "--duration",
            "-d",
            help="Nights between the outbound and the return, '5', '5-7' or "
            "'5..7'. Round-trip only — a one-way calendar has no trip length.",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = _DEFAULT_CALENDAR_DURATION,
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
        "date grid (one-way, single-airport, Tier-1 filters) instead of also "
        "running the authoritative Matrix calendar. Exits 1 rather than falling "
        "back, so a no-grid result is never mistaken for a fast one.",
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
    dmin, dmax = _resolve_duration(duration, round_trip=not one_way)
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
    # (full per-duration grid). `--fast` stops after the grid.
    blocker = _grid_branch_blocker(
        search, json_out=json_out, one_way=one_way, origins=origins, dests=dests
    )
    if fast and blocker is not None:
        # `--fast` exists only inside the branch below. Everywhere else there is no
        # grid to serve, so it fails closed instead of letting the ~45s Matrix calendar
        # answer in its place at exit 0 — a wrapper doing `--fast || fallback` would
        # read that as the grid it asked for (work-h70kv.9). Ahead of every Matrix call
        # and of the JSON writer, so neither runs.
        #
        # On stderr, not stdout: one of the shapes this refuses IS `--format json`, and
        # a caller piping to `jq` must get a JSON document or an empty stdout, never
        # prose. The other shapes go the same way so the stream doesn't depend on which
        # condition failed.
        err.print(
            "[yellow]--fast applies only to one-way, single-airport, non-JSON "
            f"calendars; this is {_safe_text(blocker)}. Run without --fast for Matrix.[/]"
        )
        raise typer.Exit(1)
    if blocker is None:
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
        _run_fast_calendar_grid(
            search,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            matrix_url=matrix_url,
            google_url=google_url,
        )
        return

    # Matrix (authoritative; also the only path for round-trip, multi-airport,
    # Tier-2/3 routing, or when the grid was empty/throttled).
    # CalendarSearch → CalendarResult by client._parse_response dispatch.
    # On a multi-airport brownout, _run_calendar splits into one sub-query per
    # (origin, destination group) and merges.
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
        # On stderr, beside the coverage note the fan-out prints: it is provenance
        # about the answer rather than part of it, and it is written BEFORE the
        # delivery below, so on stdout a failure there would leave it standing alone
        # under exit 1 — a document, to a caller that reads the stream.
        err.print(
            f"[dim]Queried {n_split} origin/destination groups separately and merged "
            f"— Matrix under-reports the combined multi-airport calendar grid.[/]"
        )

    def _write_answer() -> None:
        _render_calendar(
            res,
            dmin=dmin,
            dmax=dmax,
            origin=origins,
            destination=dests,
            sd=sd,
            ed=ed,
            round_trip=len(search.legs) == _ROUND_TRIP_LEGS,
        )
        _emit_urls(search, matrix_url=matrix_url, google_url=google_url)

    _deliver_calendar(_write_answer)


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
            help="Original duration range (round-trip only — ignored without --return)",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = _DEFAULT_CALENDAR_DURATION,
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
    dmin, dmax = _resolve_duration(duration, round_trip=ret_d is not None)

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
    t = Table(title=f"Airport lookup: {_quote(query)}", show_header=True, header_style="bold blue")
    t.add_column("code")
    t.add_column("name")
    t.add_column("city")
    t.add_column("tz")
    for loc in locs:
        t.add_row(
            _safe_text(loc.code),
            _safe_text(loc.display_name or ""),
            _safe_text(loc.city_name or ""),
            _safe_text(loc.timezone or ""),
        )
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
