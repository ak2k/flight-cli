"""CLI: thin shells over the domain types. Each command parses args, builds
a Search variant, hands it to MatrixClient.execute() (or fli for gflight),
and renders.

Commands:
  flight search    — specific-date search (auto-picks Matrix vs Google Flights)
  flight calendar  — lowest-fare grid (Matrix only)
  flight detail    — phase-2 itineraries for a date picked from the grid
  flight airport   — IATA autocomplete
  flight explore   — where an origin flies, cheapest first (Google Flights, Chrome)
  flight doctor    — pass, fail or skip for every backend, transport and credential
  flight explain   — a --routing string in plain English, one line per token
  flight watch     — save, list and remove route watches (stored only; nothing polls)
  flight fare      — [deprecated] alias for `search --backend matrix`
  flight gflight   — [deprecated] alias for `search --backend gflight`
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import shlex
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial, wraps
from itertools import groupby, pairwise
from statistics import median
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NamedTuple,
    NoReturn,
    assert_never,
    cast,
)

import anyio
import anyio.to_thread
import httpx
import typer
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import _config, _envelope, _verify
from ._calendar_split import (
    Pair,
    calendar_pair,
    is_empty_calendar,
    merge_calendar_results,
    price_currencies,
    split_calendar_search,
    with_fanout_currency,
)
from ._carrier_names import CARRIER_NAMES
from ._console_text import CTRL as _CTRL
from ._console_text import quote as _quote
from ._console_text import safe_text as _safe_text
from ._cross_check import (
    Answers,
    cross_check,
    every_matrix_price_in,
    low_row,
    lowest_matrix_price,
)
from ._cross_check import document as cross_check_document
from ._enrich import party_price
from ._explain import decode_routing

# The `--gf-transport` vocabulary, from the leaf that costs nothing to import.
# `_gflight_ids` owns the ladder but costs fli (~95 ms), and EVERY search
# validates this flag — including the Matrix-only ones that never reach a rung.
# One definition, so the CLI's accepted set cannot drift from the ladder's type.
from ._gf_common import TRANSPORT_BROWSER, TRANSPORT_HTTP, VALID_TRANSPORT_MODES, GfTransportMode
from ._gf_errors import (
    BROWSER_DEFAULT_REMEDY,
    GfBackendError,
    GfBrowserUnavailableError,
    GfConsentError,
    GfPageShapeError,
    GfPinIgnoredError,
    GfSearchServerError,
    GfTfsUnsupportedError,
    GfThrottledError,
    GfTransportError,
    GfUpstreamStatusError,
)
from ._metro import (
    MAX_GF_LEG_AIRPORTS,
    expand_airports,
    gf_leg_pages,
    gf_leg_refusal,
    gf_pages_refusal,
)
from ._multi_cabin import (
    MultiCabinRow,
    cheapest,
    itinerary_key,
    parse_price,
    price_currency,
    price_rank,
)
from ._multi_cabin import merge as _merge_cabins
from ._watch import watch_app
from .client import MatrixApiError, MatrixClient
from .domain import (
    Bags,
    Cabin,
    CalendarFollowup,
    CalendarSearch,
    CalendarWindow,
    ClockWindow,
    Leg,
    Pax,
    Search,
    SearchOptions,
    SpecificDateSearch,
    TimeOfDay,
    TimeWindow,
    covers_one_window,
    window_label,
    within_price_cap,
)
from .links import (
    extract_pin_segments_from_slice,
    google_flights_booking_url,
    google_flights_explore_url,
    google_flights_pinned_url,
    google_flights_url,
    is_inverse_pair,
    matrix_deep_link,
    matrix_itinerary_url,
    pin_dates_are_stated,
    search_page_cap,
)
from .log import configure as configure_logging
from .models import CalendarDay, CalendarMonth, FareRulesResult, Itinerary
from .pp import cli as _pp_cli
from .pp.auth import load_tokens
from .pp.cli import auth_app, run_pp_for_search
from .providers.base import LegQuery

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator, Iterable, Mapping, Sequence

    from ._cross_check import CrossCheck
    from ._gf_booking import BookingOptions
    from ._gf_calgraph import GraphRange, LostLength, PriceGraph
    from ._gf_explore import Destination, ExploreAnswer, TripLength
    from ._gf_postfilter import StopDrops
    from ._gflight_ids import (
        Board,
        GfTransport,
        ItineraryKey,
        PriceHistory,
        PriceInsight,
        RouteFacets,
        SeparateTickets,
    )
    from ._open_jaw import Combination
    from .models import (
        BookedItinerary,
        BookingDetails,
        BookingDetailsResult,
        CalendarResult,
        DurationOption,
        FareRule,
        FareRules,
        LegInfo,
        Location,
        PricedFare,
        SearchResult,
        Slice,
    )
    from .routing_predicates import Predicate

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


def _amount(s: str | None, title_ccy: str = "") -> str:
    """The amount with its currency prefix stripped, ready for a markup console;
    '—' where there is no price.

    With `title_ccy`, the prefix is stripped only when it is that currency, the
    one the table's title names: a price in any other currency keeps its own
    label rather than reading as the title's.

    Sanitized here rather than at each print site: Matrix chooses the whole string
    and every caller drops it into a Rich table cell or a summary line, both of
    which parse markup — an unbalanced `[/x]` there raises `MarkupError` and loses
    a query that succeeded."""
    if not s:
        return "—"
    ccy, amount = _split_price(s)
    return _safe_text(amount if not title_ccy or ccy == title_ccy else s)


def _title_currency(prices: Iterable[str | None]) -> str:
    """The currency of the first price that names one, for a table's title."""
    return next((c for c in (_split_price(p)[0] for p in prices) if c), "")


def _usd_amount(price: str | None) -> float | None:
    """The number in a USD price, or None for any other currency or none.

    The award table computes cents per mile from this, and a fare in another
    currency divided as though it were dollars is a wrong valuation printed as
    a right one."""
    ccy, _ = _split_price(price)
    usd = ccy == "USD" or (not ccy and (price or "").lstrip().startswith("$"))
    return parse_price(price) if usd else None


app = typer.Typer(rich_markup_mode="rich", help="CLI for ITA Matrix's Alkali backend.")
app.add_typer(auth_app, name="auth")
app.add_typer(watch_app, name="watch")
# Emoji off, so a `:name:` in remote text prints as received.
console = Console(emoji=False)
err = Console(stderr=True, emoji=False)


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


_CLOCK_WINDOW = re.compile(r"([0-9]{1,2}):([0-9]{2})-([0-9]{1,2}):([0-9]{2})")
_LAST_HOUR = 23
_LAST_MINUTE_OF_HOUR = 59


def _parse_search_times(s: str | None, flag: str) -> tuple[TimeWindow, ...]:
    """A search's time flag: the bucket names `_parse_times` takes, or one
    `H:MM-H:MM` window, both ends included, from 00:00 to 23:59."""
    if not s or not any(c.isdigit() for c in s):
        return _parse_times(s)
    if m := _CLOCK_WINDOW.fullmatch(s.strip()):
        h1, m1, h2, m2 = map(int, m.groups())
        first, last = h1 * 60 + m1, h2 * 60 + m2
        if max(h1, h2) <= _LAST_HOUR and max(m1, m2) <= _LAST_MINUTE_OF_HOUR and first < last:
            return (ClockWindow(first=first, last=last),)
    err.print(
        f"[red]bad {_safe_text(flag)} {_quote(s)}:[/] give one H:MM-H:MM window from "
        "00:00 to 23:59, its first minute before its last, or a comma list of "
        "early,morning,midday,afternoon,evening,night"
    )
    raise typer.Exit(2)


def _parse_arrival_times(s: str | None, flag: str) -> tuple[TimeWindow, ...]:
    """`--arrive-times` / `--return-arrive-times`: as `_parse_search_times`,
    and one window, because Google Flights takes one arrival window a leg and
    Matrix none, so nothing else could serve a list that is not one."""
    windows = _parse_search_times(s, flag)
    if not covers_one_window(windows):
        names = ", ".join(dict.fromkeys(map(window_label, windows)))
        err.print(
            f"[red]{_safe_text(flag)} takes one window, and {_safe_text(names)} are not one:[/] "
            "Google Flights takes one arrival window a leg, and Matrix none."
        )
        raise typer.Exit(2)
    return windows


def _google_only(
    *,
    bags: Bags | None,
    arrive_times: str | None = None,
    return_arrive_times: str | None = None,
    exclude_basic: bool = False,
) -> list[tuple[str, str]]:
    """The flags asked of Google Flights alone, each with what Matrix lacks
    for it, completing "Matrix …". No Matrix answer stands in for a search
    carrying one, so none is handed to Matrix after the fact either."""
    return [
        (flag, gap)
        for flag, gap, asked in (
            ("--bags", "prices no bags", bags is not None),
            ("--arrive-times", "takes no arrival time", bool(arrive_times)),
            ("--return-arrive-times", "takes no arrival time", bool(return_arrive_times)),
            ("--exclude-basic", "is not asked to leave out basic economy", exclude_basic),
        )
        if asked
    ]


# Matrix's date options beside "This day only", as (days before, days after) the
# date: the value `--flex` and a slice's `f=` take, and the label the SPA's form
# shows for it.
_FLEX_DAYS = {"before": (1, 0), "after": (0, 1), "1": (1, 1), "2": (2, 2)}
_FLEX_LABELS = {
    (1, 0): "or day before",
    (0, 1): "or day after",
    (1, 1): "+/- 1 day",
    (2, 2): "+/- 2 days",
}


def _date_option_reasons(
    *, flex: tuple[int, int], return_flex: tuple[int, int], arrive: bool, return_arrive: bool
) -> list[str]:
    """One reason per date option a direction carries, each Matrix's: Google's
    page takes one departure date a slice."""
    reasons: list[str] = []
    if flex != (0, 0):
        reasons.append(f"a flexible outbound date ({_FLEX_LABELS[flex]})")
    if arrive:
        reasons.append("an outbound arrival date")
    if return_flex != (0, 0):
        reasons.append(f"a flexible return date ({_FLEX_LABELS[return_flex]})")
    if return_arrive:
        reasons.append("a return arrival date")
    return reasons


def _matrix_remedy(google_only: list[tuple[str, str]]) -> str:
    """How to put a search on Matrix: under a Google-only flag, by dropping it."""
    if google_only:
        flag, gap = google_only[0]
        return f"drop {flag} to search Matrix, which {gap}"
    return "use --backend matrix"


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


_RE_CURRENCY = re.compile(r"\A[A-Z]{3}\Z")


def _resolve_currency(code: str | None) -> str | None:
    """`--currency`, upper-cased, or None when it was not given.

    The shape only: which codes a backend prices in is that backend's answer,
    and Matrix names a code it does not know in its own error."""
    if code is None:
        return None
    up = code.strip().upper()
    if not _RE_CURRENCY.match(up):
        err.print(f"[red]bad currency {_quote(code)}; use a 3-letter ISO 4217 code such as EUR[/]")
        raise typer.Exit(2)
    return up


# CHECKED or CHECKED,CARRY. Google's filter takes a carry-on as present or absent.
_RE_BAGS = re.compile(r"\A(\d{1,2})(?:,([01]))?\Z")


def _parse_bags(spec: str) -> Bags:
    """`--bags` as CHECKED or CHECKED,CARRY, CARRY defaulting to 0, or exit 2
    naming the value."""
    m = _RE_BAGS.match("".join(spec.split()))
    if m is None:
        err.print(
            f"[red]bad --bags {_quote(spec)}; use CHECKED or CHECKED,CARRY with CARRY "
            "0 or 1, such as 1 or 1,1[/]"
        )
        raise typer.Exit(2)
    checked, carry_on = int(m.group(1)), int(m.group(2) or 0)
    if not (checked or carry_on):
        err.print(f"[red]--bags {_quote(spec)} asks for no bag; drop it, or ask for one[/]")
        raise typer.Exit(2)
    return Bags(checked=checked, carry_on=carry_on)


def _refuse_cap_and_bag_conflicts(
    *,
    cabins: tuple[Cabin, ...],
    bags: Bags | None,
    seated: int,
    arrival_flags: tuple[str, ...] = (),
    exclude_basic: bool = False,
) -> None:
    """Refuse an arrival window beside several cabins, whose compare can end
    on Matrix, `--exclude-basic` beside any cabin but economy, and `--bags`
    for more than one traveler. `--max-price` and `--bags` beside several
    cabins pass: each cabin's search asks for them as a one-cabin search does.
    Before the backend is announced: a refusal after "Using Matrix" reads as a
    search that started and then failed."""
    if len(cabins) > 1 and arrival_flags:
        err.print(
            f"[red]{_safe_text(arrival_flags[0])} takes one --cabin: a multi-cabin compare "
            "can end on Matrix, which takes no arrival time.[/] Drop the extra --cabin values."
        )
        raise typer.Exit(2)
    if exclude_basic and cabins != (Cabin.COACH,):
        err.print(
            "[red]--exclude-basic takes --cabin economy alone:[/] basic economy is an "
            "economy fare, and a multi-cabin compare can end on Matrix, which is not asked "
            "to leave it out. Drop --exclude-basic, or search economy alone."
        )
        raise typer.Exit(2)
    if bags is not None and seated > 1:
        err.print(
            "[red]--bags takes one traveler:[/] Google counts the bags for the whole "
            "party, and once they are asked for it says nothing of what a party's fare "
            "includes. Drop --bags, or search for one traveler."
        )
        raise typer.Exit(2)


def _parse_flex(value: str | None, flag: str) -> tuple[int, int]:
    """`--flex` / `--return-flex` as (days before, days after) the date, (0, 0)
    when unset, or exit 2 listing the four choices."""
    if value is None:
        return (0, 0)
    days = _FLEX_DAYS.get(value.strip().lower())
    if days is None:
        err.print(
            f"[red]bad {_safe_text(flag)} {_quote(value)}:[/] choose before (or day before), "
            "after (or day after), 1 (+/- 1 day) or 2 (+/- 2 days)"
        )
        raise typer.Exit(2)
    return days


def _refuse_date_option_conflicts(
    *,
    slice_specs: list[str] | None,
    dep: str | None,
    arrive: str | None,
    ret: str | None,
    return_arrive: str | None,
    flex: str | None,
    return_flex: str | None,
    depart_times: str | None,
    return_times: str | None,
) -> None:
    """Refuse a date option that cannot be read one way, before the backend is
    announced: one beside `--slice`, which takes its own; two dates for one
    direction; a return option with no return; and a departure window on an
    arrival-date slice, since Matrix holds that slice's window to the arrival."""
    given = [
        flag
        for flag, value in (
            ("--flex", flex),
            ("--return-flex", return_flex),
            ("--arrive", arrive),
            ("--return-arrive", return_arrive),
        )
        if value is not None
    ]
    if slice_specs and given:
        err.print(
            f"[red]{_safe_text(given[0])} dates a search given by origin and destination.[/] "
            "A --slice takes its own in its f= and d=arrive fields."
        )
        raise typer.Exit(2)
    if dep and arrive:
        err.print(
            "[red]--dep and --arrive both date the outbound:[/] give --dep for the day it "
            "leaves, or --arrive for the day it lands."
        )
        raise typer.Exit(2)
    if ret and return_arrive:
        err.print(
            "[red]--return and --return-arrive both date the return:[/] give --return for "
            "the day it leaves, or --return-arrive for the day it lands."
        )
        raise typer.Exit(2)
    if return_flex is not None and not (ret or return_arrive):
        err.print(
            "[red]--return-flex widens the return's date, and needs a --return or "
            "--return-arrive.[/] Drop it, or add one."
        )
        raise typer.Exit(2)
    if arrive and depart_times:
        err.print(
            "[red]--depart-times sets when the outbound leaves, and --arrive dates when it "
            "lands:[/] Matrix holds an arrival-date slice's times to its arrival. Give them "
            "as --arrive-times, or date the departure with --dep."
        )
        raise typer.Exit(2)
    if return_arrive and return_times:
        err.print(
            "[red]--return-times sets when the return leaves, and --return-arrive dates "
            "when it lands:[/] Matrix holds an arrival-date slice's times to its arrival. "
            "Give them as --return-arrive-times, or date the departure with --return."
        )
        raise typer.Exit(2)


def _return_codes(
    *,
    routing: str | None,
    extension: str | None,
    routing_return: str | None,
    extension_return: str | None,
) -> tuple[str | None, str | None]:
    """A round trip's return (routing, extension): `--routing-ret`/`--ext-ret`
    when given, `''` meaning none, else the outbound's.

    No extension code is positional, so `--ext` is copied as it is. A routing is
    copied only when it reads the same both ways: a slice's routing reads from
    its own origin, so the outbound's `UA LH` on the return asks for UA then LH
    from the far end. Such a routing without `--routing-ret` is refused before
    the backend is announced, as `_refuse_cap_and_bag_conflicts` refuses."""
    from .routing_predicates import direction_dependence, mirrored_routing  # noqa: PLC0415

    if routing_return is None and routing and (why := direction_dependence(routing)):
        mirror = mirrored_routing(routing)
        err.print(
            f"[red]--routing {_quote(routing)} {_safe_text(why)}.[/] Give the return its "
            "own: "
            + (
                f"--routing-ret {_quote(mirror)} to fly it in reverse"
                if mirror is not None
                else "--routing-ret with the return's routing in its own order"
                if "[" in routing or "]" in routing
                else "--routing-ret with the return flight's number"
            )
            + ", or --routing-ret '' for no routing on the return."
        )
        raise typer.Exit(2)
    return (
        routing if routing_return is None else routing_return or None,
        extension if extension_return is None else extension_return or None,
    )


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
    currency: str | None = None,
    max_price: int | None = None,
    bags: Bags | None = None,
    exclude_basic: bool = False,
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
        currency=currency,
        max_price=max_price,
        bags=bags,
        exclude_basic=exclude_basic,
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

    Each token must be ONE fli airport, because some callers encode one airport
    per token. `_pick_backend` hands over a metro code's member airports
    (`_metro.expand_airports`), so on search a code reported here is neither an
    airport nor a metro code in that table.

    Checked against `fli_bridge.fli_airports`, the table the bridge builds every
    request from, so this cannot drift from what the bridge will accept, and only
    where Google Flights is still in the running: a Matrix run pays neither the
    import nor the check."""
    if backend == BACKEND_MATRIX:
        return []
    # PLC0415: paid only when Google Flights would otherwise serve the request;
    # fli's package import is slow enough that a Matrix run should not carry it.
    from .fli_bridge import fli_airports  # noqa: PLC0415

    toks = (*_parse_iata_list(origin or ""), *_parse_iata_list(destination or ""))
    bad = [t for t in toks if t not in fli_airports() or t in _GF_METRO_COLLISIONS]
    return [f"a city code rather than an airport ({', '.join(bad)})"] if bad else []


def _gf_unmappable_reasons(backend: str, predicates: Sequence[Predicate]) -> list[str]:
    """Reasons a carrier include keeps this request off Google Flights: a code
    fli has no member for is left out of the page's include list, and the rows'
    carriers are read through the same table, so none would come back.

    Checked with the bridge's own lookup, and only where Google Flights is still
    in the running: a Matrix run pays neither the fli import nor the check."""
    from .routing_predicates import (  # noqa: PLC0415
        AlliancePred,
        CarrierPred,
        SpecificFlightPred,
    )

    # A flight number keeps only rows booked under its carrier, and a row whose
    # carrier fli cannot name never decodes, so it asks what an include asks.
    asked = [
        *predicates,
        *(
            CarrierPred(frozenset({p.carrier}), exclude=False, operating=False)
            for p in predicates
            if isinstance(p, SpecificFlightPred)
        ),
    ]
    if backend == BACKEND_MATRIX or not any(
        isinstance(p, CarrierPred | AlliancePred) for p in asked
    ):
        return []
    from .fli_bridge import unmappable_codes  # noqa: PLC0415 — imports fli

    bad = unmappable_codes(asked)
    return [f"a carrier Google Flights has no code for ({', '.join(bad)})"] if bad else []


# Google's passenger picker stops at nine travelers: a larger party asks for a
# page its own UI never builds.
_GF_MAX_PASSENGERS = 9

_MULTI_CITY_ON_ONE_TICKET = "a multi-city itinerary on one ticket"


def _slice_legs(
    slice_specs: list[str], *, routing: str | None, extension: str | None
) -> tuple[Leg, ...]:
    """The legs of `--slice` specs, each taking the top-level codes as defaults.

    A slice's own `r=`/`e=`, even an empty one, replaces the top-level value
    whole, as `--routing-ret` does on a round trip."""
    legs: list[Leg] = []
    for spec in slice_specs:
        leg = _parse_slice_spec(spec)
        update: dict[str, str] = {}
        if leg.route_language is None and routing is not None:
            update["route_language"] = routing
        if leg.extension is None and extension is not None:
            update["extension"] = extension
        legs.append(leg.model_copy(update=update))
    return tuple(legs)


def _google_reasons(
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
    fare_rules: bool = False,
    adults: int = 1,
    return_codes: tuple[str | None, str | None] | None = None,
    multi_cabin: bool = False,
    cabins: tuple[Cabin, ...] = (),
    flex: tuple[int, int] = (0, 0),
    return_flex: tuple[int, int] = (0, 0),
    arrive: bool = False,
    return_arrive: bool = False,
    open_jaw: bool = False,
) -> list[str]:
    """Every reason Google Flights' search page can't serve this request, each
    a phrase completing "Google Flights can't serve …"; empty when it can.
    What each reason is and why is `_pick_backend`'s docstring, which acts on
    them. With `open_jaw`, a trip of one one-way per slice
    (`_one_way_per_slice`), the reason says "on one ticket": Google still
    prices it as one-way tickets (`_answer_open_jaw`)."""
    from ._gf_postfilter import search_page_reasons  # noqa: PLC0415
    from .routing_predicates import classify  # noqa: PLC0415

    reasons: list[str] = []
    if fare_rules:
        reasons.append("fare rules")
    if slice_specs:
        reasons.append(_MULTI_CITY_ON_ONE_TICKET if open_jaw else "a multi-city itinerary")
    reasons.extend(
        _date_option_reasons(
            flex=flex, return_flex=return_flex, arrive=arrive, return_arrive=return_arrive
        )
    )
    for which, option, flag in (
        ("departure", "--depart-times", depart_times),
        ("return", "--return-times", return_times),
    ):
        buckets = _parse_search_times(flag, option)
        if buckets and not covers_one_window(buckets):
            names = ", ".join(dict.fromkeys(map(window_label, buckets)))
            reasons.append(f"{which} times that are not one window ({names})")
    if seniors or youth:
        reasons.append("a senior or youth passenger")
    if (inf_seat or inf_lap) and multi_cabin:
        reasons.append("an infant passenger on a multi-cabin compare")
    if children and not adults:
        reasons.append("a child passenger with no adult")
    if adults + children + inf_seat + inf_lap > _GF_MAX_PASSENGERS:
        reasons.append(f"more than {_GF_MAX_PASSENGERS:d} passengers")
    if not allow_airport_changes:
        # Both of these reach the Matrix REQUEST and the Matrix deep link and
        # nothing else: `fli_bridge`, which the search page's `tfs=` is encoded
        # from, has no field for either. Served on Google the constraint is
        # simply absent, and the board that comes back is the unconstrained one
        # — the shape this picker exists to keep off the fast backend.
        reasons.append("a ban on changing airports")
    if not show_only_available:
        reasons.append("unavailable itineraries included")
    origins, destinations = _parse_iata_list(origin or ""), _parse_iata_list(destination or "")
    leg_refusal = (gf_leg_refusal if multi_cabin else gf_pages_refusal)(origins, destinations)
    if leg_refusal is not None:
        reasons.append(leg_refusal)
    reasons.extend(
        _gf_unserveable_reasons(
            backend, ",".join(expand_airports(origins)), ",".join(expand_airports(destinations))
        )
    )
    predicates = classify(routing, extension).predicates
    reasons.extend(search_page_reasons(predicates, stops, cabins[0] if len(cabins) == 1 else None))
    reasons.extend(_gf_unmappable_reasons(backend, predicates))
    if return_codes is not None and set(classify(*return_codes).predicates) != set(predicates):
        reasons.append("different routing or extension codes on the outbound and the return")
    return reasons


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
    fare_rules: bool = False,
    adults: int = 1,
    bags: Bags | None = None,
    return_codes: tuple[str | None, str | None] | None = None,
    arrive_times: str | None = None,
    return_arrive_times: str | None = None,
    exclude_basic: bool = False,
    multi_cabin: bool = False,
    cabins: tuple[Cabin, ...] = (),
    flex: tuple[int, int] = (0, 0),
    return_flex: tuple[int, int] = (0, 0),
    arrive: bool = False,
    return_arrive: bool = False,
    open_jaw: bool = False,
) -> str:
    """Resolve --backend to a concrete backend.

    auto: matrix iff the request needs it, else gflight (~1s vs Matrix's ~45s).
    `--routing`/`--extension` don't force Matrix on their own — they're parsed
    and classified, and Google Flights serves them when every predicate is
    either encoded in the search page's tfs= or a Tier-2 predicate the post-
    filter applies to the page's full board (`search_page_reasons`). Any other
    constraint goes to Matrix WITH ITS REASON PRINTED.

    Hard-Matrix flags always force Matrix: `--slice` (multi-city), a stop
    ceiling above two, the strictest of `--stops` and every `MAXSTOPS` being the
    one the page is asked for (fli maps a higher one to "any", so the tfs field
    would be omitted and the constraint lost), seniors and youth (Google has no
    such passenger kind), and `--no-airport-changes` / `--include-unavailable`,
    which the search page's `tfs=` parameter has no field for at all.
    `--fare-rules` too: fare bases and rules come from Matrix's
    `/v1/summarize`, which Google has no equivalent of. So does a flexible or
    an arrival date (`flex`, `arrive` and the return's): the page searches one
    departure date a slice. Children and infants
    stay on Google beside an adult, in a party of nine or fewer. Google has
    answered a route with flights with no rows for any infant, so that empty
    board is handed to Matrix afterwards (`_run_gflight_path`); a multi-cabin
    compare, which hands on only boards its row filter emptied, takes an infant
    to Matrix here.
    `--depart-times`/`--return-times` stay on Google when a leg's buckets form
    one window, or the leg has one window to the minute: the page takes one
    hour window per leg, and the row filter holds each row to the minute.

    An airport set or a metro code stays on Google Flights, which is asked for
    every member airport (`_metro`), as several pages when one page can't take
    them all (`gf_leg_pages`). What goes to Matrix is a leg past that
    (`gf_pages_refusal`: more than `MAX_GF_PAGES` pages, or one airport at both
    ends), a code that is neither an airport nor a metro code in the table,
    and a `multi_cabin` leg over one page (`gf_leg_refusal`), because every
    cabin pins the sort cabin's outbounds from one page.

    The page writes one filter set onto every slice, so a round trip whose
    return (`return_codes`) carries a different predicate set from the
    outbound's is Matrix's.

    `cabins` are the cabins `--cabin` asked for. A `+CABIN` naming exactly the
    one of them stays on Google, which is asked for that cabin and holds every
    leg of every row to it; any other `+CABIN`, or one beside several cabins or
    none, is Matrix's.

    A constraint the page cannot carry has to be a reason here and nowhere
    else. Left out, `auto` serves it on Google with the constraint silently
    dropped and `--backend gflight` accepts it without a word, while the deep
    link printed underneath still carries it — three surfaces disagreeing about
    what was asked.

    Whatever the cause, `auto` names it on stderr. Silently taking the 45x
    slower backend leaves the user with no way to tell a constraint they could
    drop from one they can't.

    Explicit --backend matrix: matrix. --backend gflight: gflight, unless the
    request is inexpressible on GF (error). A trip of one one-way per slice
    (`open_jaw`) whose only reason is that one ticket stays on gflight, which
    answers it with separate tickets alone (`_answer_multi_city_on_google`).

    `--bags` needs Google Flights, because Matrix prices no bags, and so do an
    arrival window and `--exclude-basic` (`_google_only`). With one,
    `--backend matrix` is refused, and so is any reason above: `auto` refuses
    the search naming that reason instead of answering it on Matrix without
    the flag. No refusal under one points at `--backend matrix`."""
    google_only = _google_only(
        bags=bags,
        arrive_times=arrive_times,
        return_arrive_times=return_arrive_times,
        exclude_basic=exclude_basic,
    )
    if google_only and backend == BACKEND_MATRIX:
        flag, gap = google_only[0]
        raise typer.BadParameter(
            f"{flag} needs Google Flights: Matrix {gap}. Drop {flag}, or drop --backend matrix."
        )
    reasons = _google_reasons(
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
        show_only_available=show_only_available,
        fare_rules=fare_rules,
        adults=adults,
        return_codes=return_codes,
        multi_cabin=multi_cabin,
        cabins=cabins,
        flex=flex,
        return_flex=return_flex,
        arrive=arrive,
        return_arrive=return_arrive,
        open_jaw=open_jaw,
    )

    # The same reasons go out two ways, and only one of them is markup. A
    # reason quotes the user's --routing string verbatim, so one square bracket
    # decides between a MarkupError traceback and a backslash the user can see.
    remedy = _matrix_remedy(google_only)
    if backend == BACKEND_AUTO:
        if not reasons:
            return BACKEND_GFLIGHT
        if google_only:
            raise typer.BadParameter(
                f"{google_only[0][0]} needs Google Flights, which can't serve "
                f"{_join_reasons(reasons)}. Drop it, or {remedy}.",
            )
        err.print(
            f"[dim]Using Matrix: Google Flights can't serve "
            f"{_safe_text(_join_reasons(reasons))}.[/]"
        )
        return BACKEND_MATRIX
    if backend == BACKEND_GFLIGHT and reasons and reasons != [_MULTI_CITY_ON_ONE_TICKET]:
        # typer renders a BadParameter as plain Text, never markup — escaping
        # here would print the backslashes instead of hiding them.
        raise typer.BadParameter(
            f"--backend gflight can't serve this request: {_join_reasons(reasons)}. "
            f"Drop it, or {remedy}.",
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
        _envelope.explain("awards", "--cash-only skips the award search")
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
        _explain_no_awards(sel, "no award provider is configured")
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
        _explain_no_awards(sel, "--providers names no configured provider")
        return False
    # The search runs without the named providers that have no credentials, and
    # nothing downstream sees them: the registry builds configured ones only.
    if missing := [n for n in sel.provider_filter or () if not known.get(n, False)]:
        _envelope.narrow(_named_not_configured(missing))
    return True


def _named_not_configured(names: list[str]) -> str:
    verb = "is" if len(names) == 1 else "are"
    return f"--providers names {', '.join(names)}, which {verb} not configured"


def _explain_no_awards(sel: ProviderSelection, reason: str) -> None:
    """Say why no award search runs, and narrow the answer when a provider was
    asked for: named in `--providers`. A machine with no tokens never asked
    PointsPath."""
    if not _envelope.active():
        return
    named = sel.provider_filter
    lost: list[str] = []
    if named is not None and "pp" in named:
        lost.append("PointsPath was asked for, and it has no tokens")
    if others := [n for n in named or () if n != "pp"]:
        lost.append(_named_not_configured(others))
    if lost:
        _envelope.narrow()
        reason = "; ".join(lost)
    _envelope.explain("awards", reason)


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

    return _run_matrix(go, said="Matrix search failed")


def _run_matrix[T](go: Callable[[], Coroutine[Any, Any, T]], *, said: str) -> T:
    """Run one Matrix conversation to its answer, or leave with a typed line and
    exit 1. `said` opens that line; it is escaped like any value printed here."""
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
        _reraise_if_orderly(e, said=said)
        err.print(f"[red]{_safe_text(said)}:[/] {_failure_text(e)}")
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
    console. Every arm that fails a command on a `MatrixApiError` reports through
    here, so one Matrix error reads the same whichever command asked for it;
    `tests/test_matrix_error_census.py` asserts it. An arm that is soft, one part
    of several that still answers, prints a yellow line naming that part and wraps
    the fields itself."""
    err.print(f"[red]Matrix returned an error ({_safe_text(e.kind)}):[/] {_safe_text(e.message)}")
    if e.request_id:
        err.print(f"[dim]request_id: {_safe_text(e.request_id)}[/]")


# Matrix silently UNDER-REPORTS multi-airport calendar grids under compute-budget
# pressure — even when the result is non-empty (a 3-destination query returned 12
# solutions where one destination alone returns 155). The only query guaranteed
# to fully price is a single (origin, destination), so a multi-airport calendar is
# always run as one sub-search per airport pair, metro codes split into their
# member airports, in parallel, and merged — the only way to get complete results.
# A mirrored pair never prices a round trip back into another airport of the set,
# so a round trip also runs the user's own combined query beside the pairs, which
# takes a day only where it is cheaper than every pair. Every query asks one
# currency when there is more than one origin, since the merge compares numbers.
#
# `split_calendar_search` returns the cartesian product of the expanded origins x
# destination GROUPS, less any pair with one airport at both ends, so the fan-out
# is |origins| x ceil(|destinations| / --max-per-query), plus one on a round trip:
# NYC-LON is 3 x 6 pairs and the combined query, 19 in all.
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

    # Each pair's answer with the pair that asked it, in sub-query order.
    results: list[tuple[Pair, CalendarResult]]
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
    # The combined query's answer, when it ran beside a round trip's pairs and
    # answered; its failure is in `failures` like any pair's.
    floor: tuple[Pair, CalendarResult] | None = None

    @property
    def failed(self) -> int:
        """How many origin/destination groups dropped out of the merge."""
        return len(self.failures)


class _CalendarCurrencyError(Exception):
    """A sub-query Matrix answered in another currency than the rest of the grid.

    The merge compares fares by number, so such an answer is left out rather
    than merged, and it is reported as a lost group. The message names the route
    itself: when every group is lost, the refusal prints the causes alone."""

    def __init__(self, route: str, got: Sequence[str], want: str | None) -> None:
        theirs = " and ".join(g or "no currency" for g in got)
        super().__init__(
            f"{route} came back priced in {theirs}, "
            + (f"not the grid's {want}" if want else "and no answer was in one currency")
        )


async def _gather_calendar(
    c: MatrixClient,
    subs: list[CalendarSearch],
    *,
    cache: bool,
    floor: CalendarSearch | None = None,
    currency: str | None = None,
) -> _CalendarFanout:
    """Run the sub-searches, and `floor` after them, concurrently on one client
    (its rate-limiter + semaphore bound the in-flight count). Each covers one
    (origin, destination group); a sub-query that fails just drops its own group
    from the merge rather than sinking the whole run, and is counted so the
    caller can say so.

    An answer in another currency than the grid's drops the same way. The grid's
    currency is `currency`, the one every query asked, else that of the first
    answer, in sub-query order, priced in one currency throughout, so the same
    answers keep the same groups on every run. An answer in two currencies, or
    in none, drops without deciding it for the answers after it."""
    queries = [*subs, floor] if floor is not None else subs
    results: list[CalendarResult | None] = [None] * len(queries)
    errors: list[Exception | None] = [None] * len(queries)

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
        for i, s in enumerate(queries):
            tg.start_soon(one, i, s)
    answered: list[tuple[Pair, CalendarResult]] = []
    failures: list[Exception] = []
    lost: list[str] = []
    floor_answer: tuple[Pair, CalendarResult] | None = None
    priced_in = [price_currencies(res) if res is not None else () for res in results]
    want = currency or next((got[0] for got in priced_in if len(got) == 1 and got[0]), None)
    for i, (s, res, error, got) in enumerate(zip(queries, results, errors, priced_in, strict=True)):
        e = error
        off = tuple(g for g in got if g != want)
        if off:
            e = _CalendarCurrencyError(_calendar_route_label(s), off, want)
        if e is not None:
            failures.append(e)
            lost.append(_calendar_route_label(s))
        elif res is not None and i == len(subs):
            floor_answer = (calendar_pair(s), res)
        elif res is not None:
            answered.append((calendar_pair(s), res))
    return _CalendarFanout(answered, failures, lost, floor_answer)


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
    _envelope.narrow()
    err.print(
        f"[yellow]{fan.failed:d} of {total:d} sub-queries failed; those origin/destination "
        f"groups are missing from the grid below: {_safe_text(', '.join(fan.lost))}.[/]"
    )
    # Each group with its own cause, because a brownout is waited out and a
    # refused query is rewritten. Yellow, like the per-cabin fan-out's line: the
    # grid below still answers, so this is not a failed command's red report.
    for route, cause in zip(fan.lost, fan.failures, strict=True):
        if isinstance(cause, MatrixApiError):
            err.print(
                f"[yellow]  {_safe_text(route)}: Matrix returned an error "
                f"({_safe_text(cause.kind)}): {_safe_text(cause.message)}[/]"
            )
            if cause.request_id:
                err.print(f"[dim]  request_id: {_safe_text(cause.request_id)}[/]")
        elif isinstance(cause, _CalendarCurrencyError):
            err.print(f"[yellow]  {_failure_text(cause)}[/]")  # it names its route
        else:
            err.print(f"[yellow]  {_safe_text(route)}: {_failure_text(cause)}[/]")


def _calendar_route_label(s: CalendarSearch) -> str:
    """`JFK,EWR→LHR` for a sub-query, for failure messages."""
    if not s.legs:
        return "?"
    return "→".join(calendar_pair(s))


def _run_calendar(
    search: CalendarSearch,
    *,
    rps: float,
    impersonate: str,
    no_cache: bool,
    max_per_query: int = 1,
    max_concurrency: int = _CALENDAR_FANOUT_CONCURRENCY,
) -> tuple[CalendarResult, int, bool]:
    """Execute a calendar search. A multi-airport query is fanned out into
    sub-searches of up to `max_per_query` destinations each, run in parallel
    (≤ `max_concurrency` at a time), and merged — Matrix under-reports a combined
    multi-airport grid, so the default of one destination per query is the only
    size guaranteed complete. A round trip also runs `search` itself beside them,
    for the returns into another airport of the set that no mirrored pair asks.

    Returns `(result, n_queries, floor_lost)` where `n_queries > 1` means the
    fan-out path was used (for a one-line note), counting that combined query, and
    `floor_lost` that it ran and its answer is not in the merge. A single-airport
    calendar runs as one query and returns `n_queries == 0`.
    """
    subs = split_calendar_search(search, max_per_query)
    floor: CalendarSearch | None = None
    if subs:
        # Split again from the pinned search, so every pair and the combined
        # query ask the one currency the merge can compare fares in.
        search = with_fanout_currency(search)
        subs = split_calendar_search(search, max_per_query)
        floor = search if len(search.legs) == _ROUND_TRIP_LEGS else None
    n = len(subs) + (1 if floor is not None else 0)
    multi = bool(subs)  # split returns [] when one query already covers the request
    conc = min(n, max(1, max_concurrency)) if multi else 3
    # Matrix may under-report a request naming several destinations; a split
    # whose queries each name one is not that request.
    if multi and any(len(q.legs[0].destinations) > 1 for q in subs):
        _envelope.narrow()
        err.print(
            "[yellow]--max-per-query > 1: Matrix may under-report a "
            "multi-destination request, so results could be incomplete.[/]"
        )
    elif not multi and max_per_query > 1 and len(expand_airports(search.legs[0].destinations)) > 1:
        # One group held every destination, so nothing split and the one query
        # asks them all: the request Matrix may under-report.
        _envelope.narrow(
            "--max-per-query > 1: one query asked every destination, and Matrix may "
            "under-report a multi-destination request, so results could be incomplete."
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

    answer: tuple[CalendarResult, int, bool] | None = None

    async def go() -> tuple[CalendarResult, int, bool]:
        nonlocal answer
        async with MatrixClient(
            rps=max(rps, float(conc)), impersonate=impersonate, concurrency=conc
        ) as c:
            if not multi:
                result = cast("CalendarResult", await c.execute(search, cache=not no_cache))
                answer = (result, 0, False)
            else:
                fan = await _gather_calendar(
                    c,
                    subs,
                    cache=not no_cache,
                    floor=floor,
                    currency=search.options.currency,
                )
                # Merge BEFORE reporting: whether what survived says anything is
                # what decides between a note and a refusal, and only the merge
                # knows.
                merged = merge_calendar_results(fan.results, fan.floor, window=search.window)
                empty = is_empty_calendar(merged)
                _report_calendar_fanout(fan, n, merged_empty=empty)
                floor_lost = floor is not None and fan.floor is None
                answer = (merged, 0, False) if empty else (merged, n, floor_lost)
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
    "no data to this client."
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


def _http_date_grid(search: CalendarSearch, *, json_out: bool) -> dict[str, float]:
    """`date_grid`, for the one shape it prices: one-way dates between two
    airports, into a table.

    A round trip, a JSON document and an airport set or metro code are the page
    grid's, so they get the gate's note and its remedy rather than a narrower
    grid under a wider question: `date_grid` writes one airport per side."""
    from ._gf_dategrid import GfGridUnavailableError, date_grid  # noqa: PLC0415

    airport_set = any(
        len(expand_airports(lg.origins)) > 1 or len(expand_airports(lg.destinations)) > 1
        for lg in search.legs
    )
    if json_out or len(search.legs) == _ROUND_TRIP_LEGS or airport_set:
        raise GfGridUnavailableError
    return date_grid(search)


def _run_fast_calendar_grid(
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    matrix_url: bool,
    google_url: bool,
    json_out: bool = False,
) -> None:
    """`--fast` over http: the Google Flights date-grid alone, or a refusal. No Matrix."""
    from ._gf_dategrid import GfGridUnavailableError  # noqa: PLC0415
    from ._gflight_ids import GfThrottledError  # noqa: PLC0415

    grid: dict[str, float] = {}
    try:
        grid = _http_date_grid(search, json_out=json_out)
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
        # Ahead of the broad except, as in the weave. The remedy is this arm's
        # alone: the weave's note shares the sentence above, and there
        # `--gf-transport` is a usage error.
        err.print(
            f"[dim]{_GF_GRID_UNAVAILABLE_NOTE} Use [bold]--gf-transport browser[/] "
            "(or [bold]auto[/]) to read it from the page in Chrome.[/]"
        )
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


def _run_fast_browser_grid(
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    json_out: bool,
    headed: bool,
    matrix_url: bool,
    google_url: bool,
) -> None:
    """`--fast` over the browser: the search page's own price graph, or a refusal.

    Every no-grid outcome leaves by the one exit at the bottom, on stderr, exactly
    as `_run_fast_calendar_grid`'s do, so stdout carries a grid or nothing.

    A trip-length range is one graph per length (`price_graphs`): a column each
    in the table, and in JSON the range document (`_graph_range_document`). A
    length that was lost is named on stderr and, in JSON, under `lost`; the
    lengths that priced still answer. When none priced, each is named ahead of
    the no-grid line.

    The guard is armed and the session scope held here, outside any `anyio.run`:
    the page loads run on this thread, the one a Ctrl-C lands on, and the scope
    closes Chrome once however many loads the window took."""
    from ._gf_browser import interrupt_guard, session_scope  # noqa: PLC0415 — GF-only
    from ._gf_calgraph import (  # noqa: PLC0415 — fli, ~95 ms
        document,
        graph_lengths,
        price_graph,
        price_graphs,
    )

    lengths = graph_lengths(search)
    graphs: Sequence[PriceGraph] = ()
    lost: Sequence[LostLength] = ()
    try:
        with interrupt_guard(), session_scope():
            if len(lengths) > 1:
                graphs, lost = price_graphs(search, headed=headed, raise_unpriced=False)
            else:
                graphs = (price_graph(search, headed=headed),)
    except GfThrottledError:
        err.print("[dim]Google Flights rate-limited the browser rung; no grid to show.[/]")
    except GfBrowserUnavailableError as e:
        # The default remedy offers `--gf-transport http`, which has no grid to
        # serve under `--fast`; a launch or install remedy is kept.
        remedy = e.remedy.removesuffix(BROWSER_DEFAULT_REMEDY).strip()
        err.print(
            f"[yellow]{_safe_text(_GF_GRID_NAME)} failed:[/] "
            f"{_safe_text(f'{e.reason} {remedy}'.strip())}"
        )
    except (typer.Exit, typer.Abort):
        raise  # an orderly exit is not a grid failure; see the weave's arm
    except Exception as e:  # noqa: BLE001 — any other cause is still just "no grid"
        err.print(f"[yellow]{_safe_text(_GF_GRID_NAME)} failed:[/] {_safe_text(e)}")
    for nights, cause in lost:
        # The range asked for this length's graph, so the answer is narrower.
        _envelope.narrow()
        err.print(
            f"[yellow]Google Flights price graph not shown:[/] "
            f"{_safe_text(f'{nights}-night trips: {_graph_failure_text(cause)}')}"
        )
    if not graphs:
        err.print("[yellow]No Google Flights grid; drop --fast for Matrix.[/]")
        raise typer.Exit(1)
    priced = graphs

    def _write_answer() -> None:
        if json_out:
            origin, destination = ",".join(origins), ",".join(dests)
            if len(lengths) > 1:
                doc = _graph_range_document(
                    priced, lost, lengths=lengths, origin=origin, destination=destination
                )
                graphs: list[dict[str, Any]] = doc["graphs"]
            else:
                doc = document(priced[0], origin=origin, destination=destination)
                graphs = [doc]
            if _envelope.active():
                _record_price_graphs(priced)
                # A range's cells each carry their return date, so one list of
                # every length's cells still names each cell's length.
                _envelope.record_calendar(
                    backend="gflight",
                    rows=[
                        _envelope.CalendarRow.model_validate(
                            {
                                "price": cell["price"],
                                "currency": graph["currency"],
                                "row": cell,
                                "departure": cell["departure"],
                                "return": cell.get("return"),
                            }
                        )
                        for graph in graphs
                        for cell in cast("list[dict[str, Any]]", graph["grid"])
                    ],
                )
                return
            sys.stdout.write(json.dumps(doc, indent=2))
            return
        if len(lengths) > 1:
            _show_graphs(priced, origins=origins, dests=dests, sd=sd, ed=ed)
        else:
            _render_date_grid(
                {cell.departure.isoformat(): cell.price for cell in priced[0].cells},
                origin=origins,
                destination=dests,
                sd=sd,
                ed=ed,
                trip_length=priced[0].trip_length,
            )
        _emit_urls(search, matrix_url=matrix_url, google_url=google_url)

    _deliver_calendar(_write_answer, backend=_GF_GRID_NAME)


def _graph_range_document(
    graphs: Sequence[PriceGraph],
    lost: Sequence[LostLength],
    *,
    lengths: Sequence[int | None],
    origin: str,
    destination: str,
) -> dict[str, Any]:
    """The `--fast --format json` document of a trip-length range, and the
    `google_price_graph` a default calendar's carries: every length asked, one
    `document` per length that priced, and every length lost with its reason.
    Its shape follows what was asked, not what priced."""
    from ._gf_calgraph import document  # noqa: PLC0415 — fli, ~95 ms

    return {
        "origin": origin,
        "destination": destination,
        "currency": "USD",
        "trip_lengths": list(lengths),
        "graphs": [document(g, origin=origin, destination=destination) for g in graphs],
        "lost": [
            {"trip_length": nights, "reason": _graph_failure_text(cause)} for nights, cause in lost
        ],
    }


def _record_price_graphs(graphs: Sequence[PriceGraph]) -> None:
    """Google's graphs as the envelope's `price_graph`, one per trip length that
    priced, in the USD every graph prices in."""
    if not _envelope.active():
        return
    _envelope.record_price_graph(
        [
            _envelope.PriceGraph(
                trip_length=graph.trip_length,
                currency="USD",
                cells=[
                    _envelope.PriceGraphCell.model_validate(
                        {
                            "departure": cell.departure,
                            "return": cell.return_date,
                            "price": cell.price,
                        }
                    )
                    for cell in graph.cells
                ],
            )
            for graph in graphs
        ]
    )


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
    """The `--gf-transport http` calendar for a GF-serveable window (one-way,
    single-airport), progressive: dispatch the Google Flights date-grid and the
    Matrix calendar CONCURRENTLY under one event loop, paint the GF grid
    immediately (~1s) while Matrix is in flight, then paint the authoritative
    Matrix calendar (~45s) — total ≈ max(GF, Matrix), not the sum. Mirrors the
    SHAPE of `_run_enriched_path` (the search-path weave) and not its stream
    discipline: every status line here goes to stderr, while that one still writes
    some of its own to stdout. `--fast` never reaches here (the command serves the
    grid alone for that), and neither does the default transport, which reads the
    page's price graph beside Matrix instead (`_run_calendar_beside_graph`). The
    gate keeps an airport set or metro code off this path, so the Matrix side is
    one `execute` (no fan-out): one combined Matrix calendar under-reports a set
    (quirk #7), and `date_grid` writes one airport per side. A set goes to
    `_run_calendar` instead.

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
    _say_unpriced(res, search)


def _combined_only_sides(search: CalendarSearch, max_per_query: int) -> str:
    """Which round trips only the combined query prices, as the clause after
    "Round trips that". A return to another origin airport needs more than one
    origin. A sub-query's return leg asks every airport of its destination group,
    so a return from another destination airport needs more destinations than
    one group holds, and counts only when it comes from another group."""
    out = search.legs[0]
    per_query = max(1, max_per_query)
    sides: list[str] = []
    if len(expand_airports(out.origins)) > 1:
        sides.append("return to a different origin airport")
    if len(expand_airports(out.destinations)) > per_query:
        sides.append(
            "come back from a different destination airport"
            if per_query == 1
            else "come back from a destination airport in another group"
        )
    return " or ".join(sides)


def _run_matrix_calendar(
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
    max_per_query: int,
    max_concurrency: int,
    json_out: bool,
    matrix_url: bool,
    google_url: bool,
    deliver: bool = True,
) -> CalendarResult:
    """The Matrix calendar, delivered: the whole answer for every calendar that
    neither `--fast` nor the http weave serves, and the half of the default one
    that Google's price graph is printed after. Returns the grid. Without
    `deliver` the caller writes it (`_write_matrix_calendar`): a JSON or envelope
    document waits for Google's graph.

    Authoritative, and the only path for Tier-2/3 routing. A
    multi-airport calendar `_run_calendar` splits into one sub-query per
    (origin, destination group), beside the combined query on a round trip, and
    merges."""
    # CalendarSearch → CalendarResult by client._parse_response dispatch.
    res, n_split, floor_lost = _run_calendar(
        search,
        rps=rps,
        impersonate=impersonate,
        no_cache=no_cache,
        max_per_query=max_per_query,
        max_concurrency=max_concurrency,
    )
    if n_split:
        # On stderr, beside the coverage note the fan-out prints, in both formats:
        # it is provenance about the answer rather than part of it, and it is
        # written BEFORE the delivery below, so on stdout a failure there would
        # leave it standing alone under exit 1 — a document, to a caller that reads
        # the stream. On a round trip `n_split` counts the combined query
        # `_run_calendar` runs beside the pairs; it alone prices the trips
        # `_combined_only_sides` names, and Matrix may under-report it, so the
        # note says the grid answers that part less completely than the rest.
        # When it failed, the grid holds none of those trips, and the note says
        # that.
        round_trip = len(search.legs) == _ROUND_TRIP_LEGS
        pairs = n_split - 1 if round_trip else n_split
        with_floor = round_trip and not floor_lost
        sides = _combined_only_sides(search, max_per_query)
        err.print(
            f"[dim]Queried {pairs:d} "
            + ("origin/destination groups" if max_per_query > 1 else "airport pairs")
            + (" separately plus the combined query," if with_floor else " separately")
            + " and merged — Matrix under-reports the combined multi-airport calendar grid.[/]"
        )
        if with_floor:
            _envelope.narrow()
            err.print(
                f"[dim]Round trips that {sides} come only "
                "from the combined query, which Matrix may under-report.[/]"
            )
        elif round_trip:
            err.print(
                f"[dim]Round trips that {sides} are missing: "
                "only the combined query prices them, and it failed.[/]"
            )
    if deliver:
        _write_matrix_calendar(
            res,
            search,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            dmin=dmin,
            dmax=dmax,
            json_out=json_out,
            matrix_url=matrix_url,
            google_url=google_url,
        )
    return res


def _write_matrix_calendar(
    res: CalendarResult,
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    dmin: int,
    dmax: int,
    json_out: bool,
    matrix_url: bool,
    google_url: bool,
    graph: dict[str, Any] | None = None,
) -> None:
    """Matrix's calendar in the format asked, then the dates it left unpriced.

    `graph` is Google's price-graph document, which `--format json` carries
    beside Matrix's body as `google_price_graph`: Matrix's own keys are
    camelCase, so it cannot meet one of them."""
    if _envelope.active():
        _envelope.record_calendar(backend="matrix", rows=_calendar_envelope_rows(res, sd=sd, ed=ed))
    elif json_out:
        body = res.raw if graph is None else {**(res.raw or {}), "google_price_graph": graph}
        sys.stdout.write(json.dumps(body, indent=2))
    else:

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
    _say_unpriced(res, search)


def _default_graph_blocker(
    search: CalendarSearch,
    *,
    one_way: bool,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
) -> str | None:
    """Why the calendar without `--fast` cannot ask Google's price graph, or
    None when it can. The phrase completes "this is …".

    The `--fast` browser gate decides, over a copy of one trip length: every
    length shares the legs and the filters, and each is asked as a graph of its
    own. The page budget is counted over all of them. The output format is no
    reason: a JSON or envelope document carries the graph too."""
    window = search.window
    one_length = search.model_copy(
        update={"window": window.model_copy(update={"duration_max": window.duration_min})}
    )
    reason = _grid_branch_blocker(
        one_length,
        json_out=False,
        one_way=one_way,
        origins=origins,
        dests=dests,
        fast=True,
        graph=True,
    )
    if reason is not None:
        return reason
    from ._gf_calgraph import page_budget_blocker  # noqa: PLC0415 — the gate above loaded it

    return page_budget_blocker(search)


def _run_calendar_beside_graph(
    search: CalendarSearch,
    matrix: Callable[[], CalendarResult],
    *,
    deliver: Callable[[CalendarResult, dict[str, Any] | None], None] | None,
    headed: bool,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    asked: Sequence[str],
) -> None:
    """Matrix's calendar, then Google's price graph read while Matrix ran.

    `matrix` runs on this thread as it would alone. The graph is read on a
    worker started before it and is waited for only once Matrix has answered,
    so nothing of Google's holds that answer back or changes it: any Google
    failure is one stderr line, and the exit code is Matrix's. Two lows that
    differ get one stderr line after Google's answer (`_two_lows_note`);
    `asked` is the calendar's own flags, which the searches that line suggests
    repeat.

    A table is delivered by `matrix` itself, and Google's table follows it.
    A JSON or envelope document is one document, so `deliver` writes it once
    the graph is in, with the graph inside it; the envelope records a graph
    that priced even when Matrix failed.

    A plain worker, not a second event loop: the graph is sync, and the pool's
    exit waits for the worker on every path out, which is what keeps its Chrome
    from outliving the command. The session is thread-local, so the scope that
    closes it is held on the worker.

    The guard is armed here, outside Matrix's `anyio.run`, as `_run_the_weave`
    arms its own: a Ctrl-C stops the driver the worker is waiting on, and lands
    on this thread as a plain `KeyboardInterrupt`."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 — this path alone

    from ._gf_browser import interrupt_guard, session_scope, stop_all_drivers  # noqa: PLC0415
    from ._gf_calgraph import graph_lengths, price_graphs  # noqa: PLC0415 — the gate loaded it

    def _read_graphs() -> GraphRange:
        with session_scope():
            return price_graphs(search, headed=headed)

    matrix_exit: typer.Exit | None = None
    answer: CalendarResult | None = None
    graphs: Sequence[PriceGraph] = ()
    lost: Sequence[LostLength] = ()
    failure: Exception | None = None
    try:
        with interrupt_guard(), ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(_read_graphs)
            try:
                answer = matrix()
            except typer.Exit as e:
                # Matrix failed and has said so. Its exit code is kept for the end,
                # so a graph that priced still prints above it.
                matrix_exit = e
            except BaseException:
                # The command ends here, and the pool's exit would otherwise wait
                # out every page load still to come.
                stop_all_drivers()
                raise
            try:
                graphs, lost = job.result()
            except Exception as e:  # noqa: BLE001 — every cause is one line; Matrix stands
                failure = e
    except* KeyboardInterrupt:
        # typer turns a bare `KeyboardInterrupt` into exit 130 and a group of them
        # into a traceback; Matrix's task group can hand one back wrapped.
        raise KeyboardInterrupt from None
    if deliver is not None:
        _record_price_graphs(graphs)
        if answer is not None:
            graph_doc: dict[str, Any] | None = None
            if graphs:
                graph_doc = _graph_range_document(
                    graphs,
                    lost,
                    lengths=graph_lengths(search),
                    origin=",".join(origins),
                    destination=",".join(dests),
                )
            deliver(answer, graph_doc)
    elif failure is None:
        try:
            _show_graphs(graphs, origins=origins, dests=dests, sd=sd, ed=ed)
        except (typer.Exit, typer.Abort):
            raise  # an orderly exit is not a render failure
        except Exception as e:  # noqa: BLE001 — Google's table must not fail Matrix's run
            failure = e
    _say_beside_graph(
        failure,
        lost,
        answer,
        graphs,
        search,
        origins=origins,
        dests=dests,
        sd=sd,
        ed=ed,
        asked=asked,
    )
    if matrix_exit is not None:
        raise matrix_exit


def _say_beside_graph(
    failure: Exception | None,
    lost: Sequence[LostLength],
    answer: CalendarResult | None,
    graphs: Sequence[PriceGraph],
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    asked: Sequence[str],
) -> None:
    """The lines after both answers, in every format: the graph's failure or its
    lost lengths, then the two-lows note."""
    if failure is not None:
        text = _graph_failure_text(failure)
        _report_graph_failure(text)
        _envelope.explain("price_graph", f"not shown: {text}")
    elif lost:
        named = (f"{nights}-night trips: {_graph_failure_text(cause)}" for nights, cause in lost)
        _report_graph_failure(" ".join(named))
    if failure is None and answer is not None:
        note: str | None = None
        # The note explains two answers that are already out; failing to compose
        # it must not turn them into a traceback and a failed exit.
        with contextlib.suppress(Exception):
            note = _two_lows_note(
                answer, graphs, search, origins=origins, dests=dests, sd=sd, ed=ed, asked=asked
            )
        if note is not None:
            # Left for the terminal to wrap: a newline inside one of its commands
            # would cut the command short when pasted.
            err.print(f"[yellow]{_safe_text(note)}[/]", soft_wrap=True)


def _show_graphs(
    graphs: Sequence[PriceGraph],
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
) -> None:
    """Google's table after Matrix's: the date grid for one graph, one column per
    trip length for several."""
    across_set = len(expand_airports(origins)) > 1 or len(expand_airports(dests)) > 1
    if len(graphs) == 1:
        (graph,) = graphs
        _render_date_grid(
            {cell.departure.isoformat(): cell.price for cell in graph.cells},
            origin=origins,
            destination=dests,
            sd=sd,
            ed=ed,
            trip_length=graph.trip_length,
            across_set=across_set,
        )
        return
    _render_graph_range(
        graphs, origin=origins, destination=dests, sd=sd, ed=ed, across_set=across_set
    )


def _graph_failure_text(cause: Exception) -> str:
    """A price-graph failure in the words of the line that reports it.

    A launch or install remedy is kept. The default one offers `--backend
    matrix`, which the calendar has no use for; the line ends by offering the
    transport that opens no Chrome instead."""
    if isinstance(cause, GfBrowserUnavailableError):
        remedy = cause.remedy.removesuffix(BROWSER_DEFAULT_REMEDY).strip()
        text = f"{cause.reason} {remedy}".strip()
    elif isinstance(cause, GfThrottledError):
        text = "Google Flights rate-limited the browser rung."
    else:
        text = str(cause).strip() or type(cause).__name__
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _report_graph_failure(text: str) -> None:
    """The one line for a price graph, or the trip lengths of one, that could not
    be shown beside Matrix."""
    err.print(
        f"[yellow]Google Flights price graph not shown:[/] {_safe_text(text)} "
        "[dim]--gf-transport http skips Chrome.[/]"
    )


# Each cabin's `--cabin` name, which the note also calls it by.
_CABIN_NAMES: dict[Cabin, str] = {
    Cabin.COACH: "economy",
    Cabin.PREMIUM_COACH: "premium economy",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}


def _asked_flags(
    opts: SearchOptions,
    *,
    one_way: bool,
    routing: str | None,
    extension: str | None,
    routing_return: str | None,
    extension_return: str | None,
    depart_times: str | None,
) -> tuple[str, ...]:
    """The calendar's own constraints as the flags `flight search` and `flight
    detail` take, so a search on one of its date pairs asks the same question.
    The codes go as typed: each command derives the return's from them as the
    calendar did, except that each whitespace character goes as a space: every
    parser of them splits on any whitespace, but `_safe_text` drops a carriage
    return from the note, joining a code to its argument, and a newline would
    break the note's one line."""
    flags: list[str] = []
    if opts.cabin is not Cabin.COACH:
        flags += ["--cabin", _CABIN_NAMES[opts.cabin]]
    if opts.pax.adults != 1:
        flags += ["--adults", f"{opts.pax.adults:d}"]
    if opts.max_extra_stops is not None:
        flags += ["--stops", f"{opts.max_extra_stops:d}"]
    typed = [("--routing", routing), ("--ext", extension), ("--depart-times", depart_times)]
    if not one_way:
        typed += [("--routing-ret", routing_return), ("--ext-ret", extension_return)]
    for flag, value in typed:
        if value is not None:
            flags += [flag, "".join(" " if c.isspace() else c for c in value)]
    if opts.currency is not None:
        flags += ["--currency", opts.currency]
    return tuple(flags)


class _Low(NamedTuple):
    """One side's lowest fare, as its own table shows it."""

    price: str  # with the currency code the side priced in
    amount: float
    departure: date
    return_date: date | None  # None on a one-way
    origin: str
    destination: str


def _window_date(
    month: int | None, day: int, sd: date, ed: date, *, year: int | None = None
) -> date | None:
    """The one date of the window with this day of the month, in this month and
    year where the grid names them; None when no date of the window or more than
    one is."""
    days = (sd + timedelta(days=i) for i in range((ed - sd).days + 1))
    hits = [
        d
        for d in days
        if d.day == day and (not month or d.month == month) and (not year or d.year == year)
    ]
    return hits[0] if len(hits) == 1 else None


def _window_days(res: CalendarResult, sd: date, ed: date) -> Generator[tuple[date, CalendarDay]]:
    """Each day of Matrix's grid that falls on one date of the window, with that
    date. A disabled day is the padding of a neighboring month.

    A window of a year or more holds some day of a month twice, and only the
    month's year tells the two apart. The parsed month drops the year Matrix's
    body names for it, so it is read from the body. A merged grid's months name
    their year only where its window holds some day of a month twice."""
    raw: Any = (res.raw or {}).get("calendar")
    found: Any = cast("dict[str, Any]", raw).get("months") if isinstance(raw, dict) else None
    bodies: list[Any] = cast("list[Any]", found) if isinstance(found, list) else []
    if len(bodies) != len(res.months):
        bodies = [None] * len(res.months)
    for month, body in zip(res.months, bodies, strict=True):
        year: Any = cast("dict[str, Any]", body).get("year") if isinstance(body, dict) else None
        for day in month.days:
            when = _window_date(
                month.month, day.date, sd, ed, year=year if isinstance(year, int) else None
            )
            if not day.disabled and when is not None:
                yield when, day


def _date_runs(dates: Sequence[date]) -> str:
    """Ascending dates, comma-separated, each run of consecutive ones as `A to B`."""
    runs: list[tuple[date, date]] = []
    for d in dates:
        if runs and d - runs[-1][1] == timedelta(days=1):
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    return ", ".join(
        a.isoformat() if a == b else f"{a.isoformat()} to {b.isoformat()}" for a, b in runs
    )


def _unpriced_lines(
    res: CalendarResult, *, sd: date, ed: date, lengths: Sequence[int | None]
) -> list[str]:
    """One line for each trip length (None on a one-way) that Matrix's grid holds
    no fare for on some departure date of the window, naming those dates.

    A day is placed as `_matrix_low` places it. A round-trip day prices a length
    only through that length's option, since the day's own price is its cheapest
    length's. A grid the table prints as empty prices no date."""
    asked = [sd + timedelta(days=i) for i in range((ed - sd).days + 1)]
    priced: dict[int | None, set[date]] = {nights: set() for nights in lengths}
    if not is_empty_calendar(res):
        for when, day in _window_days(res, sd, ed):
            if None in priced and day.min_price:
                priced[None].add(when)
            for option in day.options:
                if option.trip_length in priced and option.min_price:
                    priced[option.trip_length].add(when)
    lines: list[str] = []
    for nights in lengths:
        missing = [d for d in asked if d not in priced[nights]]
        if missing:
            trips = "" if nights is None else f" for {nights:d}-night trips"
            lines.append(
                f"Matrix priced no fare on {len(missing):d} of {len(asked):d} departure "
                f"dates asked{trips}: {_date_runs(missing)}."
            )
    return lines


def _say_unpriced(res: CalendarResult, search: CalendarSearch) -> None:
    """Name the asked departure dates Matrix's grid left unpriced, after its
    answer, in every format.

    Matrix under-reports a calendar without saying so (quirk #7), so a date its
    grid holds no fare for is named rather than left out. The grid cannot tell
    such a date from a day with no service; either way the answer is narrower
    than the question."""
    window = search.window
    lengths: tuple[int | None, ...] = (None,)
    if len(search.legs) == _ROUND_TRIP_LEGS:
        lengths = tuple(range(window.duration_min, window.duration_max + 1))
    for line in _unpriced_lines(res, sd=window.start, ed=window.end, lengths=lengths):
        _envelope.narrow()
        # One line, as the two-lows note, so a reader matches it whole.
        err.print(f"[yellow]{_safe_text(line)}[/]", soft_wrap=True)


def _cheapest_option(day: CalendarDay) -> DurationOption | None:
    """The trip length a round-trip day's own price is: its cheapest, the shortest
    of a tie. An option with an empty price prices no trip, so it is never that."""
    priced = [o for o in day.options if o.min_price]
    return min(priced, key=lambda o: (o.price_value, o.trip_length), default=None)


def _matrix_low(
    res: CalendarResult,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    round_trip: bool,
) -> _Low | None:
    """The row Matrix's table lists first: its cheapest priced day, the earliest
    of a tie, at that day's cheapest trip length, the shortest of a tie.

    A day that is no single date of the window is passed over. A round-trip day
    with no trip length has no return date to name, so it gives no low rather
    than a wrong one. A grid the table prints as empty has no low either."""
    if is_empty_calendar(res):
        return None
    best: tuple[float, date, str, CalendarDay] | None = None
    for when, day in _window_days(res, sd, ed):
        price, amount = day.min_price, day.price_value
        if price and amount is not None and (best is None or (amount, when) < best[:2]):
            best = (amount, when, price, day)
    if best is None:
        return None
    amount, when, price, day = best
    option = _cheapest_option(day)
    if round_trip and option is None:
        return None
    back = when + timedelta(days=option.trip_length) if round_trip and option else None
    # A merged grid names the pair that priced each length and each day.
    holder = option if option is not None and option.origin is not None else day
    origin, destination = holder.origin or ",".join(origins), holder.destination or ",".join(dests)
    return _Low(price, amount, when, back, origin, destination)


def _graph_low(
    graphs: Sequence[PriceGraph], *, origins: tuple[str, ...], dests: tuple[str, ...]
) -> _Low | None:
    """Google's cheapest cell over every trip length, the earliest departure of
    a tie, then the shortest length, which is its table's first row."""
    cells = [(cell, graph.trip_length) for graph in graphs for cell in graph.cells]
    if not cells:
        return None
    cell, nights = min(cells, key=lambda c: (c[0].price, c[0].departure, c[1] or 0))
    back = cell.return_date
    if back is None and nights is not None:
        back = cell.departure + timedelta(days=nights)
    # Compared as the table prints it, in whole dollars, so the note agrees or
    # differs with what the reader sees.
    shown = f"{cell.price:.0f}"
    return _Low(
        f"USD{shown}", float(shown), cell.departure, back, ",".join(origins), ",".join(dests)
    )


def _stops_reach_the_page(search: CalendarSearch) -> bool:
    """Whether Google's page was asked a stop limit: `--stops`, or a stop code
    on a leg, as `_gf_calgraph.page_url` writes one."""
    from .routing_predicates import StopsPred, classify  # noqa: PLC0415 — loaded by the gate

    if search.options.max_extra_stops is not None:
        return True
    return any(
        isinstance(p, StopsPred)
        for leg in search.legs
        for p in classify(leg.route_language, leg.extension).predicates
    )


def _two_lows_note(
    res: CalendarResult,
    graphs: Sequence[PriceGraph],
    search: CalendarSearch,
    *,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    asked: Sequence[str],
) -> str | None:
    """The line for a default calendar whose two tables show different lows, or
    None when both priced and agree, or either priced nothing.

    Both sides were asked one question, and each answers it differently: Matrix
    lists fares it priced, under its default limit of one leg more than the
    fewest on a route in each direction, and Google's graph is one price per date pair with no
    itinerary behind it. So two lows can differ with neither table wrong, and
    the line names both, what both asked, how they differ, and the search on
    each date pair that shows what is bookable. Google's table prints whole
    dollars, so two USD lows less than a dollar apart agree. A Matrix grid in
    another currency is named in it and not compared."""
    round_trip = len(search.legs) == _ROUND_TRIP_LEGS
    matrix = _matrix_low(res, origins=origins, dests=dests, sd=sd, ed=ed, round_trip=round_trip)
    google = _graph_low(graphs, origins=origins, dests=dests)
    if matrix is None or google is None:
        return None
    ccy = _split_price(matrix.price)[0]
    usd = ccy == "USD"
    if usd and abs(matrix.amount - google.amount) < 1:
        return None

    def trip(low: _Low) -> str:
        if low.return_date is None:
            return f"{low.departure.isoformat()}, one-way"
        nights = (low.return_date - low.departure).days
        return (
            f"{low.departure.isoformat()} to {low.return_date.isoformat()}, "
            f"{nights:d} night{'' if nights == 1 else 's'}"
        )

    # As Google's table title says: a set's cell is the cheapest of its pairs.
    across = len(expand_airports(origins)) > 1 or len(expand_airports(dests)) > 1
    lows = (
        f"Matrix {matrix.price} ({trip(matrix)}, {matrix.origin}→{matrix.destination}), "
        f"Google Flights {google.price} ({trip(google)}, "
        f"{'cheapest across ' if across else ''}{google.origin}→{google.destination})"
    )
    opening = (
        f"Matrix and Google Flights differ on the lowest fare: {lows}."
        if usd
        else "Matrix and Google Flights priced in different currencies, so their "
        f"lowest fares are not compared: {lows}; --currency USD asks Matrix in USD."
    )
    window = search.window
    span = (
        f"{window.duration_min:d}"
        if window.duration_min == window.duration_max
        else f"{window.duration_min:d}-{window.duration_max:d}"
    )
    graph_lengths = [g.trip_length for g in graphs if g.trip_length is not None]
    lost = round_trip and graph_lengths != list(range(window.duration_min, window.duration_max + 1))
    adults = search.options.pax.adults
    shared = [_CABIN_NAMES[search.options.cabin], f"{adults:d} adult{'' if adults == 1 else 's'}"]
    if not lost:
        shared.append(f"{span} nights" if round_trip else "one-way")
    if usd:
        shared.append("in USD")
    shared.append("between the same airports")
    both = "Both asked " + ", ".join(shared)
    if lost:
        # A length the graph lacks may never have been loaded, so it is named as
        # what Google priced, not as what Google was asked.
        both += (
            f", Matrix for {span} nights; Google's graph priced "
            f"{_trip_lengths_text(graph_lengths)} trips only"
        )
    if not _stops_reach_the_page(search):
        # Matrix's limit holds each slice, so a round trip may carry an extra stop
        # each way.
        both += (
            f"; Matrix held each {'direction' if round_trip else 'trip'} to one stop "
            "more than the fewest on its route, Google allowed any number of stops"
        )
    detail = ["flight", "detail", matrix.origin, matrix.destination]
    detail += ["--dep", matrix.departure.isoformat()]
    if matrix.return_date is not None:
        detail += ["--return", matrix.return_date.isoformat()]
        if span != _DEFAULT_CALENDAR_DURATION:
            detail += ["-d", span]
    # Several origins with no `--currency` asked the grid in USD; `detail` on the
    # one pair it names would answer in that origin's currency.
    fanout = with_fanout_currency(search).options.currency
    if search.options.currency is None and fanout is not None:
        asked_detail = [*asked, "--currency", fanout]
    else:
        asked_detail = [*asked]
    gsearch = ["flight", "search", google.origin, google.destination]
    gsearch += ["--dep", google.departure.isoformat()]
    if google.return_date is not None:
        gsearch += ["--return", google.return_date.isoformat()]
    return (
        f"{opening} {both}. Matrix's grid is fares Matrix priced; Google's graph is "
        "one price per date pair with no itinerary behind it; either can leave out a "
        f"fare the other lists. A search on the date {'pair ' if round_trip else ''}"
        f"shows what is bookable: {shlex.join([*detail, *asked_detail])} (Matrix), "
        f"{shlex.join([*gsearch, *asked, '--backend', 'gflight'])} (Google)."
    )


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
            f"[yellow]--pick {pick:d} is out of range (1-{len(result.solutions):d}); "
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


type _PinSegments = tuple[list[dict[str, str]], list[dict[str, str]] | None]


def _pin_segments(result: SearchResult | None, idx: int) -> _PinSegments | None:
    """Itinerary `idx`'s outbound and return segments in the shape the pinned
    URL builders take, or None when the result is empty or any slice can't be
    reduced to a segment list (see `extract_pin_segments_from_slice` for the
    bail-out cases).

    None for a trip Google sells as separate tickets too: a link pinned to its
    flights opens their one-ticket page, which lists neither its price nor, on
    a round trip, any return for it."""
    if result is None or idx >= len(result.solutions):
        return None
    if result.solutions[idx].ticketing is not None:
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
    return out_segments, ret_segments


def _try_pinned_gflight_url(search: Search, result: SearchResult | None, idx: int) -> str | None:
    """Build a Google Flights URL that pre-selects itinerary `idx` in `result`,
    if the data supports it. Returns None when `_pin_segments` finds none or
    the search shape doesn't support pinning (calendar-grid mode).
    """
    segments = _pin_segments(result, idx)
    if segments is None:
        return None
    out_segments, ret_segments = segments
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
        for note in _matrix_link_caveats(search):
            console.print(f"  [yellow]note: {_safe_text(note)}[/]")
    if google_url:
        # `google_flights_url` builds protobuf-encoded tfs= URLs via fast_flights.
        # That library has no documented exception surface — catch broadly so a
        # missing IATA or unsupported variant degrades the URL line, not the run.
        try:
            pinned = _try_pinned_gflight_url(search, result, idx) if idx is not None else None
            if pinned is not None:
                console.print(f"[dim]Google Flights ({pinned_label} pinned):[/]")
                console.print(f"  [link]{_safe_text(pinned)}[/]")
                for note in _pinned_gflight_url_caveats(search):
                    console.print(f"  [yellow]note: {_safe_text(note)}[/]")
            else:
                console.print("[dim]Google Flights (tfs= structured):[/]")
                console.print(f"  [link]{_safe_text(google_flights_url(search))}[/]")
                for note in _gflight_url_caveats(search):
                    console.print(f"  [yellow]note: {_safe_text(note)}[/]")
                # Said, unlike a pin refused for missing data: this refusal is
                # about the trip itself, and another `--pick` avoids it.
                if idx is not None and result is not None and result.solutions[idx].ticketing:
                    console.print(
                        f"  [yellow]note: Google sells #{idx + 1:d} as separate tickets, so this "
                        "link opens the search, not that trip.[/]"
                    )
        except Exception as e:  # noqa: BLE001 - third-party undocumented errors; non-fatal fallback
            console.print(f"[dim]Google Flights link: {_safe_text(e)}[/]")


# ─────────────────────────── booking options (--sellers) ───────────────────


def _sellers_blocker(  # noqa: PLR0911 — one return per reason the run is refused
    *,
    backend: str,
    multi_cabin: bool,
    awards_only: bool,
    awards_json: bool,
    pick: int | None,
    page_size: int,
    bags: Bags | None = None,
    exclude_basic: bool = False,
) -> str | None:
    """Why `--sellers` cannot run on this search, or None. Decided before any
    request, so a refusal costs the user nothing but the reading.

    The flag names one row of one numbered table, so a run that prints none —
    a multi-cabin table, the award overlay alone — has no row to open. Under
    `--format json` with awards on, the award renderer owns the document and
    there is nowhere to put the sellers."""
    if multi_cabin:
        return "opens one row of one table; drop the extra --cabin values"
    if awards_only:
        return "opens a row of the results table, and --awards-only prints none"
    if bags is not None:
        return (
            "lists fares from a booking page that is not asked for bags; drop --bags or --sellers"
        )
    if exclude_basic:
        return (
            "lists fares from a booking page that is not asked to leave out basic economy; "
            "drop --exclude-basic or --sellers"
        )
    if backend == BACKEND_MATRIX:
        return "needs a Google Flights result, and this search runs on Matrix"
    if awards_json:
        return "cannot join the award document --format json writes; add --cash-only"
    n = 1 if pick is None else pick
    if not 1 <= n <= page_size:
        return f"--pick {n:d} names no row of the {page_size:d} that -n asks for"
    return None


def _split_blocker(  # noqa: PLR0911 — one return per reason the run is refused
    *,
    multi_city: bool,
    one_way: bool,
    multi_cabin: bool,
    backend: str,
    sellers: bool,
    verify: bool,
    awards_only: bool,
    awards_format: str | None,
    open_jaw: bool = False,
    fare_rules: bool = False,
) -> str | None:
    """Why `--split` cannot run on this search, or None, as a phrase that
    completes "--split …". Decided before the backend is picked, so a refusal
    costs no request.

    The pair is priced on Google Flights beside a one-cabin round-trip table,
    and the JSON document it joins is the cash one, not the one `--sellers` or
    `--verify` writes. `awards_format` is the `--format` an award search would
    write its document into, or None when none runs.

    An `open_jaw` (two or more `--slice` that are not a round trip's,
    `_one_way_per_slice`) is priced as one one-way per slice instead, beside
    Matrix's table or alone under `--backend gflight`, and its document is
    Matrix's, which `--fare-rules` writes a document of its own around. The
    envelope records its tickets beside an award search's rows without
    `--split`, so asking for them there is no conflict."""
    if open_jaw:
        if fare_rules:
            return "cannot run beside --fare-rules; drop one of them"
    elif multi_city:
        return "prices a round trip as two one-ways, and --slice is a multi-city search"
    elif one_way:
        return "prices a round trip as two one-ways; add --return"
    if multi_cabin:
        return "prices one cabin; drop the extra --cabin values"
    if backend == BACKEND_MATRIX:
        return "needs Google Flights, and --backend matrix searches Matrix"
    if sellers:
        return "cannot run beside --sellers; drop one of them"
    if verify:
        return "cannot run beside --verify; drop one of them"
    if awards_only:
        return "prints beside the results table, and --awards-only prints none"
    if awards_format is not None and not (open_jaw and awards_format == "envelope"):
        return f"cannot join the award document --format {awards_format} writes; add --cash-only"
    return None


def _no_booking_options(heading: str, why: str) -> NoReturn:
    """Fail a `--sellers` run: the search may have answered, the sellers did not."""
    err.print(f"[red]{_safe_text(heading)}:[/] {_safe_text(why)}")
    raise typer.Exit(1)


def _pick_for_sellers(pick: int | None, rows: int) -> int:
    """The 1-based row `--sellers` opens, or exit 2. No fallback to row one: a
    block headed "#N" has to describe row N. A board with no rows is an answer
    to the search and none to `--sellers`, so it fails the command."""
    if rows == 0:
        _no_booking_options("No booking options", "the search returned no itinerary to open.")
    n = 1 if pick is None else pick
    if not 1 <= n <= rows:
        err.print(f"[red]--pick {n:d} is out of range (1-{rows:d}); --sellers opens that row.[/]")
        raise typer.Exit(2)
    return n


def _report_page_refusal(heading: str, e: GfBackendError) -> None:
    """One line for a refusal on a page only Chrome can read: the booking page
    behind `--sellers`, and `explore`. `_gf_refusal` is not used because its
    remedies are `--gf-transport http` and `--backend matrix`, and neither
    reads these pages."""
    match e:
        case GfBrowserUnavailableError():
            remedy = e.remedy.removesuffix(BROWSER_DEFAULT_REMEDY).strip() or "Retry."
            detail = f"{e.reason} {remedy}"
        case GfUpstreamStatusError():
            detail = f"Google Flights' page returned HTTP {e.status_code}"
        case _:
            detail = str(e)
    err.print(f"[red]{_safe_text(heading)}:[/] {_safe_text(detail)}")


def _booking_options(
    search: SpecificDateSearch,
    result: SearchResult,
    n: int,
    *,
    gf_price: str | None,
    headed: bool,
) -> BookingOptions:
    """Row `n`'s sellers, read off its booking page in Chrome and held to the
    search's price cap, or exit 1 with the reason on stderr.

    The URL is the pinned link's, so the row opened is the row the link pins,
    and the page is asked in the currency of `gf_price`, the row's Google price.
    This step owns its Chrome: the search before it may not have opened one."""
    from ._gf_booking import booking_options  # noqa: PLC0415 — Chrome paths only
    from ._gf_browser import interrupt_guard, session_scope  # noqa: PLC0415 — patchright

    heading = f"No booking options for #{n:d}"
    # Before the date check and the pin, which both refuse this row too, for
    # a reason that is not its own.
    if result.solutions[n - 1].ticketing is not None:
        _no_booking_options(
            heading,
            f"Google sells #{n:d} as separate tickets; --sellers reads one-ticket "
            "booking pages only.",
        )
    # A page asked for a day the row does not state answers for another trip
    # with the same flight numbers, which the seller check cannot tell apart
    # from this one. `_pin_segments` refuses such a row too; checking first
    # lets the refusal name the reason and its remedy.
    itinerary = result.solutions[n - 1].itinerary
    if itinerary is not None and not all(pin_dates_are_stated(s) for s in itinerary.slices):
        _no_booking_options(
            heading,
            "Matrix gives no date for each flight of this connection. With --fast, rows "
            "come from Google and carry each flight's date.",
        )
    segments = _pin_segments(result, n - 1)
    if segments is None:
        _no_booking_options(
            heading, "its flights cannot be written into a Google Flights booking link."
        )
    outbound, returning = segments
    url = google_flights_booking_url(
        search,
        outbound_segments=outbound,
        return_segments=returning,
        currency=_split_price(gf_price)[0] or None,
    )
    flights = [(seg["carrier"], seg["flight"]) for seg in (*outbound, *(returning or []))]
    try:
        with interrupt_guard(), session_scope():
            options = booking_options(url, flights=flights, headed=headed)
    except GfBackendError as e:
        _report_page_refusal(heading, e)
        raise typer.Exit(1) from e
    return _offers_under_cap(options, search.options, heading)


def _offers_under_cap(options: BookingOptions, opts: SearchOptions, heading: str) -> BookingOptions:
    """`options` holding only the offers the search's price cap admits, by the
    rule every row is held to, or `options` itself when there is no cap. None
    left fails the command, as no seller at all does: an empty list would read
    as nobody selling the row."""
    cap = opts.max_price
    if cap is None:
        return options
    currency = opts.currency or "USD"
    kept = tuple(
        s
        for s in options.sellers
        if within_price_cap(s.price, options.currency, cap=cap, cap_currency=currency)
    )
    if not kept:
        _no_booking_options(heading, f"no booking offer is at or under {currency} {cap:d}.")
    return options._replace(sellers=kept)


def _search_and_sellers(
    search_doc: list[Any], search: SpecificDateSearch, result: SearchResult, n: int, *, headed: bool
) -> dict[str, Any]:
    """The `--sellers --format json` document: the search's own document
    unchanged, beside row `n`'s booking options. `result` is the Google board
    the document lists."""
    from ._gf_booking import document  # noqa: PLC0415 — Chrome paths only

    options = _booking_options(
        search, result, n, gf_price=result.solutions[n - 1].price, headed=headed
    )
    return {"search": search_doc, "booking_options": document(options)}


def _print_booking_options(
    search: SpecificDateSearch,
    result: SearchResult,
    n: int,
    *,
    gf_price: str | None,
    matrix_price: str | None = None,
    headed: bool,
) -> None:
    """Row `n`'s booking options under the table it was numbered in, set
    against the prices that row shows: its Google price, and on the merged
    table its Matrix price."""
    options = _booking_options(search, result, n, gf_price=gf_price, headed=headed)
    _render_booking_options(
        options, n=n, table_prices=[gf_price, matrix_price], round_trip=len(search.legs) > 1
    )


def _undercut(options: BookingOptions, table_prices: list[str | None]) -> float | None:
    """The table price the cheapest seller beats, or None.

    A seller beats the table only by beating every price the row shows, so one
    in another currency than the sellers', or one that does not parse or is not
    finite (`nan`, `inf`), leaves nothing to claim. A seller price is whole
    units, so `d` stands for anything below `d + 0.5`: it beats a table price
    only when that whole range sits under it."""
    amounts: list[float] = []
    for price in table_prices:
        if not price:
            continue
        currency, amount = _split_price(price)
        if currency != options.currency:
            return None
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        amounts.append(value)
    cheapest = options.sellers[0].price
    if not amounts or cheapest is None or cheapest + 0.5 > min(amounts):
        return None
    return min(amounts)


def _render_booking_options(
    options: BookingOptions, *, n: int, table_prices: list[str | None], round_trip: bool
) -> None:
    """The "Booking options for #N" block: every seller, cheapest first, then
    a line per seller beside its number with the bag fees it states and its
    booking link."""
    t = Table(title=f"Booking options for #{n:d}", show_header=True, header_style="bold green")
    # On a narrow console Rich shrinks only the columns that may wrap, so the
    # numbers keep their width and the names fold rather than end in "…".
    t.add_column("#", justify="right", no_wrap=True)
    t.add_column("seller", overflow="fold")
    t.add_column("price", justify="right", no_wrap=True)
    t.add_column("fare", overflow="fold")
    for i, s in enumerate(options.sellers, 1):
        t.add_row(
            f"{i:d}",
            _safe_text(s.name),
            "—" if s.price is None else f"{_safe_text(options.currency)}{s.price:.2f}",
            _safe_text(s.fare or ""),
        )
    console.print(t)
    whole_trip = round_trip and any(s.bags for s in options.sellers)
    if any(s.link for s in options.sellers):
        console.print(
            "[dim]Each link goes through Google to that seller's own page for this fare"
            + ("; bag fees cover the whole trip.[/]" if whole_trip else ".[/]")
        )
    elif whole_trip:
        console.print("[dim]Bag fees cover the whole trip.[/]")
    for i, s in enumerate(options.sellers, 1):
        fees = {(b.bag, b.nth): b.fee for b in s.bags}
        bags = ", ".join(
            f"{label} free" if fee == 0 else f"{label} {options.currency}{fee:.2f}"
            for label, fee in (
                ("carry-on", fees.get(("carry-on", 1))),
                ("1st checked", fees.get(("checked", 1))),
                ("2nd checked", fees.get(("checked", 2))),
            )
            if fee is not None
        )
        if not (bags or s.link):
            continue
        # Off the table, so no fee or link is ever cut to fit a narrow console:
        # folded or cropped, a multi-KB link no longer opens when copied. The
        # name prints as its table cell does. Unhighlighted, so a URL inside a
        # name is not styled as if this program marked it.
        console.print(
            f"{i:d} {_safe_text(s.name)}" + (f": {_safe_text(bags)}" if bags else ""),
            end=" " if s.link else "\n",
            soft_wrap=True,
            highlight=False,
        )
        if s.link:
            console.print(_safe_text(s.link), soft_wrap=True, highlight=False)
    table = _undercut(options, table_prices)
    cheapest = options.sellers[0]
    if table is not None and cheapest.price is not None:
        console.print(
            f"[green]{_safe_text(cheapest.name)} at "
            f"{_safe_text(options.currency)}{cheapest.price:.2f} beats the table price, "
            f"{_safe_text(options.currency)}{table:.2f}.[/]"
        )


# ─────────────────────────── split tickets (--split) ───────────────────────


class _SplitTicket(NamedTuple):
    """The cheapest pair of priced one-ways of a round trip whose return
    leaves the airport its outbound lands at, after it lands: the Google rows,
    their fares and the one currency both are priced in."""

    outbound: Any
    outbound_price: float
    back: Any
    back_price: float
    currency: str

    @property
    def total(self) -> float:
        return round(self.outbound_price + self.back_price, 2)


# Reached only when the weave stopped before the one-way searches returned.
_SPLIT_UNFINISHED = "the one-way searches did not finish"


class _OneWaysUnpriced(str):
    """Why one-way tickets asked for are unpriced: a board failed, or a stop
    left later slices unasked. Every other reason `_one_way_boards` gives is
    the boards' own answer."""

    __slots__ = ()


class _NoInfantRows(str):
    """Why a leg's one-way board for a party with an infant holds no row:
    Google has served such a board on routes with flights, so it answers
    nothing about the leg."""

    __slots__ = ()


def _one_way_boards(
    legs: Sequence[tuple[Leg, str]],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    *,
    narrow: bool = False,
) -> list[list[Any]] | str:
    """Each leg of `legs` asked alone on Google Flights, as its priced rows sold
    on one ticket, in price order; or the plain-text reason a leg has none,
    naming it by the label beside it. A failed search is a reason, not a raise:
    the trip's own answer has been or will be shown regardless, and on the
    enriched path this runs inside the weave, where a raise would cancel Matrix.

    Asked without the price cap: it bounds the whole trip's fare, and held to
    each one-way it would admit tickets costing up to twice the cap together.
    One Chrome serves every leg on the browser rung.

    A board missing a page, or rows its pages served that could not be read,
    narrows the envelope run, the unread rows in a note naming the leg: the rows
    it lacks may be the leg's cheapest tickets. `narrow` is the open jaw's: its
    unpriced tickets narrow whoever answers the search.

    A leg whose pages met a stop (`_search_stop`) ends the asking, as a page's
    does in `_PageAsk`: every later leg would meet the same wall, so each is
    named as not asked instead. A failed or stopped leg's reason is an
    `_OneWaysUnpriced`. A leg Google served no row at all for a party with an
    infant is an `_NoInfantRows`, which narrows the run as `_run_gflight_path`
    does for the same board: whoever answers with `narrow`, Google's answer alone
    on a round trip, as `unpriced` narrows.

    A board at Google's row cap says so (`_note_row_cap`) once every leg
    answered, since the tickets drawn from it stop at its cap, or when it holds
    no ticket, since one priced above its cap may be what it lacks. The rows it
    served over the stop ceiling are counted beside that line
    (`_note_stop_drops`), as on every other board shown."""
    from ._gf_browser import interrupt_guard  # noqa: PLC0415 — GF-only

    def unpriced(reason: str) -> _OneWaysUnpriced:
        # The tickets were asked for and are unpriced, where every other
        # reason is the boards' answer. A round trip's pair is priced
        # beside Google's answer; a multi-city trip's tickets (`narrow`)
        # beside Matrix's, or alone, so they name no backend.
        _envelope.narrow(of=None if narrow else "gflight")
        return _OneWaysUnpriced(reason)

    infant = bool(opts.pax.infants_in_seat or opts.pax.infants_in_lap)
    one_way = opts.model_copy(update={"max_price": None})
    requested = opts.currency or "USD"
    boards: list[list[Any]] = []
    read: list[tuple[str, Any]] = []
    with interrupt_guard(), _browser_scope(gf_mode):
        for i, (leg, which) in enumerate(legs):
            rest = [later for _, later in legs[i + 1 :]]
            try:
                board = _gflight_results((leg,), one_way, top_n, gf_mode, gf_headed)
                if getattr(board, "partial", False):
                    _envelope.narrow()
                if unread := cast("int", getattr(board, "unread", 0)):
                    _envelope.narrow(
                        f"Google Flights {which} one-way: {unread:d} rows its pages served "
                        "could not be read and are left out of the answer"
                    )
                if rest and (stop := _search_stop(board)) is not None:
                    return unpriced(
                        f"{_one_ways_not_asked(rest)} after the {which} one-way stopped the "
                        f"search ({str(stop) or type(stop).__name__})"
                    )
                priced = [
                    r
                    for r in _price_ordered(board, currency=requested)
                    if r.flight.price is not None
                ]
                # A one-way sold as separate tickets is already more than one
                # booking, so tickets holding it would not be one booking each.
                single = [r for r in priced if not _separately_ticketed(r)]
                if not single:
                    _note_stop_drops(board, one_way=which)
                    _note_row_cap(board, requested, one_way=which)
                    if infant and not (board or getattr(board, "dropped", 0)):
                        _envelope.narrow(of=None if narrow else "gflight")
                        return _NoInfantRows(
                            "Google Flights served no rows for a party with an infant on the "
                            f"{which} one-way, as it has on routes with flights"
                        )
                    ticket = " on one ticket" if priced else ""
                    return f"Google Flights priced no {which} one-way{ticket}"
                boards.append(single)
                read.append((which, board))
            except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
                raise
            except Exception as e:  # noqa: BLE001 — see the docstring
                failed = f"the {which} one-way failed ({str(e) or type(e).__name__})"
                if rest and isinstance(e, _GF_STOPS):
                    failed += f", and {_one_ways_not_asked(rest)} after it stopped the search"
                return unpriced(failed)
    for which, board in read:
        _note_stop_drops(board, one_way=which)
        _note_row_cap(board, requested, one_way=which)
    return boards


def _one_ways_not_asked(which: Sequence[str]) -> str:
    """The one-ways labeled `which`, said as not asked. Plain text."""
    asked = "one-way was" if len(which) == 1 else "one-ways were"
    return f"the {_join_reasons(list(which))} {asked} not asked"


def _split_ticket(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    *,
    stopped: GfBackendError | None = None,
) -> _SplitTicket | str:
    """The `--split` pair for the round trip `legs`, or the plain-text reason
    there is none, from each leg's one-way board (`_one_way_boards`).
    `stopped` is the stop the round trip's own search met (`_search_stop`),
    after which neither one-way is asked."""
    if stopped is not None:
        # The pair was asked for and is unpriced, as a failed one-way leaves it.
        _envelope.narrow(of="gflight")
        return (
            f"{_one_ways_not_asked(('outbound', 'return'))} after the round trip stopped the "
            f"search ({str(stopped) or type(stopped).__name__})"
        )
    requested = opts.currency or "USD"
    boards = _one_way_boards(
        ((legs[0], "outbound"), (legs[1], "return")), opts, top_n, gf_mode, gf_headed
    )
    if isinstance(boards, str):
        return boards
    pair = _cheapest_flown_pair(*boards)
    if pair is None:
        return (
            "Google Flights priced no return one-way that leaves the airport an outbound "
            "one-way lands at, after it lands"
        )
    out, back = pair
    out_ccy = out.flight.currency or requested
    back_ccy = back.flight.currency or requested
    if out_ccy != back_ccy:
        return (
            f"Google Flights priced the outbound one-way in {out_ccy} and the return in {back_ccy}"
        )
    return _SplitTicket(out, float(out.flight.price), back, float(back.flight.price), out_ccy)


def _cheapest_flown_pair(outbounds: list[Any], backs: list[Any]) -> tuple[Any, Any] | None:
    """The cheapest outbound and return whose return leaves the airport the
    outbound lands at, after it lands, from two priced boards in price order; a
    tie keeps the earlier row.

    Each one-way is asked alone, so nothing orders the two: the cheapest each
    way can be a return that leaves before the outbound lands, or from another
    of the leg's airports, which no one can fly on these two tickets alone. At
    one airport both times are its local clock, so they compare as they are."""
    best: tuple[Any, Any] | None = None
    best_total = 0.0
    for out in outbounds:
        if best is not None and out.flight.price + backs[0].flight.price >= best_total:
            break
        last = out.flight.legs[-1]
        back = next(
            (
                b
                for b in backs
                if b.flight.legs[0].departure_airport == last.arrival_airport
                and b.flight.legs[0].departure_datetime > last.arrival_datetime
            ),
            None,
        )
        if back is not None and (best is None or out.flight.price + back.flight.price < best_total):
            best, best_total = (out, back), out.flight.price + back.flight.price
    return best


def _flight_numbers(row: Any) -> str:
    """A Google row's flights as one token, carrier and number per leg:
    `B6188+B6917`. Plain text, for the caller to wrap."""
    return "+".join(
        f"{str(getattr(leg.airline, 'name', '')).removeprefix('_')}{leg.flight_number}"
        for leg in row.flight.legs
    )


def _report_no_split(reason: str) -> None:
    err.print(f"[yellow]No split tickets: {_safe_text(reason)}.[/]")


def _print_split_ticket(ticket: _SplitTicket | str) -> None:
    """The one `--split` line under a round-trip table, or on stderr why there
    is none. Unwrapped, so a terminal or a `grep` sees it as one line.

    Amounts in the price column's own `.2f`, so the total reads against the
    round-trip fares above it."""
    if isinstance(ticket, str):
        _report_no_split(ticket)
        return
    console.print(
        f"Two one-way tickets: {_safe_text(ticket.currency)}{ticket.outbound_price:.2f} "
        f"({_safe_text(_flight_numbers(ticket.outbound))}) out + "
        f"{_safe_text(ticket.currency)}{ticket.back_price:.2f} "
        f"({_safe_text(_flight_numbers(ticket.back))}) back = "
        f"{_safe_text(ticket.currency)}{ticket.total:.2f}, booked as two separate tickets.",
        soft_wrap=True,
    )


def _split_ticket_object(ticket: _SplitTicket | str, bags: Bags | None) -> dict[str, Any]:
    """The `split_ticket` object: the pair or, also said on stderr, why there
    is none."""
    if isinstance(ticket, str):
        _report_no_split(ticket)
        return {"error": ticket}
    total = ticket.total
    return {
        "outbound": _gflight_json_row(ticket.outbound, bags),
        "return": _gflight_json_row(ticket.back, bags),
        "total": int(total) if total.is_integer() else total,
        "currency": ticket.currency,
    }


def _with_split_ticket(
    search_doc: list[Any], ticket: _SplitTicket | str, bags: Bags | None
) -> dict[str, Any]:
    """The `--split --format json` document: the search's own document
    unchanged, beside the `split_ticket` object."""
    return {"search": search_doc, "split_ticket": _split_ticket_object(ticket, bags)}


def _record_split_ticket(ticket: _SplitTicket | str, bags: Bags | None) -> None:
    """Hand the `split_ticket` object to the envelope run."""
    obj = _split_ticket_object(ticket, bags)
    _envelope.record_split_ticket(json.loads(json.dumps(obj, default=str)))


# ───────────────────── multi-city on separate tickets ──────────────────────


class _OpenJaw(NamedTuple):
    """A multi-city trip's answer on Google Flights: the cheapest combinations
    of one one-way ticket per slice, the currency each total is summed in, and
    how many one-way rows were priced in another, which no total adds."""

    combinations: list[Combination]
    currency: str
    other_currency: int


def _one_way_per_slice(legs: Sequence[Leg]) -> bool:
    """Two or more slices that are not a round trip's (`links.is_inverse_pair`):
    an open jaw or a longer multi-city trip, which Google Flights sells as one
    one-way ticket per slice."""
    if len(legs) == _ROUND_TRIP_LEGS:
        return not is_inverse_pair(legs[0], legs[1])
    return len(legs) > _ROUND_TRIP_LEGS


def _slice_route(leg: Leg) -> str:
    """A slice as `JFK→LHR`, its airport sets comma-joined. Plain text."""
    return f"{','.join(leg.origins)}→{','.join(leg.destinations)}"


def _open_jaw_blocker(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    no_separate_tickets: bool,
    top_codes: Sequence[tuple[str, str | None]],
    cabins: int = 1,
) -> str | None:
    """Why a multi-city trip's one-ways (`_one_way_per_slice`) are not asked of
    Google Flights, as a plain-text phrase, or None when they are: an opt-out,
    several `cabins` (a combination's tickets are priced in one), a top-level
    time window, which applies to no slice, or a slice the search page can't serve as
    a one-way (`_google_reasons`, asked of the slice alone)."""
    if no_separate_tickets:
        return "--no-separate-tickets was given"
    if cabins > 1:
        return f"each is priced in one cabin, and --cabin asks for {cabins:d}"
    flags = [flag for flag, value in top_codes if value]
    if flags:
        verb, them = ("reaches", "it") if len(flags) == 1 else ("reach", "them")
        return (
            f"{_join_reasons(flags)} {verb} no --slice, so the one-ways could not be held to {them}"
        )
    p = opts.pax
    for i, leg in enumerate(legs, 1):
        reasons = _google_reasons(
            backend=BACKEND_AUTO,
            routing=leg.route_language,
            extension=leg.extension,
            slice_specs=None,
            depart_times=None,
            return_times=None,
            stops=opts.max_extra_stops,
            children=p.children,
            seniors=p.seniors,
            youth=p.youth,
            inf_seat=p.infants_in_seat,
            inf_lap=p.infants_in_lap,
            origin=",".join(leg.origins),
            destination=",".join(leg.destinations),
            allow_airport_changes=opts.allow_airport_changes,
            show_only_available=opts.show_only_available,
            adults=p.adults,
            return_codes=None,
            cabins=(opts.cabin,),
            flex=(leg.date_minus, leg.date_plus),
            arrive=leg.is_arrival_date,
        )
        if reasons:
            return (
                f"Google Flights can't serve slice {i:d} ({_slice_route(leg)}) as a one-way: "
                f"{_join_reasons(reasons)}"
            )
    return None


def _open_jaw_tickets(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
) -> _OpenJaw | str:
    """The `top_n` cheapest combinations of one one-way per slice of the
    multi-city trip `legs` (`_open_jaw.combine`), or the plain-text reason
    there is none, typed as `_one_way_boards` types it.

    A price cap holds each combination's total, as it would a trip's fare."""
    from ._gflight_ids import search_escalation  # noqa: PLC0415 — fli, ~95 ms
    from ._open_jaw import combine  # noqa: PLC0415 — only a multi-city trip combines

    currency = opts.currency or "USD"
    # The one-ways are one search to `auto`: a throttle on the first moves the
    # rest to Chrome too, rather than each to a ladder of its own on one IP.
    with search_escalation():
        boards = _one_way_boards(
            tuple((leg, _slice_route(leg)) for leg in legs),
            opts,
            top_n,
            gf_mode,
            gf_headed,
            narrow=True,
        )
    if isinstance(boards, str):
        return boards
    other = sum((r.flight.currency or currency) != currency for board in boards for r in board)
    combos = combine(*boards, currency=currency, limit=top_n, cap=opts.max_price)
    if combos:
        return _OpenJaw(combos, currency, other)
    for leg, board in zip(legs, boards, strict=True):
        if all((r.flight.currency or currency) != currency for r in board):
            return f"Google Flights priced no {_slice_route(leg)} one-way in {currency}"
    pair = len(legs) == _ROUND_TRIP_LEGS
    if opts.max_price is not None and combine(*boards, currency=currency, limit=1):
        return f"no {'pair' if pair else 'combination'} at or under {currency} {opts.max_price:d}"
    if pair:
        return (
            "no pair where the second ticket leaves after the first lands "
            "(from another airport, on a later day)"
        )
    return (
        "no combination where each ticket leaves after the one before it lands "
        "(from another airport, on a later day)"
    )


def _one_way_link(leg: Leg, row: Any, opts: SearchOptions) -> tuple[str, bool] | None:
    """A Google Flights link to the one-way `row` of slice `leg`, and whether it
    pins that row; its search's prefill where the row can't be pinned, None
    where no link can be built."""
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    search = SpecificDateSearch(legs=(leg,), options=opts.model_copy(update={"max_price": None}))
    try:
        pinned = _try_pinned_gflight_url(search, fli_results_to_search_result([row]), 0)
        return (pinned, True) if pinned is not None else (google_flights_url(search), False)
    except Exception:  # noqa: BLE001 — fast_flights documents no exception surface
        return None


def _report_no_open_jaw(reason: str) -> None:
    err.print(f"[yellow]No separate tickets on Google Flights: {_safe_text(reason)}.[/]")


def _note_open_jaw_currencies(answer: _OpenJaw) -> None:
    if answer.other_currency:
        err.print(
            f"[yellow]Google Flights priced {answer.other_currency:d} one-way "
            + ("row" if answer.other_currency == 1 else "rows")
            + f" in another currency than {_safe_text(answer.currency)}; no total adds "
            + ("it" if answer.other_currency == 1 else "them")
            + ".[/]"
        )


def _render_open_jaw(
    answer: _OpenJaw, legs: tuple[Leg, ...], opts: SearchOptions, *, google_url: bool
) -> None:
    """A multi-city trip's combinations, a row per ticket under its numbered total,
    then the key to the total's `†` and, under `google_url`, a link to each
    ticket of the cheapest.

    Every amount carries its currency, as the Google table's does: each total
    is the sum of the tickets printed under it."""
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    passengers = opts.pax.total
    t = Table(
        title="Separate tickets on Google Flights · "
        + f"{_safe_text(' + '.join(map(_slice_route, legs)))} ({_safe_text(answer.currency)}"
        + (f", {passengers:d} travelers)" if passengers > 1 else ")"),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    # Never wrapped, so the mark stays on its amount's line.
    t.add_column("total", justify="right", no_wrap=True)
    t.add_column("ticket")
    t.add_column("price", justify="right", no_wrap=True)
    for i, combo in enumerate(answer.combinations, 1):
        for j, row in enumerate(combo.tickets):
            itn = fli_results_to_search_result([row]).solutions[0].itinerary
            ticket = _fmt_slice_cell(itn.slices[0]) if itn and itn.slices else "?"
            t.add_row(
                f"{i:d}" if j == 0 else "",
                f"{_safe_text(combo.currency)}{combo.total:.2f} †" if j == 0 else "",
                ticket,
                f"{_safe_text(combo.currency)}{row.flight.price:.2f}",
            )
    console.print(t)
    console.print(
        "[dim]† separate tickets: one one-way ticket per slice, each bought on its own; "
        "a missed flight on one is not protected on the next.[/]"
    )
    if not google_url:
        return
    for j, (leg, row) in enumerate(zip(legs, answer.combinations[0].tickets, strict=True), 1):
        link = _one_way_link(leg, row, opts)
        console.print()
        if link is None:
            console.print(f"[dim]Google Flights (#1, ticket {j:d}): no link could be built.[/]")
            continue
        url, pinned = link
        console.print(
            f"[dim]Google Flights (#1, ticket {j:d} "
            + ("pinned" if pinned else "tfs= structured")
            + "):[/]"
        )
        console.print(f"  [link]{_safe_text(url)}[/]")


def _open_jaw_object(
    answer: _OpenJaw | str, legs: tuple[Leg, ...], opts: SearchOptions
) -> dict[str, Any]:
    """The `split_ticket` object of a multi-city trip: its combinations, each
    ticket a Google row with a link to it, or why there are none."""
    if isinstance(answer, str):
        return {"error": answer}
    return {
        "currency": answer.currency,
        "combinations": [
            {
                "separate_tickets": True,
                "total": int(c.total) if c.total.is_integer() else c.total,
                "currency": c.currency,
                "tickets": [
                    {
                        **_gflight_json_row(row),
                        "google_flights_url": (
                            link[0] if (link := _one_way_link(leg, row, opts)) else None
                        ),
                    }
                    for leg, row in zip(legs, c.tickets, strict=True)
                ],
            }
            for c in answer.combinations
        ],
    }


def _answer_open_jaw(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    blocker: str | None,
    output: str,
    split: bool,
    google_url: bool,
    awards: bool = False,
) -> dict[str, Any] | None:
    """Google Flights' separate-ticket answer to the multi-city trip `legs`
    (`_one_way_per_slice`), shown ahead of Matrix's one-ticket answer: the
    table, or in a document the `split_ticket` object, which is returned for
    Matrix's document to carry.
    `blocker` is why nothing is asked (`_open_jaw_blocker`), said on stderr.
    A failed or empty board is said on stderr and leaves Matrix to answer.

    The envelope asks what the table asks, so the two hold the same fares.
    `--format json` asks nothing without `split`: its document is Matrix's own
    body, which has no place for the tickets. With `awards` on, it is the
    award document, which `--split` joins only beside `--cash-only`."""
    asked = split or output != "json"
    if blocker is None and not asked:
        blocker = "--format json carries them only with --split" + (
            " and --cash-only" if awards else ""
        )
    answer: _OpenJaw | str
    if blocker is not None:
        err.print(f"[dim]No separate tickets on Google Flights: {_safe_text(blocker)}.[/]")
        if split:
            # Asked for and not priced, as a round trip Matrix answers is; a
            # failed board narrows where it fails, and an empty one is an answer.
            _envelope.narrow()
        answer = blocker
    else:
        answer = _open_jaw_tickets(legs, opts, top_n, gf_mode, gf_headed)
        if isinstance(answer, str):
            _report_no_open_jaw(answer)
        else:
            _note_open_jaw_currencies(answer)
    if not asked:
        return None
    if output != "table":
        obj = json.loads(json.dumps(_open_jaw_object(answer, legs, opts), default=str))
        _envelope.record_split_ticket(obj)
        return obj
    if not isinstance(answer, str):
        _render_open_jaw(answer, legs, opts, google_url=google_url)
    return None


_SEPARATE_ALONE = (
    "--backend gflight answers a multi-city search with Google Flights' separate tickets alone"
)


def _gflight_multi_city_blocker(
    *,
    blocker: str | None,
    awards_only: bool,
    awards_json: bool,
    sellers: bool,
    enrich: bool,
    bags: Bags | None,
    exclude_basic: bool,
    arrive_times: str | None,
    return_arrive_times: str | None,
    pick: int | None,
) -> str | None:
    """Why `--backend gflight` cannot answer a multi-city trip
    (`_one_way_per_slice`), as a plain-text sentence ending in its remedy, or
    None. Read off the flags alone, so a refusal costs no request.

    Google Flights answers the trip with one one-way per slice and nothing
    else (`_answer_multi_city_on_google`), so a flag that acts on a row on one
    ticket has none to act on. `blocker` is why no one-way would be asked
    (`_open_jaw_blocker`), which leaves nothing to answer with."""
    if blocker is not None:
        return f"{_SEPARATE_ALONE}, and none is asked: {blocker}. Drop --backend gflight"
    if awards_only:
        # Ahead of the award document's refusal, whose --cash-only it excludes;
        # dropping --awards-only alone would meet that refusal next.
        remedy = (
            "Drop --backend gflight, or drop --awards-only and add --cash-only"
            if awards_json
            else "Drop either"
        )
        return f"--awards-only prints award space alone, and {_SEPARATE_ALONE}. {remedy}"
    if awards_json:
        return (
            f"{_SEPARATE_ALONE}, and an award search writes its own --format json document. "
            "Add --cash-only"
        )
    for flag, given, acts, remedy in (
        ("--sellers", sellers, "opens the booking page of a row on one ticket", "Drop it"),
        ("--enrich", enrich, "checks rows on one ticket against Matrix", "Drop it"),
        ("--bags", bags is not None, "prices bags on rows on one ticket", "Drop it"),
        (
            "--exclude-basic",
            exclude_basic,
            "asks for rows on one ticket without basic economy",
            "Drop it",
        ),
        ("--arrive-times", bool(arrive_times), "holds rows on one ticket to a window", "Drop it"),
        (
            "--return-arrive-times",
            bool(return_arrive_times),
            "holds rows on one ticket to a window",
            "Drop it",
        ),
        (
            "--pick",
            pick is not None,
            "names a row on one ticket",
            "Drop it, or drop --backend gflight",
        ),
    ):
        if given:
            return f"{flag} {acts}, and {_SEPARATE_ALONE}. {remedy}"
    return None


def _answer_multi_city_on_google(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    output: str,
    google_url: bool,
    awards: bool,
) -> None:
    """`--backend gflight` on a multi-city trip (`_one_way_per_slice`):
    Google Flights' separate tickets alone, as `_answer_open_jaw` shows them
    beside Matrix's answer, and Matrix asked nothing. The document is
    `{"search": [], "split_ticket": …}`, `--split` or not, since the tickets
    are the whole answer. `awards` is whether an award search would run: it
    matches rows on one ticket, and there are none.

    Exits 1 when a board failed or stopped the search (`_OneWaysUnpriced`),
    with a table's or JSON's stdout empty; an empty board, or no flyable
    combination, is an answer."""
    if awards:
        no_awards = "awards are matched to rows on one ticket, and none is asked"
        err.print(f"[dim]No award search: {_safe_text(no_awards)}.[/]")
        _envelope.explain("awards", no_awards)
    answer = _open_jaw_tickets(legs, opts, top_n, gf_mode, gf_headed)
    if isinstance(answer, _NoInfantRows):
        _report_no_open_jaw(f"{answer}. For Matrix's answer, drop --backend gflight")
    elif isinstance(answer, str):
        _report_no_open_jaw(answer)
    else:
        _note_open_jaw_currencies(answer)
    failed = isinstance(answer, _OneWaysUnpriced)
    if not failed:
        _envelope.record_search(backend="gflight", cabin=opts.cabin.value, rows=[])
        _envelope.explain(
            "results",
            "Google Flights is asked no multi-city itinerary on one ticket; its separate "
            "tickets are in split_ticket",
        )
    if output == "table":
        if not isinstance(answer, str):
            _render_open_jaw(answer, legs, opts, google_url=google_url)
    else:
        obj = json.loads(json.dumps(_open_jaw_object(answer, legs, opts), default=str))
        _envelope.record_split_ticket(obj)
        if not failed and not _envelope.active():
            sys.stdout.write(json.dumps({"search": [], "split_ticket": obj}, indent=2))
    if failed:
        raise typer.Exit(1)


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

    A metro code counts as its member airports: fast_flights writes the code
    itself as the airport, which is not the set the search covered.
    """
    legs: tuple[Leg, ...] = getattr(search, "legs", ()) or ()
    notes: list[str] = []
    for lg in legs:
        origins, destinations = expand_airports(lg.origins), expand_airports(lg.destinations)
        if len(origins) > 1 or len(destinations) > 1:
            notes.append(
                f"multi-airport search narrowed to {lg.origins[0]}→{lg.destinations[0]} "
                "(Google's link format takes one airport code per end; the search covered "
                f"{','.join(origins)}→{','.join(destinations)})"
            )
            break
    if any(lg.route_language or lg.extension for lg in legs):
        notes.append("routing/extension codes are not expressible in a Google link")
    if moved := _flexed_or_arrival_dates(legs):
        notes.append(
            f"the link searches {_join_reasons(moved)} as departure "
            f"{'date' if len(moved) == 1 else 'dates'} only: Google's link takes no flexible "
            "or arrival date"
        )
    return notes + _google_option_link_caveats(search)


def _flexed_or_arrival_dates(legs: Sequence[Leg]) -> list[str]:
    """The typed date of each leg with a flexible or arrival date, which only
    Matrix searches as asked."""
    return [
        lg.date.isoformat()
        for lg in legs
        if lg.date is not None and (lg.date_minus or lg.date_plus or lg.is_arrival_date)
    ]


def _pinned_gflight_url_caveats(search: Search) -> list[str]:
    """The pinned Google link's narrowings: a leg whose airport sets Google's
    page can't take is pinned with the itinerary's own airports instead, and
    the link asks for no bags and leaves basic economy in."""
    legs: tuple[Leg, ...] = getattr(search, "legs", ()) or ()
    reason = next(
        (r for lg in legs if (r := gf_leg_refusal(lg.origins, lg.destinations)) is not None), None
    )
    notes = (
        []
        if reason is None
        else [
            "the link shows the pinned itinerary's own airports, not every airport searched: "
            f"Google's page can't take {reason}"
        ]
    )
    return notes + _google_option_link_caveats(search)


def _google_option_link_caveats(search: Search) -> list[str]:
    """A Google link carries no bag count and no basic-economy exclusion, so
    under `--bags` its prices leave the bags out, and under `--exclude-basic`
    it may list basic fares."""
    notes: list[str] = []
    if search.options.bags is not None:
        notes.append("the linked page's prices do not include the bags --bags asked for")
    if search.options.exclude_basic:
        notes.append("the linked page is not asked to leave out basic economy")
    return notes


def _matrix_link_caveats(search: Search) -> list[str]:
    notes: list[str] = []
    if search.options.bags is not None:
        notes.append(
            "Matrix prices no bags, so the linked page's prices leave out those --bags asked for"
        )
    if search.options.exclude_basic:
        notes.append(
            "Matrix is not asked to leave out basic economy, so the linked page may list it"
        )
    # An arrival-date leg's `time_ranges` are its arrival times, which the page
    # takes as times-of-day too.
    for kind, arrival_date in (("a departure", False), ("an arrival", True)):
        left_out = [
            window_label(w)
            for lg in search.legs
            if lg.is_arrival_date == arrival_date
            for w in (
                *(t for t in lg.time_ranges if isinstance(t, ClockWindow)),
                *lg.arrival_ranges,
            )
        ]
        if left_out:
            notes.append(
                f"Matrix's page takes only times-of-day for {kind}, so the link leaves out "
                + ", ".join(left_out)
            )
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


def _slice_headers(count: int) -> list[str]:
    """Column headers for `count` slices. A multi-city slice 2 is not a return,
    so the round-trip names are kept only for tables of two slices or fewer."""
    if count <= _ROUND_TRIP_LEGS:
        return ["outbound", "return"]
    return [f"slice {n:d}" for n in range(1, count + 1)]


def _keep_cells_whole(table: Table, count: int) -> None:
    """Rich fits a table wider than the console by narrowing its columns and
    cutting with `…` what no longer fits, which on a table of many slices can
    drop the flight number or price digits two rows differ by. There every cell
    folds onto more lines instead. No column is held to one line: Rich narrows
    the widest columns first, so a price stays on one line until the slice
    columns are as narrow as it is, while a held column takes the width the
    slice columns need and can leave them none. A column narrowed to its two
    padding cells prints nothing, so the padding goes once the console cannot
    give each column a border, its padding and one character."""
    if count <= _ROUND_TRIP_LEGS:
        return
    if console.width < 4 * len(table.columns) + 1:
        table.padding = (0, 0)
    for column in table.columns:
        column.overflow = "fold"


def _slice_cells(slcs: list[Slice], count: int) -> list[str]:
    """One cell per slice column, `—` where the itinerary has fewer slices."""
    return [_fmt_slice_cell(slcs[i]) if i < len(slcs) else "—" for i in range(count)]


def _seated_pax(p: Pax) -> int:
    """Occupants needing their own seat.

    An infant IN SEAT buys a seat, so it counts; only a LAP infant does not.
    Omitting it made the award query ask for fewer seats than the cash query
    on the same run, so an award with too little availability rendered as
    bookable for the party.
    """
    return p.adults + p.children + p.seniors + p.youth + p.infants_in_seat


_DEFAULT_RENDER_LIMIT = 10  # matches the `-n/--page-size` default


def _render_search(
    res: SearchResult,
    limit: int = _DEFAULT_RENDER_LIMIT,
    cap: str | None = None,
    *,
    passengers: int = 1,
) -> None:
    """Render the itinerary table, showing at most `limit` rows. `cap` names a
    price cap `res` was cut to, for the sentence an empty answer prints.

    For a party of `passengers`, each row prints `party_price`, the total Google
    prints and a cap reads; a row Matrix states no total for prints one
    passenger's price marked as such. The cheapest line and the grid are
    Matrix's per-passenger minima, which have no party total, so they say so.

    `limit` MUST be the same bound `--pick` is validated against. It was
    hardcoded to 10 while `--pick` checked against `len(res.solutions)`, so
    `-n 15 --pick 15` printed 10 rows and then emitted a booking link labelled
    "itinerary #15 pinned" for a row the user never saw — with no out-of-range
    warning, because 15 was in range for the unrendered list.
    """
    if cap is not None and not res.solutions:
        console.print(f"[yellow]No solutions at or under {_safe_text(cap)}.[/]")
        return
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
    party = passengers > 1
    console.print(
        f"[bold]{res.solution_count} solutions[/]  · "
        + ("cheapest per traveler" if party else "cheapest")
        + f": [bold cyan]{_safe_text(cheapest or '—')}{ccy_tag}[/]"
    )

    cm = res.carrier_stop_matrix
    if cm and cm.columns and cm.rows:
        t = Table(
            title=f"Carrier x stops grid{ccy_tag}" + (" per traveler" if party else ""),
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
                p = _amount(c.min_price, ccy)
                mark = "★" if c.min_price_in_grid else ("·" if c.min_price_in_row else "")
                cells.append(f"{p} {mark}")
            t.add_row(*cells)
        console.print(t)

    st = Table(title=f"Itineraries{ccy_tag}", show_header=True, header_style="bold green")
    st.add_column("#", justify="right")
    st.add_column(f"total ({passengers:d} travelers)" if party else "price", justify="right")
    st.add_column("carriers")
    shown = res.solutions[:limit]
    count = max([_ROUND_TRIP_LEGS, *(len(it.itinerary.slices) for it in shown if it.itinerary)])
    for header in _slice_headers(count):
        st.add_column(header)
    _keep_cells_whole(st, count)
    for i, it in enumerate(shown, 1):
        itn = it.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        it_carriers = ",".join(_safe_text(c.code or "?") for c in (itn.carriers if itn else []))

        slice_cells = _slice_cells(slcs, count)
        total = party_price(it, passengers)
        st.add_row(
            f"{i:d}",
            _amount(total, ccy)
            if total or not it.price
            else f"{_amount(it.price, ccy)} per traveler",
            it_carriers or "?",
            *slice_cells,
        )
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
    trip_length: int | None = None,
    currency: str = "USD",
    across_set: bool = False,
) -> None:
    """Render the GF native date-grid: cheapest fare per departure day, sorted
    cheapest-first. One-way, or a round trip of one `trip_length` in nights,
    which the grid prices by departure day alone. `across_set` says so in the
    title when a cell is the cheapest of several airport pairs."""
    if not grid:
        return
    priced_days = len(grid)
    cheapest = min(grid.values())
    trip = "" if trip_length is None else f"  · {trip_length:d}-night round trip"
    console.print(
        f"[bold]{priced_days} priced days[/]{trip}  · cheapest: "
        f"[bold cyan]{cheapest:.0f} ({_safe_text(currency)})[/]  · "
        f"window {_safe_text(sd.isoformat())} → {_safe_text(ed.isoformat())}"
    )
    t = Table(
        title=f"{_safe_text(','.join(origin))} → {_safe_text(','.join(destination))}: "
        "lowest fare per departure day"
        + (_ACROSS_SET_TITLE if across_set else "")
        + " (Google Flights)",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("departure", justify="right")
    t.add_column(f"min ({_safe_text(currency)})", justify="right")
    for day, price in sorted(grid.items(), key=lambda kv: kv[1]):
        # The day is a key off the Google Flights grid, not a date this module built.
        t.add_row(_safe_text(day), f"{price:.0f}")
    console.print(t)


# The graph prices each date at the cheapest pair of a set and says nothing about
# which pair that was, so a cell read as one airport pair's fare would be wrong.
_ACROSS_SET_TITLE = ", cheapest across every airport pair"


def _render_graph_range(
    graphs: Sequence[PriceGraph],
    *,
    origin: tuple[str, ...],
    destination: tuple[str, ...],
    sd: date,
    ed: date,
    across_set: bool,
) -> None:
    """Render one price graph per trip length side by side: a row per departure
    date any length priced, its lowest price, then each length's, cheapest row
    first. A length that priced nothing on a date shows "—" there."""
    lengths = [g.trip_length or 0 for g in graphs]
    by_day: dict[date, dict[int, float]] = {}
    for nights, graph in zip(lengths, graphs, strict=True):
        for cell in graph.cells:
            by_day.setdefault(cell.departure, {})[nights] = cell.price
    if not by_day:
        return
    rows = sorted(by_day.items(), key=lambda kv: (min(kv[1].values()), kv[0]))
    cheapest = min(rows[0][1].values())
    console.print(
        f"[bold]{len(rows):d} priced days[/]  · {_safe_text(_trip_lengths_text(lengths))} "
        f"round trips  · cheapest: [bold cyan]{cheapest:.0f} (USD)[/]  · "
        f"window {_safe_text(sd.isoformat())} → {_safe_text(ed.isoformat())}"
    )
    t = Table(
        title=f"{_safe_text(','.join(origin))} → {_safe_text(','.join(destination))}: "
        "lowest fare per departure day and trip length"
        + (_ACROSS_SET_TITLE if across_set else "")
        + " (Google Flights)",
        show_header=True,
        header_style="bold green",
    )
    t.add_column("departure", justify="right")
    t.add_column("min (USD)", justify="right")
    for nights in lengths:
        t.add_column(f"{nights:d}n", justify="right")
    for day, prices in rows:
        cells = [f"{prices[n]:.0f}" if n in prices else "—" for n in lengths]
        t.add_row(_safe_text(day.isoformat()), f"{min(prices.values()):.0f}", *cells)
    console.print(t)


def _trip_lengths_text(lengths: Sequence[int]) -> str:
    """The trip lengths a range table shows: "5-7-night" with no gap between
    them, "5- and 7-night" with one, "5-night" when one is left. A lost length
    leaves a gap, and a span across it would name a length the table has no
    column for."""
    first, last = lengths[0], lengths[-1]
    if first == last:
        return f"{first:d}-night"
    if list(lengths) == list(range(first, last + 1)):
        return f"{first:d}-{last:d}-night"
    return _join_reasons([*(f"{n:d}-" for n in lengths[:-1]), f"{last:d}-night"])


def _grid_branch_blocker(  # noqa: PLR0911 — one return per named reason, cheapest first
    search: CalendarSearch,
    *,
    json_out: bool,
    one_way: bool,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    fast: bool = False,
    graph: bool = False,
) -> str | None:
    """Why the GF date-grid can't serve this calendar, or None if it can.

    The string is user-facing: it completes "this is …" in the `--fast` refusal, so
    every branch returns a noun phrase. The shape branches name the SHAPE, which is
    the whole story for them; the routing branch names the flag and the tier
    instead, because a constraint the grid can't honor is not visible in the shape
    of the command line. Ordered cheapest-first so the fli-heavy `_gf_dategrid`
    import is still skipped for the shapes that never need it.

    The grid has no itineraries, so it is served only when the request carries
    EVERY constraint on the search; anything else would price a wider question and
    print it as the answer. Under `--fast` that request is the search page's URL,
    whose encoder carries fewer constraints than the RPC the weave calls, so its
    limits apply there alone. JSON, a round trip of one trip length and an airport
    set or metro code are served by that page grid too, so without `--fast` they
    still go to Matrix: the weave's Matrix half is one query, and `date_grid`
    writes one airport per side.

    `graph` (with `fast`) asks for Chrome's price graph, whose own gate
    (`_gf_calgraph.graph_blocker`) replaces the routing and page checks: it
    admits the carrier, alliance, duration and layover bounds and the time
    windows Google was measured applying from the page URL, and a trip-length
    range whose graphs fit the page-load budget. Without it, `--fast
    --gf-transport http` keeps the narrower checks and one trip length.

    The page asks for every airport of a set, so under `--fast` the airports are
    checked expanded, against fli's airport table and ONE page's per-leg bound
    (`gf_leg_refusal`): a grid is one page, where a single-cabin search over
    the bound is asked as several. The return leg is the same airports
    reversed, so one check covers both.

    The currency comes first, ahead of every admission test after it: the grid
    prices in USD whatever the page is asked for, so no shape of calendar makes
    it an answer in another currency.
    """
    if search.options.currency not in (None, "USD"):
        return "a non-USD currency"
    if json_out and not fast:
        return "JSON output"
    if not one_way and not fast:
        return "a round-trip window"
    window = search.window
    ranged = not one_way and window.duration_min != window.duration_max
    if ranged and not (fast and graph):
        return f"a trip-length range ({window.duration_min:d}-{window.duration_max:d} nights)"
    # Before anything builds an fli filter: fli has no member for most city codes
    # and resolves two of them to another city's airport.
    if fast:
        airports = [r for r in (gf_leg_refusal(origins, dests),) if r is not None]
        airports += _gf_unserveable_reasons(
            BACKEND_GFLIGHT, ",".join(expand_airports(origins)), ",".join(expand_airports(dests))
        )
        if airports:
            return _join_reasons(airports)
    else:
        if len(origins) > 1 or len(dests) > 1:
            return "a multi-airport route"
        city_codes = _gf_unserveable_reasons(BACKEND_GFLIGHT, ",".join(origins), ",".join(dests))
        if city_codes:
            return city_codes[0]
    if fast and graph:
        from ._gf_calgraph import (  # noqa: PLC0415 — fli, as below
            graph_blocker,
            page_budget_blocker,
        )

        reason = graph_blocker(search)
        # A range only: one length's window is cut short by the loads it is
        # given, and reports that, rather than being refused up front.
        return page_budget_blocker(search) if reason is None and ranged else reason
    from ._gf_dategrid import grid_can_serve, grid_routing_blocker  # noqa: PLC0415

    if not grid_can_serve(search, round_trip=fast, airport_sets=fast):
        # `grid_can_serve` is False for Tier-2 AND Tier-3, so ask which: the grid
        # returns no itineraries (Tier-2's problem) and cannot reach fare
        # construction at all (Tier-3's), and only one of those is a routing tier
        # the reader can do anything about. A Tier-1 code or bound the request
        # would leave out is named too. The fallback covers a future gate
        # condition that routing does not explain.
        return grid_routing_blocker(search) or "a constraint the price grid can't honor"
    if not fast:
        return None
    from ._gf_calgraph import page_blocker  # noqa: PLC0415 — fli, like the import above

    return page_blocker(search)


# A calendar day is a deal when it prices at least this percent under the median
# of its window, once the window has `_DEAL_MIN_DAYS` days priced in one currency.
_DEAL_UNDER_PCT = 20
_DEAL_MIN_DAYS = 5


def _deal_days(days: Sequence[CalendarDay], ccy: str) -> list[bool]:
    """Per day of `days`, in order: is its price at least `_DEAL_UNDER_PCT` percent
    under the median of the days priced in `ccy`. All False when fewer than
    `_DEAL_MIN_DAYS` are priced in `ccy`. A day in another currency is neither
    in the median nor a deal: its amount is not in the table's unit."""
    values = [d.price_value if _split_price(d.min_price)[0] == ccy else None for d in days]
    # Exact decimals: as floats 40.20 * 100 > 50.25 * 80, and whole cents round
    # 0.804 of a three-decimal currency to 0.80. `str` of a float is the shortest
    # decimal that reads back as it, so it is the amount as written.
    amounts = [None if v is None else Decimal(str(v)) for v in values]
    priced = [a for a in amounts if a is not None]
    if len(priced) < _DEAL_MIN_DAYS:
        return [False] * len(days)
    cutoff = median(priced) * (100 - _DEAL_UNDER_PCT)
    return [a is not None and a * 100 <= cutoff for a in amounts]


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
    # Only a merged grid names the pair behind each day; Matrix's own grid has one
    # query behind every cell, the one the title names.
    routed = any(d.origin is not None for d in res.priced_days)
    t.add_column("departure", justify="right")
    t.add_column("min", justify="right")
    if routed:
        t.add_column("route")
    if round_trip:
        for dur in range(dmin, dmax + 1):
            t.add_column(f"{dur:d}n", justify="right")
    t.add_column("sols", justify="right")
    days = sorted(res.priced_days, key=lambda x: x.price_value or 9e9)
    for d, deal in zip(days, _deal_days(days, ccy), strict=True):
        cell = _amount(d.min_price, ccy)
        row = [f"{d.date:d}", f"[green]{cell:s}[/]" if deal else cell]
        if routed:
            row.append(_safe_text(f"{d.origin}→{d.destination}") if d.origin else "—")
        if round_trip:
            opts = {o.trip_length: o for o in d.options}
            day_pair = (d.origin, d.destination)
            for dur in range(dmin, dmax + 1):
                o = opts.get(dur)
                if o is None:
                    row.append(_amount(None, ccy))
                elif o.origin is not None and (o.origin, o.destination) != day_pair:
                    # A length another pair priced names it, or the row reads as the route's.
                    row.append(
                        f"{_amount(o.min_price, ccy)} {_safe_text(f'{o.origin}→{o.destination}')}"
                    )
                else:
                    row.append(_amount(o.min_price, ccy))
        row.append(f"{d.solution_count:d}")
        t.add_row(*row)
    console.print(t)


# ─────────────────────────── backend execution ─────────────────────────────


def _build_pp_legs(legs: tuple[Leg, ...]) -> list[LegQuery]:
    """One award query per airport pair of each leg, a metro code asked as its
    member airports, in typed order. The providers take one airport per end.

    A leg's queries share its slice_index, date and label, which names the
    typed tokens, a repeated one once: slice_index lets the matcher join award
    results to the correct Itinerary slice, and `run_pp_for_search` reads
    consecutive queries with one slice_index as one leg. A pair with one
    airport at both ends is skipped, unless it is the only one a leg has.

    The providers take one departure day a leg, so a leg with a flexible or
    arrival date is asked for departures on its typed date, and a dim line on
    stderr says so."""
    if moved := _flexed_or_arrival_dates(legs):
        err.print(
            f"[dim]Award providers were asked for departures on {_safe_text(_join_reasons(moved))} "
            "only: they take no flexible or arrival date.[/]"
        )
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
        route = "→".join(",".join(dict.fromkeys(ends)) for ends in (leg.origins, leg.destinations))
        label = " ".join((kind, route, iso))
        origins, destinations = expand_airports(leg.origins), expand_airports(leg.destinations)
        pairs = [(o, d) for o in origins for d in destinations if o != d] or [
            (origins[0], destinations[0])
        ]
        out.extend(
            LegQuery(origin=o, destination=d, date=iso, slice_index=i, label=label)
            for o, d in pairs
        )
    return out


def _cap_text(opts: SearchOptions | None) -> str | None:
    """A search's price cap with its currency ("USD 250"), or None."""
    if opts is None or opts.max_price is None:
        return None
    return f"{opts.currency or 'USD'} {opts.max_price:d}"


def _page_cap_text(opts: SearchOptions | None) -> str | None:
    """`_cap_text`, where Google's page was asked for the cap; otherwise None,
    since a board fetched uncapped is not emptied by it."""
    if opts is None or search_page_cap(opts.max_price, opts.currency or "USD") is None:
        return None
    return _cap_text(opts)


def _price_capped(res: SearchResult, opts: SearchOptions, *, passengers: int = 1) -> SearchResult:
    """`res` holding only the solutions priced in the search's currency at or
    under its price cap, or `res` itself when there is no cap. The cap reads
    the price a Matrix row prints for a party of `passengers`: the total
    Matrix states for more than one, its listed price for one.

    Matrix has no price input, and it answers in price order, so the cut loses
    no cheaper fare. Where every fare the cap drops from this page is priced in
    its currency over it, the fares past the page are over it too, so
    `solutionCount`, at the top of the raw document and in its solution list,
    becomes the count kept. Where it drops none, or drops a fare it cannot read
    (another currency, no price), the fares past the page and that fare went
    unchecked, so Matrix's own total stays. The carrier x stops grid is dropped
    from the table, because its cells are minima over every fare; in the JSON
    it and Matrix's other blocks stay as served, describing its whole answer."""
    cap = opts.max_price
    if cap is None:
        return res
    currency = opts.currency or "USD"

    def price(it: Itinerary) -> tuple[str, float | None]:
        code, amount = _split_price(party_price(it, passengers))
        try:
            return code, float(amount)
        except ValueError:
            return code, None

    def admitted(it: Itinerary) -> bool:
        code, value = price(it)
        return within_price_cap(value, code, cap=cap, cap_currency=currency)

    def over(it: Itinerary) -> bool:
        code, value = price(it)
        return code == currency and value is not None and value > cap

    kept = [it for it in res.solutions if admitted(it)]
    dropped = [it for it in res.solutions if not admitted(it)]
    count = {"solutionCount": len(kept)} if dropped and all(map(over, dropped)) else {}
    raw = res.raw
    if raw is not None:
        raw = {**raw, **count}
        listing = raw.get("solutionList")
        if isinstance(listing, dict):
            served = cast("dict[str, Any]", listing)
            raw["solutionList"] = {
                **served,
                "solutions": [
                    sol
                    for sol in cast("list[Any]", served.get("solutions") or [])
                    if admitted(Itinerary.model_validate(sol))
                ],
                **count,
            }
    return res.model_copy(
        update={
            "solutions": kept,
            "solution_count": count.get("solutionCount", res.solution_count),
            "carrier_stop_matrix": None,
            "raw": raw,
        }
    )


def _run_matrix_path(  # noqa: PLR0912 — one arm per way Matrix's answer is written
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
    fare_rules: bool = False,
    split_ticket: dict[str, Any] | None = None,
) -> None:
    """Matrix path: Alkali call → optional cash render → optional fare rules →
    optional PP augmentation → URLs.

    `split_ticket` is a multi-city trip's separate-ticket object, which the JSON
    document carries beside Matrix's as `{"search": …, "split_ticket": …}`."""
    # A cap holds each fare in the cap's currency, so Matrix is asked in it:
    # left unset, it prices in its own default (GBP from LHR) and the cap keeps
    # nothing. Uncapped, the body stays without the key.
    if opts.max_price is not None:
        opts = opts.model_copy(update={"currency": opts.currency or "USD"})
    search = SpecificDateSearch(legs=legs, options=opts)
    rps, impersonate = _resolve_rps(rps), _resolve_impersonate(impersonate)
    # SpecificDateSearch → SearchResult by client._parse_response dispatch.
    res = cast(
        "SearchResult",
        _run(
            search,
            rps,
            impersonate,
            # Fare rules are asked of the search's session, and a cached answer
            # carries one Matrix may no longer hold.
            fare_rules or _resolve_no_cache(no_cache),
        ),
    )
    # Before anything reads it, so the pick, the fare rules, the awards and the
    # links all draw from the fares under the cap.
    res = _price_capped(res, opts, passengers=opts.pax.total)
    shown = res.solutions[: opts.page_size]
    # `--awards-only` prints no numbered table, so a pick names no row, and the
    # links below are unpinned; `--fare-rules` is refused beside it.
    if sel.awards_only:
        _refuse_pick_where_nothing_is_numbered(
            pick, links=not json_out and (matrix_url or google_url)
        )
        pick = None

    def _rules() -> _FareRulesAnswer | None:
        if not shown:
            err.print("[yellow]No itinerary to show fare rules for.[/]")
            return None
        return _fetch_fare_rules(res, (pick or 1) - 1, rps=rps, impersonate=impersonate)

    if fare_rules and shown:
        # Checked ahead of the JSON arm, because under `--format json` a pick
        # still chooses the itinerary whose rules the document carries.
        pick = _pick_in_range(
            pick,
            len(shown),
            pin_follows=lambda: (
                not json_out
                and _pins_row_one(search, res, matrix_url=matrix_url, google_url=google_url)
            ),
            fare_rules=True,
        )
    if _envelope.active():
        _envelope.record_search(
            backend="matrix",
            cabin=opts.cabin.value,
            rows=_matrix_envelope_rows(res, opts.pax.total),
        )
        if not run_pp:
            return
    elif json_out and not run_pp:
        if split_ticket is not None:
            doc = {"search": res.raw, "split_ticket": split_ticket}
            sys.stdout.write(json.dumps(doc, indent=2))
            return
        if not fare_rules:
            sys.stdout.write(json.dumps(res.raw, indent=2))
            return
        # `--fare-rules` refuses JSON with awards on, so its document is always
        # written here. It carries the search even when the rules fail, as the
        # table path prints the fares before asking for them; the exit status
        # still reports the failure.
        try:
            rules_doc = _fare_rules_document(_rules())
        except typer.Exit:
            sys.stdout.write(json.dumps({"search": res.raw, "fare_rules": None}, indent=2))
            raise
        sys.stdout.write(json.dumps({"search": res.raw, "fare_rules": rules_doc}, indent=2))
        return
    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
        _render_search(res, opts.page_size, cap=_cap_text(opts), passengers=opts.pax.total)
    # After the table, so a failure fetching the rules leaves the fares shown.
    rules = _rules() if fare_rules else None
    if rules is not None:
        _render_fare_rules(rules)
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
        # The visible count is the render's trim, not `len(res.solutions)`:
        # Matrix is asked for `page_size` rows and nothing holds its answer to
        # that.
        #
        # An empty result is numbered nowhere, so it gets no sentence at all
        # rather than an empty `(1-0)` interval and a pin claim nothing honours.
        pick = (
            _pick_in_range(
                pick,
                len(shown),
                pin_follows=lambda: _pins_row_one(
                    search, res, matrix_url=matrix_url, google_url=google_url
                ),
            )
            if shown and not sel.awards_only
            else None
        )
        # Unpinned under `--awards-only`: no numbered list backs a pin's label.
        _emit_urls(
            search,
            matrix_url=matrix_url,
            google_url=google_url,
            result=None if sel.awards_only else res,
            pick=pick,
        )


# ─────────────────────────────── fare rules ────────────────────────────────

# The ATPCO rule categories a traveler reads before buying, in print order.
_RULE_CATEGORIES = {16: "Penalties", 31: "Voluntary changes", 33: "Voluntary refunds"}


class _FareRulesAnswer(NamedTuple):
    itinerary: int  # 1-based, the number the table printed
    details: BookingDetailsResult
    rules: list[FareRulesResult]  # one per fare, in the booking details' order


def _refuse_fare_rules_conflicts(
    *, cabins: tuple[Cabin, ...], sel: ProviderSelection, json_out: bool
) -> None:
    """Refuse `--fare-rules` where there is no single cash row for it to describe,
    or no document it can be written into."""
    if sel.awards_only:
        err.print(
            "[red]--fare-rules describes a row of the fare table, and --awards-only prints none.[/]"
        )
        raise typer.Exit(2)
    if len(cabins) > 1:
        err.print(
            "[red]--fare-rules takes one --cabin: it describes one itinerary's fares, "
            "and a multi-cabin row is several.[/]"
        )
        raise typer.Exit(2)
    if json_out and _should_run_awards(sel):
        err.print(
            "[red]--fare-rules with --format json needs --cash-only: with awards on, "
            "the document is the award match.[/]"
        )
        raise typer.Exit(2)


async def _rules_of(
    c: MatrixClient,
    fares: list[PricedFare],
    *,
    session: str,
    solution_set: str,
    solution_id: str,
) -> list[FareRulesResult]:
    """The rules of each fare one solution's booking details name, in order."""
    # A fare named without a key cannot be asked for its rules. It keeps its
    # place as an empty answer, so the block says so for that fare.
    return [
        await c.fare_rules(
            session=session,
            solution_set=solution_set,
            solution_id=solution_id,
            fare_key=f.key,
        )
        if f.key
        else FareRulesResult(fareRules=None)
        for f in fares
    ]


def _fetch_fare_rules(
    res: SearchResult, idx: int, *, rps: float, impersonate: str
) -> _FareRulesAnswer:
    """Booking details for solution `idx` of `res`, then the rules of each fare
    they name — a round trip usually has two, on different fare bases.

    A run and a client of its own: Matrix answers these from the search's
    session, which outlives the client that ran the search."""
    session, solution_set, solution_id = res.session, res.solution_set, res.solutions[idx].id
    if not (session and solution_set and solution_id):
        err.print(
            "[red]Matrix answered without a session for this itinerary, "
            "so its fare rules cannot be asked for.[/]"
        )
        raise typer.Exit(1)

    async def go() -> _FareRulesAnswer:
        async with MatrixClient(rps=rps, impersonate=impersonate) as c:
            details = await c.booking_details(
                session=session, solution_set=solution_set, solution_id=solution_id
            )
            fares = details.booking_details.fares if details.booking_details else []
            rules = await _rules_of(
                c, fares, session=session, solution_set=solution_set, solution_id=solution_id
            )
            return _FareRulesAnswer(idx + 1, details, rules)

    answer = _run_matrix(go, said="Matrix fare rules failed")
    # Details that name no fare have no basis, booking code or rule to show.
    if answer.details.booking_details is None or not answer.details.booking_details.fares:
        err.print(
            f"[red]Matrix returned no booking details for itinerary #{answer.itinerary:d}.[/]"
        )
        raise typer.Exit(1)
    return answer


def _fare_rules_document(answer: _FareRulesAnswer | None) -> dict[str, Any] | None:
    """Matrix's own bodies, whole: the table trims rule text, this does not."""
    if answer is None:
        return None
    return {
        "itinerary": answer.itinerary,
        "booking_details": (answer.details.raw or {}).get("bookingDetails"),
        "rules": [(r.raw or {}).get("fareRules") for r in answer.rules],
    }


def _rule_lines(rule: FareRule) -> tuple[list[str], int]:
    """A rule's text without its NOTE asides, and how many lines those held.

    ATPCO text sets its asides under an indented `NOTE -`: waivers, agency
    fees, how fares combine. They are most of a penalties rule by volume, and
    dropping them is what leaves the cancel and change terms on screen. The
    rest is kept whole: a rule lists alternatives under `OR -`, and a line
    after one can qualify it, so a rule cut short can end on the wrong answer."""
    lines = [ln.rstrip() for b in rule.blocks for ln in b.splitlines() if ln.strip()]
    kept: list[str] = []
    note_indent: int | None = None
    for ln in lines:
        indent = len(ln) - len(ln.lstrip())
        if note_indent is not None and indent > note_indent:
            continue
        note_indent = None
        if ln.strip() == "NOTE -":
            note_indent = indent
            continue
        kept.append(ln)
    margin = min((len(ln) - len(ln.lstrip()) for ln in kept), default=0)
    return [ln[margin:] for ln in kept], len(lines) - len(kept)


def _render_fare_rules(answer: _FareRulesAnswer) -> None:
    """The fare-rules block under the table: per segment the fare basis, booking
    code and cabin, the fare calculation, then per fare its title and its
    penalty, change and refund rules, then the pricing notes.

    Every value is Matrix's. Amounts inside the rule text and the fare
    calculation are quoted in the fare's filing currency, as filed."""
    bd = answer.details.booking_details
    if bd is None:
        return
    console.print()
    console.print(
        f"[bold]Fare rules[/] · itinerary #{answer.itinerary:d}"
        + (f" · {_safe_text(bd.display_total)}" if bd.display_total else "")
    )
    for fare in bd.fares:
        for info in fare.booking_infos:
            seg = info.segment
            console.print(
                f"  {_safe_text(seg.origin if seg and seg.origin else '?')}→"
                f"{_safe_text(seg.destination if seg and seg.destination else '?')}  "
                f"{_safe_text(fare.carrier or '?')}  fare basis {_safe_text(fare.code or '?')}  "
                f"booking code {_safe_text(info.booking_code or '?')}  "
                f"{_safe_text(info.cabin or '?')}"
            )
    for pricing in bd.pricings:
        for calc in pricing.fare_calculations:
            for line in calc.lines:
                console.print(f"  [dim]Fare calculation:[/] {_safe_text(line)}")
    seen: set[tuple[str | None, ...]] = set()
    for fare, result in zip(bd.fares, answer.rules, strict=True):
        fr = result.fare_rules
        if fr is None:
            console.print(
                f"[yellow]Matrix returned no rules for fare {_safe_text(fare.code or '?')}.[/]"
            )
            continue
        # Several passengers can price on one fare, and its rules are one text.
        identity = (
            fr.carrier.code if fr.carrier else None,
            fr.code,
            fr.origin_city,
            fr.destination_city,
        )
        if identity not in seen:
            seen.add(identity)
            _render_one_fare(fr)
    notes = [n for p in bd.pricings for n in p.notes]
    if notes:
        console.print()
        console.print("[bold]Notes[/]")
        for note in notes:
            console.print(f"  {_safe_text(note)}")


def _render_one_fare(fr: FareRules) -> None:
    """One fare: carrier, basis and cities, its title — the first line of its
    category-0 text, since Matrix sends no title field — then its penalty,
    change and refund rules, each rule of a category in turn."""
    title = next(
        (
            " ".join(r.blocks[0].strip().splitlines()[0].split())
            for r in fr.rules
            if r.category == 0 and r.blocks and r.blocks[0].strip()
        ),
        "",
    )
    console.print()
    console.print(
        f"[bold]{_safe_text(fr.carrier.code if fr.carrier and fr.carrier.code else '?')} "
        f"{_safe_text(fr.code or '?')}[/]  "
        f"{_safe_text(fr.origin_city or '?')}→{_safe_text(fr.destination_city or '?')}"
        + (f"  {_safe_text(title)}" if title else "")
    )
    filed = [
        (label, r) for cat, label in _RULE_CATEGORIES.items() for r in fr.rules if r.category == cat
    ]
    if not filed:
        console.print("  [dim]No penalty, change or refund rules are filed for this fare.[/]")
    for label, rule in filed:
        console.print(
            f"  [bold]{_safe_text(label)}[/] [dim](category {rule.category or 0:d})[/]"
            + (f" [dim]{_safe_text(rule.type)}[/]" if rule.type else "")
        )
        lines, asides = _rule_lines(rule)
        for line in lines:
            console.print(f"    {_safe_text(line)}")
        if asides:
            console.print(
                f"    [dim]… {asides:d} lines of NOTE asides not shown; "
                "--format json carries the full text[/]"
            )


# ──────────────────────────────── --verify ─────────────────────────────────


def _verify_blocker(  # noqa: PLR0911 — one return per reason the run is refused
    *,
    fmt: str,
    backend: str,
    slice_specs: list[str] | None,
    multi_cabin: bool,
    awards_only: bool,
    awards_json: bool,
    sellers: bool,
    fare_rules: bool,
    bags: Bags | None,
    exclude_basic: bool,
    pick: int | None,
    page_size: int,
    date_options: bool = False,
) -> str | None:
    """Why `--verify` cannot run on this search, or None. Read off the flags
    alone, so it is decided before the backend is announced and before any
    request.

    The flag checks one row of one Google table, so a run that prints no such
    row has nothing to check. `fmt` is the resolved format: one this block
    cannot write into is refused here rather than ignored."""
    if fmt not in ("table", "json", "envelope"):
        return f"writes into a table or a JSON document, not --format {fmt}"
    if multi_cabin:
        return "checks one row of one table; drop the extra --cabin values"
    if awards_only:
        return "checks a row of the results table, and --awards-only prints none"
    if awards_json:
        return "cannot join the award document --format json writes; add --cash-only"
    if sellers:
        return "and --sellers each take the row --pick names; run one at a time"
    if fare_rules:
        return "shows the fare rules of the row it checks; drop --fare-rules"
    if bags is not None:
        return "asks Matrix, which prices no bags; drop --bags"
    if exclude_basic:
        return "asks Matrix, which is not asked to leave out basic economy; drop --exclude-basic"
    if slice_specs:
        return "checks a Google Flights row on one ticket, and a --slice search shows none"
    if backend == BACKEND_MATRIX or date_options:
        return "needs a Google Flights row, and this search runs on Matrix"
    n = 1 if pick is None else pick
    if not 1 <= n <= page_size:
        return f"--pick {n:d} names no row of the {page_size:d} that -n asks for"
    return None


def _pick_for_verify(pick: int | None, rows: int) -> int:
    """The 1-based row `--verify` checks, or exit. No fallback to row one: the
    check names row N, and a board with no rows leaves nothing to check."""
    if rows == 0:
        err.print("[red]Not verified on Matrix:[/] the search returned no itinerary to check.")
        raise typer.Exit(1)
    n = 1 if pick is None else pick
    if not 1 <= n <= rows:
        err.print(f"[red]--pick {n:d} is out of range (1-{rows:d}); --verify checks that row.[/]")
        raise typer.Exit(2)
    return n


class _Checked(NamedTuple):
    verdict: _verify.Verdict
    rules: _FareRulesAnswer | None  # the matched solution's, headed by the Google row's number


def _same_itinerary(
    res: SearchResult,
    idxs: list[int],
    row: _verify.Row,
    n: int,
    *,
    rps: float,
    impersonate: str,
) -> tuple[int, _FareRulesAnswer] | None:
    """The first of `idxs` whose booking details are row `n` flight by flight,
    with its fare rules, or None when `res` is read whole and none is. One
    booking-details call per candidate, in Matrix's order, so a cheaper
    candidate that is another trip is passed over."""
    session, solution_set = res.session, res.solution_set
    sids = [sid for sid in (res.solutions[i].id for i in idxs) if sid]
    if not (session and solution_set and len(sids) == len(idxs)):
        err.print(
            "[red]Matrix answered without a session for these flights, "
            "so they cannot be checked flight by flight.[/]"
        )
        raise typer.Exit(1)
    read: list[BookingDetails | None] = []

    async def go() -> tuple[int, _FareRulesAnswer] | None:
        async with MatrixClient(rps=rps, impersonate=impersonate) as c:
            for i, sid in zip(idxs, sids, strict=True):
                details = await c.booking_details(
                    session=session, solution_set=solution_set, solution_id=sid
                )
                bd = details.booking_details
                if (
                    bd is not None
                    and bd.itinerary is not None
                    and _verify.same_flights(row, bd.itinerary)
                ):
                    rules = await _rules_of(
                        c, bd.fares, session=session, solution_set=solution_set, solution_id=sid
                    )
                    return i, _FareRulesAnswer(n, details, rules)
                read.append(bd)
            return None

    found = _run_matrix(go, said="Matrix booking details failed")
    if found is None:
        _read_whole(res, row, read)
    return found


def _trip_text(row: _verify.Row) -> str:
    """Each slice's chain of flights and its day, as a check names the row."""
    return " · ".join(
        f"{chain} {legs[0].departure[:10]}"
        for chain, legs in zip(_verify.routings(row), row.slices, strict=True)
    )


def _check_on_matrix(
    row: _verify.Row, n: int, opts: SearchOptions, *, rps: float | None, impersonate: str | None
) -> _Checked:
    """Row `n` asked of Matrix as exactly that itinerary, or exit 1 with the
    reason on stderr.

    The chain search is uncached, because booking details are asked of its
    session. When it finds nothing, the same legs are asked again without the
    chain, to tell a carrier none of Matrix's returned trips name from a fare
    it does not have.

    A row Google sells as separate tickets is not asked at all: Matrix prices
    one ticket, never that booking."""
    if (separate := _verify.on_separate_tickets(row)) is not None:
        return _Checked(separate, None)
    rps_, imp = _resolve_rps(rps), _resolve_impersonate(impersonate)
    # Each search below can take tens of seconds; the booking details and
    # fare rules after a match take a second or two and say nothing.
    err.print(f"[dim]Asking Matrix for itinerary #{n:d}: {_safe_text(_trip_text(row))}…[/]")
    chain = cast(
        "SearchResult",
        _run(
            SpecificDateSearch(
                legs=_verify.matrix_legs(row), options=_verify.matrix_options(row, opts)
            ),
            rps_,
            imp,
            True,
        ),
    )
    idxs = _verify.candidates(row, chain)
    try:
        if not idxs:
            # With no candidate the page alone decides, so what keeps it from
            # being read whole is named before a second search or a missing
            # session, as the low check names it.
            _read_whole(chain, row, [])
            if not chain.solutions:
                err.print(
                    "[dim]Matrix has no fare on those flights; asking which carriers it lists…[/]"
                )
                probe = SpecificDateSearch(
                    legs=_verify.matrix_legs(row, routed=False),
                    options=_verify.matrix_options(row, opts, max_stops=_verify.most_stops(row)),
                )
                return _Checked(
                    _verify.unpriced(row, cast("SearchResult", _run(probe, rps_, imp, True))), None
                )
        found = _same_itinerary(chain, idxs, row, n, rps=rps_, impersonate=imp)
    except _UncheckableAnswerError as e:
        err.print(f"[red]{_safe_text(str(e))}[/]")
        raise typer.Exit(1) from None
    if found is None:
        return _Checked(_verify.other_itinerary(len(chain.solutions)), None)
    idx, answer = found
    return _Checked(
        _verify.Verdict(
            "match", solution=chain.solutions[idx], details=answer.details.booking_details
        ),
        answer,
    )


def _price_gap(google: str | None, matrix: str | None) -> str:
    """How Matrix's price stands to Google's, from Google minus Matrix; empty
    where the two cannot be compared."""
    gap = _verify.delta(google, matrix)
    if gap is None:
        return ""
    if gap == 0:
        return " · same price"
    ccy = _split_price(matrix)[0]
    return f" · Matrix {ccy}{abs(gap):.2f} {'cheaper' if gap > 0 else 'dearer'}"


def _print_verified(
    r: Any, n: int, opts: SearchOptions, *, rps: float | None, impersonate: str | None
) -> None:
    """The `--verify` block under the table: Matrix's price beside Google's
    and the fare rules, or the one line saying why Matrix does not price this
    itinerary. An exit 1 here leaves the table above it shown."""
    row = _verify.google_row(r)
    checked = _check_on_matrix(row, n, opts, rps=rps, impersonate=impersonate)
    verdict = checked.verdict
    console.print()
    if verdict.outcome != "match" or verdict.solution is None:
        console.print(
            f"[yellow]Not verified on Matrix · itinerary #{n:d}: "
            f"{_safe_text(verdict.reason or verdict.outcome)}[/]"
        )
        return
    console.print(
        f"[bold green]Verified on Matrix[/] · itinerary #{n:d} · {_safe_text(_trip_text(row))}"
    )
    matrix = party_price(verdict.solution, opts.pax.total)
    shown = matrix or "—"
    if matrix is None and opts.pax.total > 1 and verdict.solution.price:
        # One passenger's fare beside Google's party total has no gap to state.
        shown = f"{verdict.solution.price} per traveler"
    console.print(
        f"Matrix {_safe_text(shown)} · Google {_safe_text(row.price or '—')}"
        f"{_safe_text(_price_gap(row.price, matrix))}"
    )
    if checked.rules is not None:
        _render_fare_rules(checked.rules)


def _write_verified(
    search_doc: list[Any],
    r: Any,
    n: int,
    opts: SearchOptions,
    *,
    rps: float | None,
    impersonate: str | None,
) -> None:
    """The `--verify --format json` document: the search's own document
    unchanged, beside row `n`'s check. A Matrix failure still writes the
    search, with `verify` null, and exits 1."""
    try:
        doc = _verify_document(r, n, opts, rps=rps, impersonate=impersonate)
    except typer.Exit:
        sys.stdout.write(json.dumps({"search": search_doc, "verify": None}, indent=2, default=str))
        raise
    sys.stdout.write(json.dumps({"search": search_doc, "verify": doc}, indent=2, default=str))


def _verify_document(
    r: Any, n: int, opts: SearchOptions, *, rps: float | None, impersonate: str | None
) -> dict[str, Any]:
    """Row `n`'s `verify` object, or exit 1 with the reason on stderr."""
    row = _verify.google_row(r)
    checked = _check_on_matrix(row, n, opts, rps=rps, impersonate=impersonate)
    return _verify.document(
        n, row, checked.verdict, _fare_rules_document(checked.rules), passengers=opts.pax.total
    )


class _GfQuery(NamedTuple):
    """One Google Flights query as `_gflight_results` sends it."""

    filters: Any  # fli's FlightSearchFilters; fli ships no stubs
    transport: GfTransport
    currency: str
    keep: Callable[[int, Any], bool] | None
    checks: str
    stop_drops: StopDrops
    fits: Callable[[int, Any], bool] | None


@contextlib.contextmanager
def _gflight_query(
    legs: tuple[Leg, ...], opts: SearchOptions, gf_mode: GfTransportMode, gf_headed: bool
) -> Generator[_GfQuery]:
    """The query for `legs`, and the rung-2 session's end once it has run.

    What the page encodes narrows the fli query natively, and the row filter
    drops violating rows from each board as it is served, re-checking what the
    page encodes wherever the row shows it. `search` applies the same
    routing/extension to every leg, so the first leg's constraints cover the
    trip for the native query; the post-filter is per slice.

    One builder for `_gflight_results` and `_gflight_outbound`, so the page a
    multi-cabin round trip fetches ahead of its pins is the page the pins are
    then taken from.

    This is also where a rung-2 browser session dies. It is created lazily on
    whichever thread runs the query — the enrich path runs it inside
    `anyio.to_thread.run_sync` — and a playwright object may only be closed by
    the thread that made it, so the `finally` here is the guarantee. A SIGINT
    landing while that thread sits in `page.goto` escapes it, which is why the
    profile-lock refusal names the interrupted-run case.
    """
    # Every import in this block is deferred for one reason: the Google Flights
    # backend must not load on a Matrix-only search, and this function is the
    # first point that has committed to Google Flights.
    from ._gf_postfilter import (  # noqa: PLC0415 — GF-only; see above
        StopDrops,
        listing_fits,
        routing_keep,
    )
    from ._gflight_ids import GfTransport  # noqa: PLC0415 — fli, ~95 ms
    from .fli_bridge import apply_gf_native_filters, to_fli_filter  # noqa: PLC0415 — fli
    from .routing_predicates import (  # noqa: PLC0415 — pulled in by the two above
        CabinPred,
        ExcludeOvernightsPred,
        ExcludeRedeyesPred,
        classify,
    )

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
    requested = opts.currency or "USD"
    stops = opts.max_extra_stops
    stop_drops = StopDrops()
    try:
        yield _GfQuery(
            filters=fli_filter,
            transport=transport,
            currency=requested,
            keep=routing_keep(
                per_slice_preds,
                [lg.time_ranges for lg in legs],
                per_slice_arrivals=[lg.arrival_ranges for lg in legs],
                max_price=opts.max_price,
                currency=requested,
                max_stops=stops,
                stop_drops=stop_drops,
            ),
            # A cap, a stop limit, a window, a night-flight check or a cabin
            # requirement can empty a return board with no routing asked at all.
            checks=(
                _row_checks(legs, opts)
                if opts.max_price is not None
                or (stops is not None and stops >= 0)
                or any(
                    lg.arrival_ranges or any(isinstance(t, ClockWindow) for t in lg.time_ranges)
                    for lg in legs
                )
                or any(
                    isinstance(p, ExcludeRedeyesPred | ExcludeOvernightsPred | CabinPred)
                    for preds in per_slice_preds
                    for p in preds
                )
                else "the routing"
            ),
            stop_drops=stop_drops,
            fits=listing_fits(per_slice_preds),
        )
    finally:
        # Every transport but `http`, which promises it never holds a Chrome:
        # `browser` opens one, and `auto` opens one once it escalates.
        if transport.mode != TRANSPORT_HTTP:
            from ._gf_browser import close_thread_session  # noqa: PLC0415 — GF-only; see above

            close_thread_session()


def _gflight_results(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
    *,
    first: Board[Any] | None = None,
    prefer: Sequence[ItineraryKey] = (),
    separate_tickets: SeparateTickets = "off",
) -> list[Any]:
    """Query Google Flights for `legs`, honoring routing/extension and time
    windows (see `_gflight_query`). Returns the (filtered) raw fli result list,
    with the page's price insight and the count of rows the filter dropped.

    `first` and `prefer` go to `search_with_ids` as they are: the outbound page
    `_gflight_outbound` already fetched for the same arguments, and the
    outbounds to pin ahead of this board's own. So does `separate_tickets`,
    which only a caller whose renderer marks a separate-ticket row may set.

    A leg with more airports than one page takes is asked as several pages
    and their boards merged (`_gflight_pages`); with `first` or `prefer` the
    caller has already chosen one page.
    """
    from ._gflight_ids import Board, search_with_ids  # noqa: PLC0415 — fli, ~95 ms

    if first is None and not prefer and len(pages := _gf_pages(legs)) > 1:
        return _gflight_pages(
            pages, opts, top_n, gf_mode, gf_headed, separate_tickets=separate_tickets
        )
    # Only a multi-cabin round trip, or a round trip asked as several pages,
    # sets `first` or `prefer`, only a search whose renderer marks a
    # separate-ticket row `separate_tickets`, and only a `+CABIN` search `fits`,
    # so every other search makes the one call to `search_with_ids` a single
    # search makes.
    handed: dict[str, Any] = {}
    if first is not None:
        handed["first"] = first
    if prefer:
        handed["prefer"] = prefer
    if separate_tickets != "off":
        handed["separate_tickets"] = separate_tickets
    with _gflight_query(legs, opts, gf_mode, gf_headed) as query:
        if query.fits is not None:
            handed["fits"] = query.fits
        served = search_with_ids(
            query.filters,
            top_n=top_n,
            transport=query.transport,
            currency=query.currency,
            keep=query.keep,
            checks=query.checks,
            **handed,
        )
    # Widened: the callers read any list, `dropped` and this tally by
    # `getattr`. A Board carries the tally to whichever path shows it
    # (`_note_stop_drops`).
    results = cast("list[Any]", Board[Any]() if served is None else served)  # None: nothing served
    if isinstance(results, Board):
        results.stop_drops = query.stop_drops
    # Untrimmed on purpose: a round trip's combinations are built pin-major, so
    # the first `top_n` of them are one outbound's returns and nothing else.
    # Every caller trims what it renders, in the order that surface ranks by.
    return results


def _google_board_label(cabin: Cabin | None, one_way: str | None) -> str:
    """How `_note_stop_drops` and `_note_row_cap` name the board they describe:
    a multi-cabin search's cabin, a one-way board a multi-city trip or `--split`
    reads, else the search's one board."""
    if cabin is not None:
        return f"Google Flights {cabin.value}"
    if one_way is not None:
        return f"Google Flights {one_way} one-way"
    return "Google Flights"


def _note_stop_drops(
    results: list[Any], cabin: Cabin | None = None, *, one_way: str | None = None
) -> None:
    """One stderr line counting the rows Google served over the stop ceiling it
    was asked for, from `_gflight_results`' tally. Called, like
    `_note_other_currencies`, by each path that answers with the board: a board
    the filter emptied says so in its own line, and one handed to Matrix is not
    shown at all. `one_way` labels a one-way board a multi-city trip or
    `--split` reads (`_one_way_boards`), as `_note_row_cap` labels it; such a board is
    counted even when the drops emptied it, since its own line says only that
    it priced no one-way."""
    drops: StopDrops | None = getattr(results, "stop_drops", None)
    if drops is None or not drops.rows or drops.ceiling is None:
        return
    if not results and one_way is None:
        return
    rows = f"{drops.rows:d} row{'' if drops.rows == 1 else 's'}"
    shown = "it is" if drops.rows == 1 else "they are"
    google = _google_board_label(cabin, one_way)
    note = (
        f"{google} returned {rows} over the stop ceiling it was asked for "
        f"({drops.ceiling:d}); {shown} not shown."
    )
    err.print(f"[dim]{_safe_text(note)}[/]")


def _note_row_cap(
    results: list[Any], requested: str, cabin: Cabin | None = None, *, one_way: str | None = None
) -> None:
    """One stderr line when the board stopped at Google's row cap, naming its
    highest fare: one above it may be missing. Called beside
    `_note_stop_drops`, by each path that shows the board, a board the routing
    emptied included: a match priced above the cap may be what is missing.
    Each cap is named in the currency of the page that stopped there
    (`Board.capped_at`), which no row may carry once a filter has run;
    `requested` names a cap whose page decoded no currency. `one_way` labels a
    one-way board a multi-city trip or `--split` reads (`_one_way_boards`), as
    `cabin` labels a cabin's.

    A plain line, not a narrowing, so the envelope carries it as a note and
    `complete` keeps its meaning: every fare at or below the cap is on the
    board."""
    from ._gflight_ids import (  # noqa: PLC0415 — fli, ~95 ms
        _ROW_CAP,  # pyright: ignore[reportPrivateUsage] — the cap `capped_at` was read against
        lowest_caps,
    )

    capped_at: dict[str, float] = getattr(results, "capped_at", None) or {}
    caps = lowest_caps(*({ccy or requested: amount} for ccy, amount in capped_at.items()))
    if not caps:
        return
    bounds = " and ".join(f"{ccy}{amount:.2f}" for ccy, amount in sorted(caps.items()))
    google = _google_board_label(cabin, one_way)
    note = (
        f"{google} stops at {_ROW_CAP:d} rows for this search: fares above {bounds} may be missing."
    )
    err.print(f"[dim]{_safe_text(note)}[/]")


class _Outbound(NamedTuple):
    """One cabin's outbound page, the row filter its pins are held to, that
    filter's count of the rows over the stop ceiling, and the cabin check that
    picks which listing of a row the filter is handed."""

    board: Board[Any]
    keep: Callable[[int, Any], bool] | None
    stop_drops: StopDrops | None = None
    fits: Callable[[int, Any], bool] | None = None


def _gflight_outbound(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
) -> _Outbound:
    """The page `_gflight_results` starts from for the same arguments: one GET,
    fetched ahead of it so the pins can be chosen across cabins."""
    from ._gflight_ids import outbound_page  # noqa: PLC0415 — fli, ~95 ms

    with _gflight_query(legs, opts, gf_mode, gf_headed) as query:
        page = outbound_page(query.filters, transport=query.transport, currency=query.currency)
    return _Outbound(page, query.keep, query.stop_drops, query.fits)


def _gf_pages(legs: tuple[Leg, ...]) -> list[tuple[Leg, ...]]:
    """`legs` as the Google pages that ask for them (`gf_leg_pages`): `legs`
    itself unless the outbound's airports need more than one page.

    A round trip's page flies back between the same two groups, which is the
    return `search` builds: the outbound reversed. Any other return is left
    whole, since no grouping of the outbound describes it."""
    if not legs or len(legs) > _ROUND_TRIP_LEGS:
        return [legs]
    out = legs[0]
    if len(legs) == _ROUND_TRIP_LEGS and (
        expand_airports(legs[1].origins),
        expand_airports(legs[1].destinations),
    ) != (expand_airports(out.destinations), expand_airports(out.origins)):
        return [legs]
    plan = gf_leg_pages(out.origins, out.destinations)
    if plan is None or len(plan) == 1:
        return [legs]
    pages: list[tuple[Leg, ...]] = []
    for origins, destinations in plan:
        page = (out.model_copy(update={"origins": origins, "destinations": destinations}),)
        if len(legs) == _ROUND_TRIP_LEGS:
            page += (legs[1].model_copy(update={"origins": destinations, "destinations": origins}),)
        pages.append(page)
    return pages


# A failure of the IP, the network or the browser session, not of one page (`_PageAsk`).
_GF_STOPS = (GfThrottledError, GfTransportError, GfBrowserUnavailableError)


def _search_stop(board: Any) -> GfBackendError | None:
    """The stop `board`'s search met, or None: one that ended its pins or its
    pages (`Board.stopped`), or its Cheapest tab refused for one, as `_PageAsk`
    reads a page's answer. Any later request of the search would meet it too."""
    held: GfBackendError | None = getattr(board, "stopped", None)
    tab: GfBackendError | None = getattr(board, "separate_failed", None)
    return held if held is not None else tab if isinstance(tab, _GF_STOPS) else None


class _PageAsk:
    """The pages of one search, asked in order, and what each one met.

    A refusal of one page's URL is that page's own, so the pages after it are
    still asked. A throttle, a spent transport ladder or a dead browser is not
    a fact about a page: the wall is per-IP, the network is one network, and
    every later page would navigate on the same session. It ends the asking,
    and every page after it is named as not asked. A pin loop that stopped
    after serving some pins answers with its rows, and the stop it carries ends
    the asking as a raised one does. So does one a page's Cheapest tab met: the
    page answered, so no page line names it, and the tab's line says why, ahead of
    an earlier page's refused tab, which gets a line of its own. A
    round trip asks every page's outbounds before any page's returns, so a page
    that answered its outbounds before the stop is named for the returns it did
    not get. A page whose pin loop stopped after serving is named `short`, not
    `missing`, since its rows are on the board."""

    def __init__(
        self, pages: list[tuple[Leg, ...]], *, gf_mode: GfTransportMode, bags: bool
    ) -> None:
        self.pages = pages
        self.gf_mode: GfTransportMode = gf_mode
        self.bags = bags
        self.failed: dict[int, GfBackendError] = {}
        self.unasked: set[int] = set()
        self.answered: set[int] = set()
        self.short: set[int] = set()
        self.stopped_at: int | None = None
        self.stop: GfBackendError | None = None
        # A page's refused Cheapest tab that the stop's line is printed ahead of.
        self.displaced: dict[int, GfBackendError] = {}

    def ask[T](self, i: int, call: Callable[[], T]) -> T | None:
        """`call`'s answer for page `i`, or None once its failure is recorded."""
        if self.stopped_at is not None:
            self.unasked.add(i)
            return None
        try:
            answer = call()
        except _GF_STOPS as e:
            self._stopped(i, e)
            self.failed[i] = e
        except GfBackendError as e:
            self.failed[i] = e
        else:
            self.answered.add(i)
            # `getattr`: the answer is a Board, an outbound or a stand-in list.
            held: GfBackendError | None = getattr(answer, "stopped", None)
            tab: GfBackendError | None = getattr(answer, "separate_failed", None)
            if held is not None:
                self._stopped(i, held)
                self.failed[i] = held
                self.short.add(i)
            elif isinstance(tab, _GF_STOPS):
                self._stopped(i, tab)
            return answer
        return None

    def _stopped(self, i: int, e: GfBackendError) -> None:
        self.stopped_at = i
        self.stop = e

    def tab_refusal(self, tabs: dict[int, GfBackendError]) -> GfBackendError | None:
        """The refusal the one tab line says, of the pages' refused tabs `tabs`:
        the first page's, but a stop no page line names goes ahead of it, and
        the first page's refusal is then `displaced` onto a line of its own."""
        if not tabs:
            return None
        first = min(tabs)
        stop = self.stop
        if (
            stop is None
            or tabs[first] is stop
            or stop in self.failed.values()
            or stop not in tabs.values()
        ):
            return tabs[first]
        self.displaced[first] = tabs[first]
        return stop

    def report(self) -> None:
        """One stderr line per page that did not answer, in page order."""
        _report_pages(self)

    def raise_if_empty(self, rows: list[Any]) -> None:
        """Raise the first failed page's error when nothing was merged, as one
        page raises its own, or else the stop that left a page unasked. An
        empty board beside a page that never answered would read as a route
        with no flights, and a refusal is handed to Matrix or exits with its
        reason where that would not."""
        if rows:
            return
        if self.failed:
            raise self.failed[min(self.failed)]
        if self.unasked and self.stop is not None:
            raise self.stop


def _report_pages(asked: _PageAsk) -> None:
    n = len(asked.pages)
    for i in sorted({*asked.failed, *asked.unasked}):
        out = asked.pages[i][0]
        e = asked.failed.get(i)
        # A page whose pins stopped after serving has its rows on the board.
        state = "short" if i in asked.short else "missing"
        if e is not None:
            rung = _rung_reached(asked.gf_mode)
            why = _gf_refusal(e, transport=rung, bags=asked.bags).note.removesuffix(".")
        elif i in asked.answered:
            why = (
                f"its returns were not asked after page {(asked.stopped_at or 0) + 1:d} "
                "stopped the search"
            )
        else:
            why = f"not asked after page {(asked.stopped_at or 0) + 1:d} stopped the search"
        err.print(
            f"[yellow]Google Flights page {i + 1:d} of {n:d} "
            f"({_safe_text(','.join(out.origins))}→{_safe_text(','.join(out.destinations))}) "
            f"is {state}: {why}.[/]"
        )
    for i, e in sorted(asked.displaced.items()):
        rung = _rung_reached(asked.gf_mode)
        why = _gf_refusal(e, transport=rung, bags=asked.bags).note.removesuffix(".")
        out = asked.pages[i][0]
        err.print(
            f"[dim]Google Flights page {i + 1:d} of {n:d} "
            f"({_safe_text(','.join(out.origins))}→{_safe_text(','.join(out.destinations))}): "
            f"itineraries on separate tickets not read: {why}.[/]"
        )


def _merged_boards(boards: Sequence[Board[Any]], *, currency: str) -> list[Any]:
    """Every row of `boards` once, by the whole trip and how Google sells it
    (`row_key`, `ticketing`): the cheaper listing is kept, in the place the
    first one took. A row on separate tickets is a booking of its own, so it
    stands beside the one-ticket row on its flights, as on one page. A member
    either page put on its Top flights board is a top flight on the row kept."""
    from ._gflight_ids import row_key  # noqa: PLC0415 — fli, ~95 ms

    at: dict[tuple[tuple[ItineraryKey, ...], tuple[Any, ...]], int] = {}
    rows: list[Any] = []
    for board in boards:
        for r in board:
            key = (row_key(r), _ticketings(r))
            seen = at.get(key)
            if seen is None:
                at[key] = len(rows)
                rows.append(r)
            elif _terminal_fare_key(r, currency=currency) < _terminal_fare_key(
                rows[seen], currency=currency
            ):
                rows[seen] = _with_top_marks(r, rows[seen])
            else:
                rows[seen] = _with_top_marks(rows[seen], r)
    return rows


def _with_top_marks(kept: Any, dropped: Any) -> Any:
    """`kept`, each member a top flight where it or `dropped`'s member in the
    same slice is one."""
    one_way = not isinstance(kept, tuple)
    ours = (kept,) if one_way else cast("tuple[Any, ...]", kept)
    theirs = (dropped,) if one_way else cast("tuple[Any, ...]", dropped)
    members = tuple(
        replace(k, top_flight=True)
        if getattr(d, "top_flight", False) and not getattr(k, "top_flight", False)
        else k
        for k, d in zip(ours, theirs, strict=True)
    )
    return members[0] if one_way else members


def _kept(outbound: _Outbound) -> list[Any]:
    """The rows `outbound`'s filter keeps, each the listing `search_with_ids`
    hands that filter, so the pins across pages are the ones a page takes."""
    from ._gflight_ids import (  # noqa: PLC0415 — fli, ~95 ms
        _kept_outbounds,  # pyright: ignore[reportPrivateUsage] — the listing a cabin picks
    )

    return _kept_outbounds(outbound.board, outbound.keep, outbound.fits)


def _union_pins(
    kept: dict[int, list[Any]], top_n: int, *, currency: str
) -> dict[int, list[ItineraryKey]]:
    """Each page's share of the `pinned_fanout(top_n)` cheapest outbounds kept
    across every page (`kept`, by page), as the keys that page pins.

    An outbound two pages list is pinned once, on the page that priced it
    lower, so no return board is fetched twice for one flight."""
    from ._gflight_ids import Board, pin_keys, row_key  # noqa: PLC0415 — fli, ~95 ms

    owner: dict[ItineraryKey, int] = {}
    union: list[Any] = []
    at: dict[ItineraryKey, int] = {}
    for i, rows in kept.items():
        for r in rows:
            (key,) = row_key(r)
            seen = at.get(key)
            if seen is None:
                at[key] = len(union)
                union.append(r)
                owner[key] = i
            elif _terminal_fare_key(r, currency=currency) < _terminal_fare_key(
                union[seen], currency=currency
            ):
                union[seen] = r
                owner[key] = i
    shares: dict[int, list[ItineraryKey]] = {}
    for key in pin_keys(Board(union), top_n=top_n):
        shares.setdefault(owner[key], []).append(key)
    return shares


def _mark_tops(boards: Sequence[Board[Any]], kept: Iterable[Sequence[Any]]) -> None:
    """Mark, in place, every listing on `boards` of an outbound that any page's
    `kept` rows mark. `_union_pins` pins an outbound on one page alone, so its
    pairs never meet another page's copy in `_merged_boards`."""
    from ._gflight_ids import row_key  # noqa: PLC0415 — fli, ~95 ms

    tops = {row_key(r) for rows in kept for r in rows if getattr(r, "top_flight", False)}
    if not tops:
        return
    for board in boards:
        for j, r in enumerate(board):
            if not r.top_flight and row_key(r) in tops:
                others = tuple(replace(o, top_flight=True) for o in r.others)
                board[j] = replace(r, top_flight=True, others=others)


def _gflight_pages(  # noqa: PLR0915 — one pass over the pages, an arm per way a page answers
    pages: list[tuple[Leg, ...]],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    *,
    separate_tickets: SeparateTickets = "off",
) -> Board[Any]:
    """`pages`' boards as one board, each page asked as its own search and
    the rows merged by the whole trip (`_merged_boards`).

    A round trip fetches every page's outbounds first, then pins the
    `pinned_fanout(top_n)` cheapest of all of them, each on its own page: a
    GET a page plus one a pin, as one page costs. A return is priced on its
    outbound's page, so it flies back between that page's airports.

    `dropped` sums what every page's filter removed, its outbounds whether
    pinned or not and its returns, because an empty board is handed to Matrix
    on it. `unread` sums the rows the parser could not read the same way, a
    page missing because none of its rows parsed included, because the
    cross-check calls no flight absent from Google while any are.
    `stop_drops` sums the rows over the stop ceiling the same way, for the
    one line that counts them.
    A page's price insight, history and facets describe its own airports, so
    the merged board's `insight`, `history` and `facets` are None and each
    answered page's ride in `page_insights`, `page_histories` and
    `page_facets`, in page order. The board is `partial` where a page is
    missing or the trip is round: its rows then stop short of what one search
    would list.

    `separate_tickets` goes to every page, so each reads its own Cheapest tab
    once, after its pins, a round-trip page that holds no pin included.
    `separate_hidden` sums the pages' counts and `separate_failed` is the first
    page's, in page order, unless a stop met by a later page's tab goes ahead
    of it (`_PageAsk.tab_refusal`). A page that holds no pin and was not
    reached because the search stopped takes the stop as its reason: no page
    line names it, since its outbounds answered."""
    from ._gf_postfilter import StopDrops  # noqa: PLC0415 — GF-only
    from ._gflight_ids import (  # noqa: PLC0415 — fli, ~95 ms
        Board,
        _kept_insight,  # pyright: ignore[reportPrivateUsage] — a page's insight past its filter
        _PageUnreadError,  # pyright: ignore[reportPrivateUsage] — the refusal that counts rows
        lowest_caps,
    )

    asked = _PageAsk(pages, gf_mode=gf_mode, bags=opts.bags is not None)
    boards: list[Board[Any]] = []
    dropped = pinned = unread = hidden = 0
    tabs_failed: dict[int, GfBackendError] = {}
    stop_drops = StopDrops()
    extras: dict[int, tuple[PriceInsight | None, PriceHistory | None, RouteFacets | None]] = {}
    unboarded_caps: list[dict[str, float]] = []
    with _browser_scope(gf_mode):
        if len(pages[0]) < _ROUND_TRIP_LEGS:
            for i, page in enumerate(pages):
                board = asked.ask(
                    i,
                    partial(
                        _page_board,
                        page,
                        opts,
                        top_n,
                        gf_mode,
                        gf_headed,
                        separate_tickets=separate_tickets,
                    ),
                )
                if board is not None:
                    boards.append(board)
                    extras[i] = (board.insight, board.history, board.facets)
                    dropped += board.dropped
                    unread += board.unread
                    hidden += board.separate_hidden
                    if board.separate_failed is not None:
                        tabs_failed[i] = board.separate_failed
                    _add_stop_drops(stop_drops, board.stop_drops)
        else:
            outbounds = [
                (i, ob)
                for i, page in enumerate(pages)
                if (ob := asked.ask(i, partial(_gflight_outbound, page, opts, gf_mode, gf_headed)))
                is not None
            ]
            # Once a page, so its filter counts each row over the stop ceiling once.
            kept = {i: _kept(ob) for i, ob in outbounds}
            shares = _union_pins(kept, top_n, currency=opts.currency or "USD")
            _mark_tops([ob.board for _, ob in outbounds], kept.values())
            for i, ob in outbounds:
                keys = shares.get(i, [])
                # A page with no pin is still asked for its Cheapest tab: a row
                # sold as separate tickets is its outbound alone.
                asks = bool(keys) or separate_tickets != "off"
                if asks and not keys and asked.stop is not None:
                    tabs_failed[i] = asked.stop
                    asks = False
                board = (
                    asked.ask(
                        i,
                        partial(
                            _page_board,
                            pages[i],
                            opts,
                            len(keys),
                            gf_mode,
                            gf_headed,
                            first=ob.board,
                            prefer=keys,
                            separate_tickets=separate_tickets,
                        ),
                    )
                    if asks
                    else None
                )
                if board is None:
                    left = len(ob.board) - len(kept[i])
                    if not keys or i in asked.unasked:
                        # Its outbounds answered and none of its returns was
                        # asked: their insight, past its filter, is still this
                        # page's.
                        insight = _kept_insight(ob.board.insight, kept[i], left)
                        extras[i] = (insight, ob.board.history, ob.board.facets)
                    dropped += left
                    unread += ob.board.unread
                    _add_stop_drops(stop_drops, ob.stop_drops)
                    # Read though none of its trips is shown: one through its
                    # airports priced above its cap may be missing.
                    unboarded_caps.append(ob.board.capped_at)
                else:
                    boards.append(board)
                    extras[i] = (board.insight, board.history, board.facets)
                    dropped += board.dropped
                    pinned += board.pinned
                    unread += board.unread
                    hidden += board.separate_hidden
                    if board.separate_failed is not None:
                        tabs_failed[i] = board.separate_failed
                    _add_stop_drops(stop_drops, board.stop_drops)
    # Google served the rows of a page none of whose rows parsed, so a flight
    # on one of them is on its board though the page is missing.
    unread += sum(e.unread for e in asked.failed.values() if isinstance(e, _PageUnreadError))
    separate_failed = asked.tab_refusal(tabs_failed)
    asked.report()
    rows = _merged_boards(boards, currency=opts.currency or "USD")
    asked.raise_if_empty(rows)
    if rows and len(pages[0]) >= _ROUND_TRIP_LEGS:
        err.print(
            f"[dim]Google Flights asked this round trip as {len(pages):d} pages of at most "
            f"{MAX_GF_LEG_AIRPORTS:d} airports; each return is priced within its own "
            "page's airports.[/]"
        )
    merged = Board(
        rows,
        dropped=dropped,
        pinned=pinned,
        partial=bool(asked.failed or asked.unasked) or len(pages[0]) >= _ROUND_TRIP_LEGS,
        unread=unread,
        separate_hidden=hidden,
        separate_failed=separate_failed,
        capped_at=lowest_caps(*(b.capped_at for b in boards), *unboarded_caps),
        # For a caller that asks Google more after this board (`_one_way_boards`).
        stopped=asked.stop,
    )
    merged.stop_drops = stop_drops
    answered = [extras[i] for i in sorted(extras)]
    merged.page_insights = tuple(ins for ins, _, _ in answered if ins is not None)
    merged.page_histories = tuple(h for _, h, _ in answered if h is not None)
    merged.page_facets = tuple(f for _, _, f in answered if f is not None)
    return merged


def _add_stop_drops(total: StopDrops, page: StopDrops | None) -> None:
    """Add one page's stop-ceiling count to `total`. Every page is asked for
    the one ceiling the search names."""
    if page is not None and page.rows:
        total.rows += page.rows
        total.ceiling = page.ceiling


def _page_board(
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    gf_mode: GfTransportMode,
    gf_headed: bool,
    *,
    first: Board[Any] | None = None,
    prefer: Sequence[ItineraryKey] = (),
    separate_tickets: SeparateTickets = "off",
) -> Board[Any]:
    """One page's `_gflight_results` as a Board, which carries the counts
    `_gflight_pages` sums: a stand-in for the search can return a plain list.
    A `top_n` of 0 pins nothing, for a round-trip page that only reads its
    Cheapest tab."""
    from ._gflight_ids import Board  # noqa: PLC0415 — fli, ~95 ms

    served = _gflight_results(
        legs,
        opts,
        top_n,
        gf_mode,
        gf_headed,
        first=first,
        prefer=prefer,
        separate_tickets=separate_tickets,
    )
    return served if isinstance(served, Board) else Board(served)


def _browser_scope(gf_mode: GfTransportMode) -> contextlib.AbstractContextManager[None]:
    """One Chrome for every page of a search that can open one, closed once at
    the end: `browser`, or `auto` once it escalates. Nothing opens on a thread
    that never reached rung 2, so the close there is a no-op."""
    if gf_mode == TRANSPORT_HTTP:
        return contextlib.nullcontext()
    from ._gf_browser import session_scope  # noqa: PLC0415 — GF-only

    return session_scope()


def _note_other_currencies(results: list[Any], requested: str) -> None:
    """One stderr line when Google priced rows in a currency other than the one
    asked for. Each such row keeps its own label in the table and the JSON, so
    this is the note that says why two currencies are on one board.

    Called by each path that answers with the board, not by `_gflight_results`:
    a multi-cabin search can still hand its boards to Matrix, and the note would
    then describe rows nobody is shown."""
    other: set[str] = set()
    for r in results:
        for m in cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,):
            ccy = cast("str | None", getattr(getattr(m, "flight", None), "currency", None))
            if ccy and ccy != requested:
                other.add(ccy)
    if other:
        err.print(
            f"[yellow]Google Flights priced some rows in {_safe_text(', '.join(sorted(other)))}, "
            f"not the requested {_safe_text(requested)}; each row is labeled with its own.[/]"
        )


def _gflight_json_row(g: Any, bags: Bags | None = None) -> dict[str, Any]:
    """One `--format json` itinerary: fli's FlightResult plus the two things
    only this backend knows — Google's opaque `flight_id` and the per-leg
    legroom/amenity extract, which the human table shows and `model_dump()`
    alone doesn't carry.

    Under `--bags`, also `bags_included`: the checked and carry-on bags Google
    says this row's price covers, null where it does not say.

    `separate_tickets` is true for a row Google sells as more than one booking,
    a self transfer included, and null where the page does not say; fli's
    `self_transfer` is the subset on which bags are rechecked.

    `top_flight` is true for a row the page that listed it put on Google's Top
    flights board.

    A leg's `departure_airport`/`arrival_airport` are IATA codes and its
    `*_airport_name` fields hold fli's names for those airports."""
    row: dict[str, Any] = {**g.flight.model_dump(mode="json"), "flight_id": g.flight_id}
    if getattr(g, "ticketing", None) is not None:
        row["separate_tickets"] = True
    else:
        row["separate_tickets"] = None if g.flight.self_transfer is None else False
    row["top_flight"] = getattr(g, "top_flight", False)
    legs: list[Any] = row.get("legs") or []
    # fli dumps an `Airport` member by its value, the name; its member name is the code.
    for leg, src in zip(legs, g.flight.legs, strict=False):
        leg["departure_airport"] = src.departure_airport.name
        leg["arrival_airport"] = src.arrival_airport.name
        leg["departure_airport_name"] = src.departure_airport.value
        leg["arrival_airport_name"] = src.arrival_airport.value
    amenities: list[Any] = list(g.amenities)
    # A misaligned extract leaves the surplus legs as fli dumped them.
    for leg, a in zip(legs, amenities, strict=False):
        leg["legroom_class"] = a.legroom_class
        # Same key as fli's own `FlightLeg.amenities`, a different schema. Safe
        # only because `_flight_leg` never populates fli's — if it ever does,
        # this write silently replaces it and needs its own key.
        leg["amenities"] = asdict(a)
    if bags is not None:
        checked, carry_on = cast("tuple[int | None, int | None]", g.bags_included)
        row["bags_included"] = {"checked": checked, "carry_on": carry_on}
    return row


def _gflight_json_document(results: list[Any], bags: Bags | None = None) -> list[Any]:
    """The `--format json` document of a Google board: one row per itinerary,
    a round trip's as its `[outbound, return]` pair, or as `[outbound]` alone
    for a trip on separate tickets whose return Google does not list."""
    out: list[Any] = []
    for r in results:
        items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
        dumped = [_gflight_json_row(g, bags) for g in items]
        out.append(dumped if isinstance(r, tuple) else dumped[0])
    return out


def _record_google_cabin(
    cabin: Cabin, results: list[Any], served: Any, *, bags: Bags | None = None
) -> None:
    """Hand one cabin's Google rows to the envelope run, with the insight,
    history and facets of each page that answered `served`, the board as the
    search returned it. Each row is the object `_gflight_json_document` prints
    for it, priced by its last member: a round trip's fare is the one every
    surface prints for the combination.

    `served.unread` is the board's: the rows its pages served that the parser
    could not read, so the answer is narrower by them. Counted on the board and
    not per page, the note gives the number the cross-check's `google.unread`
    does. A board asked as several pages is `partial` where a page is missing
    or the trip is round, so rows it holds stop short of what was asked; the
    stderr lines that say so are the notes."""
    if not _envelope.active():
        return
    if served and getattr(served, "partial", False):
        _envelope.narrow(of="gflight")
    _note_google_unread(cabin, served)
    printed: list[Any] = json.loads(json.dumps(_gflight_json_document(results, bags), default=str))
    rows: list[_envelope.ResultRow] = []
    for r, row in zip(results, printed, strict=True):
        last = cast("tuple[Any, ...]", r)[-1] if isinstance(r, tuple) else r
        rows.append(
            _envelope.ResultRow(
                price=cast("float | None", last.flight.price),
                currency=cast("str | None", last.flight.currency),
                row=row,
            )
        )
    insight: PriceInsight | None = getattr(served, "insight", None)
    history: PriceHistory | None = getattr(served, "history", None)
    insights: Sequence[PriceInsight] = (
        (insight,) if insight is not None else getattr(served, "page_insights", ())
    )
    histories: Sequence[PriceHistory] = (
        (history,) if history is not None else getattr(served, "page_histories", ())
    )
    own: RouteFacets | None = getattr(served, "facets", None)
    facets: Sequence[RouteFacets] = (
        (own,) if own is not None else getattr(served, "page_facets", ())
    )
    _envelope.record_search(
        backend="gflight",
        cabin=cabin.value,
        rows=rows,
        insights=[
            _envelope.Insight(
                cabin=cabin.value,
                currency=i.currency,
                cheapest=i.cheapest,
                typical_low=i.typical_low,
                typical_high=i.typical_high,
                level=i.level,
            )
            for i in insights
        ],
        histories=[
            _envelope.PriceHistory(
                cabin=cabin.value,
                currency=h.currency,
                points=[_envelope.PricePoint(date=d, price=v) for d, v in h.points],
            )
            for h in histories
        ],
        facets=[
            _envelope.RouteFacets(
                cabin=cabin.value,
                origins=list(f.origins),
                destinations=list(f.destinations),
                currency=f.currency,
                price=_envelope.PriceRange(low=f.price_low, high=f.price_high),
                duration_minutes=_envelope.MinuteRange(low=f.duration_low, high=f.duration_high),
                layover_minutes=_envelope.MinuteRange(low=f.layover_low, high=f.layover_high),
                airlines=[_envelope.CodeName(code=c, name=n) for c, n in f.airlines],
                # Spelled as `--extension 'ALLIANCE …'` takes it, the inverse of
                # how the page is asked for one, so a code passes straight back.
                alliances=[
                    _envelope.CodeName(code=c.lower().replace("_", "-"), name=n)
                    for c, n in f.alliances
                ],
                connecting_airports=[
                    _envelope.ConnectingAirport(code=c, city=n) for c, n in f.connections
                ],
            )
            for f in facets
        ],
    )


def _note_google_unread(cabin: Cabin, served: Any) -> None:
    """Note on the envelope the rows `served`'s pages served that the parser
    could not read: a flight on one of them is on Google's board though no row
    names it, whichever backend then answers."""
    unread: int = getattr(served, "unread", 0)
    if unread:
        _envelope.narrow(
            f"Google Flights: {unread:d} {_CABIN_NAMES[cabin]} rows its pages served "
            "could not be read and are left out of the answer",
            of="gflight",
        )


def _matrix_envelope_rows(res: SearchResult, passengers: int) -> list[_envelope.ResultRow]:
    """`res`'s solutions as envelope rows: each the solution object of Matrix's own
    answer, the body `--format json` prints, or its parsed model where that body
    holds no list matching it. Priced at `party_price`, the total a Google row
    is priced at for a party, so a row with no total Matrix states has none."""
    listed: Any = (res.raw or {}).get("solutionList")
    raw: Any = cast("dict[str, Any]", listed).get("solutions") if isinstance(listed, dict) else None
    objs: list[Any] = (
        cast("list[Any]", raw)
        if isinstance(raw, list) and len(cast("list[Any]", raw)) == len(res.solutions)
        else [s.model_dump(mode="json", by_alias=True, exclude_none=True) for s in res.solutions]
    )
    rows: list[_envelope.ResultRow] = []
    for it, obj in zip(res.solutions, objs, strict=True):
        price = party_price(it, passengers)
        rows.append(
            _envelope.ResultRow(price=parse_price(price), currency=price_currency(price), row=obj)
        )
    return rows


def _calendar_envelope_rows(
    res: CalendarResult, *, sd: date, ed: date
) -> list[_envelope.CalendarRow]:
    """Each priced day of a Matrix calendar as envelope rows, the day object as
    the body `--format json` prints it, with the dates it prices beside it. A
    grid the table prints as empty has none: a day priced under no solutions is
    not a fare the table shows, and the unpriced-dates note names its date.

    The departure is placed as `_window_days` places it, by the month its parsed
    model reads and the body's `year`, and is null where no one date of the window is. The
    return is `_matrix_low`'s: that departure plus the day's cheapest trip
    length, null on a one-way and on a day naming no length."""
    if is_empty_calendar(res):
        return []

    def items(holder: Any, key: str) -> list[Any]:
        found: Any = cast("dict[str, Any]", holder).get(key) if isinstance(holder, dict) else None
        return cast("list[Any]", found) if isinstance(found, list) else []

    rows: list[_envelope.CalendarRow] = []
    for month in items((res.raw or {}).get("calendar"), "months"):
        body = cast("dict[str, Any]", month) if isinstance(month, dict) else {}
        number = CalendarMonth.model_validate(body).month
        year: Any = body.get("year")
        for week in items(month, "weeks"):
            for day in items(week, "days"):
                if not isinstance(day, dict):
                    continue
                fields = cast("dict[str, Any]", day)
                price: Any = fields.get("minPrice")
                if isinstance(price, str) and price and not fields.get("disabled"):
                    parsed = CalendarDay.model_validate(fields)
                    when = _window_date(
                        number,
                        parsed.date,
                        sd,
                        ed,
                        year=year if isinstance(year, int) else None,
                    )
                    option = _cheapest_option(parsed)
                    back = when + timedelta(days=option.trip_length) if when and option else None
                    rows.append(
                        _envelope.CalendarRow.model_validate(
                            {
                                "price": parse_price(price),
                                "currency": price_currency(price),
                                "row": fields,
                                "departure": when,
                                "return": back,
                            }
                        )
                    )
    return rows


def _bags_by_itinerary(
    results: list[Any], sr: SearchResult
) -> dict[int, list[tuple[int | None, int | None]]]:
    """Each itinerary of `sr`, by `id`, to the bags Google states for each of
    its members, in slice order. `sr` is `results` adapted, and the adapter
    carries no itinerary for an empty combination, so neither does this."""
    members = [list(cast("tuple[Any, ...]", r)) if isinstance(r, tuple) else [r] for r in results]
    members = [items for items in members if items]
    return {
        id(it): [cast("tuple[int | None, int | None]", g.bags_included) for g in items]
        for it, items in zip(sr.solutions, members, strict=True)
    }


def _terminal_fare_key(r: Any, *, currency: str) -> tuple[int, str, float]:
    """Sort key for one Google row: `price_rank` of a one-way row's fare, or of
    a round-trip combination's terminal member's, the fare every surface prints
    for the combination. A fare in a currency other than `currency`, the one
    asked for, ranks after every fare in it; a row with no decoded currency is
    read in `currency`, as `_with_board_currency` fills it; an unpriced row is
    last."""
    flight = (cast("tuple[Any, ...]", r)[-1] if isinstance(r, tuple) else r).flight
    price: float | None = flight.price
    shown = None if price is None else f"{flight.currency or currency}{price:.2f}"
    return price_rank(shown, price, currency=currency)


def _ticketings(r: Any) -> tuple[Any, ...]:
    """How Google sells each member of row `r`, in slice order."""
    members = cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,)
    return tuple(getattr(m, "ticketing", None) for m in members)


def _separately_ticketed(r: Any) -> bool:
    """Whether Google sells a member of row `r` as separate tickets."""
    members = cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,)
    return any(getattr(m, "ticketing", None) is not None for m in members)


def _separate_tickets_mode(*, awards_only: bool, no_separate_tickets: bool) -> SeparateTickets:
    """How a Google search reads its Cheapest tab. An awards-only run prints
    no Google row, and its award table matches one-ticket rows only, so the
    tab would buy it nothing."""
    return "off" if awards_only else "hide" if no_separate_tickets else "show"


def _note_unchecked_return(unchecked: str) -> None:
    """Say on stderr that the Cheapest tab went unread for `unchecked`, the
    return check (`_return_checks_google_skips`) its rows could not be held to."""
    err.print(
        "[dim]Itineraries on separate tickets not read: Google lists no return for "
        f"them to check against {_safe_text(unchecked)}.[/]"
    )


def _note_separate_tickets(
    results: Any,
    *,
    gf_mode: GfTransportMode,
    bags: bool,
    unchecked: str | None = None,
    cabin: Cabin | None = None,
) -> None:
    """Say on stderr why the Cheapest tab went unread, or how many of its
    separate-ticket itineraries `--no-separate-tickets` hid. `unchecked` is the
    return check (`_return_checks_google_skips`) it was left unread for, and
    `cabin` the cabin whose tab it was, on a multi-cabin search.

    A tab that failed narrows the answer: it may hold rows the user did not opt
    out of. Hidden rows were opted out of, and a tab left unread for a return
    check holds no row that check can be held to, so both are notes alone."""
    if unchecked is not None:
        _note_unchecked_return(unchecked)
    unread: GfBackendError | None = getattr(results, "separate_failed", None)
    if unread is not None:
        _envelope.narrow(of="gflight")
        # `removesuffix`: a browser refusal's note ends in its remedy's full stop.
        rung = _rung_reached(gf_mode)
        note = _gf_refusal(unread, transport=rung, bags=bags).note.removesuffix(".")
        if cabin is None:
            err.print(f"[dim]Itineraries on separate tickets not read: {note}.[/]")
        else:
            err.print(
                f"[dim]Google Flights {_safe_text(cabin.value)}: itineraries on separate "
                f"tickets not read: {note}.[/]"
            )
    hidden: int = getattr(results, "separate_hidden", 0)
    if hidden:
        err.print(
            "[dim]Google Flights"
            + ("" if cabin is None else f" {_safe_text(cabin.value)}")
            + f": {hidden:d} "
            + ("itinerary" if hidden == 1 else "itineraries")
            + " on separate tickets hidden (--no-separate-tickets).[/]"
        )


def _note_award_skips(separate: int) -> None:
    """Say on stderr how many shown rows on separate tickets the award table,
    which matches one-ticket rows only, leaves out."""
    if separate:
        err.print(
            f"[dim]Awards are matched to one-ticket rows; {separate:d} "
            + ("row on separate tickets is" if separate == 1 else "rows on separate tickets are")
            + " not in the award table.[/]"
        )


def _price_ordered(results: list[Any], *, currency: str) -> list[Any]:
    """A Google answer in price order, fares in `currency`, the one asked for,
    before any other currency's and unpriced rows last; the argument is in the
    memo's `-n` section.

    The sort is stable, so rows sharing a fare keep the order they arrived in:
    the page's on a one-way board, the pins' for combinations."""
    return sorted(results, key=lambda r: _terminal_fare_key(r, currency=currency))


def _pins_row_one(
    search: Search, result: SearchResult | None, *, matrix_url: bool, google_url: bool
) -> bool:
    """Whether `_emit_urls` prints a link pinned to `result`'s first row, the
    row an out-of-range pick falls back to. A link can follow and pin nothing:
    the Google one refuses a row whose flights' dates no source states."""
    return (matrix_url and _try_pinned_matrix_url(search, result, 0) is not None) or (
        google_url and _try_pinned_gflight_url(search, result, 0) is not None
    )


def _pick_in_range(
    pick: int | None, rows: int, *, pin_follows: Callable[[], bool], fare_rules: bool = False
) -> int | None:
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
    emits no link pins nothing, and neither does one whose links cannot pin
    row one (`_pins_row_one`), so `pin_follows` is what keeps the second
    clause from describing something that did not happen — the defect this
    whole reporter exists to avoid, one sentence in. It is asked only once a
    pick has fallen back, because answering it can read rows the render has
    yet to check: one it cannot read pins nothing, and the render reports it.

    A board with NO rows is the case the callers keep away from here rather
    than one this reports, and for the same reason the second clause exists:
    `1-0` is an empty interval, so it cannot say what a valid pick would be,
    and nothing is pinned for a fallback clause to name. Every caller skips
    this on an empty list and prints nothing there.

    The fallback names ROW ONE rather than "the cheapest", because that is what
    every caller of this does with the None: they pin the first row of the list
    the table numbered. That row is the cheapest only when it is priced: a row
    with no fare sorts last, so a list of them has a row one and no cheapest.

    stderr, because a `--format json` document on stdout stays a document —
    the same rule every other note on this path follows.

    `fare_rules` is the other thing a pick chooses: the fare rules shown are
    row one's too, and the clause says so for the same reason."""
    if pick is None or 1 <= pick <= rows:
        return pick
    try:
        pinned = pin_follows()
    except Exception:  # noqa: BLE001 - a row the link builders cannot read pins nothing
        pinned = False
    if pinned and fare_rules:
        fallback = "; pinning itinerary #1 and showing its fare rules instead."
    elif pinned:
        fallback = "; pinning itinerary #1 instead."
    elif fare_rules:
        fallback = "; showing itinerary #1's fare rules instead."
    else:
        fallback = "."
    err.print(f"[yellow]--pick {pick:d} is out of range (1-{rows:d}){fallback}[/]")
    return None


def _refuse_pick_where_nothing_is_numbered(pick: int | None, *, links: bool) -> None:
    """Say once, on stderr, that `--awards-only` gives `pick` no row to name.

    The award renderer is the only surface that mode prints and its columns hold
    no `#`, so a pick there is not out of range, it has no range. The second
    clause is conditional for the reason `_pick_in_range`'s is: where no link
    prints (both links off, or `--format json`), a sentence about the links would
    describe something that does not happen. The caller unpins its links."""
    if pick is None:
        return
    unpinned = "; the links below are unpinned." if links else "."
    err.print(
        f"[yellow]--pick {pick:d} names a row in the results table, and this mode "
        f"prints none{unpinned}[/]"
    )


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
    carried, on the default search path.

    `remedy` is the sentence of `message` that names the user's next move,
    pre-rendered like `message`, for a caller that prints `note` and would
    otherwise leave the user without one. Only the http rung's throttle sets
    it; every other refusal leaves it empty."""

    note: str
    message: str
    remedy: str = ""


_GF_DECLINED = "Google Flights declined the request"


def _rung_reached(gf_mode: GfTransportMode) -> GfTransportMode:
    """The rung this search's Google requests last ran on: `gf_mode`, or the
    browser once `auto` escalated to it, so a refusal is worded as that rung's."""
    from ._gflight_ids import escalated  # noqa: PLC0415 — fli, ~95 ms

    return TRANSPORT_BROWSER if escalated() else gf_mode


def _gf_refusal(  # noqa: PLR0911, PLR0912 — one return per refusal type; see the docstring
    e: GfBackendError,
    *,
    transport: GfTransportMode = TRANSPORT_HTTP,
    bags: bool = False,
    offer_browser: bool = True,
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
    caller does not have. `bags` changes the way out: Matrix prices no bags, so
    under `--bags` it is reached only by dropping them. `offer_browser=False`
    leaves the browser rung out of the http throttle's way out, for a caller
    that is on http because that rung could not open."""
    remedy, remedy_opening = (
        (
            "drop [bold]--bags[/] to search Matrix, which prices no bags",
            "Drop [bold]--bags[/] to search Matrix, which prices no bags",
        )
        if bags
        else ("use [bold]--backend matrix[/]", "Use [bold]--backend matrix[/]")
    )
    match e:
        case GfThrottledError() if transport == TRANSPORT_BROWSER:
            return _GfRefusal(
                "Google Flights rate-limited the browser rung",
                "[yellow]Google Flights rate-limited the browser rung.[/] It does not "
                f"retry, so {remedy}.",
            )
        case GfThrottledError():
            # Not "this IP": the budget is per client context, which is why the
            # browser rung keeps working from an IP that is throttling this one.
            browser = "use [bold]--gf-transport browser[/], " if offer_browser else ""
            retry = f"Wait a moment and retry, {browser}or {remedy}."
            return _GfRefusal(
                "Google Flights rate-limited",
                f"[yellow]Google Flights rate-limited the request.[/] {retry}",
                retry,
            )
        case GfConsentError():
            return _GfRefusal(
                "Google Flights served its consent page",
                "[yellow]Google served its consent page instead of flight results.[/] "
                f"{remedy_opening}.",
            )
        case GfBrowserUnavailableError() if bags and e.remedy.endswith(BROWSER_DEFAULT_REMEDY):
            from ._gflight_ids import browser_remedy as _browser_remedy  # noqa: PLC0415 — fli

            browser_remedy = _browser_remedy(e, bags=True)
            return _GfRefusal(
                f"Google Flights' browser rung is unavailable — "
                f"{_safe_text(e.reason)} {_safe_text(browser_remedy)}",
                f"[yellow]{_safe_text(f'{e.reason} {browser_remedy}')}[/]",
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
                f"[yellow]Google Flights returned HTTP {status}.[/] {remedy_opening}, "
                "or fetch the page the other way with [bold]--gf-transport http[/] or "
                "[bold]browser[/].",
            )
        case GfSearchServerError():
            # Through `_safe_text` for the HTTP arm's reason: nothing holds a
            # caller to the `int` the annotation says.
            code = _safe_text(e.code)
            return _GfRefusal(
                f"Google Flights answered with a server error (status {code})",
                f"[yellow]Google Flights answered with a server error (status {code}).[/] "
                f"Retry later, or {remedy}.",
            )
        case GfPageShapeError():
            return _GfRefusal(
                "Google Flights' page shape changed",
                "[red]Google Flights' page shape changed[/] — no rows could be read. "
                f"{remedy_opening}. ({_safe_text(e)})",
            )
        case GfTfsUnsupportedError():
            # Generic note: the backend picker keeps these queries off Google
            # Flights, so the enrich path never has one to render.
            return _GfRefusal(
                _GF_DECLINED,
                f"[red]Google Flights can't express this search:[/] {escape(e.reason)}. "
                f"{remedy_opening}.",
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
                f"query. {remedy_opening}.",
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
                f"{_safe_text(e)}. {remedy_opening}.",
            )
        case GfBackendError():
            # The base type is raised directly — a 5xx from the search page is
            # the reachable one — so this is a case, not a fallback. It names no
            # wall because it knows none.
            return _GfRefusal(_GF_DECLINED, f"[red]{_GF_DECLINED}:[/] {_safe_text(e)}")
        case _:
            assert_never(e)


_MERGE_SOURCE_TAG = {"both": "GF+MX", "matrix": "MX", "gf": "GF"}


def _render_merged(
    rows: list[Any], *, legs: tuple[Leg, ...], top_n: int, check: CrossCheck | None = None
) -> None:
    """Render the reconciled GF+Matrix view: one row per itinerary with the GF
    and Matrix prices attributed side-by-side and a source tag.

    `check` explains the first `top_n` rows, in order: each one's delta, or why
    it has none, and a caption saying where Matrix's page ends. Without it the
    two columns read "—" and there is no caption.

    A Google price Google sells as separate tickets ends in `†`, or `‡` for a
    self transfer, as on the Google table, with its key under the table."""
    origin = ",".join(legs[0].origins) or "?"
    destination = ",".join(legs[0].destinations) or "?"
    has_return = len(legs) >= _ROUND_TRIP_LEGS
    ccy = _title_currency(p for r in rows[:top_n] for p in (r.matrix_price, r.gf_price))
    b = check.boundary if check is not None else None
    t = Table(
        title=f"Google Flights + Matrix · {_safe_text(origin)}→{_safe_text(destination)}"
        + (" + return" if has_return else "")
        + (f" ({_safe_text(ccy)})" if ccy else ""),
        caption=(
            (
                f"Matrix listed {b.listed:d} of {b.solution_count:d} solutions"
                + (f" (to {_safe_text(b.last_price)})" if b.last_price else "")
                + (
                    f"; Google listed {b.google_listed:d} rows"
                    + (
                        f" and {b.google_separate:d} on separate tickets"
                        if b.google_separate
                        else ""
                    )
                    + (f", {b.google_unread:d} unread" if b.google_unread else "")
                    + "."
                    if b.google_answered
                    else "; Google gave no answer."
                )
                + " delta = Google - Matrix."
            )
            if b is not None
            else None
        ),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("src")
    t.add_column("Matrix", justify="right")
    t.add_column("Google", justify="right")
    t.add_column("delta", justify="right")
    t.add_column("why")
    t.add_column("outbound")
    t.add_column("return")
    explained = check.rows if check is not None else ()
    # The slice count of each shown row Google sells as separate tickets.
    marked: list[int] = []
    for i, row in enumerate(rows[:top_n], 1):
        itn = row.itinerary.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        ticketing = getattr(getattr(row, "google", None), "ticketing", None)
        if ticketing is not None:
            marked.append(len(slcs))
        out = _fmt_slice_cell(slcs[0]) if slcs else "—"
        ret = _fmt_slice_cell(slcs[1]) if len(slcs) > 1 else "—"
        c = explained[i - 1] if i <= len(explained) else None
        delta = c.delta if c is not None else None
        why = c.reason if c is not None else None
        t.add_row(
            f"{i:d}",
            # `rows` is duck-typed, and the lookup falls back to the tag it was
            # handed when it is not one of the three this module writes.
            _safe_text(_MERGE_SOURCE_TAG.get(row.source, row.source)),
            _amount(row.matrix_price, ccy),
            _amount(row.gf_price, ccy)
            + (" ‡" if ticketing == "self_transfer" else " †" if ticketing else ""),
            f"{delta:+,.2f}" if delta is not None else "—",
            # Carrier codes and prices in it are remote text.
            _safe_text(why) if why else "—",
            out,
            ret,
        )
    console.print(t)
    if marked:
        # A one-way row is one slice whether or not it is on separate tickets.
        _print_ticketing_key(outbound_only=has_return and 1 in marked)


def _row_checks(legs: tuple[Leg, ...], opts: SearchOptions | None = None) -> str:
    """What the row filter holds `legs`' rows to, for the sentence that says it
    emptied a board, `opts`' price cap and stop limit among them. Plain text:
    it quotes the user's own codes."""
    from ._gf_postfilter import row_check_names  # noqa: PLC0415 — GF-only
    from .routing_predicates import classify  # noqa: PLC0415

    names = row_check_names(
        [classify(lg.route_language, lg.extension).predicates for lg in legs],
        [lg.time_ranges for lg in legs],
        per_slice_arrivals=[lg.arrival_ranges for lg in legs],
        max_price=opts.max_price if opts is not None else None,
        currency=(opts.currency if opts is not None else None) or "USD",
        max_stops=opts.max_extra_stops if opts is not None else None,
    )
    return _join_reasons(names) or "the routing"


def _return_checks_google_skips(legs: tuple[Leg, ...]) -> str | None:
    """What the row filter holds the later slices of `legs` to that Google's
    query does not: a post-filter predicate, or a time window, which Google
    widens to whole hours. None when there is none, as on a one-way.

    A round trip on separate tickets comes without its return, so it could not
    be held to these."""
    from ._gf_postfilter import row_check_names  # noqa: PLC0415 — GF-only
    from .routing_predicates import Tier, classify  # noqa: PLC0415

    later = legs[1:]
    preds = [
        [p for p in classify(lg.route_language, lg.extension).predicates if p.tier > Tier.GF_NATIVE]
        for lg in later
    ]
    times = [lg.time_ranges for lg in later]
    if not any(preds) and not any(times):
        return None
    # The empty first slice keeps `row_check_names` calling a window the return's.
    names = row_check_names([[], *preds], [(), *times])
    return _join_reasons(names) or "the routing"


def _answer_gf_empty(
    dropped: int,
    *,
    json_out: bool,
    pinned: int = 0,
    checks: str = "the routing",
    cap: str | None = None,
    answer_follows: bool = False,
) -> None:
    """Answer a Google Flights search that has no rows and is not handed on.

    `dropped` is how many served rows the row filter removed, and `checks`
    names what it held them to (`_row_checks`). The reason goes to stderr, so a
    `--format json` stdout is still the one document.

    `pinned` is how many outbounds a round trip searched returns for. The
    reason names it, because the outbounds it did not pin may have matching
    returns that were never searched. `cap` names the price cap the page was
    asked for, if it was, so a board it served empty says no fare is under it.

    `answer_follows` says another writer gives this run's stdout after this:
    the award renderer (the document under `--format json`, the whole answer
    under `--awards-only`) or the `--split` document. Only the stderr reason is
    given here then, as it is in an envelope run, whose envelope is the
    document."""
    if dropped and pinned:
        plural = "" if pinned == 1 else "s"
        err.print(
            f"[yellow]Google Flights: no round trip matched {_safe_text(checks)} "
            f"({dropped:d} rows filtered out; returns were searched for the "
            f"{pinned:d} cheapest outbound option{plural}).[/]"
        )
    elif dropped:
        err.print(
            f"[yellow]Google Flights: no itinerary matched {_safe_text(checks)} "
            f"({dropped:d} rows filtered out).[/]"
        )
    if answer_follows or _envelope.active():
        return
    if json_out:
        # No rows is a value, and a document is what was asked for. A sentence
        # is for a person; to a consumer it is a parse error where an empty
        # answer belongs, and the two are indistinguishable from the exit code.
        sys.stdout.write(json.dumps([], indent=2))
    elif not dropped and cap is not None:
        console.print(f"[yellow]Google Flights: no fare at or under {_safe_text(cap)}.[/]")
    elif not dropped:
        console.print("[yellow]Google Flights: no results.[/]")


def _run_gflight_path(  # noqa: PLR0911, PLR0912, PLR0915 — every outcome of one Google answer, read in one place
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
    sellers: bool = False,
    matrix_fallback: bool = False,
    hand_off_failure: bool = False,
    split: bool = False,
    matrix_remedy: str = "use --backend matrix",
    verify: bool = False,
    rps: float | None = None,
    impersonate: str | None = None,
    separate_tickets: SeparateTickets = "off",
) -> int | None:
    """Google Flights path: build fli filter → query → render. Single-leg or round-trip.

    Returns None once it has answered. The one answer it does not give itself:
    when the routing filter emptied Google's board and `matrix_fallback` is set,
    it prints nothing and returns how many rows the filter dropped, for the
    caller to hand the search to Matrix with that reason. A party with an
    infant that Google served no rows at all is handed on the same way, with
    its own note and a return of 0: Google has answered a route with flights
    with an empty board for any infant. Not handed on, that empty answer
    carries a note saying so and `matrix_remedy`, how to ask Matrix. With
    `hand_off_failure` set, a query that fails is handed on as well: the reason
    is said here, on stderr, in the words the enriched table uses for it, and
    the return is 0 so the caller adds none of its own. An award search the
    filter left with separate-ticket rows alone is handed on the same way. A
    failure that is not handed on exits 1 with stdout empty.

    An empty board that is not handed on still runs the awards when `run_pp`
    is set, so the document an awards run writes has one shape whatever
    Google served.

    When run_pp=True, fli's results are adapted into a SearchResult shape so
    the existing PP matcher + renderer reuse cleanly. PP runs on the same
    (origin, dest, date) per leg as the matrix path.

    This is where `top_n` becomes the answer's size. The query cannot ask for a
    count, so everything below the trim — the table, the JSON document, the
    pinned link and the awards — is drawn from the same `top_n` rows, and
    everything above it reads the whole board. The awards take the first
    `top_n` one-ticket rows instead when a separate-ticket row is among them.

    `gf_mode` defaults to rung 1 — the deprecated `gflight` command has no
    transport flag, so it never asks for another.

    `sellers` adds row `pick`'s booking options after everything else, or
    wraps the JSON document as `{"search": …, "booking_options": …}`.
    `verify` adds row `pick`'s check on Matrix the same way, as
    `{"search": …, "verify": …}`; `rps` and `impersonate` are for its client.

    `separate_tickets` adds ("show") or counts ("hide") the itineraries Google
    sells as separate tickets. Every surface that acts on a row skips one with
    its reason: the award matcher reads the one-ticket rows, `--sellers`
    refuses it, `--verify` answers without asking Matrix, and a link does not
    pin it. A round trip whose return the row filter alone checks reads no
    Cheapest tab (`_return_checks_google_skips`).

    `split` adds the cheapest one-way each way of a round trip on one line
    under the table, empty board included, or wraps the JSON document as
    `{"search": …, "split_ticket": …}`.
    """
    # Deferred like the adapter below: this arm reaches rung 2 only when the
    # transport says so, and the module pulls in nothing patchright at import.
    from ._gf_browser import interrupt_guard  # noqa: PLC0415 — GF-only; see above
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    unchecked = _return_checks_google_skips(legs) if separate_tickets != "off" else None
    try:
        # Armed around the whole search, not around the browser: a Ctrl-C is only
        # answerable while the process still holds the driver, and on this arm
        # the navigation runs on the thread the signal is delivered to.
        with interrupt_guard():
            results = _gflight_results(
                legs,
                opts,
                top_n,
                gf_mode,
                gf_headed,
                separate_tickets="off" if unchecked else separate_tickets,
            )
    except GfBackendError as e:
        refusal = _gf_refusal(e, transport=_rung_reached(gf_mode), bags=opts.bags is not None)
        if hand_off_failure:
            # `removesuffix`, because a browser refusal's note already ends in
            # the full stop its remedy carries and every other refusal's does not.
            note = refusal.note.removesuffix(".")
            err.print(f"[dim]Using Matrix: {note}.[/]")
            return 0
        err.print(refusal.message)
        raise typer.Exit(1) from e
    except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
        raise
    except Exception as e:
        if hand_off_failure:
            note = f"Google Flights query failed: {_safe_text(e)}".removesuffix(".")
            err.print(f"[dim]Using Matrix: {note}.[/]")
            return 0
        err.print(f"[red]Google Flights query failed:[/] {_safe_text(e)}")
        raise typer.Exit(1) from e

    dropped: int = getattr(results, "dropped", 0)
    # Said before the hand-off below: shown or read, these itineraries could
    # have answered a search that now goes to Matrix.
    _note_separate_tickets(
        results,
        gf_mode=gf_mode,
        bags=opts.bags is not None,
        unchecked=unchecked if separate_tickets == "show" else None,
    )
    # Handed on before the pin-cap and currency notes, because both describe
    # Google's answer and Matrix gives this one. Never with `--sellers`: an
    # empty board fails that below, as a board with no row to open.
    infant_board = not (results or dropped) and bool(
        opts.pax.infants_in_seat or opts.pax.infants_in_lap
    )
    if (dropped or infant_board) and matrix_fallback and not sellers:
        if not results:
            _note_google_unread(opts.cabin, results)
            if infant_board:
                err.print(
                    "[dim]Using Matrix: Google Flights served no rows for a party with an "
                    "infant.[/]"
                )
            return dropped
        # The award table matches one-ticket rows, so a board of separate-ticket
        # rows alone answers an award search no better than an empty one.
        if run_pp and all(_separately_ticketed(r) for r in results):
            _note_google_unread(opts.cabin, results)
            err.print(
                "[dim]Using Matrix: no one-ticket Google Flights itinerary matched "
                f"{_safe_text(_row_checks(legs, opts))} ({dropped:d} rows filtered out). "
                f"Awards are matched to one-ticket rows; {len(results):d} "
                + (
                    "itinerary on separate tickets did match, and --cash-only lists it."
                    if len(results) == 1
                    else "itineraries on separate tickets did match, and --cash-only lists them."
                )
                + "[/]"
            )
            return 0
    if infant_board:
        _envelope.narrow(of="gflight")
        err.print(
            "[yellow]Google Flights served no rows for a party with an infant, as it has "
            f"on routes with flights. For Matrix's answer, {_safe_text(matrix_remedy)}.[/]"
        )
    _pin_cap_note(legs=legs, top_n=top_n)
    _note_other_currencies(results, opts.currency or "USD")
    _note_stop_drops(results)
    _note_row_cap(results, opts.currency or "USD")

    if not results and (sellers or verify):
        # Why the board is empty is the search's answer; the flag's own exit
        # below says only that there is no row to take. That exit leaves a
        # `--format json` stdout empty, so no document is written here.
        _answer_gf_empty(
            dropped,
            json_out=json_out,
            pinned=getattr(results, "pinned", 0),
            checks=_row_checks(legs, opts),
            cap=_page_cap_text(opts),
            answer_follows=json_out,
        )
    # Checked before the answer is printed: a `--sellers` pick outside the
    # table is a usage error, not a pin to fall back from, and an empty board
    # leaves nothing to open.
    seller_row = _pick_for_sellers(pick, min(len(results), top_n)) if sellers else None
    verify_row = _pick_for_verify(pick, min(len(results), top_n)) if verify else None
    awards_only = sel.awards_only if sel is not None else False

    def run_awards(rows: list[Any], sr: SearchResult) -> None:
        run_pp_for_search(
            sr,
            legs=_build_pp_legs(legs),
            num_passengers=_seated_pax(opts.pax),
            airlines=sel.pp_airlines() if sel is not None else None,
            cabins=sel.pp_cabins() if sel is not None else None,
            pp_only=awards_only,
            json_out=json_out,
            provider_filter=sel.provider_filter if sel is not None else None,
            seats_sources=sel.seats_sources() if sel is not None else None,
            cash_per_cabin=_cash_per_cabin_single(sr, opts.cabin),
            bags_included=_bags_by_itinerary(rows, sr) if opts.bags is not None else None,
        )

    if not results:
        _record_google_cabin(opts.cabin, results, results)
        _answer_gf_empty(
            dropped,
            json_out=json_out,
            pinned=getattr(results, "pinned", 0),
            checks=_row_checks(legs, opts),
            cap=_page_cap_text(opts),
            answer_follows=(run_pp and (json_out or awards_only)) or (split and json_out),
        )
        if split:
            ticket = _split_ticket(
                legs, opts, top_n, gf_mode, gf_headed, stopped=_search_stop(results)
            )
            if _envelope.active():
                _record_split_ticket(ticket, opts.bags)
            elif json_out:
                doc = _with_split_ticket([], ticket, opts.bags)
                sys.stdout.write(json.dumps(doc, indent=2, default=str))
            else:
                _print_split_ticket(ticket)
        # Award space does not depend on Google's cash board, and the award
        # renderer is what writes an awards run's document, as it does when
        # Matrix's answer is empty.
        if run_pp:
            run_awards(results, fli_results_to_search_result(results))
        return None

    # `-n` is one number for everything the user can act on. Google's page
    # serves its whole board whatever count is asked of it, so the
    # count is a trim rather than a query parameter, and everything below this
    # line is drawn from the same rows: the table, the JSON document, the range
    # `--pick` accepts, the itineraries the award matcher is fanned out over
    # (its one-ticket rows, counted again from the whole board).
    # The trim is HERE rather than in the query because the wide board is what
    # the Tier-2 post-filter above and the multi-cabin join elsewhere are drawn
    # from — narrowing the query would answer a filtered search with fewer rows
    # than exist, which is the failure this backend is most prone to.
    insight = getattr(results, "insight", None)
    served = results
    ordered = _price_ordered(results, currency=opts.currency or "USD")
    results = ordered[:top_n]
    # A pinned link follows only where one is asked for, the format has room for
    # it and row one can be pinned: `--format json` emits no link at all, and a
    # Google row carries no ids a Matrix link could pin. The range is still
    # reported; the fallback is not claimed.
    if awards_only:
        # No numbered table is printed, so the pick names no row and the links
        # below are unpinned, as `_run_enriched_path` does.
        _refuse_pick_where_nothing_is_numbered(
            pick, links=not json_out and (matrix_url or google_url)
        )
        pick = None
    else:
        pick = (seller_row or verify_row) or _pick_in_range(
            pick,
            len(results),
            pin_follows=lambda: (
                not json_out
                and _pins_row_one(
                    SpecificDateSearch(legs=legs, options=opts),
                    fli_results_to_search_result(results),
                    matrix_url=matrix_url,
                    google_url=google_url,
                )
            ),
        )

    # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType,
    #                 reportUnknownArgumentType, reportUnknownParameterType]
    # fli/fast_flights have no type stubs; results are duck-typed pydantic
    # models. Suppressing the noisy unknown-type chatter for this rendering
    # block keeps the boundary localized.
    if _envelope.active():
        _record_google_cabin(opts.cabin, results, served, bags=opts.bags)
        if verify_row is not None:
            check = _verify_document(
                results[verify_row - 1], verify_row, opts, rps=rps, impersonate=impersonate
            )
            _envelope.record_verify(json.loads(json.dumps(check, default=str)))
        if split:
            ticket = _split_ticket(
                legs, opts, top_n, gf_mode, gf_headed, stopped=_search_stop(served)
            )
            _record_split_ticket(ticket, opts.bags)
        if not run_pp:
            return None
    elif json_out and not run_pp:
        out = _gflight_json_document(results, opts.bags)
        if verify_row is not None:
            _write_verified(
                out,
                results[verify_row - 1],
                verify_row,
                opts,
                rps=rps,
                impersonate=impersonate,
            )
            return None
        if split:
            doc: Any = _with_split_ticket(
                out,
                _split_ticket(legs, opts, top_n, gf_mode, gf_headed, stopped=_search_stop(served)),
                opts.bags,
            )
        elif seller_row is not None:
            doc = _search_and_sellers(
                out,
                SpecificDateSearch(legs=legs, options=opts),
                fli_results_to_search_result(results),
                seller_row,
                headed=gf_headed,
            )
        else:
            doc = out
        sys.stdout.write(json.dumps(doc, indent=2, default=str))
        return None

    # `not json_out` as well as `not awards_only`: the early return above fires
    # only with awards OFF, so with them on the document is written further
    # down by the award renderer and every human surface between here and it
    # would land in the same stream. A caller asking for a document gets a
    # document — one, and nothing else — whatever else was asked for beside it.
    if not awards_only and not json_out:
        try:
            _render_gflight_table(
                results,
                legs=legs,
                top_n=top_n,
                match_carriers=_match_carriers(legs),
                insight=insight,
                bags=opts.bags,
                passengers=opts.pax.total,
                currency=opts.currency or "USD",
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
        if split:
            _print_split_ticket(
                _split_ticket(legs, opts, top_n, gf_mode, gf_headed, stopped=_search_stop(served))
            )

    # Always adapt to SearchResult shape so the URL emission has segment
    # info for the pinned link (cheap: just shuffles existing fields).
    sr = fli_results_to_search_result(results)

    if run_pp:
        # The first `-n` one-ticket rows of the whole board, so a separate-ticket
        # row that takes a table row takes none from the award table.
        one_ticket = [r for r in ordered if not _separately_ticketed(r)][:top_n]
        separate = sum(_separately_ticketed(r) for r in results)
        _note_award_skips(separate)
        run_awards(one_ticket, fli_results_to_search_result(one_ticket) if separate else sr)

    # The URL lines are prose on stdout, and `_emit_urls` is shared text that
    # cannot know which format asked for it, so the guard belongs here.
    if not json_out:
        _emit_urls(
            SpecificDateSearch(legs=legs, options=opts),
            matrix_url=matrix_url,
            google_url=google_url,
            result=None if awards_only else sr,
            # The pin LABEL names the row it pins. `sr` is built from the rows
            # the table numbered, so row 1 is the default pin, and it is the
            # cheapest only when Google priced it: on a board of unpriced rows
            # the label "cheapest itinerary" over row 1 is false. `1` makes the
            # label say what the link does on every board.
            pick=pick or 1,
        )
    if seller_row is not None:
        _print_booking_options(
            SpecificDateSearch(legs=legs, options=opts),
            sr,
            seller_row,
            gf_price=sr.solutions[seller_row - 1].price,
            headed=gf_headed,
        )
    if verify_row is not None:
        _print_verified(results[verify_row - 1], verify_row, opts, rps=rps, impersonate=impersonate)
    return None


def _paint_first_gf_table(
    state: dict[str, Any],
    gf: list[Any],
    *,
    legs: tuple[Leg, ...],
    top_n: int,
    awards_only: bool,
    opts: SearchOptions | None = None,
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
            _render_gflight_table(
                gf,
                legs=legs,
                top_n=top_n,
                match_carriers=_match_carriers(legs),
                insight=getattr(gf, "insight", None),
                passengers=opts.pax.total if opts else 1,
                currency=(opts.currency if opts else None) or "USD",
            )
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            raise
        except Exception as e:  # noqa: BLE001 — see the docstring
            state["paint_err"] = e
        else:
            err.print("[dim]…comparing with Matrix's fares…[/]")
    elif not gf and "gf_err" not in state:
        cap = _page_cap_text(opts)
        if getattr(gf, "dropped", 0):
            err.print(
                f"[yellow]Google Flights: no itinerary matched "
                f"{_safe_text(_row_checks(legs, opts))}; awaiting Matrix…[/]"
            )
        elif cap is not None:
            err.print(
                f"[yellow]Google Flights: no fare at or under {_safe_text(cap)}; "
                "awaiting Matrix…[/]"
            )
        else:
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
            # Armed only for the transports that can open a browser, `auto`
            # among them since it escalates a throttle to one. This half runs on
            # a worker no interrupt reaches, so on `http` the first Ctrl-C has
            # nothing to free and an ignored second one takes away the only
            # thing that could end the process.
            with interrupt_guard(armed=gf_mode != TRANSPORT_HTTP):
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
    json_out: bool = False,
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

    `transport` is the search's, read as the rung it reached (`_rung_reached`).
    Every wording below is dispatched with it, or the browser rung's throttle —
    which has no retry ladder to wait for — reaches the default search path
    telling the user to wait a moment and try again.

    `json_out` is the cross-check document, where stdout holds the document
    alone and the rows Matrix answered are all it explains."""
    if not isinstance(e, GfBackendError):
        err.print(f"[yellow]Google Flights query failed:[/] {_safe_text(e)}")
    else:
        refusal = _gf_refusal(e, transport=_rung_reached(transport))
        if matrix_answered and json_out:
            err.print(f"[yellow]{refusal.note}[/] — the cross-check holds Matrix's rows only.")
        elif matrix_answered and not awards_only:
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


# Matrix answers in price order, and a deeper page costs no measurable time, so
# the cross-check's one request asks for a page that holds Matrix's whole answer:
# a Google row is then compared with every trip Matrix found, not its first `-n`.
_CROSS_CHECK_PAGE = 500

# How long the cross-check waits on Matrix for Google's low row. The merged
# table is printed before it starts, so the wait delays only the line under it.
_LOW_CHECK_SECONDS: float = 60


def _cross_check_answers(
    state: dict[str, Any],
    board: SearchResult,
    matrix_res: SearchResult,
    *,
    uncapped: SearchResult,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    currency: str,
) -> Answers:
    """The two answers a weave left, as the cross-check reads them, with
    Matrix's page before the price cap as `uncapped`. Google's board is no
    answer where its half failed or never ran, and a board the row filter cut
    cannot show a flight absent from what Google served. A board asked as
    several pages is `partial` where a page is missing or the trip is round
    (`_gflight_pages`). The board's `unread`, the rows Google served that the
    parser could not read, is read as `dropped` is."""
    stops = opts.max_extra_stops
    return Answers(
        matrix=matrix_res,
        uncapped=uncapped,
        google=board if "gf" in state and "gf_err" not in state else None,
        google_filtered=bool(getattr(state.get("gf"), "dropped", 0)),
        google_partial=bool(getattr(state.get("gf"), "partial", False)),
        stop_limit=stops is not None and stops >= 0,
        round_trip=len(legs) >= _ROUND_TRIP_LEGS,
        currency=currency,
        passengers=opts.pax.total,
        google_unread=getattr(state.get("gf"), "unread", 0),
    )


def _cross_check_blocker(
    *, run_awards: bool, awards_only: bool, sellers: bool, split: bool
) -> str | None:
    """Why `--enrich --format json` cannot write its document, or None. The
    document is one search's cash rows explained against Matrix's; the award,
    booking-option and split-ticket documents are each a different one."""
    if awards_only:
        return "cross-checks cash fares; drop --awards-only"
    if run_awards:
        return "cross-checks cash fares only; add --cash-only"
    if sellers:
        return "writes no booking options; drop --sellers"
    if split:
        return "writes no split ticket; drop --split"
    return None


def _answer_cross_check_document(
    state: dict[str, Any],
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    top_n: int,
    currency: str,
    gf_mode: GfTransportMode,
    rps: float,
    impersonate: str,
) -> None:
    """Write the weave's answer as `{"search": …, "cross_check": …}`.

    `search` is the document `--format json` writes for this search, from the
    same `top_n` rows; `cross_check` is the merged table's rows, explained.
    Each half fails on its own: a failed Google half leaves `search` empty and
    every Matrix row saying why, a failed Matrix half leaves `cross_check` null
    with its reason on stderr, and with both failed stdout stays empty and the
    exit is 1. An envelope run records the two halves in place of the write."""
    from ._enrich import merge_results  # noqa: PLC0415 — as in `_run_enriched_path`
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    gf: list[Any] = state.get("gf") or []
    google_answered = "gf" in state and "gf_err" not in state
    matrix_res = state.get("matrix")
    if "gf_err" in state:
        _report_enriched_gf_failure(
            state["gf_err"],
            matrix_answered=matrix_res is not None,
            awards_only=False,
            transport=gf_mode,
            json_out=True,
        )
        if matrix_res is not None:
            # `results` holds Google's rows alone, so no backend answered it.
            alone = "Google Flights failed, and cross_check holds Matrix's rows alone"
            _envelope.explain("backend", alone)
            _envelope.explain("results", alone)
    if (
        google_answered
        and not (gf or getattr(state["gf"], "dropped", 0))
        and (opts.pax.infants_in_seat or opts.pax.infants_in_lap)
    ):
        # Google has served an infant no rows on a route with flights, so its
        # empty board is not the route's answer.
        infant = (
            "Google Flights served no rows for a party with an infant, as it has on routes "
            "with flights"
        )
        _envelope.narrow()
        _envelope.explain(
            "results",
            infant if matrix_res is None else f"{infant}, and cross_check holds Matrix's rows",
        )
    checked: dict[str, Any] | None = None
    if matrix_res is None:
        _report_search_matrix_failure(state)
        _envelope.narrow()
        _envelope.explain("cross_check", "the Matrix search failed")
        if not google_answered:
            raise typer.Exit(1)
    else:
        page = cast("SearchResult", matrix_res)
        matrix_res = _price_capped(page, opts, passengers=opts.pax.total)
        _report_weave_aftermath(state)
        board = fli_results_to_search_result(gf)
        merged = merge_results(board, matrix_res, currency=currency, passengers=opts.pax.total)
        shown = merged[:top_n]
        answers = _cross_check_answers(
            state, board, matrix_res, uncapped=page, legs=legs, opts=opts, currency=currency
        )
        checked = cross_check_document(shown, cross_check(shown, answers))
        low = _low_check(
            merged,
            board,
            gf,
            uncapped=page,
            top_n=top_n,
            opts=opts,
            currency=currency,
            rps=rps,
            impersonate=impersonate,
        )
        checked["low_check"] = _low_check_document(low)
    rows = _price_ordered(gf, currency=currency)[:top_n]
    if _envelope.active():
        if google_answered:
            served = state.get("gf")
            _record_google_cabin(opts.cabin, rows, served, bags=opts.bags)
        if checked is not None:
            _envelope.record_cross_check(json.loads(json.dumps(checked, default=str)))
        return
    search = _gflight_json_document(rows, opts.bags)
    sys.stdout.write(json.dumps({"search": search, "cross_check": checked}, indent=2, default=str))


class _LowCheck(NamedTuple):
    """Matrix asked for row `n`'s exact flights, which Google prices at
    `google` under `matrix_low`, Matrix's cheapest fare in its own answer.
    `matrix_price` is Matrix's price for the party on a match; `reason`
    says why there is none."""

    n: int
    row: _verify.Row
    google: str
    matrix_low: str | None
    outcome: Literal["match", "other-itinerary", "no-solution", "no-answer"]
    matrix_price: str | None = None
    reason: str | None = None


class _UncheckableAnswerError(Exception):
    """Matrix answered the chain in a shape that cannot be read flight by flight."""


_DETAILS_WITHOUT_FLIGHTS = (
    "Matrix returned booking details without their flights, "
    "so this itinerary cannot be checked flight by flight."
)
_DETAILS_SHORT_OF_A_FLIGHT = (
    "Matrix returned booking details that do not state every flight's "
    "number, airports and times, so this itinerary cannot be checked "
    "flight by flight."
)
_SUMMARY_SHORT_OF_A_SLICE = (
    "Matrix listed an itinerary that does not state every slice's flights, "
    "airports and times, so it cannot be checked flight by flight."
)


def _states_every_slice(solution: Itinerary, row: _verify.Row) -> bool:
    """Whether Matrix's summary of `solution` states each of `row`'s slices:
    its flights, stops, end airports and times. The chain admits only the
    row's flights, so a summary naming others has left one out. Legs connect
    at one airport fewer than there are of them, and either side may write a
    through flight as one leg, so the stops are at least the longer side's
    legs less one."""
    slices = solution.itinerary.slices if solution.itinerary else []
    return len(slices) == len(row.slices) and all(
        _verify.routing([_verify._token(f) for f in s.flights])  # pyright: ignore[reportPrivateUsage] — the token form candidates compare
        == _verify.routing([f.code for f in legs])
        and len(s.stops) >= max(len(s.flights), len(legs)) - 1
        and all(p is not None and p.code for p in (s.origin, s.destination, *s.stops))
        and _verify.wall_clock(s.departure)
        and _verify.wall_clock(s.arrival)
        for s, legs in zip(slices, row.slices, strict=True)
    )


def _flights_of(legs: Sequence[_verify.Flight]) -> list[tuple[str, str, str]]:
    """Each flight in a slice's legs, by number and the airports it leaves and
    reaches. Consecutive legs under one number are one through flight, which
    either side may split; one whose legs do not join end to end has lost a
    leg, so it has no airports."""
    out: list[tuple[str, str, str]] = []
    for code, run in groupby(legs, key=lambda f: f.code):
        flown = list(run)
        joined = all(a.destination == b.origin for a, b in pairwise(flown))
        out.append((code, flown[0].origin, flown[-1].destination) if joined else (code, "", ""))
    return out


def _states_every_flight(itinerary: BookedItinerary, row: _verify.Row) -> bool:
    """Whether booking details state each of `row`'s flights, slice by slice,
    between the airports the row flies it, and every leg's carrier, number,
    airports and times. A candidate's summary has the row's flights and
    airports, so details short of them have left a flight or a leg out."""
    booked = _verify.booked_flights(itinerary)
    return len(booked) == len(row.slices) and all(
        _flights_of(b) == _flights_of(legs) and all(all(f) for f in b)
        for b, legs in zip(booked, row.slices, strict=True)
    )


def _read_whole(
    chain: SearchResult, row: _verify.Row, read: Sequence[BookingDetails | None]
) -> None:
    """Raise `_UncheckableAnswerError` unless `chain` is Matrix's whole answer
    on `row`'s flights, every summary on it states the row's slices, and
    `read`, the booking details of each candidate that is not the row, in
    Matrix's order, state every flight. Short of that, the row's own itinerary
    may be one the answer leaves out, so no solution being the row does not
    show that Matrix prices these flights only on other itineraries, or not at
    all."""
    for details in read:
        itinerary = details.itinerary if details is not None else None
        if itinerary is None:
            raise _UncheckableAnswerError(_DETAILS_WITHOUT_FLIGHTS)
        if not _states_every_flight(itinerary, row):
            raise _UncheckableAnswerError(_DETAILS_SHORT_OF_A_FLIGHT)
    listed = len(chain.solutions)
    # An empty answer leaves out its zero count; a listed one without its
    # count may stop short of it.
    if listed and "solutionCount" not in (chain.raw or {}):
        raise _UncheckableAnswerError(
            f"Matrix listed {listed:d} itinerar{'y' if listed == 1 else 'ies'} on these "
            "flights without saying how many it found, and none listed is these exact flights."
        )
    if chain.solution_count > listed:
        raise _UncheckableAnswerError(
            f"Matrix listed only {listed:d} of its {chain.solution_count:d} itineraries "
            "on these flights, and none listed is these exact flights."
        )
    if not all(_states_every_slice(s, row) for s in chain.solutions):
        raise _UncheckableAnswerError(_SUMMARY_SHORT_OF_A_SLICE)


async def _exact_flights_on(
    c: MatrixClient, row: _verify.Row, opts: SearchOptions
) -> _verify.Verdict:
    """`row` asked of Matrix as exactly its flights: the chain search, uncached
    because booking details are asked of its session, then one booking-details
    call per candidate in Matrix's order. No fare rules and no unrouted second
    search: this answers only whether Matrix prices these flights."""
    chain = cast(
        "SearchResult",
        await c.execute(
            SpecificDateSearch(
                legs=_verify.matrix_legs(row), options=_verify.matrix_options(row, opts)
            ),
            cache=False,
        ),
    )
    idxs = _verify.candidates(row, chain)
    read: list[BookingDetails | None] = []
    if idxs:
        session, solution_set = chain.session, chain.solution_set
        sids = [sid for sid in (chain.solutions[i].id for i in idxs) if sid]
        if not (session and solution_set and len(sids) == len(idxs)):
            raise _UncheckableAnswerError(
                "Matrix answered without a session for these flights, "
                "so they cannot be checked flight by flight."
            )
        for i, sid in zip(idxs, sids, strict=True):
            answer = await c.booking_details(
                session=session, solution_set=solution_set, solution_id=sid
            )
            details = answer.booking_details
            itinerary = details.itinerary if details is not None else None
            if itinerary is not None and _verify.same_flights(row, itinerary):
                return _verify.Verdict("match", solution=chain.solutions[i], details=details)
            read.append(details)
    _read_whole(chain, row, read)
    if not chain.solutions:
        return _verify.Verdict("no-solution", "Matrix returned no fare on these exact flights")
    return _verify.other_itinerary(len(chain.solutions))


def _low_check_failure(e: Exception) -> str:
    """Each failure inside `e` by its kind and message. An HTTP status error
    is named by its status line alone: its own text quotes the request URL,
    which carries the API key."""
    parts: list[str] = []
    for f in _failures_inside(e) or [e]:
        if isinstance(f, MatrixApiError):
            parts.append(f"Matrix returned an error ({f.kind}): {f.message}")
        elif isinstance(f, httpx.HTTPStatusError):
            status = f"HTTP {f.response.status_code:d} {f.response.reason_phrase}".strip()
            parts.append(f"Matrix answered {status}")
        else:
            parts.append(f"{type(f).__name__}: {f}" if str(f) else type(f).__name__)
    return "; ".join(parts)


def _ask_low_row(
    n: int,
    google: str,
    fli_row: Any,
    opts: SearchOptions,
    *,
    matrix_low: str | None,
    rps: float,
    impersonate: str,
) -> _LowCheck:
    """Row `n` asked of Matrix as exactly its flights, within
    `_LOW_CHECK_SECONDS`. One event loop holds the whole conversation, so the
    bound cancels whichever request is in flight. The client may not
    re-bootstrap its key, which runs synchronously: a 403 is the check's
    no-answer. Every failure is the outcome
    "no-answer": the table is already printed, and the exit code stays the
    search's."""
    row = _verify.google_row(fli_row)
    bound = f"{_LOW_CHECK_SECONDS:g}"
    err.print(
        f"[dim]Asking Matrix for row {n:d}'s exact flights (at most {_safe_text(bound)} s)…[/]"
    )

    async def go() -> _verify.Verdict | None:
        async with MatrixClient(rps=rps, impersonate=impersonate, rebootstrap=False) as c:
            with anyio.move_on_after(_LOW_CHECK_SECONDS):
                return await _exact_flights_on(c, row, opts)
        return None

    def unanswered(reason: str) -> _LowCheck:
        return _LowCheck(n, row, google, matrix_low, "no-answer", reason=reason)

    try:
        verdict = anyio.run(go)
    except (typer.Exit, typer.Abort):
        raise
    except _UncheckableAnswerError as e:
        return unanswered(str(e))
    except Exception as e:  # noqa: BLE001 — no failure of the check may change the search's outcome
        return unanswered(_low_check_failure(e))
    if verdict is None:
        return unanswered(f"Matrix did not answer within {bound} s")
    if verdict.outcome == "match" and verdict.solution is not None:
        return _LowCheck(
            n,
            row,
            google,
            matrix_low,
            "match",
            matrix_price=party_price(verdict.solution, opts.pax.total),
        )
    outcome = "other-itinerary" if verdict.outcome == "other-itinerary" else "no-solution"
    return _LowCheck(n, row, google, matrix_low, outcome, reason=verdict.reason)


def _low_check(
    merged: list[Any],
    board: SearchResult,
    gf: list[Any],
    *,
    uncapped: SearchResult,
    top_n: int,
    opts: SearchOptions,
    currency: str,
    rps: float,
    impersonate: str,
) -> _LowCheck | None:
    """Matrix asked for the exact flights of the first Google-only row the
    table shows under every fare in Matrix's own answer, or None where no row
    is, which asks Matrix nothing more. A row whose listing Google books in a
    cabin other than the search's is passed over, as Matrix is asked in the
    search's cabin. A Matrix fare with no price for the party in `currency`
    cannot be compared, so it leaves no row under every fare. It is looked for
    in `uncapped`, Matrix's page before the price cap, because the cap drops
    such a fare from `merged`."""
    from ._enrich import merge_results  # noqa: PLC0415 — as in `_run_enriched_path`
    from ._gf_postfilter import states_other_cabin  # noqa: PLC0415 — GF-only

    whole = merge_results(board, uncapped, currency=currency, passengers=opts.pax.total)
    if not every_matrix_price_in(whole, currency):
        return None
    matrix_low = lowest_matrix_price(merged, currency)
    # `board` holds one itinerary per fli result, in order, less an empty
    # round trip, so the row's own result is found by the itinerary's identity.
    results = cast("list[Any]", [r for r in gf if not (isinstance(r, tuple) and not r)])
    if len(results) != len(board.solutions):
        return None
    listed = {id(it): r for it, r in zip(board.solutions, results, strict=True)}

    def in_cabin(row: Any) -> bool:
        listing = listed.get(id(row.google))
        return listing is None or not states_other_cabin(listing, opts.cabin)

    n = low_row(merged[:top_n], matrix_low, currency, comparable=in_cabin)
    if n is None:
        return None
    chosen = merged[n - 1]
    fli_row = listed.get(id(chosen.google))
    if fli_row is None or chosen.gf_price is None:
        return None
    return _ask_low_row(
        n,
        chosen.gf_price,
        fli_row,
        opts,
        matrix_low=matrix_low,
        rps=rps,
        impersonate=impersonate,
    )


def _print_low_check(lc: _LowCheck) -> None:
    """The one line under the merged table: Matrix's price for row N's exact
    flights beside Google's, both for the party, or why Matrix does not price
    them as those flights, or that it gave no answer."""
    trip = "; ".join(
        f"{chain} {legs[0].departure[:10]}"
        for chain, legs in zip(_verify.routings(lc.row), lc.row.slices, strict=True)
    )
    if lc.outcome == "match":
        answer = (
            f"Matrix {lc.matrix_price or '—'} · Google {lc.google}"
            f"{_price_gap(lc.google, lc.matrix_price)}"
        )
    elif lc.outcome == "no-answer":
        answer = f"no answer: {lc.reason}"
    else:
        answer = f"not priced as these flights: {lc.reason}"
    console.print(
        f"Matrix asked for row {lc.n:d}'s flights ({_safe_text(trip)}): {_safe_text(answer)}",
        style="yellow" if lc.outcome == "no-answer" else None,
    )


def _low_check_document(lc: _LowCheck | None) -> dict[str, Any] | None:
    """`cross_check.low_check`, null where no row was checked. Prices are for
    the party and `delta` is Google's minus Matrix's, on a match only."""
    if lc is None:
        return None
    return {
        "row": lc.n,
        "google_low": lc.google,
        "matrix_low": lc.matrix_low,
        "outcome": lc.outcome,
        "matrix_price": lc.matrix_price,
        "delta": _verify.delta(lc.google, lc.matrix_price),
        "reason": lc.reason,
        "routing": _verify.routings(lc.row),
    }


def _run_enriched_path(  # noqa: PLR0912, PLR0915 — one weave's outcome arms, read in one place
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
    sellers: bool = False,
    split: bool = False,
    json_out: bool = False,
    separate_tickets: SeparateTickets = "off",
) -> None:
    """GF-serveable query, progressive: dispatch Google Flights + Matrix
    concurrently under one event loop, paint GF immediately (~1s), then repaint a
    reconciled GF+Matrix table once Matrix lands (~45s). PP/awards + URLs run on
    Matrix's first `top_n` fares. `--fast` skips this for GF-only speed.

    Where a Google-only row is under every fare in Matrix's own answer,
    `_low_check` asks Matrix for the first such row's exact flights, and the
    answer is the line under the table (or `cross_check.low_check`).

    `sellers` opens row `pick` of the merged table, the one the Google link
    pins, or of the Google table when Matrix does not answer, and prints its
    booking options last.

    `split` asks for the cheapest one-way each way once Google's round trip
    has answered, while Matrix is still in flight, and prints the pair under
    whichever table is the answer.

    `json_out` paints nothing and writes the comparison as one document
    (`_answer_cross_check_document`); the caller has refused awards and
    `sellers` beside it.

    `separate_tickets` is `_run_gflight_path`'s: the Google half also reads
    the Cheapest tab, in the worker that already runs beside Matrix, and its
    separate-ticket rows are marked on both tables and never priced against
    Matrix. As there, a round trip whose return the row filter alone checks
    reads no Cheapest tab."""
    # Imported here rather than deeper in: every enriched run executes these two
    # lines, so a packaging fault in either module fails the same way on every
    # run instead of only on the runs where Matrix happens to land.
    from ._enrich import merge_results  # noqa: PLC0415
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    unchecked = _return_checks_google_skips(legs) if separate_tickets != "off" else None
    _pin_cap_note(legs=legs, top_n=top_n)
    # Matrix is asked in the currency Google is asked in, so the merged table
    # ranks like with like: left unset, Matrix prices in its own default (GBP
    # from LHR) while Google prices in USD. Only this path merges the two.
    requested = opts.currency or "USD"
    matrix_search = SpecificDateSearch(
        legs=legs, options=opts.model_copy(update={"currency": requested})
    )
    # Only the request is deeper: the links and the booking page keep `-n`.
    asked = matrix_search.model_copy(
        update={
            "options": matrix_search.options.model_copy(
                update={"page_size": max(top_n, _CROSS_CHECK_PAGE)}
            )
        }
    )
    awards_only = sel.awards_only if sel is not None else False
    state: dict[str, Any] = {}

    async def _go() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_matrix_into, state, asked, rps, impersonate, not no_cache)
            # Google Flights is sync (curl_cffi) — run it in a worker thread so the
            # Matrix request progresses concurrently on the event loop.
            gf: list[Any]
            try:
                gf = await anyio.to_thread.run_sync(
                    partial(
                        _gflight_results,
                        legs,
                        opts,
                        top_n,
                        gf_mode,
                        gf_headed,
                        separate_tickets="off" if unchecked else separate_tickets,
                    )
                )
            except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
                raise
            except Exception as e:  # noqa: BLE001 - reported below; Matrix may still succeed
                state["gf_err"] = e
                gf = []
            state["gf"] = gf
            _note_other_currencies(gf, requested)
            _note_stop_drops(gf)
            # With Google's board empty the merged table is Matrix's, and the
            # line would describe a board nobody is shown. The document's
            # `search` half is Google's board, emptied or not.
            if gf or json_out:
                _note_row_cap(gf, requested)
            _note_separate_tickets(
                gf,
                gf_mode=gf_mode,
                bags=opts.bags is not None,
                # A failed query is reported on its own, and its note says why
                # nothing of Google's was read.
                unchecked=(
                    unchecked if separate_tickets == "show" and "gf_err" not in state else None
                ),
            )
            # A document paints no table, as an awards-only run does; the
            # empty-board note still goes to stderr, where it is true.
            _paint_first_gf_table(
                state, gf, legs=legs, top_n=top_n, awards_only=awards_only or json_out, opts=opts
            )
            if split and "gf_err" not in state:
                state["split"] = await anyio.to_thread.run_sync(
                    partial(
                        _split_ticket,
                        legs,
                        opts,
                        top_n,
                        gf_mode,
                        gf_headed,
                        stopped=_search_stop(gf),
                    )
                )

    _run_the_weave(_go, state, gf_mode)

    if json_out:
        _answer_cross_check_document(
            state,
            legs=legs,
            opts=opts,
            top_n=top_n,
            currency=requested,
            gf_mode=gf_mode,
            rps=rps,
            impersonate=impersonate,
        )
        return
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
        if split:
            _report_no_split("the Google Flights round trip failed, so no one-way was asked")
    if matrix_res is None:
        # Every stash is read here: the paint failure is part of why nothing
        # reached the user, and the Matrix one IS the outcome.
        if state.get("paint_err") is not None:
            _report_paint_failure(state["paint_err"])
        _report_search_matrix_failure(state)
        if sellers and not painted:
            _no_booking_options("No booking options", "no table was printed to pick a row from.")
        if sellers:
            # The Google table painted first is the only numbered one on
            # screen, so the pick names its row, as it does under `--fast`.
            rows = _price_ordered(gf, currency=requested)[:top_n]
            n = _pick_for_sellers(pick, len(rows))
            sr = fli_results_to_search_result(rows)
            _print_booking_options(
                matrix_search,
                sr,
                n,
                gf_price=sr.solutions[n - 1].price,
                headed=gf_headed,
            )
        if not painted:
            raise typer.Exit(1)
        if split:
            _print_split_ticket(state.get("split", _SPLIT_UNFINISHED))
        return
    # Before the merge and the award overlay, so neither sees a fare over the cap.
    page = cast("SearchResult", matrix_res)
    matrix_res = _price_capped(page, opts, passengers=opts.pax.total)
    _report_weave_aftermath(state)

    # Repaint: reconciled GF + Matrix, prices attributed.
    #
    # `shown` is the list the user saw numbered, which is what `--pick N` names
    # and what the pin label claims. The merged rows are price-sorted and can
    # include Google-only itineraries, so their order and their length both
    # differ from `matrix_res.solutions`: indexing those instead pins a row the
    # table numbered differently, under the number read off the screen.
    pinnable: SearchResult | None = None
    booking_row: tuple[SearchResult, int, str | None, str | None] | None = None
    if not awards_only:
        board = fli_results_to_search_result(gf)
        merged = merge_results(board, matrix_res, currency=requested, passengers=opts.pax.total)
        answers = _cross_check_answers(
            state, board, matrix_res, uncapped=page, legs=legs, opts=opts, currency=requested
        )
        _render_merged(merged, legs=legs, top_n=top_n, check=cross_check(merged[:top_n], answers))
        if split and "gf_err" not in state:
            _print_split_ticket(state.get("split", _SPLIT_UNFINISHED))
        shown = [r.itinerary for r in merged[:top_n]]
        seller_row = _pick_for_sellers(pick, len(shown)) if sellers else None
        # The same contract `_run_gflight_path` has: the range a pick is
        # measured against is the VISIBLE count. Clamping here also means the
        # duplicate warning inside `_emit_urls` is never reached from this path.
        #
        # An empty board is numbered nowhere, so it gets no clamp sentence at
        # all: `(1-0)` is an empty interval that cannot say what a valid pick
        # would be, and the fallback clause beside it would name a pin that does
        # not happen — this arm renders a header-only table and carries on where
        # the sibling has already returned.
        pinnable = matrix_res.model_copy(update={"solutions": shown})
        pick = seller_row or (
            _pick_in_range(
                pick,
                len(shown),
                pin_follows=lambda: _pins_row_one(
                    matrix_search, pinnable, matrix_url=matrix_url, google_url=google_url
                ),
            )
            if shown
            else None
        )
        if seller_row is not None:
            chosen = merged[seller_row - 1]
            booking_row = (pinnable, seller_row, chosen.gf_price, chosen.matrix_price)
        # After the picks are checked, so a pick that exits does not wait on
        # Matrix first; their notes go to stderr, and the line stays under the table.
        low = _low_check(
            merged,
            board,
            gf,
            uncapped=page,
            top_n=top_n,
            opts=opts,
            currency=requested,
            rps=rps,
            impersonate=impersonate,
        )
        if low is not None:
            _print_low_check(low)
    else:
        # Nothing this arm prints carries a row number: the award renderer is
        # the only surface it has and its columns hold no `#`. So a pick names
        # no row here — not one out of range, one that does not exist — and it
        # is refused rather than clamped. The links stay unpinned for the same
        # reason: with no numbered list, neither `itinerary #N` nor `cheapest
        # itinerary` is a label the user could check against anything.
        _refuse_pick_where_nothing_is_numbered(pick, links=matrix_url or google_url)
        pick = None

    if run_pp:
        # `pinnable` holds the rows the merged table numbered.
        if pinnable is not None:
            _note_award_skips(sum(it.ticketing is not None for it in pinnable.solutions))
        # Matrix's page is deeper than `-n` only to explain the table.
        firsts = matrix_res.model_copy(update={"solutions": matrix_res.solutions[:top_n]})
        _overlay_awards(firsts, legs=legs, opts=opts, sel=sel, awards_only=awards_only)

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
    if booking_row is not None:
        result, n, gf_price, matrix_price = booking_row
        _print_booking_options(
            matrix_search,
            result,
            n,
            gf_price=gf_price,
            matrix_price=matrix_price,
            headed=gf_headed,
        )


# ─────────────────────────── multi-cabin orchestration ─────────────────────

# When --cabin selects multiple cabins, each cabin's per-query top-N is bumped
# so the client-side merge has overlap to work with. Cheapest economy and
# cheapest business on a given route are often different carriers entirely
# (e.g. JFK-LHR: VS in economy, FI in business) — a top-5 query per cabin
# almost never overlaps, leaving the J column rendered as all "—".
#
# On Google Flights the page serves its whole board whatever the page size,
# and a round trip's pinned fan-out is clamped by
# `_gflight_ids._PINNED_FANOUT_CAP` whatever this returns — see that constant
# for the budget and its cost. A Google round trip's cabins overlap because
# every cabin pins the sort cabin's outbounds (`_CabinSearches`), not because
# of the bump.
#
# Capped to bound response size (each itinerary costs bytes + parse time);
# Matrix and gflight both tolerate page sizes in this range comfortably.
_MULTI_CABIN_QUERY_BUMP_FACTOR = 5
_MULTI_CABIN_QUERY_BUMP_CAP = 100


def _pin_cap_note(*, legs: tuple[Leg, ...], top_n: int) -> None:
    """Say so when a round trip will search fewer outbounds than were asked for.

    A round trip prices returns against its cheapest outbounds, and the number
    of those is capped however large `-n` is. Without a word the user reads a
    short table as the market rather than as the budget, so every round-trip
    path says it: the enriched one, `--fast`, `--format json` and multi-cabin
    alike. A search handed to Matrix does not, because the table it
    prints is Matrix's.

    Cheapest, because the pin loop takes the filtered board's outbounds in
    price order (`_gflight_ids._pins`), and an outbound row's price is already
    the cheapest round trip through it.

    "Up to", because the cap bounds the count and the board may hold fewer. The
    exact number is known only once the pin loop has run; an empty filtered
    round trip states it (`_answer_gf_empty`), where it changes what the answer
    means.

    stderr, so a `--format json` document on stdout stays a document."""
    from ._gflight_ids import pinned_fanout  # noqa: PLC0415

    pins = pinned_fanout(top_n)
    if len(legs) >= _ROUND_TRIP_LEGS and pins < top_n:
        _envelope.narrow(of="gflight")
        err.print(
            f"[dim]Google Flights combines returns against up to {pins:d} cheapest outbounds.[/]"
        )


def _multi_cabin_join_note(pins: int, leader: Cabin | None) -> str:
    """Why a cabin cell can be empty on a multi-cabin round trip.

    `leader` is the cabin whose outbounds every cabin was asked to pin, or None
    when none led and each cabin pinned its own. The count comes from the pin
    budget rather than a literal, because the sentence is only true while they
    agree: the cap is what decides how many outbounds the join can see, and
    `-n` below it lowers the number further.

    A led '—' is not always a board that lacks the itinerary: a filter such as
    `--max-price` can remove a fare the board lists, a return board can be
    refused, and a row from a follower's own outbounds was never searched in
    the sort cabin. "Returned no fare" is true of all of them. Every part is
    ours, so there is nothing here to escape."""
    if leader is None:
        return (
            f"Google Flights joins cabins on up to {pins} of each cabin's cheapest "
            "outbounds; '—' means no shared itinerary, not no fare."
        )
    return (
        f"Google Flights prices every cabin on up to {pins} of the "
        f"{_CABIN_TO_LETTER[leader]} cabin's cheapest outbounds; "
        "'—' means that cabin's search returned no fare for the itinerary."
    )


def _bumped_query_top_n(top_n: int, cabin_count: int) -> int:
    """Per-cabin query page size for a multi-cabin search.

    Single-cabin invocations get `top_n` unchanged. Multi-cabin gets
    `top_n * factor` capped at the bump ceiling. The visible row count
    after merge is still `top_n` (renderer trims by sort cabin) — the
    bump only widens the search space the join can draw from, which is
    Matrix's page. Google Flights serves its whole board whatever this is, and
    caps a round trip's pins on their own budget.
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
    applied uniformly. A fare in any currency but USD is left out, so no ¢/mi
    is computed from it.
    """
    name = _CABIN_TO_PP_NAME[query_cabin]
    out: dict[int, dict[str, float]] = {}
    for it in res.solutions:
        cash = _usd_amount(it.price)
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
            cash = _usd_amount(price)
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


class _CabinBoards(dict[Cabin, list[Any]]):
    """Each cabin's answer from a Google Flights multi-cabin fan-out.

    `leader` is the cabin whose outbounds every cabin was asked to pin, or None
    when each pinned its own. The fan-out reports it rather than a board,
    because a sort cabin whose every return board was refused has no board and
    still led."""

    def __init__(
        self, boards: Mapping[Cabin, list[Any]] | None = None, *, leader: Cabin | None = None
    ) -> None:
        super().__init__(boards or {})
        self.leader = leader


class _CabinSearches(NamedTuple):
    """The per-cabin calls of one Google Flights multi-cabin fan-out.

    On a round trip over two or more cabins the sort cabin leads: every cabin
    pins the outbounds the sort cabin pins, wherever its own filtered board
    lists them, and fills the rest of the same budget with its own cheapest
    rows. A cabin pinning its own ten cheapest may price none of the
    itineraries the table shows, which are the sort cabin's. The sort cabin's
    page is fetched ahead of its pins and handed back to its search, so the
    GETs are the ones each cabin would spend alone. Anything else is each
    cabin's whole search.

    `separate_tickets` is each cabin's Cheapest-tab mode, as a one-cabin
    search sets it (`_gflight_results`): one GET more per cabin."""

    legs: tuple[Leg, ...]
    opts: SearchOptions
    cabins: tuple[Cabin, ...]
    top_n: int
    gf_mode: GfTransportMode
    gf_headed: bool
    leader: Cabin
    separate_tickets: SeparateTickets = "off"

    @property
    def shares_pins(self) -> bool:
        return len(self.legs) >= _ROUND_TRIP_LEGS and len(self.cabins) > 1

    def outbound(self, cab: Cabin) -> Callable[[], _Outbound]:
        return partial(_gflight_outbound, self.legs, self._opts(cab), self.gf_mode, self.gf_headed)

    def pins(self, lead: _Outbound | None) -> list[ItineraryKey]:
        """The outbounds the sort cabin pins from its page `lead`: the ones it
        would take alone, so its rows are unchanged. Empty with no page, or
        nothing its filter kept, and then each cabin pins its own cheapest
        rows."""
        if lead is None:
            return []
        from ._gflight_ids import pin_keys  # noqa: PLC0415 — fli, ~95 ms

        return pin_keys(lead.board, top_n=self.top_n, keep=lead.keep)

    def search(self, cab: Cabin) -> Callable[[], list[Any]]:
        """`cab`'s whole search."""
        return partial(
            _gflight_results,
            self.legs,
            self._opts(cab),
            self.top_n,
            self.gf_mode,
            self.gf_headed,
            separate_tickets=self.separate_tickets,
        )

    def led(
        self, cab: Cabin, pins: Sequence[ItineraryKey], page: _Outbound | None = None
    ) -> Callable[[], list[Any]]:
        """`cab`'s search pinning `pins` first, from `page` when that was
        fetched ahead of it."""
        return partial(
            _gflight_results,
            self.legs,
            self._opts(cab),
            self.top_n,
            self.gf_mode,
            self.gf_headed,
            first=None if page is None else page.board,
            prefer=pins,
            separate_tickets=self.separate_tickets,
        )

    def _opts(self, cab: Cabin) -> SearchOptions:
        return self.opts.model_copy(update={"cabin": cab})


def _gflight_cabins_in_series(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    top_n: int,
    gf_headed: bool,
    sort_by: Cabin | None = None,
    separate_tickets: SeparateTickets = "off",
) -> _CabinBoards | None:
    """Rung 2's multi-cabin shape: one Chrome, one cabin at a time, on this thread.

    The parallel fan-out below cannot run rung 2. A thread per cabin is a
    session per cabin, and Chromium single-instances the profile directory, so
    the second cabin fails on the first one's lock. Serialising here is what
    lets ONE session serve every cabin: the launch is paid once, and every
    navigation runs on the thread that made the session.

    A round trip's sort cabin goes first, its page and then its pins, because
    every other cabin pins its outbounds (`_CabinSearches`); each other cabin
    follows as one whole search. Fetching every cabin's page ahead instead
    would load pages a fallback to rung 1 then loads again.

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
    came from two different rungs is not one answer. The sort cabin's page is
    not yet rows, so a Chrome that loads it and dies on its pins still moves
    the search to rung 1.
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

    def serve_cabin[T](cab: Cabin, call: Callable[[], T], *, served: bool) -> T | None:
        """`call`'s answer, or None once why this cabin's column is missing has
        been printed. A rung that never opened is raised instead while nothing
        is `served`, for the whole fan-out to move to rung 1."""
        try:
            return call()
        except GfBrowserUnavailableError as e:
            # Ahead of the `GfBackendError` arm below, which is its base
            # class and would otherwise report a rung that never opened as
            # one cabin's missing column.
            if not served:
                raise
            note_missing_column(cab, e)
        except GfBackendError as e:
            note_missing_column(cab, e)
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            raise
        except Exception as e:  # noqa: BLE001 — fli has no documented exception surface
            err.print(f"[yellow]Google Flights {cab.value} query failed: {_safe_text(e)}[/]")
        return None

    plan = _CabinSearches(
        legs,
        opts,
        cabins,
        top_n,
        TRANSPORT_BROWSER,
        gf_headed,
        sort_by or cabins[0],
        separate_tickets,
    )
    results: dict[Cabin, list[Any]] = {}
    pins: list[ItineraryKey] = []
    with shared_throttle_ladder(), interrupt_guard(), session_scope():
        try:
            if plan.shares_pins:
                lead = serve_cabin(plan.leader, plan.outbound(plan.leader), served=False)
                pins = plan.pins(lead)
                calls = {cab: plan.led(cab, pins) for cab in cabins if cab != plan.leader}
                if lead is not None:
                    calls = {plan.leader: plan.led(plan.leader, pins, lead), **calls}
            else:
                calls = {cab: plan.search(cab) for cab in cabins}
            for cab, call in calls.items():
                answer = serve_cabin(cab, call, served=bool(results))
                if answer is not None:
                    results[cab] = answer
        except GfBrowserUnavailableError as e:
            # The phrase leads the line so that no console width can break it.
            # The remedy follows the reason because the other half of it —
            # install Chrome, point the binary — is what a user whose http rung
            # is also refused has left to try.
            err.print(
                f"[dim]multi-cabin is using http: {_safe_text(e.reason)} {_safe_text(e.remedy)}[/]"
            )
            return None
    # In the order the cabins were asked for, which the sort cabin going first
    # would otherwise change for `--format json`.
    return _CabinBoards(
        {cab: results[cab] for cab in cabins if cab in results},
        leader=plan.leader if pins else None,
    )


def _run_gflight_multi(
    *,
    legs: tuple[Leg, ...],
    opts: SearchOptions,
    cabins: tuple[Cabin, ...],
    top_n: int,
    gf_mode: GfTransportMode = TRANSPORT_HTTP,
    gf_headed: bool = False,
    sort_by: Cabin | None = None,
    separate_tickets: SeparateTickets = "off",
) -> _CabinBoards:
    """Fan out N parallel gflight queries (one per cabin). fli is sync, so
    each query runs in a worker thread via `anyio.to_thread.run_sync`.

    Each cabin runs the SAME query builder as the single-cabin path, so the
    native filters and the Tier-2 post-filter cannot drift apart. They also
    share ONE throttle ladder: Google's wall is per-IP, so a cabin per thread
    laddering against it separately spends the cabin count times the requests to
    be told the same thing. They are one search, too, so they share one window
    of pauses for Google's server errors (`search_escalation`). On a round trip
    (`_CabinSearches`, led by `sort_by`, default the first cabin) every cabin's
    outbound page, then every cabin's pins, are two fan-outs inside that one
    ladder and one event loop."""
    from ._gflight_ids import search_escalation, shared_throttle_ladder  # noqa: PLC0415

    if gf_mode == TRANSPORT_BROWSER:
        served = _gflight_cabins_in_series(
            legs=legs,
            opts=opts,
            cabins=cabins,
            top_n=top_n,
            gf_headed=gf_headed,
            sort_by=sort_by,
            separate_tickets=separate_tickets,
        )
        if served is not None:
            return served

    # Rung 1 for every cabin below: the caller asked for it, rung 2 could not
    # open at all and said so, or `auto` asked, which does not escalate here. A
    # mode that can open a browser reaching the fan-out would open a session per
    # worker thread, which is the profile-lock collision the series runner
    # exists to avoid.
    plan = _CabinSearches(
        legs, opts, cabins, top_n, TRANSPORT_HTTP, gf_headed, sort_by or cabins[0], separate_tickets
    )
    results = _CabinBoards()

    async def query_cabin[T](cab: Cabin, call: Callable[[], T], into: dict[Cabin, T]) -> None:
        try:
            into[cab] = await anyio.to_thread.run_sync(call)
        except GfBackendError as e:
            # A typed refusal is why this cabin's column will be missing; the
            # bare handler below would print it as an unexplained failure.
            # The options shape only the remedy; the note reads the same under
            # every one of them. This fan-out runs under `browser` only when
            # that rung could not open, so the remedy must not send the user
            # back to it.
            refusal = _gf_refusal(e)
            remedy = _gf_refusal(
                e, bags=opts.bags is not None, offer_browser=gf_mode != TRANSPORT_BROWSER
            ).remedy
            tail = f" {remedy}" if remedy else ""
            err.print(f"[yellow]Google Flights {cab.value}: {refusal.note}.{tail}[/]")
        except (typer.Exit, typer.Abort):  # an orderly exit is not a failure
            # `typer.Exit` subclasses `RuntimeError` on the installed click, so
            # the arm below would swallow the stop and print the exit CODE as
            # this cabin's error message.
            raise
        except Exception as e:  # noqa: BLE001 — fli has no documented exception surface
            err.print(f"[yellow]Google Flights {cab.value} query failed: {_safe_text(e)}[/]")

    async def fan_out[T](calls: dict[Cabin, Callable[[], T]], into: dict[Cabin, T]) -> None:
        async with anyio.create_task_group() as tg:
            for cab, call in calls.items():
                tg.start_soon(query_cabin, cab, call, into)

    async def go() -> None:
        if not plan.shares_pins:
            await fan_out({cab: plan.search(cab) for cab in cabins}, results)
            return
        # Every cabin's page first, in parallel: a cabin's pins wait on the sort
        # cabin's page, not on its whole search.
        pages: dict[Cabin, _Outbound] = {}
        await fan_out({cab: plan.outbound(cab) for cab in cabins}, pages)
        pins = plan.pins(pages.get(plan.leader))
        results.leader = plan.leader if pins else None
        await fan_out(
            {cab: plan.led(cab, pins, pages[cab]) for cab in cabins if cab in pages}, results
        )

    with shared_throttle_ladder(), search_escalation():
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
    # In the order the cabins were asked for, not the order they finished in,
    # which `--format json` would otherwise list them in.
    return _CabinBoards(
        {cab: results[cab] for cab in cabins if cab in results}, leader=results.leader
    )


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
    passengers: int = 1,
    slices: int = 1,
    results_by_cabin: dict[Cabin, SearchResult] | None = None,
    currency: str = "USD",
    total_of: Callable[[Itinerary], str | None] | None = None,
    bag_mark: Callable[[Itinerary], str] | None = None,
    insights: Mapping[Cabin, PriceInsight] | None = None,
) -> None:
    """Render multi-cabin merged rows. One row per itinerary, one price column
    per requested cabin, '—' for missing.

    For a party of `passengers`, each cell prints the row's total for that
    cabin; a cabin with no total prints one passenger's price, starred.
    A row Google sells as separate tickets ends each of its prices in `†`, or
    `‡` for a self transfer, as the one-cabin table does, and the key follows.
    `slices` is how many the search asked for: such a row with fewer is a
    round trip's outbound alone.

    `results_by_cabin` is what `rows` were merged from, in `currency`, the one
    the merge ranked by: each cabin's own cheapest the table does not show is
    named under it (`_print_own_cheapest`), at its total from `total_of`, the
    one the merge was given, for a party.

    Under `--bags`, `bag_mark` marks each price with whether the cell's own
    listing includes the bags asked for, ahead of `†` or `‡`, and a key line
    follows. `insights` are Google's price insights by cabin, one line each
    after every other line, as the one-cabin table prints its one."""
    if not rows:
        console.print("[yellow]No itineraries.[/]")
        return
    ccy = _title_currency(p for row in rows for p in row.prices.values())
    ccy_tag = f" ({_safe_text(ccy)})" if ccy else ""
    cabin_labels = "+".join(_CABIN_TO_LETTER[c] for c in cabins)
    sort_label = _CABIN_TO_LETTER[sort_by]
    party = passengers > 1
    starred = False
    marked = [row.itinerary for row in rows if row.itinerary.ticketing is not None]
    shows_mark = bool(marked) or bag_mark is not None

    t = Table(
        # `title_prefix` is a parameter: its value is chosen by whoever calls, and
        # a claim about every present and future caller is not one this function
        # can keep. The two callers pass a literal, so the wrap costs nothing.
        title=f"{_safe_text(title_prefix)} · {cabin_labels} (sorted by {sort_label}){ccy_tag}"
        + (f" · total for {passengers:d} travelers" if party else ""),
        show_header=True,
        header_style="bold green",
    )
    t.add_column("#", justify="right")
    t.add_column("carriers")
    slice_counts = [len(r.itinerary.itinerary.slices) for r in rows if r.itinerary.itinerary]
    count = max([_ROUND_TRIP_LEGS, *slice_counts])
    for header in _slice_headers(count):
        t.add_column(header)
    for letter in (_CABIN_TO_LETTER[c] for c in cabins):
        # Folded: a party cell squeezed by Rich's default ellipsis loses its last
        # digits and the star that marks a per-traveler fare. Unwrapped where a
        # mark is shown, so it stays on its amount's line.
        t.add_column(
            f"{letter} total{ccy_tag}" if party else f"{letter}{ccy_tag}",
            justify="right",
            overflow="fold" if party else "ellipsis",
            no_wrap=shows_mark,
        )
    _keep_cells_whole(t, count)

    for i, row in enumerate(rows, 1):
        itn = row.itinerary.itinerary
        slcs: list[Slice] = itn.slices if itn else []
        # Wrapped per code, as `_render_search` does with the same field.
        carriers = ",".join(_safe_text(c.code or "?") for c in (itn.carriers if itn else []))

        slice_cells = _slice_cells(slcs, count)
        price_cells: list[str] = []
        ticketing = row.itinerary.ticketing
        mark = " ‡" if ticketing == "self_transfer" else " †" if ticketing else ""
        for cab in cabins:
            total = row.totals.get(cab)
            if not party:
                cell = _amount(row.prices.get(cab), ccy)
            elif total or cab not in row.prices:
                cell = _amount(total, ccy)
            else:
                # No space before the star: Rich wraps a narrow cell at its spaces.
                cell = f"{_amount(row.prices[cab], ccy)}*"
                starred = True
            priced = row.prices.get(cab)
            bag = f" {bag_mark(row.listings[cab])}" if priced and bag_mark is not None else ""
            price_cells.append(cell + (bag + mark if priced else ""))
        t.add_row(f"{i:d}", carriers or "?", *slice_cells, *price_cells)
    console.print(t)
    if starred:
        console.print("* per traveler: Matrix states no total for the party")
    if results_by_cabin is not None:
        named = _print_own_cheapest(
            rows,
            results_by_cabin,
            cabins=cabins,
            sort_by=sort_by,
            currency=currency,
            passengers=passengers,
            total_of=total_of,
            bag_mark=bag_mark,
        )
        marked.extend(it for it in named if it.ticketing is not None)
    if bag_mark is not None:
        console.print(
            "Bags: ✓ the fare includes the bags asked for, ✗ it does not, ? Google does not say."
        )
    if marked:
        _print_ticketing_key(
            outbound_only=any(
                len(it.itinerary.slices if it.itinerary else []) < slices for it in marked
            )
        )
    _print_cabin_insights(cabins, insights or {})


def _print_cabin_insights(
    cabins: tuple[Cabin, ...], insights: Mapping[Cabin, PriceInsight]
) -> None:
    """The one-cabin table's price insight line, named for its cabin, for each
    of `cabins` that has one, in that order."""
    for cab in cabins:
        if (insight := insights.get(cab)) is not None:
            console.print(
                f"Price insight for {_safe_text(_CABIN_TO_LETTER[cab])}: prices are "
                f"{_safe_text(insight.level)} for this trip "
                f"(usually {_safe_text(insight.currency)}{insight.typical_low:.2f}"
                f"-{_safe_text(insight.currency)}{insight.typical_high:.2f})."
            )


def _named_own_cheapest(
    rows: list[MultiCabinRow],
    results_by_cabin: dict[Cabin, SearchResult],
    *,
    cabins: tuple[Cabin, ...],
    sort_by: Cabin,
    currency: str,
) -> dict[Cabin, tuple[Itinerary, float]]:
    """Each cabin but `sort_by` whose own cheapest listing (`cheapest`: in
    `currency`, else in the first other currency by code that the cabin is
    priced in) is priced below every fare its column shows in that listing's
    currency, or whose column shows none in it, with that listing and its
    amount, in `cabins` order. No rate is known, so two currencies' fares never
    compare.

    The one test of what the line under the multi-cabin table names
    (`_print_own_cheapest`) and of what a document adds to a cabin's rows
    (`_cabin_document_rows`): a listing that ties a fare the column shows is
    named by neither."""
    named: dict[Cabin, tuple[Itinerary, float]] = {}
    for cab in cabins:
        res = results_by_cabin.get(cab)
        own = cheapest(res, currency=currency) if cab != sort_by and res is not None else None
        amount = parse_price(own.price) if own is not None else None
        if own is None or amount is None:
            continue
        own_currency = price_currency(own.price) or currency
        # Compared on `prices`, the basis `merge` ranks on: a party's column can
        # mix totals with starred one-traveler prices, and the two do not compare.
        shown = [
            parse_price(p)
            for row in rows
            if (p := row.prices.get(cab)) and price_currency(p) == own_currency
        ]
        if not any(p is not None and p <= amount for p in shown):
            named[cab] = (own, amount)
    return named


def _print_own_cheapest(
    rows: list[MultiCabinRow],
    results_by_cabin: dict[Cabin, SearchResult],
    *,
    cabins: tuple[Cabin, ...],
    sort_by: Cabin,
    currency: str,
    passengers: int = 1,
    total_of: Callable[[Itinerary], str | None] | None = None,
    bag_mark: Callable[[Itinerary], str] | None = None,
) -> list[Itinerary]:
    """One line under the multi-cabin table for each cabin
    `_named_own_cheapest` names, and the listings named.

    For a party of `passengers`, the line names the listing's total
    (`total_of`), as the party's column prints its cells, or one traveler's
    price where it has none, and says which. Under `--bags`, `bag_mark` marks
    the amount as the table marks a cell.

    The table prices every cabin on the sort cabin's itineraries, so another
    cabin's cheapest fare can be on an itinerary no row shows, while a document
    of the same search lists it as that cabin's first row."""
    named = _named_own_cheapest(
        rows, results_by_cabin, cabins=cabins, sort_by=sort_by, currency=currency
    )
    for cab, (own, amount) in named.items():
        own_currency = price_currency(own.price) or currency
        letter = _CABIN_TO_LETTER[cab]
        mark = " ‡" if own.ticketing == "self_transfer" else " †" if own.ticketing else ""
        flights = " / ".join(
            "+".join(s.flights) for s in (own.itinerary.slices if own.itinerary else [])
        )
        total = total_of(own) if passengers > 1 and total_of is not None else None
        summed = parse_price(total) if total else None
        if passengers <= 1:
            basis, figure, code = "", amount, own_currency
        elif summed is not None:
            basis = f", total for {passengers:d} travelers"
            figure, code = summed, price_currency(total) or own_currency
        else:
            basis, figure, code = ", per traveler", amount, own_currency
        bag = f" {bag_mark(own)}" if bag_mark is not None else ""
        console.print(
            f"{_safe_text(letter)}'s own cheapest{_safe_text(basis)}: {_safe_text(code)}"
            f"{figure:.2f}{_safe_text(bag)}{_safe_text(mark)} ({_safe_text(flights)}), "
            "on no row above; "
            f"--sort {_safe_text(_CABIN_FLAG_NAMES[cab])} lists {_safe_text(letter)}'s "
            "cheapest first.",
            soft_wrap=True,
        )
    return [own for own, _ in named.values()]


def _validate_sort_cabin(sort_by: Cabin, cabins: tuple[Cabin, ...]) -> None:
    if sort_by not in cabins:
        names = ", ".join(c.value for c in cabins)
        err.print(f"[red]--sort {sort_by.value!r} must be one of --cabin: {names}[/]")
        raise typer.Exit(2)


def _note_google_rows_unshown(cabins: Iterable[Cabin], *, cap: str | None = None) -> None:
    """Name each cabin Google Flights had rows for that Matrix, answering the
    search in its place, returned no itinerary for: those rows are not in the
    output. Under a `cap`, Matrix may have returned fares the cap removed, so
    the line says Matrix shows none under it."""
    names = ", ".join(c.value for c in cabins)
    if not names:
        return
    _envelope.narrow()
    if cap is None:
        err.print(
            f"[yellow]Matrix returned no itinerary for {_safe_text(names)}, where Google "
            "Flights had rows; --backend gflight shows them.[/]"
        )
    else:
        err.print(
            f"[yellow]Matrix shows no itinerary at or under {_safe_text(cap)} for "
            f"{_safe_text(names)}, where Google Flights had rows; --backend gflight "
            "shows them.[/]"
        )


def _cabins_capped(
    results_by_cabin: dict[Cabin, SearchResult], opts: SearchOptions
) -> dict[Cabin, SearchResult]:
    """Each cabin's Matrix answer cut to the fares under the search's cap
    (`_price_capped`), each cabin it leaves with no fare named on stderr.

    A cabin keeps Matrix's own count where the cap dropped a fare it could not
    read, one stating no total in the cap's currency, so only a count of zero
    says no fare is under the cap; otherwise the line says those fares are not
    shown."""
    capped = {
        cab: _price_capped(res, opts, passengers=opts.pax.total)
        for cab, res in results_by_cabin.items()
    }
    if (cap := _cap_text(opts)) is not None:
        for cab, res in capped.items():
            if res.solutions:
                continue
            if res.solution_count == 0:
                err.print(
                    f"[yellow]Matrix {_safe_text(cab.value)}: no fare at or under "
                    f"{_safe_text(cap)}.[/]"
                )
            else:
                err.print(
                    f"[yellow]Matrix {_safe_text(cab.value)}: no fare that states a "
                    f"{_safe_text(opts.currency or 'USD')} total is at or under "
                    f"{_safe_text(cap)}; those that state none are not shown.[/]"
                )
    return capped


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
    google_answered: tuple[Cabin, ...] = (),
) -> None:
    """Matrix multi-cabin: N parallel cabin queries → client-side join → render.

    `google_answered` names the cabins Google Flights had rows for when the
    search was handed here; any of them Matrix shows no itinerary for, by
    failing, by finding none or by the cap removing all it found, is named on
    stderr, since the hand-off already set Google's rows aside.

    Under a cap, each cabin's answer is asked and cut as `_run_matrix_path`
    cuts its one answer, and a cabin left with no fare says so on stderr."""
    # As in `_run_matrix_path`: left unset, Matrix prices in its own default
    # currency and the cap keeps nothing.
    if opts.max_price is not None:
        opts = opts.model_copy(update={"currency": opts.currency or "USD"})
    # Widen each per-cabin query so the join has overlap to render — top_n
    # rows visible after merge, but each cabin's underlying query pulls
    # `_bumped_query_top_n` candidates. See _bumped_query_top_n docstring.
    query_opts = opts.model_copy(update={"page_size": _bumped_query_top_n(top_n, len(cabins))})
    try:
        results_by_cabin = _run_matrix_multi(
            legs=legs,
            opts=query_opts,
            cabins=cabins,
            rps=_resolve_rps(rps),
            impersonate=_resolve_impersonate(impersonate),
            no_cache=_resolve_no_cache(no_cache),
        )
    except typer.Exit:
        _note_google_rows_unshown(google_answered)
        raise
    # Before anything reads them, so the documents, the join, the lines under
    # the table and the awards all draw from the fares under the cap.
    results_by_cabin = _cabins_capped(results_by_cabin, opts)
    found = {c for c, r in results_by_cabin.items() if r.solutions}
    _note_google_rows_unshown((c for c in google_answered if c not in found), cap=_cap_text(opts))
    if not results_by_cabin:
        err.print("[red]All cabin queries failed.[/]")
        raise typer.Exit(1)

    if _envelope.active():
        # Each cabin's whole answer, as the `{cabin: raw}` document carries it.
        for cab, res in results_by_cabin.items():
            _envelope.record_search(
                backend="matrix", cabin=cab.value, rows=_matrix_envelope_rows(res, opts.pax.total)
            )
        if not run_pp:
            return
    elif json_out and not run_pp:
        # JSON shape: {cabin: raw} so consumers can re-merge if they want.
        sys.stdout.write(
            json.dumps({c.value: r.raw for c, r in results_by_cabin.items()}, indent=2)
        )
        return

    # Left unset, Matrix prices in its own default (GBP from LHR): the join
    # ranks, and names each cabin's cheapest, in the currency it answered in.
    currency = (
        opts.currency
        or _title_currency(it.price for res in results_by_cabin.values() for it in res.solutions)
        or "USD"
    )
    total_of = partial(party_price, passengers=opts.pax.total)
    rows = _merge_cabins(
        results_by_cabin, sort_by=sort_by, top_n=top_n, currency=currency, total_of=total_of
    )
    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
        _render_multi_cabin_search(
            rows,
            cabins=cabins,
            sort_by=sort_by,
            passengers=opts.pax.total,
            results_by_cabin=results_by_cabin,
            currency=currency,
            total_of=total_of,
        )

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


class _MatrixHandOff(NamedTuple):
    """A multi-cabin search Google Flights hands WHOLE to Matrix."""

    emptied: dict[Cabin, int]  # each cabin the routing emptied, with the rows it dropped
    answered: tuple[Cabin, ...]  # each cabin Google had rows for, now set aside


def _cabin_document_rows(
    board: list[Any],
    rows: list[MultiCabinRow],
    cabin: Cabin,
    top_n: int,
    *,
    own: Itinerary | None,
    currency: str,
) -> list[Any]:
    """`cabin`'s Google board as a multi-cabin document carries it: its `top_n`
    cheapest rows, then, in price order, each other row whose fare the joined
    table `rows` prints in `cabin`, and `own`, the cabin's own cheapest listing
    where the table names it under the cabin (`_named_own_cheapest`).

    The table prices every cabin on the sort cabin's itineraries, so a fare it
    prints can sit far down another cabin's board; without it, the document
    and the table of one search would hold different fares. The `top_n` are
    cheapest in `currency`, the one asked for, so a row Google priced in another
    currency fills them only after every row in it. The count is the user's,
    not the bumped one the cabins were queried at, which only gives the join
    overlap. A listing is found by its itinerary key and price, the first such
    row in price order, as the join keeps it."""
    from .pp.gflight_adapter import fli_results_to_search_result  # noqa: PLC0415

    def listing(r: Any) -> tuple[object, str | None] | None:
        adapted = fli_results_to_search_result([r]).solutions
        return (itinerary_key(adapted[0]), adapted[0].price) if adapted else None

    ordered = _price_ordered(board, currency=currency)
    carried = ordered[:top_n]
    wanted = {
        (itinerary_key(row.itinerary), price)
        for row in rows
        if (price := row.prices.get(cabin)) is not None
    }
    if own is not None and own.price is not None:
        wanted.add((itinerary_key(own), own.price))
    wanted -= {listing(r) for r in carried}
    for r in ordered[top_n:]:
        if not wanted:
            break
        if (found := listing(r)) in wanted:
            wanted.discard(found)
            carried.append(r)
    return carried


def _run_gflight_path_multi(  # noqa: PLR0912 — one arm per surface the boards are written to
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
    matrix_fallback: bool = False,
    separate_tickets: SeparateTickets = "off",
) -> _MatrixHandOff | None:
    """Google Flights multi-cabin: N cabin queries → join → render.

    Parallel on rung 1 and serial on rung 2; `_run_gflight_multi` chooses.

    `separate_tickets` reads each cabin's Cheapest tab as a one-cabin search
    does (`_run_gflight_path`), so each cabin's board holds the itineraries
    Google sells as separate tickets ("show") or counts them ("hide"). The
    award matcher reads the one-ticket rows of the table alone.

    Returns None once it has answered. When the routing filter emptied any
    cabin's board and `matrix_fallback` is set, it prints nothing to stdout and
    no note on Google's table, and returns the hand-off for the caller to give
    the WHOLE search to Matrix: a per-cabin hand-off would put Google's rows and
    Matrix's documents in one answer and join prices from two sources. A cabin
    Google served nothing for is Google's answer and is not handed on."""
    # Widen per-cabin queries so the join has overlap; see _bumped_query_top_n.
    query_top_n = _bumped_query_top_n(top_n, len(cabins))
    unchecked = _return_checks_google_skips(legs) if separate_tickets != "off" else None
    fli_by_cabin = _run_gflight_multi(
        legs=legs,
        opts=opts,
        cabins=cabins,
        top_n=query_top_n,
        gf_mode=gf_mode,
        gf_headed=gf_headed,
        sort_by=sort_by,
        separate_tickets="off" if unchecked else separate_tickets,
    )
    if not fli_by_cabin:
        err.print("[red]All Google Flights cabin queries failed.[/]")
        raise typer.Exit(1)
    # In the order the cabins were asked for; the fan-out fills `fli_by_cabin`
    # in the order they finish.
    emptied: dict[Cabin, int] = {
        cab: dropped
        for cab in cabins
        if cab in fli_by_cabin
        and not fli_by_cabin[cab]
        and (dropped := getattr(fli_by_cabin[cab], "dropped", 0))
    }
    # Said before the hand-off below, as the one-cabin path says them: shown or
    # read, these itineraries could have answered a search that now goes to
    # Matrix.
    for cab in cabins:
        if cab in fli_by_cabin:
            _note_separate_tickets(
                fli_by_cabin[cab], gf_mode=gf_mode, bags=opts.bags is not None, cabin=cab
            )
    if unchecked and separate_tickets == "show":
        _note_unchecked_return(unchecked)
    if emptied and matrix_fallback:
        for cab, board in fli_by_cabin.items():
            _note_google_unread(cab, board)
        return _MatrixHandOff(emptied, tuple(cab for cab in cabins if fli_by_cabin.get(cab)))
    # Only below the hand-off: each note describes Google's table, and a
    # handed-off search prints Matrix's, where '—' is a cabin with no price.
    #
    # The user's count, not the bumped one. The bump widens the pool each cabin
    # keeps so the join has overlap; it is not what anyone asked for, and
    # quoting it tells someone who asked for a handful of rows that returns are
    # combined against the whole pin cap — more than they wanted, from a note
    # whose whole job is to say when they will get fewer.
    _pin_cap_note(legs=legs, top_n=top_n)
    if len(legs) >= _ROUND_TRIP_LEGS and len(cabins) > 1:
        from ._gflight_ids import pinned_fanout  # noqa: PLC0415

        join_note = _multi_cabin_join_note(pinned_fanout(query_top_n), fli_by_cabin.leader)
        err.print(f"[dim]{join_note}[/]")
    for cab in cabins:
        if cab in fli_by_cabin:
            _note_other_currencies(fli_by_cabin[cab], opts.currency or "USD")
            _note_stop_drops(fli_by_cabin[cab], cab)
            _note_row_cap(fli_by_cabin[cab], opts.currency or "USD", cab)
    page_cap = _page_cap_text(opts)
    for cab in cabins:
        if cab in emptied:
            err.print(
                f"[yellow]Google Flights {_safe_text(cab.value)}: "
                f"no itinerary matched {_safe_text(_row_checks(legs, opts))}.[/]"
            )
        elif page_cap is not None and cab in fli_by_cabin and not fli_by_cabin[cab]:
            err.print(
                f"[yellow]Google Flights {_safe_text(cab.value)}: no fare at or under "
                f"{_safe_text(page_cap)}.[/]"
            )

    # The sort cabin first, then the rest as asked: a row several cabins price
    # shows the first itinerary `_merge_cabins` meets, so the table sorted on a
    # cabin shows that cabin's seats whichever search finished first.
    results_by_cabin = _gflight_to_search_result_per_cabin(
        {cab: fli_by_cabin[cab] for cab in dict.fromkeys((sort_by, *cabins)) if cab in fli_by_cabin}
    )

    # Google prices the whole party, so its listed price is the total.
    def total_of(it: Itinerary) -> str | None:
        return it.price

    rows = _merge_cabins(
        results_by_cabin,
        sort_by=sort_by,
        top_n=top_n,
        currency=opts.currency or "USD",
        total_of=total_of,
        slices=len(legs),
    )
    # Every cabin's listings, by `id`, to the bags Google states for each
    # member: a row's cells and its award matches each read their own listing.
    bags_by_id = (
        {
            key: stated
            for cab, res in results_by_cabin.items()
            for key, stated in _bags_by_itinerary(fli_by_cabin[cab], res).items()
        }
        if opts.bags is not None
        else None
    )
    bag_mark = (
        None
        if bags_by_id is None or opts.bags is None
        else partial(_bag_mark, bags_included=bags_by_id, asked=opts.bags)
    )

    # What the line under the table names: a listing the table does not name is
    # not a row of its cabin's document.
    named_own = _named_own_cheapest(
        rows, results_by_cabin, cabins=cabins, sort_by=sort_by, currency=opts.currency or "USD"
    )

    def document_rows(cab: Cabin, board: list[Any]) -> list[Any]:
        named = named_own.get(cab)
        return _cabin_document_rows(
            board,
            rows,
            cab,
            top_n,
            own=named[0] if named else None,
            currency=opts.currency or "USD",
        )

    if _envelope.active():
        for cab, board in fli_by_cabin.items():
            _record_google_cabin(cab, document_rows(cab, board), board, bags=opts.bags)
        if not run_pp:
            return None
    elif json_out and not run_pp:
        out = {
            cab.value: _gflight_json_document(document_rows(cab, board), opts.bags)
            for cab, board in fli_by_cabin.items()
        }
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return None

    # `not json_out` for the reason given at the same gate in
    # `_run_gflight_path`: with awards on, the document is written below this.
    if not sel.awards_only and not json_out:
        _render_multi_cabin_search(
            rows,
            cabins=cabins,
            sort_by=sort_by,
            title_prefix="Google Flights",
            passengers=opts.pax.total,
            slices=len(legs),
            results_by_cabin=results_by_cabin,
            currency=opts.currency or "USD",
            total_of=total_of,
            bag_mark=bag_mark,
            insights={
                cab: insight
                for cab, board in fli_by_cabin.items()
                if (insight := getattr(board, "insight", None)) is not None
            },
        )

    if run_pp:
        # Matrix sells one ticket and an award is one booking, so a row Google
        # sells as separate tickets is neither matched nor valued. The first
        # `-n` one-ticket rows are joined anew, so a marked row that takes a
        # table row takes none from the award table.
        _note_award_skips(sum(r.itinerary.ticketing is not None for r in rows))
        rows = _merge_cabins(
            {
                cab: res.model_copy(
                    update={"solutions": [it for it in res.solutions if it.ticketing is None]}
                )
                for cab, res in results_by_cabin.items()
            },
            sort_by=sort_by,
            top_n=top_n,
            currency=opts.currency or "USD",
            slices=len(legs),
        )
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
            bags_included=bags_by_id,
        )
    return None


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
    # would test a string the user's `--routing` could never have named. fli
    # names a digit-leading code with a leading underscore (`_2K`).
    raw_code = (getattr(leg.airline, "name", "") or "").removeprefix("_")
    code = _safe_text(raw_code)
    number = _safe_text(getattr(leg, "flight_number", "?"))
    booking = f"{code} {number}"
    mf = _codeshare_match(amenity, match_carriers, raw_code)
    return booking if mf is None else f"{_safe_text(mf)} (op {code}{number})"


def _codeshare_match(amenity: Any, match_carriers: frozenset[str], raw_code: str) -> str | None:
    """The marketing flight a leg is relabeled to under `match_carriers`: the
    first of its codeshares whose carrier is in the filter, when the booking
    carrier `raw_code` is not."""
    if not match_carriers or raw_code in match_carriers:
        return None
    raw_mf = getattr(amenity, "marketing_flights", ()) if amenity else ()
    mflights: tuple[str, ...] = tuple(raw_mf or ())
    return next((mf for mf in mflights if mf[:2].upper() in match_carriers), None)


def _carrier_name(code: str, amenity: Any) -> str | None:
    """The full name of the airline `code` is, or None when neither the bundled
    map nor `amenity`, Google's data for the leg, names it. Google's operating
    name is used only for the operating code: a codeshare's flight is operated by
    another carrier, whose name would mislabel the code it is sold under."""
    if (name := CARRIER_NAMES.get(code)) is not None:
        return name
    if getattr(amenity, "operating_carrier", None) == code:
        # Judged as printed: a name of only characters the console drops names nothing.
        name = (getattr(amenity, "operating_carrier_name", None) or "").translate(_CTRL)
        return name.strip() or None
    return None


def _print_carrier_legend(members: list[Any], match_carriers: frozenset[str]) -> None:
    """The line under a table that names each carrier code its legs column
    shows, in the order the rows first show them. A code with no name is left
    out, and a table with none prints no line."""
    # Keyed on first show, unnamed included: a leg that names a code later fills
    # its place rather than moving it behind the codes shown in between.
    named: dict[str, str | None] = {}
    for g in members:
        amenities = getattr(g, "amenities", []) or []
        for k, leg in enumerate(g.flight.legs):
            amenity = amenities[k] if k < len(amenities) else None
            raw_code = (getattr(leg.airline, "name", "") or "").removeprefix("_")
            mf = _codeshare_match(amenity, match_carriers, raw_code)
            for shown in (raw_code,) if mf is None else (mf[:2].upper(), raw_code):
                if named.get(shown) is None:
                    named[shown] = _carrier_name(shown, amenity)
    if any(named.values()):
        console.print(
            "[dim]Carriers: "
            + _safe_text(" · ".join(f"{code} {name}" for code, name in named.items() if name))
            + "[/]"
        )


def _gflight_route(legs: Any) -> str:
    """One itinerary's airports, first departure to last arrival with every
    connection between, wrapped per code as `_fmt_slice_route` does. A change of
    airports at a connection shows both codes rather than hiding one."""
    codes: list[str] = []
    for leg in legs:
        for airport in (leg.departure_airport, leg.arrival_airport):
            code = _safe_text(getattr(airport, "name", "?"))
            if not codes or codes[-1] != code:
                codes.append(code)
    return "→".join(codes)


def _gflight_legs_lines(
    g: Any, match_carriers: frozenset[str], *, route: bool
) -> list[tuple[str, str]]:
    """One row's legs cell, a line per part, each with the legroom printed
    beside it when the legs stack: its airports first when `route`, beside
    none, then each leg's label beside that leg's own, every label but the last
    ending in " →". Joined with spaces the lines are the one-line cell."""
    fr = g.flight
    amenities = getattr(g, "amenities", []) or []
    parts = [
        (
            _leg_display(leg, amenities[k] if k < len(amenities) else None, match_carriers),
            _fmt_gflight_legroom([leg], amenities[k : k + 1]),
        )
        for k, leg in enumerate(fr.legs)
    ]
    head = _gflight_route(fr.legs) if route else ""
    return (
        ([(head, "")] if head else [])
        + [(f"{label} →", room) for label, room in parts[:-1]]
        + parts[-1:]
    )


def _gflight_leg_rows(
    g: Any, match_carriers: frozenset[str], *, route: bool, stacked: bool
) -> list[tuple[str, str]]:
    """The legs and legroom cells of one itinerary's table rows. Stacked, a row
    per part of `_gflight_legs_lines`, beside its own legroom or none: in one
    cell the legroom lines would start beside the route line, and slip a line
    further at each one Rich wraps. Else one row, the legs joined with spaces
    and the legroom a line per leg that has one."""
    parts = _gflight_legs_lines(g, match_carriers, route=route)
    if stacked and parts:
        return parts
    return [(" ".join(line for line, _ in parts), "\n".join(room for _, room in parts if room))]


def _render_gflight_table(
    results: list[Any],
    *,
    legs: tuple[Leg, ...],
    top_n: int,
    match_carriers: frozenset[str] = frozenset(),
    insight: PriceInsight | None = None,
    bags: Bags | None = None,
    passengers: int = 1,
    currency: str,
) -> None:
    """Render fli results as a rich table. Duck-typed: fli has no type stubs.

    Accepts our `GFlightWithId` wrappers — `.flight` is fli's FlightResult,
    `.amenities` is per-leg legroom data parsed from Google's response.
    `match_carriers` enables codeshare-aware leg labels (see `_leg_display`).
    `insight`, Google's price insight for the search, is one line under the
    table, its amounts formatted the way the price column formats them.
    `currency`, the one asked for, orders the rows the `top_n` keep: a fare in
    another currency ranks after every fare in it (`_price_ordered`).
    `bags`, the `--bags` asked for, adds a column saying whether each row's
    price includes them (`_bag_cell`). A `CO2 kg` column (`_co2_cell`) shows
    only when a shown row carries Google's estimate, so a board without one
    keeps its width. A table wider than the console prints each leg on its own
    line, and if it is still wider, leaves the CO2 column out with a note that
    `--format json` carries it: Rich wraps a cell at its spaces, which would
    split a designator such as "EI 152" across two lines. Google prices the
    whole party, so for `passengers` above one the price header names the party.
    A row Google sells as separate tickets ends its price in `†`, or `‡` for a
    self transfer, and a key line follows only when one is shown. A member its
    page put on Google's Top flights board is numbered `★N`, with its own key
    line under the same rule.

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
    docs/memories/gf_request_budget.md and in
    `tests/pp/test_gflight_adapter.py`."""
    origin = ",".join(legs[0].origins) or "?"
    destination = ",".join(legs[0].destinations) or "?"
    # One board ranks every airport of a set, so only the row can say which
    # airports it flies.
    per_row_route = any(
        len(expand_airports(lg.origins)) > 1 or len(expand_airports(lg.destinations)) > 1
        for lg in legs
    )
    shown = _price_ordered(results, currency=currency)[:top_n]
    members = [
        g for r in shown for g in (cast("tuple[Any, ...]", r) if isinstance(r, tuple) else (r,))
    ]
    has_co2 = any(getattr(g.flight, "co2_emissions_g", None) is not None for g in members)
    # Stacked, the legs column is as wide as its longest line and fixed there:
    # Rich narrows a column with no fixed width first, so a table wider than
    # the console wraps the other columns rather than split a designator.
    legs_width = max(
        (
            cell_len(line)
            for g in members
            for line, _ in _gflight_legs_lines(g, match_carriers, route=per_row_route)
        ),
        default=None,
    )
    any_legroom = any(
        _fmt_gflight_legroom(g.flight.legs, getattr(g, "amenities", []) or []) for g in members
    )
    # The first layout whose natural width fits the console, else the last:
    # the legs go one per line before the CO2 column goes.
    layouts = [(False, has_co2), (True, has_co2)] + ([(True, False)] if has_co2 else [])
    while True:
        stacked, show_co2 = layouts.pop(0)
        t = Table(
            title=f"Google Flights · {_safe_text(origin)}→{_safe_text(destination)}"
            + (" + return" if len(legs) >= _ROUND_TRIP_LEGS else ""),
            show_header=True,
            header_style="bold green",
        )
        t.add_column("#", justify="right")
        # Never wrapped: Rich narrows the columns it may wrap, and a price
        # wrapped at its space would put a separate-ticket mark on a line alone.
        t.add_column(
            f"total ({passengers:d} travelers)" if passengers > 1 else "price",
            justify="right",
            no_wrap=True,
        )
        t.add_column("stops", justify="right")
        t.add_column("duration")
        t.add_column("legs", width=legs_width if stacked else None)
        t.add_column("legroom")
        if show_co2:
            t.add_column("CO2 kg", justify="right")
        if bags is not None:
            t.add_column("bags")
        for i, r in enumerate(shown, 1):
            items: list[Any] = list(r) if isinstance(r, tuple) else [r]  # pyright: ignore[reportUnknownArgumentType]
            for j, g in enumerate(items):
                fr = g.flight  # unwrap GFlightWithId → fli FlightResult
                label = ("★" if getattr(g, "top_flight", False) else "") + (
                    f"{i}{'a' if j == 0 else 'b'}" if len(items) > 1 else str(i)
                )
                (legs_str, legroom_str), *more = _gflight_leg_rows(
                    g, match_carriers, route=per_row_route, stacked=stacked
                )
                mins = fr.duration
                dur = f"{mins // 60}h{mins % 60:02d}m"
                co2_cell = (_co2_cell(fr),) if show_co2 else ()
                bag_cell = () if bags is None else (_bag_cell(g.bags_included, bags),)
                ticketing = getattr(g, "ticketing", None)
                # A row Google did not price is SHOWN, with the placeholder every
                # other absent amount in this CLI uses. Dropping it would shorten a
                # board the user asked `-n` rows of and make the count a lie, and a
                # currency prefix over nothing would read as a fare of zero.
                t.add_row(
                    label,
                    (
                        "—"
                        if fr.price is None
                        else f"{_safe_text(fr.currency or 'USD')}{fr.price:.2f}"
                    )
                    + (" ‡" if ticketing == "self_transfer" else " †" if ticketing else ""),
                    _safe_text(fr.stops),
                    dur,
                    legs_str,
                    legroom_str,
                    *co2_cell,
                    *bag_cell,
                )
                for legs_str, legroom_str in more:
                    t.add_row("", "", "", "", legs_str, legroom_str)
        # `console.measure` caps the answer at the console's width, so only an
        # unbounded measure says whether the table is wider.
        unbounded = console.options.update_width(10_000)
        if not layouts or console.measure(t, options=unbounded).maximum <= console.width:
            break
    console.print(t)
    _print_carrier_legend(members, match_carriers)
    if any_legroom:
        console.print(_LEGROOM_KEY)
    if show_co2:
        console.print(
            "[dim]CO2 kg: Google's estimate for the row's flights and its difference "
            "from the route's typical ([green]green[/] lower, [red]red[/] higher).[/]"
        )
    elif has_co2:
        # One line even where it is wider than the output, so output captured
        # at 80 columns carries the sentence whole.
        console.print(
            "[dim]CO2 kg not shown: the table does not fit the output width; "
            "--format json carries it.[/]",
            soft_wrap=True,
        )
    if any(_separately_ticketed(r) for r in shown):
        _print_ticketing_key(
            outbound_only=any(
                isinstance(r, tuple) and len(cast("tuple[Any, ...]", r)) == 1 for r in shown
            )
        )
    _print_top_flight_key(members)
    if insight is not None:
        console.print(
            f"Price insight: prices are {_safe_text(insight.level)} for this trip "
            f"(usually {_safe_text(insight.currency)}{insight.typical_low:.2f}"
            f"-{_safe_text(insight.currency)}{insight.typical_high:.2f})."
        )


def _print_top_flight_key(members: list[Any]) -> None:
    """The key under a table that numbers a top flight `★N`, when `members`,
    the rows it shows, hold one."""
    if any(getattr(g, "top_flight", False) for g in members):
        console.print(
            "[dim]★ top flight: Google lists it under Top flights. The table is in price order.[/]"
        )


def _print_ticketing_key(*, outbound_only: bool) -> None:
    """The key under a table that ends a separate-ticket row's price in `†`
    or `‡`; `outbound_only` when a shown round trip on separate tickets is
    its outbound alone."""
    console.print(
        "[dim]† separate tickets: Google sells this trip as more than one booking. "
        "‡ self transfer: separate tickets, and you collect and recheck bags between "
        "flights."
        + (
            " A round trip on separate tickets lists its outbound only: Google prices "
            "the whole trip but serves no return for it."
            if outbound_only
            else ""
        )
        + "[/]"
    )


def _co2_cell(flight: Any) -> str:
    """A row's CO2 as Google states it: whole kilograms, then its signed percent
    from the route's typical, green where Google labels the row lower and red
    where higher. Blank where Google gave no figure, never a zero."""
    grams = getattr(flight, "co2_emissions_g", None)
    if grams is None:
        return ""
    delta = getattr(flight, "co2_emissions_delta_pct", None)
    text = f"{round(grams / 1000):d}" + ("" if delta is None else f" {delta:+d}%")
    label = getattr(flight, "emissions_tag", None)
    if label == "lower":
        return f"[green]{text}[/]"
    if label == "higher":
        return f"[red]{text}[/]"
    return text


def _bag_cell(stated: tuple[int | None, int | None], asked: Bags) -> str:
    """Whether a row's price includes the bags asked for: `incl.` when the row
    says it covers at least as many of each kind asked for, `not incl.` when it
    says fewer of one, `unknown` when it says neither."""
    pairs = [
        (have, want)
        for have, want in zip(stated, (asked.checked, asked.carry_on), strict=True)
        if want
    ]
    if any(have is not None and have < want for have, want in pairs):
        return "not incl."
    if all(have is not None for have, _ in pairs):
        return "incl."
    return "unknown"


def _bag_mark(
    listing: Itinerary,
    *,
    bags_included: Mapping[int, Sequence[tuple[int | None, int | None]]],
    asked: Bags,
) -> str:
    """Whether `listing`'s price includes the bags asked for, as one mark
    over `_bag_cell`'s verdict on each of its members' statements
    (`bags_included`, by `id`): `✗` when any member's leaves a bag out, `✓`
    when every member's includes them, `?` otherwise. A combination includes
    the bags only where each of its fares does."""
    verdicts = {_bag_cell(stated, asked) for stated in bags_included.get(id(listing), ())}
    if "not incl." in verdicts:
        return "✗"
    return "✓" if verdicts == {"incl."} else "?"


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
            f"{str(getattr(leg.airline, 'name', leg.airline)).removeprefix('_')}"
            f"{getattr(leg, 'flight_number', '?')}"
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

# The canonical names `_resolve_cabin` and `_parse_times` accept, the ones
# their refusals list; tab offers these and none of the aliases.
_CABIN_CHOICES = ("economy", "premium", "business", "first")
# Each cabin as `--cabin` and `--sort` take it, in one shell word.
_CABIN_FLAG_NAMES: dict[Cabin, str] = dict(zip(Cabin, _CABIN_CHOICES, strict=True))
_TIME_OF_DAY_CHOICES = ("early", "morning", "midday", "afternoon", "evening", "night")


def _completer(names: tuple[str, ...], *, comma_list: bool = False) -> Callable[[str], list[str]]:
    """A Typer `autocompletion` callback offering `names`; Typer keeps the
    ones that start with what was typed. With `comma_list`, it offers the
    next item of a comma list, skipping the items already given, and nothing
    after an item not among `names`, which the parser may refuse."""

    def complete(incomplete: str) -> list[str]:
        if not comma_list or "," not in incomplete:
            return list(names)
        head = incomplete.rsplit(",", 1)[0]
        given = {item.strip() for item in head.split(",")}
        if not given <= set(names):
            return []
        return [f"{head},{name}" for name in names if name not in given]

    return complete


_complete_cabin = _completer(_CABIN_CHOICES)
_complete_cabins = _completer(_CABIN_CHOICES, comma_list=True)
# One bucket, not a list: an arrival flag takes one window, and buckets that do
# not adjoin are refused.
_complete_time = _completer(_TIME_OF_DAY_CHOICES)
_complete_times = _completer(_TIME_OF_DAY_CHOICES, comma_list=True)
_complete_transport = _completer(VALID_TRANSPORT_MODES)

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
    try:
        if flag is not None:
            return _config.checked_rps(flag, "--rps")
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
    on top of an already-loaded `cli`. `_gflight_query` builds it instead; that
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
    autocompletion=_completer(_VALID_FORMATS),
    rich_help_panel=_GROUP_OUTPUT,
)
# `search` and `calendar` also write the envelope (`_envelope`): the same fifteen
# keys whatever path answered, for a caller that cannot know the path ahead.
_ENVELOPE_FORMATS = (*_VALID_FORMATS, "envelope")
_ENVELOPE_FORMAT_CHOICES = "/".join(_ENVELOPE_FORMATS)
_ENVELOPE_FORMAT_OPT = typer.Option(
    "table",
    "--format",
    help=f"Output format: one of {_ENVELOPE_FORMAT_CHOICES}. envelope is one versioned "
    "JSON document, written at exit 0 and 1; a usage error (exit 2) writes no document. "
    "Its keys, whichever path answered: version, command, backend, currency, complete, "
    "notes, results, awards, insight, price_history, facets, price_graph, verify, "
    "cross_check, split_ticket. complete is false when the answer is narrower than "
    "asked, and notes carries what stderr said.",
    autocompletion=_completer(_ENVELOPE_FORMATS),
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
_CURRENCY_HELP = (
    "Price in this currency: a 3-letter ISO 4217 code such as EUR. Matrix and "
    "Google Flights both price in it; the Matrix link does not carry it. Unset, "
    "Google Flights prices in USD and Matrix in its own default, often the "
    "origin's currency, except in a table merging the two or under --max-price, "
    "where both use USD."
)

_GOOGLE_URL_HELP = (
    "Print the Google Flights URL. When an itinerary can be resolved from the "
    "results the URL deep-links to the row --pick names, or to the first row of "
    "the final table; the label says which. Otherwise it pre-fills the search. "
    "No link line is printed under --format json."
)

_ROUTING_RET_HELP = (
    "The return's routing, read from the return's own origin; '' for none. Unset, "
    "a round trip copies --routing when it reads the same both ways ('AA+', "
    "'F* X:LHR F*'), and refuses an ordered chain ('UA LH') or a flight number."
)
_EXT_RET_HELP = "The return's extension codes; '' for none. Unset, a round trip copies --ext."
_SLICE_HELP = (
    "Multi-city: 'ORIG-DEST:DATE[:r=ROUTING:e=EXT:f=FLEX:d=arrive]'. Repeat. f= takes "
    "--flex's values; d=arrive makes DATE the day the slice lands. A top-level "
    "--routing/--extension is the default for a slice with no r=/e=."
)


def _resolve_format(*, fmt: str, json_flag: bool, allowed: tuple[str, ...] = _VALID_FORMATS) -> str:
    """Collapse --format + deprecated --json into a single format string.

    `--json` forwards to `--format json` with a deprecation warning. Setting
    both (--json --format X for X != json) is a hard error: ambiguous intent.
    `allowed` is the caller's: only `search` and `calendar` write the envelope.
    """
    if json_flag:
        err.print("[yellow]--json is deprecated; use --format json.[/]")
        if fmt not in ("table", "json"):
            err.print(f"[red]--json conflicts with --format {_quote(fmt)}; pick one.[/]")
            raise typer.Exit(2)
        return "json"
    if fmt in allowed:
        return fmt
    if fmt == "envelope":
        err.print(
            "[red]--format envelope is written by search and calendar only; use --format json.[/]"
        )
    elif allowed == _VALID_FORMATS:
        err.print(f"[red]--format must be one of {_FORMAT_CHOICES}; got {_quote(fmt)}[/]")
    else:
        err.print(
            f"[red]--format must be one of {_safe_text(_ENVELOPE_FORMAT_CHOICES)}; "
            f"got {_quote(fmt)}[/]"
        )
    raise typer.Exit(2)


def _envelope_command[**P](
    command: _envelope.Command,
) -> Callable[[Callable[P, None]], Callable[P, None]]:
    """Run the command as an envelope run (`_envelope.run`) under `--format envelope`.

    A decorator, so the command body keeps its shape: under the run every path
    goes as under `--format json`, and each JSON leaf hands its rows to the
    recorder instead of writing stdout. The consoles are looked up per call,
    because a test may have replaced them."""

    def wrap(fn: Callable[P, None]) -> Callable[P, None]:
        @wraps(fn)
        def run(*args: P.args, **kwargs: P.kwargs) -> None:
            if kwargs.get("fmt") != "envelope":
                fn(*args, **kwargs)
                return
            _envelope.run(
                command, partial(_said_abort, fn, *args, **kwargs), consoles=(err, _pp_cli.err)
            )

        return run

    return wrap


def _said_abort[**P](fn: Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None:
    """`fn`, saying an abort the way click does and ending with click's exit 1.

    click says it once the command has returned, after the envelope run has
    stopped hearing stderr, so its one line would be missing from the notes."""
    try:
        fn(*args, **kwargs)
    except typer.Abort:
        err.print("Aborted.", style="red")
        raise typer.Exit(1) from None


@app.command()
@_envelope_command("search")
def search(  # noqa: PLR0912, PLR0915 — one branch per flag that refuses or reroutes the search
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
    arrive: Annotated[
        str | None,
        typer.Option(
            "--arrive",
            help=(
                "YYYY-MM-DD the outbound lands, in place of --dep; --arrive-times then sets "
                "when. Matrix only: sends the search to Matrix."
            ),
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    return_arrive: Annotated[
        str | None,
        typer.Option(
            "--return-arrive",
            help=(
                "YYYY-MM-DD the return lands, in place of --return; --return-arrive-times "
                "then sets when. Matrix only."
            ),
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    flex: Annotated[
        str | None,
        typer.Option(
            "--flex",
            help=(
                "Also search beside the outbound date: before (or day before), after (or day "
                "after), 1 (+/- 1 day) or 2 (+/- 2 days). Matrix only: sends the search to "
                "Matrix."
            ),
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    return_flex: Annotated[
        str | None,
        typer.Option(
            "--return-flex",
            help="As --flex, beside the return date. Needs --return or --return-arrive.",
            rich_help_panel=_GROUP_ITINERARY,
        ),
    ] = None,
    slice_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--slice",
            "-s",
            help=_SLICE_HELP
            + " Matrix answers, on one ticket. Two or more slices that are not a round trip (an "
            "open jaw, or a longer multi-city trip) also show Google Flights' cheapest one-way "
            "per slice, combined as separate tickets; --backend gflight shows those alone.",
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
                "time-of-day/extra pax types/PP config). gflight on two or more --slice that "
                "are not a round trip shows Google Flights' separate tickets alone, one "
                "one-way per slice, and asks Matrix nothing."
            ),
            autocompletion=_completer(_VALID_BACKENDS),
            rich_help_panel=_GROUP_BACKEND,
        ),
    ] = BACKEND_AUTO,
    cabin: str = typer.Option(
        "economy",
        "--cabin",
        help=(
            "Cabin, or comma list for multi-cabin compare ('economy,business'). "
            "Multi-cabin renders one price column per cabin; '—' means that cabin's "
            "search returned no fare for the itinerary. A Google Flights round trip "
            "prices every cabin on the --sort cabin's cheapest outbounds; on "
            "Matrix, bump -n for broader overlap across cabins."
        ),
        autocompletion=_complete_cabins,
        rich_help_panel=_GROUP_ITINERARY,
    ),
    sort_cabin: Annotated[
        str | None,
        typer.Option(
            "--sort",
            help="Cabin to sort multi-cabin results by. Default: first in --cabin.",
            autocompletion=_complete_cabin,
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
            help="Routing language ('LH+', 'BA AA', 'F* X:LHR F*').",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    extension: Annotated[
        str | None,
        typer.Option(
            "--extension",
            "--ext",
            help="Extension codes ('MAXCONNECT 2:00', 'MAXSTOPS 1').",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    routing_return: Annotated[
        str | None,
        typer.Option("--routing-ret", help=_ROUTING_RET_HELP, rich_help_panel=_GROUP_FILTERING),
    ] = None,
    extension_return: Annotated[
        str | None,
        typer.Option("--ext-ret", help=_EXT_RET_HELP, rich_help_panel=_GROUP_FILTERING),
    ] = None,
    depart_times: Annotated[
        str | None,
        typer.Option(
            "--depart-times",
            help=(
                "Preferred outbound times-of-day (comma list: morning,midday), or one "
                "departure window to the minute (9:30-13:45). Beside --arrive, give "
                "--arrive-times instead."
            ),
            autocompletion=_complete_times,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    return_times: Annotated[
        str | None,
        typer.Option(
            "--return-times",
            help=(
                "Preferred return times-of-day, or one window to the minute. Beside "
                "--return-arrive, give --return-arrive-times instead."
            ),
            autocompletion=_complete_times,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    arrive_times: Annotated[
        str | None,
        typer.Option(
            "--arrive-times",
            help=(
                "When the outbound lands, local time: one window to the minute "
                "(18:00-21:30) or adjoining times-of-day. Beside --dep, Google Flights "
                "only, since Matrix takes no arrival time there: every row is checked to "
                "the minute, and it is refused where the search needs Matrix. Beside "
                "--arrive, Matrix holds the arrival to it, and takes a list of "
                "times-of-day too."
            ),
            autocompletion=_complete_time,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    return_arrive_times: Annotated[
        str | None,
        typer.Option(
            "--return-arrive-times",
            help=(
                "When the return lands, as --arrive-times, beside --return or "
                "--return-arrive. Needs one of them."
            ),
            autocompletion=_complete_time,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    stops: Annotated[
        int | None,
        typer.Option(
            "--stops",
            help="Max stops per direction (0 = nonstop only), on every backend",
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
    max_price: Annotated[
        int | None,
        typer.Option(
            "--max-price",
            min=1,
            metavar="N",
            help=(
                "Show only fares at or under N, in the search's currency (--currency; "
                "default USD). N is compared with the printed price, which for a party "
                "is the total. Google Flights is asked for a USD cap, and every row "
                "is checked; Matrix is asked in the cap's currency and its answer cut to "
                "the fares under it. Several --cabin values each take it."
            ),
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    bags_spec: Annotated[
        str | None,
        typer.Option(
            "--bags",
            metavar="CHECKED,CARRY",
            help=(
                "Price fares with CHECKED checked bags and CARRY carry-on bags (0 or 1; "
                "default 0): '1' is one checked bag, '1,1' adds a carry-on. Each row "
                "then says whether its price includes them. Google Flights only, since "
                "Matrix prices no bags: refused where the search needs Matrix. One "
                "traveler."
            ),
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    no_separate_tickets: Annotated[
        bool,
        typer.Option(
            "--no-separate-tickets",
            help=(
                "Hide the itineraries Google Flights sells as separate tickets or as a "
                "self transfer, which a search otherwise shows from Google's Cheapest tab "
                "(marked † and ‡ on the Google, merged and multi-cabin tables, "
                "separate_tickets: true in --format json), and say how many were hidden; "
                "on a multi-city --slice search, ask Google for no one-way tickets. Matrix "
                "sells every itinerary as one ticket, so there it changes nothing."
            ),
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = False,
    exclude_basic: Annotated[
        bool,
        typer.Option(
            "--exclude-basic",
            help=(
                "Ask Google Flights for economy fares without basic economy. No row says "
                "whether its fare is basic, so the rows cannot be checked, and Google has "
                "served basic fares under it (JFK-LHR). Google Flights only, since Matrix "
                "is not asked: refused where the search needs Matrix. --cabin economy alone."
            ),
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = False,
    page_size: int = typer.Option(
        10,
        "--n",
        "-n",
        min=1,
        help=(
            "Result count (matrix: page size; gflight: top_n). On Google Flights it "
            "keeps the N cheapest rows in price order, ties in Google's order, "
            "unpriced last; a round trip pins its cheapest outbounds."
        ),
        rich_help_panel=_GROUP_OUTPUT,
    ),
    rps: float | None = _RPS_OPT,
    impersonate: str | None = _IMPERSONATE_OPT,
    fmt: str = _ENVELOPE_FORMAT_OPT,
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
        help="Itinerary #N (1-based, as shown in the final table) to pin in the "
        "--matrix-url/--google-url deep links, to describe with --fare-rules, to "
        "open with --sellers and to check with --verify. Default: the first row. A link "
        "that cannot pin that row pre-fills the search instead; its label says which. A "
        "pick outside the table falls back to row 1 for the links and --fare-rules and is "
        "refused with --sellers and --verify. --format json emits no link lines, so there "
        "it only chooses the --fare-rules, --sellers or --verify row. --awards-only prints "
        "no table, so there a pick is reported as naming no row and the links are unpinned.",
        rich_help_panel=_GROUP_OUTPUT,
    ),
    sellers: bool = typer.Option(
        False,
        "--sellers",
        help="After the Google Flights table, open itinerary #N's booking page "
        "(--pick; default 1) in Chrome and list every seller with its price and fare name, "
        "cheapest first, then each seller's bag fees and booking link on a line of its own. "
        "Needs a Google Flights result and the browser extra; refused on multi-cabin and "
        '--awards-only searches. With --format json the document becomes {"search": …, '
        '"booking_options": […]}.',
        rich_help_panel=_GROUP_OUTPUT,
    ),
    split: bool = typer.Option(
        False,
        "--split",
        help="On a Google Flights round trip, also price one-way tickets each way (two more "
        "page loads, two per page on a leg asked as several pages) and show, under the "
        "round-trip table, the cheapest pair whose return leaves the airport the outbound "
        "lands at, after it lands, as two separate tickets. --max-price is not applied to "
        'them. With --format json the document becomes {"search": …, "split_ticket": {…}}; '
        "--format envelope carries the same object under split_ticket. On a multi-city "
        "search (two or more --slice that are not a round trip), whose table and --format "
        "envelope show its separate tickets anyway, --format json carries them only with "
        "--split, as split_ticket's combinations beside Matrix's document.",
        rich_help_panel=_GROUP_OUTPUT,
    ),
    currency: Annotated[
        str | None,
        typer.Option("--currency", help=_CURRENCY_HELP, rich_help_panel=_GROUP_OUTPUT),
    ] = None,
    fare_rules: Annotated[
        bool,
        typer.Option(
            "--fare-rules",
            help="After the table, show the fare basis, booking codes and fare rules "
            "(penalties, changes, refunds) of the itinerary --pick names (default 1). "
            "Matrix only: sends the search to Matrix. One --cabin; with --format json, "
            "--cash-only.",
            rich_help_panel=_GROUP_OUTPUT,
        ),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="After the Google Flights table, ask Matrix for itinerary #N (--pick; "
            "default 1) as exactly that itinerary: its flights by number, each on its own "
            "day and minute, between its airports. Prints Matrix's price beside Google's "
            "and the fare basis, booking code and fare rules of each fare, or why Matrix "
            "does not price it: those flights only on another itinerary, no fare at all, "
            "or that none of the trips Matrix returned for that route and day names its "
            "carrier. Google Flights only; one --cabin, no --bags, --sellers or --fare-rules. "
            'With --format json the document becomes {"search": …, "verify": …}, where '
            "delta is Google's price minus Matrix's.",
            rich_help_panel=_GROUP_OUTPUT,
        ),
    ] = False,
    no_cache: bool = _NO_CACHE_OPT,
    fast: bool | None = typer.Option(
        None,
        "--fast/--enrich",
        "--no-enrich/--no-fast",
        help="Skip Matrix enrichment: show only the fast Google Flights result "
        "(~1s) instead of also cross-checking it against Matrix. Default: a table "
        "enriches when Google Flights can serve the query, with a delta (Google - "
        "Matrix) on each row priced for the same trip in one currency and a reason "
        "on every other row. --format json cross-checks only with --enrich (and "
        '--cash-only), writing {"search": …, "cross_check": …}.',
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
            "[bold]auto[/] is http until Google rate-limits it past the retries, then "
            "Chrome for the rest of the search (a multi-cabin search stays on http). "
            "A multi-cabin [bold]browser[/] search runs its cabins one at "
            "a time through a single Chrome (~10s for two cabins, ~14s for three, against "
            "~1.3s over http), and falls back to http if Chrome cannot open. Needs "
            # Escaped: rich reads `[browser]` as a style tag and deletes it, which
            # printed an install command that silently omits the extra.
            "[bold]uv pip install 'flight-cli\\[browser]'[/] for browser."
        ),
        autocompletion=_complete_transport,
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
    output = _resolve_format(fmt=fmt, json_flag=json_out, allowed=_ENVELOPE_FORMATS)
    json_out = output != "table"
    if output == "envelope" and (sellers or fare_rules):
        err.print(
            "[red]--sellers and --fare-rules write a document of their own; use --format json.[/]"
        )
        raise typer.Exit(2)
    if not verify:
        _envelope.explain("verify", "--verify was not asked")
    if fast is not False:
        _envelope.explain("cross_check", "--enrich was not asked")
    if not split:
        _envelope.explain("split_ticket", "--split was not asked")
    ccy = _resolve_currency(currency)
    bags = _parse_bags(bags_spec) if bags_spec is not None else None
    _refuse_date_option_conflicts(
        slice_specs=slice_specs,
        dep=dep,
        arrive=arrive,
        ret=ret,
        return_arrive=return_arrive,
        flex=flex,
        return_flex=return_flex,
        depart_times=depart_times,
        return_times=return_times,
    )
    out_flex = _parse_flex(flex, "--flex")
    ret_flex = _parse_flex(return_flex, "--return-flex")
    out_day, ret_day = dep or arrive, ret or return_arrive
    # Matrix holds an arrival-date slice's time window to the arrival, so there
    # the arrival times are that window, a list as Matrix takes; beside a
    # departure date they are Google's arrival window, which Matrix lacks.
    out_arrivals = (
        _parse_search_times(arrive_times, "--arrive-times")
        if arrive
        else _parse_arrival_times(arrive_times, "--arrive-times")
    )
    ret_arrivals = (
        _parse_search_times(return_arrive_times, "--return-arrive-times")
        if return_arrive
        else _parse_arrival_times(return_arrive_times, "--return-arrive-times")
    )
    if ret_arrivals and not ret_day:
        err.print(
            "[red]--return-arrive-times sets when the return lands, and needs a --return "
            "or --return-arrive.[/] Drop it, or add one."
        )
        raise typer.Exit(2)
    google_arrive_times = None if arrive else arrive_times
    google_return_arrive_times = None if return_arrive else return_arrive_times
    google_only = _google_only(
        bags=bags,
        arrive_times=google_arrive_times,
        return_arrive_times=google_return_arrive_times,
        exclude_basic=exclude_basic,
    )
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
    if verify and (
        blocker := _verify_blocker(
            fmt=output,
            backend=backend,
            slice_specs=slice_specs,
            multi_cabin=len(_resolve_cabin_list(cabin)) > 1,
            awards_only=sel.awards_only,
            awards_json=json_out and _should_run_awards(sel),
            sellers=sellers,
            fare_rules=fare_rules,
            bags=bags,
            exclude_basic=exclude_basic,
            pick=pick,
            page_size=page_size,
            date_options=any((flex, return_flex, arrive, return_arrive)),
        )
    ):
        # Before the backend is announced, like the refusals below.
        err.print(f"[red]--verify {_safe_text(blocker)}.[/]")
        raise typer.Exit(2)
    if fare_rules:
        # Before the backend is announced: a refusal after "Using Matrix" reads
        # as a search that started and then failed.
        _refuse_fare_rules_conflicts(cabins=_resolve_cabin_list(cabin), sel=sel, json_out=json_out)
    _refuse_cap_and_bag_conflicts(
        cabins=_resolve_cabin_list(cabin),
        bags=bags,
        seated=adults + children + inf_seat + inf_lap,
        arrival_flags=tuple(
            flag
            for flag, windows, arrival_date in (
                ("--arrive-times", out_arrivals, arrive),
                ("--return-arrive-times", ret_arrivals, return_arrive),
            )
            # Beside an arrival date the window is Matrix's own.
            if windows and not arrival_date
        ),
        exclude_basic=exclude_basic,
    )
    if (routing_return is not None or extension_return is not None) and (
        slice_specs or not ret_day
    ):
        err.print(
            "[red]--routing-ret and --ext-ret set the return's codes, and need a --return.[/] "
            + (
                "A --slice takes its own in its r= and e= fields."
                if slice_specs
                else "Drop them, or add --return."
            )
        )
        raise typer.Exit(2)
    per_slice = _one_way_per_slice(tuple(map(_parse_slice_spec, slice_specs or [])))
    if split and (
        blocker := _split_blocker(
            multi_city=bool(slice_specs),
            one_way=not ret,
            multi_cabin=len(_resolve_cabin_list(cabin)) > 1,
            backend=backend,
            sellers=sellers,
            verify=verify,
            awards_only=sel.awards_only,
            awards_format=output
            if json_out and not sel.awards_only and _should_run_awards(sel)
            else None,
            open_jaw=per_slice,
            fare_rules=fare_rules,
        )
    ):
        err.print(f"[red]--split {_safe_text(blocker)}.[/]")
        raise typer.Exit(2)
    return_codes = (
        _return_codes(
            routing=routing,
            extension=extension,
            routing_return=routing_return,
            extension_return=extension_return,
        )
        if ret_day and not slice_specs and origin and destination and out_day
        else None
    )
    resolved = _pick_backend(
        backend=backend,
        # Beside --slice a top-level code is only the default of a slice with
        # no r=/e=, and `_open_jaw_blocker` judges each slice by its own codes.
        routing=None if slice_specs else routing,
        extension=None if slice_specs else extension,
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
        fare_rules=fare_rules,
        adults=adults,
        bags=bags,
        return_codes=return_codes,
        arrive_times=google_arrive_times,
        return_arrive_times=google_return_arrive_times,
        exclude_basic=exclude_basic,
        multi_cabin=len(_resolve_cabin_list(cabin)) > 1,
        cabins=_resolve_cabin_list(cabin),
        flex=out_flex,
        return_flex=ret_flex,
        arrive=bool(arrive),
        return_arrive=bool(return_arrive),
        open_jaw=per_slice,
    )
    if verify and resolved == BACKEND_MATRIX:
        err.print("[red]--verify needs a Google Flights row, and this search runs on Matrix.[/]")
        raise typer.Exit(2)
    if slice_specs:
        legs = _slice_legs(slice_specs, routing=routing, extension=extension)
    elif origin and destination and out_day:
        origins, destinations = _require_airports(origin, destination)
        out_times = _parse_search_times(depart_times, "--depart-times")
        ret_times = _parse_search_times(return_times, "--return-times")
        legs = (
            Leg.of(
                origins,
                destinations,
                _parse_date(out_day),
                is_arrival_date=bool(arrive),
                date_minus=out_flex[0],
                date_plus=out_flex[1],
                route_language=routing,
                extension=extension,
                time_ranges=out_arrivals if arrive else out_times,
                arrival_ranges=() if arrive else out_arrivals,
            ),
        )
        if ret_day and return_codes is not None:
            legs += (
                Leg.of(
                    destinations,
                    origins,
                    _parse_date(ret_day),
                    is_arrival_date=bool(return_arrive),
                    date_minus=ret_flex[0],
                    date_plus=ret_flex[1],
                    route_language=return_codes[0],
                    extension=return_codes[1],
                    time_ranges=ret_arrivals if return_arrive else ret_times,
                    arrival_ranges=() if return_arrive else ret_arrivals,
                ),
            )
    else:
        err.print("[red]Specify --slice ... or origin destination --dep (or --arrive)[/]")
        raise typer.Exit(2)

    cabins_tuple = _resolve_cabin_list(cabin)
    _envelope.ask_cabins(c.value for c in cabins_tuple)
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
        currency=ccy,
        max_price=max_price,
        bags=bags,
        exclude_basic=exclude_basic,
    )

    run_awards = _should_run_awards(sel)
    # Validated for every backend, so a typo is caught whether or not this
    # particular query happens to reach Google Flights. Bound to a new name
    # rather than reassigned: the parameter is declared `str` for typer's sake,
    # and reassigning it would throw away the narrowing this call just did.
    gf_mode = _resolve_gf_transport(gf_transport)

    # A time window applies to no slice, on Matrix as on Google.
    top_codes = (
        ("--depart-times", depart_times),
        ("--return-times", return_times),
    )
    beside_matrix = bool(slice_specs) and backend == BACKEND_AUTO and not sel.awards_only

    # `_pick_backend` puts a --slice search on Google only as one one-way per slice.
    if slice_specs and resolved == BACKEND_GFLIGHT:
        if blocker := _gflight_multi_city_blocker(
            blocker=_open_jaw_blocker(
                legs=legs,
                opts=opts,
                no_separate_tickets=no_separate_tickets,
                top_codes=top_codes,
                cabins=len(cabins_tuple),
            ),
            awards_only=sel.awards_only,
            awards_json=output == "json" and run_awards,
            sellers=sellers,
            enrich=fast is False,
            bags=bags,
            exclude_basic=exclude_basic,
            arrive_times=google_arrive_times,
            return_arrive_times=google_return_arrive_times,
            pick=pick,
        ):
            err.print(f"[red]{_safe_text(blocker)}.[/]")
            raise typer.Exit(2)
        _answer_multi_city_on_google(
            legs=legs,
            opts=opts,
            top_n=page_size,
            gf_mode=gf_mode,
            gf_headed=gf_headed,
            output=output,
            google_url=google_url,
            awards=run_awards,
        )
        return

    # Below the multi-city answer above, which refuses --sellers itself: this
    # check's "drop --bags or --sellers" would not hold there, since Google's
    # separate tickets refuse each of the two.
    if sellers and (
        blocker := _sellers_blocker(
            backend=resolved,
            multi_cabin=len(cabins_tuple) > 1,
            awards_only=sel.awards_only,
            awards_json=run_awards and json_out,
            pick=pick,
            page_size=page_size,
            bags=bags,
            exclude_basic=exclude_basic,
        )
    ):
        err.print(f"[red]--sellers {_safe_text(blocker)}.[/]")
        raise typer.Exit(2)

    if len(cabins_tuple) > 1:
        if beside_matrix and per_slice:
            _answer_open_jaw(
                legs=legs,
                opts=opts,
                top_n=page_size,
                gf_mode=gf_mode,
                gf_headed=gf_headed,
                blocker=_open_jaw_blocker(
                    legs=legs,
                    opts=opts,
                    no_separate_tickets=no_separate_tickets,
                    top_codes=top_codes,
                    cabins=len(cabins_tuple),
                ),
                output=output,
                split=split,
                google_url=google_url,
            )
        # `_pick_backend` already refused anything the page can't encode, so a
        # constraint that survived to here is one the fan-out honours natively.
        # Re-testing `routing or extension` here would drop it to Matrix with no
        # reason printed.
        google_answered: tuple[Cabin, ...] = ()
        if resolved == BACKEND_GFLIGHT:
            hand_off = _run_gflight_path_multi(
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
                matrix_fallback=backend == BACKEND_AUTO and not google_only,
                separate_tickets=_separate_tickets_mode(
                    awards_only=sel.awards_only, no_separate_tickets=no_separate_tickets
                ),
            )
            if hand_off is None:
                return
            reasons = ", ".join(
                f"{cab.value} ({dropped:d} rows filtered out)"
                for cab, dropped in hand_off.emptied.items()
            )
            err.print(
                f"[dim]Using Matrix: no Google Flights itinerary matched "
                f"{_safe_text(_row_checks(legs, opts))} in {_safe_text(reasons)}.[/]"
            )
            google_answered = hand_off.answered
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
            google_answered=google_answered,
        )
        return

    if resolved == BACKEND_GFLIGHT:
        # GF can serve this query — paint it fast (~1s), then enrich against
        # Matrix and repaint a merged table. `--fast` takes the
        # GF-only path, and so does a Google-only flag: Matrix would answer
        # without it. A document enriches only on an explicit `--enrich` (`fast` is
        # False rather than unset): the cross-check changes its shape and waits
        # on Matrix, which a caller of the bare list never asked for. JSON on
        # auto without `--fast` still answers wherever the merged table would,
        # so a failed Google query hands it to Matrix whole. `--sellers` is not
        # handed on: its document wraps a Google row, which Matrix cannot supply.
        if exclude_basic:
            # On every run: whether Google left basic fares out cannot be told
            # from the rows, and on JFK-LHR it did not.
            err.print(
                "[yellow]Google Flights was asked to leave out basic economy, but its rows "
                "carry no fare-family mark to check, and it has served basic fares on "
                "JFK-LHR anyway.[/]"
            )
        enrich = fast is False or (fast is None and not json_out)
        # Opened below on this thread, which starts the search's workers: each
        # copies the one escalation, so one throttle moves all of them to Chrome.
        from ._gflight_ids import search_escalation  # noqa: PLC0415 — fli, ~95 ms

        separate = _separate_tickets_mode(
            awards_only=sel.awards_only, no_separate_tickets=no_separate_tickets
        )
        if enrich and google_only:
            gaps = _join_reasons(list(dict.fromkeys(gap for _, gap in google_only)))
            err.print(f"[dim]No Matrix enrichment: Matrix {_safe_text(gaps)}.[/]")
        elif enrich and verify:
            # The numbered table has to be Google's, the rows --verify checks,
            # and a document holds `verify` in place of `cross_check`.
            err.print("[dim]No Matrix enrichment: --verify asks Matrix about one row instead.[/]")
        elif enrich:
            if json_out and (
                blocker := _cross_check_blocker(
                    run_awards=run_awards,
                    awards_only=sel.awards_only,
                    sellers=sellers,
                    split=split,
                )
            ):
                err.print(f"[red]--enrich --format {_safe_text(output)} {_safe_text(blocker)}.[/]")
                raise typer.Exit(2)
            with search_escalation():
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
                    sellers=sellers,
                    split=split,
                    json_out=json_out,
                    separate_tickets=separate,
                )
            return
        with search_escalation():
            unmatched = _run_gflight_path(
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
                sellers=sellers,
                # A row handed to Matrix is no Google row to check.
                matrix_fallback=backend == BACKEND_AUTO and not google_only and not verify,
                hand_off_failure=backend == BACKEND_AUTO
                and not google_only
                and json_out
                and not fast
                and not sellers
                and not verify,
                split=split,
                matrix_remedy=_matrix_remedy(google_only),
                verify=verify,
                rps=rps,
                impersonate=impersonate,
                separate_tickets=separate,
            )
        if unmatched is None:
            return
        if unmatched:
            err.print(
                f"[dim]Using Matrix: no Google Flights itinerary matched "
                f"{_safe_text(_row_checks(legs, opts))} ({unmatched:d} rows filtered out).[/]"
            )

    split_ticket: dict[str, Any] | None = None
    if beside_matrix and per_slice:
        split_ticket = _answer_open_jaw(
            legs=legs,
            opts=opts,
            top_n=page_size,
            gf_mode=gf_mode,
            gf_headed=gf_headed,
            blocker=_open_jaw_blocker(
                legs=legs,
                opts=opts,
                no_separate_tickets=no_separate_tickets,
                top_codes=top_codes,
            ),
            output=output,
            split=split,
            google_url=google_url,
            awards=run_awards,
        )
    elif split:
        on_matrix = "--split prices Google Flights one-ways, and this search runs on Matrix"
        # The pair was asked for, and no answer here can carry it.
        _envelope.narrow()
        _envelope.explain("split_ticket", on_matrix)
        _report_no_split(on_matrix)
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
        fare_rules=fare_rules,
        split_ticket=split_ticket,
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
        typer.Option("--slice", "-s", help=_SLICE_HELP),
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
    routing_return: str | None = typer.Option(None, "--routing-ret", help=_ROUTING_RET_HELP),
    extension_return: str | None = typer.Option(None, "--ext-ret", help=_EXT_RET_HELP),
    stops: Annotated[
        int | None,
        typer.Option(
            "--stops", help="Max stops per direction (0 = nonstop only), on every backend"
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
    if (routing_return is not None or extension_return is not None) and (slice_specs or not ret):
        err.print(
            "[red]--routing-ret and --ext-ret set the return's codes, and need a --return.[/] "
            + (
                "A --slice takes its own in its r= and e= fields."
                if slice_specs
                else "Drop them, or add --return."
            )
        )
        raise typer.Exit(2)
    # This block is `search`'s, near-duplicated. Deliberately not shared: `fare`
    # is deprecated and prints so on every run, and a helper spanning a command
    # on its way out ties the survivor's leg building to the leaving one.
    if slice_specs:
        legs = _slice_legs(slice_specs, routing=routing, extension=extension)
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
            ret_routing, ret_extension = _return_codes(
                routing=routing,
                extension=extension,
                routing_return=routing_return,
                extension_return=extension_return,
            )
            legs += (
                Leg.of(
                    destinations,
                    origins,
                    _parse_date(ret),
                    route_language=ret_routing,
                    extension=ret_extension,
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


def _slice_flex(s: str, chunk: str) -> tuple[int, int]:
    """A slice's `f=` as `--flex` reads it, or BadParameter naming the slice."""
    days = _FLEX_DAYS.get(chunk[2:].strip().lower())
    if days is None:
        raise typer.BadParameter(
            f"slice {s!r}: bad {chunk!r}; f= takes before, after, 1 or 2 "
            "(or day before, or day after, +/- 1 day, +/- 2 days)"
        )
    return days


def _slice_arrival(s: str, chunk: str) -> bool:
    """A slice's `d=`, which takes `arrive` alone: a slice dates its departure
    unless told otherwise."""
    if chunk[2:].strip().lower() != "arrive":
        raise typer.BadParameter(
            f"slice {s!r}: bad {chunk!r}; d= takes arrive, which makes the date the day "
            "the slice lands"
        )
    return True


def _parse_slice_spec(s: str) -> Leg:
    """Parse 'JFK-LHR:2026-08-15[:r=LH+:e=MAXCONNECT 2:00:f=1:d=arrive]'.

    Error paths surface the specific failure (missing colon, malformed
    origin-dest, unknown key prefix, bad date) instead of the generic
    "should be ORIGIN-DEST:DATE[:r=...:e=...]" — that message is fine
    for missing date but useless when the user typed `r-LH+` instead
    of `r=LH+` (which the lookahead split otherwise silently ignores).
    """
    parts = s.split(":", 2)
    if len(parts) < _SLICE_MIN_PARTS:
        raise typer.BadParameter(
            f"slice {s!r}: missing date — expected ORIGIN-DEST:DATE[:r=...:e=...:f=...:d=arrive]"
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
    flex, arrival = (0, 0), False
    if len(parts) == _SLICE_MAX_PARTS:
        # Chunks come in as r=..., e=..., f=... and d=... separated by ':'
        # followed by the key prefix. Anything that doesn't start with one is a
        # typo (the most common is r-VALUE instead of r=VALUE).
        for chunk in re.split(r":(?=[refd]=)", parts[2]):
            if chunk.startswith("r="):
                routing = chunk[2:]
            elif chunk.startswith("e="):
                extension = chunk[2:]
            elif chunk.startswith("f="):
                flex = _slice_flex(s, chunk)
            elif chunk.startswith("d="):
                arrival = _slice_arrival(s, chunk)
            else:
                raise typer.BadParameter(
                    f"slice {s!r}: unknown key prefix in {chunk!r}; valid keys are "
                    "r=ROUTING, e=EXTENSION, f=FLEX and d=arrive (note the '=')"
                )
    return Leg.of(
        o,
        d,
        parsed_date,
        is_arrival_date=arrival,
        date_minus=flex[0],
        date_plus=flex[1],
        route_language=routing,
        extension=extension,
    )


@app.command()
@_envelope_command("calendar")
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
    cabin: str = typer.Option(
        "economy", "--cabin", autocompletion=_complete_cabin, rich_help_panel=_GROUP_ITINERARY
    ),
    adults: int = typer.Option(1, "--adults", rich_help_panel=_GROUP_ITINERARY),
    children: int = typer.Option(0, "--children", rich_help_panel=_GROUP_ITINERARY),
    seniors: int = typer.Option(0, "--seniors", rich_help_panel=_GROUP_ITINERARY),
    youth: int = typer.Option(0, "--youth", rich_help_panel=_GROUP_ITINERARY),
    routing: str | None = typer.Option(None, "--routing", rich_help_panel=_GROUP_FILTERING),
    extension: str | None = typer.Option(
        None, "--extension", "--ext", rich_help_panel=_GROUP_FILTERING
    ),
    routing_return: str | None = typer.Option(
        None, "--routing-ret", help=_ROUTING_RET_HELP, rich_help_panel=_GROUP_FILTERING
    ),
    extension_return: str | None = typer.Option(
        None, "--ext-ret", help=_EXT_RET_HELP, rich_help_panel=_GROUP_FILTERING
    ),
    depart_times: str | None = typer.Option(
        None, "--depart-times", autocompletion=_complete_times, rich_help_panel=_GROUP_FILTERING
    ),
    return_times: str | None = typer.Option(
        None, "--return-times", autocompletion=_complete_times, rich_help_panel=_GROUP_FILTERING
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
    fmt: str = _ENVELOPE_FORMAT_OPT,
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
    currency: Annotated[
        str | None,
        typer.Option(
            "--currency",
            help=_CURRENCY_HELP + " A non-USD currency skips the Google Flights date grid, "
            "which prices in USD only, so --fast refuses it.",
            rich_help_panel=_GROUP_OUTPUT,
        ),
    ] = None,
    no_cache: bool = _NO_CACHE_OPT,
    fast: bool = typer.Option(
        False,
        "--fast/--enrich",
        "--no-enrich/--no-fast",
        help="Skip the Matrix enrichment: show only the Google Flights price "
        "grid instead of also running the authoritative Matrix calendar. Serves a "
        "calendar one-way or a round trip, a trip-length range ('-d 5-7') as one "
        "price graph per length in at most 8 page loads in all (over "
        "[bold]--gf-transport http[/] one trip length only), between "
        "airports, comma-lists or metro codes (NYC, LON; up to 11 airports a leg, "
        "each date priced at the cheapest of them), whose filters Google Flights' "
        "search page can carry (cabin, adults, stops up to 2, one carrier or alliance "
        "include, MAXDUR, MINCONNECT, MAXCONNECT, and on a one-way a --depart-times "
        "window that runs to midnight; over [bold]--gf-transport http[/] only cabin, "
        "adults and stops up to 2); table or JSON. Reads "
        "the grid from the search page in a real Chrome (see [bold]--gf-transport[/]). "
        "Exits 1 rather than falling back, so a no-grid result is never mistaken for "
        "a fast one. Without it, a table calendar prints the same graph after "
        "Matrix's, one column per trip length of a range, and a JSON or envelope "
        "calendar carries it when [bold]--gf-transport browser[/] or [bold]auto[/] is given.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    gf_transport: str | None = typer.Option(
        None,
        "--gf-transport",
        help=(
            "How Google Flights' price grid is read: [bold]auto[/] (default) is "
            "browser; [bold]browser[/] opens the filtered search page in a real Chrome "
            "(headless unless [bold]--gf-headed[/]), clicks Price graph and reads the "
            "page's own response, a few seconds per five weeks of window. Under "
            "[bold]--fast[/] that grid is the answer; without it the graph is read while "
            "Matrix runs and printed after Matrix's calendar, or carried in a JSON or "
            "envelope document when this flag names [bold]browser[/] or [bold]auto[/], and "
            "a Google failure is one line on stderr. [bold]http[/] calls the calendar RPC "
            "directly, which Google "
            "currently answers with no data, so without [bold]--fast[/] it is Matrix's "
            "calendar alone and opens no Chrome. Browser needs [bold]uv pip install "
            # Escaped: rich reads `[browser]` as a style tag and deletes it.
            "'flight-cli\\[browser]'[/] and an installed Chrome."
        ),
        autocompletion=_complete_transport,
        rich_help_panel=_GROUP_BACKEND,
    ),
    gf_headed: bool = typer.Option(
        False,
        "--gf-headed",
        help="Show the Chrome window that reads the price graph. Default: headless.",
        rich_help_panel=_GROUP_BACKEND,
    ),
    max_per_query: int = typer.Option(
        1,
        "--max-per-query",
        help=(
            "Multi-airport calendar: max destinations per Matrix request, metro codes "
            "counted as their airports. 1 (default) queries each airport pair "
            "separately, which Matrix prices completely; higher is fewer/faster "
            "requests but Matrix may under-report (incomplete). Round trips that "
            "return to a different origin airport, or come back from a destination "
            "airport in another request, are priced only by one combined query run "
            "beside them, which Matrix may under-report."
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
    """Lowest-fare grid across a date window. Default round-trip; --one-way to flip.

    Matrix's grid colors a day's min price green when it is at least 20% under the
    median of the window's priced days in the grid's currency, and colors nothing
    when fewer than 5 priced days share that currency."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out, allowed=_ENVELOPE_FORMATS) != "table"
    # A JSON or envelope calendar opens Chrome for Google's graph only when the
    # transport is named: a script's calendar launches no browser it did not ask for.
    transport_named = gf_transport is not None
    if gf_transport is None:
        # Only the browser returns Google's grid while the direct RPC answers with
        # no data, with `--fast` and beside Matrix alike.
        gf_transport = "auto"
    gf_mode = _resolve_gf_transport(gf_transport)
    if not fast and gf_mode == TRANSPORT_HTTP and gf_headed:
        # Under `--fast` the flag has always passed silently, and that stays.
        raise typer.BadParameter(
            "shows the Chrome window that reads Google Flights' price graph, and "
            "--gf-transport http opens none",
            param_hint="--gf-headed",
        )
    ccy = _resolve_currency(currency)
    origins, dests = _require_airports(origin, destination)
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
        ret_routing, ret_extension = _return_codes(
            routing=routing,
            extension=extension,
            routing_return=routing_return,
            extension_return=extension_return,
        )
        legs += (
            Leg.of(
                dests,
                origins,
                route_language=ret_routing,
                extension=ret_extension,
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
        currency=ccy,
    )
    window = CalendarWindow(start=sd, end=ed, duration_min=dmin, duration_max=dmax)
    search = CalendarSearch(legs=legs, options=opts, window=window)

    # Fast layer: the GF grid alone, for a Tier-1 window the search page can ask.
    # Without `--fast` Matrix answers first and in full, and Google's price graph
    # for the same window follows it (`_run_calendar_beside_graph`), or under
    # `--gf-transport http` the RPC grid is woven in ahead of it (a one-way window
    # between two airports).
    if not fast:
        _calendar_without_fast(
            search,
            gf_mode=gf_mode,
            headed=gf_headed,
            transport_named=transport_named,
            json_out=json_out,
            one_way=one_way,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            dmin=dmin,
            dmax=dmax,
            rps=rps,
            impersonate=impersonate,
            no_cache=no_cache,
            max_per_query=max_per_query,
            max_concurrency=max_concurrency,
            matrix_url=matrix_url,
            google_url=google_url,
            asked=_asked_flags(
                opts,
                one_way=one_way,
                routing=routing,
                extension=extension,
                routing_return=routing_return,
                extension_return=extension_return,
                depart_times=depart_times,
            ),
        )
        return
    blocker = _grid_branch_blocker(
        search,
        json_out=json_out,
        one_way=one_way,
        origins=origins,
        dests=dests,
        fast=fast,
        graph=gf_mode != TRANSPORT_HTTP,
    )
    if blocker is not None:
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
        if gf_mode == TRANSPORT_HTTP:
            err.print(
                "[yellow]--fast applies only to calendars one-way or of one trip length, "
                "between airports or metro codes Google Flights can ask for (up to 11 "
                "airports a leg), whose every filter its search page can carry; "
                f"this is {_safe_text(blocker)}. Run without --fast for Matrix.[/]"
            )
            # The http grid prices one trip length; the browser's graph prices one per
            # length. Said only when that gate admits this very search, so the remedy
            # is never another refusal. `auto` is the browser under `--fast` (below),
            # so it is named beside `browser`.
            if (
                not one_way
                and dmin != dmax
                and _grid_branch_blocker(
                    search,
                    json_out=json_out,
                    one_way=one_way,
                    origins=origins,
                    dests=dests,
                    fast=True,
                    graph=True,
                )
                is None
            ):
                err.print(
                    "[yellow]For the range on Google, run with --gf-transport browser or auto.[/]"
                )
        else:
            err.print(
                "[yellow]--fast applies only to calendars one-way, of one trip length, or of "
                "a trip-length range within the price graph's page-load budget, between "
                "airports or metro codes Google Flights can ask for (up to 11 airports a "
                "leg), whose every filter its search page can carry; "
                f"this is {_safe_text(blocker)}. Run without --fast for Matrix.[/]"
            )
        raise typer.Exit(1)
    if gf_mode == TRANSPORT_HTTP:
        _run_fast_calendar_grid(
            search,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            matrix_url=matrix_url,
            google_url=google_url,
            json_out=json_out,
        )
        return
    # `auto` is the browser here: under `--fast` the http grid has nothing to
    # serve, so escalating is the only way `auto` differs from failing.
    _run_fast_browser_grid(
        search,
        origins=origins,
        dests=dests,
        sd=sd,
        ed=ed,
        json_out=json_out,
        headed=gf_headed,
        matrix_url=matrix_url,
        google_url=google_url,
    )


def _calendar_without_fast(
    search: CalendarSearch,
    *,
    gf_mode: GfTransportMode,
    headed: bool,
    transport_named: bool,
    json_out: bool,
    one_way: bool,
    origins: tuple[str, ...],
    dests: tuple[str, ...],
    sd: date,
    ed: date,
    dmin: int,
    dmax: int,
    rps: float | None,
    impersonate: str | None,
    no_cache: bool,
    max_per_query: int,
    max_concurrency: int,
    matrix_url: bool,
    google_url: bool,
    asked: tuple[str, ...],
) -> None:
    """The calendar Matrix answers: alone, beside Google's price graph, or under
    `--gf-transport http` behind the RPC grid's weave.

    A graph not asked is the envelope's note on `price_graph` and, outside
    `--gf-transport http`, which reads no graph, one stderr line naming why in
    every format. It narrows nothing: Matrix's grid is the answer, and the
    graph is Google's estimate beside it.

    The settings are resolved after the gate, as they always were, and before the
    graph's worker starts: a bad rps setting ends the command, and should do so
    before Chrome is launched for it."""
    weave = False
    not_asked: str | None = None
    if gf_mode == TRANSPORT_HTTP:
        weave = (
            _grid_branch_blocker(
                search, json_out=json_out, one_way=one_way, origins=origins, dests=dests
            )
            is None
        )
        _envelope.explain("price_graph", "not asked: --gf-transport http reads no price graph")
    else:
        blocker = _default_graph_blocker(search, one_way=one_way, origins=origins, dests=dests)
        if blocker is not None:
            not_asked = f"this is {blocker}"
        elif json_out and not transport_named:
            fmt = "envelope" if _envelope.active() else "json"
            not_asked = (
                f"--format {fmt} reads it only when --gf-transport names browser or auto, "
                "which opens Chrome"
            )
        if not_asked is not None:
            # Beside a machine format it is one line, as the unpriced lines are, so
            # a reader matches it whole; the table wraps it to the terminal.
            if json_out:
                err.print(
                    f"[dim]Google Flights price graph not asked: {_safe_text(not_asked)}.[/]",
                    soft_wrap=True,
                )
            else:
                err.print(f"[dim]Google Flights price graph not asked: {_safe_text(not_asked)}.[/]")
            _envelope.explain("price_graph", f"not asked: {not_asked}")
    rps_now = _resolve_rps(rps)
    impersonate_now = _resolve_impersonate(impersonate)
    no_cache_now = _resolve_no_cache(no_cache)
    if weave:
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
            rps=rps_now,
            impersonate=impersonate_now,
            no_cache=no_cache_now,
            matrix_url=matrix_url,
            google_url=google_url,
        )
        return

    def _matrix(*, deliver: bool = True) -> CalendarResult:
        return _run_matrix_calendar(
            search,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            dmin=dmin,
            dmax=dmax,
            rps=rps_now,
            impersonate=impersonate_now,
            no_cache=no_cache_now,
            max_per_query=max_per_query,
            max_concurrency=max_concurrency,
            json_out=json_out,
            matrix_url=matrix_url,
            google_url=google_url,
            deliver=deliver,
        )

    def _write(res: CalendarResult, graph: dict[str, Any] | None) -> None:
        _write_matrix_calendar(
            res,
            search,
            origins=origins,
            dests=dests,
            sd=sd,
            ed=ed,
            dmin=dmin,
            dmax=dmax,
            json_out=json_out,
            matrix_url=matrix_url,
            google_url=google_url,
            graph=graph,
        )

    if gf_mode == TRANSPORT_HTTP or not_asked is not None:
        _matrix()
        return
    _run_calendar_beside_graph(
        search,
        partial(_matrix, deliver=not json_out),
        deliver=_write if json_out else None,
        headed=headed,
        origins=origins,
        dests=dests,
        sd=sd,
        ed=ed,
        asked=asked,
    )


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
    cabin: str = typer.Option(
        "economy", "--cabin", autocompletion=_complete_cabin, rich_help_panel=_GROUP_ITINERARY
    ),
    adults: int = typer.Option(1, "--adults", rich_help_panel=_GROUP_ITINERARY),
    children: int = typer.Option(0, "--children", rich_help_panel=_GROUP_ITINERARY),
    seniors: int = typer.Option(0, "--seniors", rich_help_panel=_GROUP_ITINERARY),
    youth: int = typer.Option(0, "--youth", rich_help_panel=_GROUP_ITINERARY),
    routing: str | None = typer.Option(None, "--routing", rich_help_panel=_GROUP_FILTERING),
    extension: str | None = typer.Option(
        None, "--extension", "--ext", rich_help_panel=_GROUP_FILTERING
    ),
    routing_return: str | None = typer.Option(
        None, "--routing-ret", help=_ROUTING_RET_HELP, rich_help_panel=_GROUP_FILTERING
    ),
    extension_return: str | None = typer.Option(
        None, "--ext-ret", help=_EXT_RET_HELP, rich_help_panel=_GROUP_FILTERING
    ),
    depart_times: Annotated[
        str | None,
        typer.Option(
            "--depart-times",
            help="Outbound times-of-day, as the calendar was asked (comma list: morning,midday).",
            autocompletion=_complete_times,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    return_times: Annotated[
        str | None,
        typer.Option(
            "--return-times",
            help="Return times-of-day, as the calendar was asked.",
            autocompletion=_complete_times,
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = None,
    stops: int | None = typer.Option(None, "--stops", rich_help_panel=_GROUP_ITINERARY),
    allow_airport_changes: bool = typer.Option(
        True,
        "--allow-airport-changes/--no-airport-changes",
        rich_help_panel=_GROUP_FILTERING,
    ),
    only_available: Annotated[
        bool,
        typer.Option(
            "--only-available/--include-unavailable",
            help="Show only itineraries with seats available for sale, as the calendar was asked.",
            rich_help_panel=_GROUP_FILTERING,
        ),
    ] = True,
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
    currency: Annotated[
        str | None,
        typer.Option("--currency", help=_CURRENCY_HELP, rich_help_panel=_GROUP_OUTPUT),
    ] = None,
    no_cache: bool = _NO_CACHE_OPT,
) -> None:
    """Phase-2 of the calendar flow: full itineraries for a picked date."""
    json_out = _resolve_format(fmt=fmt, json_flag=json_out) == "json"
    ccy = _resolve_currency(currency)
    origins = _parse_iata_list(origin)
    dests = _parse_iata_list(destination)
    dep_d = _parse_date(dep)
    ret_d = _parse_date(ret) if ret else None
    if ret_d is None:
        # `--routing-ret ''` asks for no codes on the return, so an empty value counts.
        names = [
            name
            for name, value in (
                ("--return-times", return_times),
                ("--routing-ret", routing_return),
                ("--ext-ret", extension_return),
            )
            if value is not None
        ]
        if names:
            err.print(
                f"[red]{_safe_text(', '.join(names))} set the return's filters, and need a "
                "--return.[/] Drop them, or add --return."
            )
            raise typer.Exit(2)
    sd = _parse_date(start) if start else dep_d
    ed = _parse_date(end) if end else sd + timedelta(days=30)
    dmin, dmax = _resolve_duration(duration, round_trip=ret_d is not None)

    legs = (
        Leg.of(
            origins,
            dests,
            dep_d,
            route_language=routing,
            extension=extension,
            time_ranges=_parse_times(depart_times),
        ),
    )
    if ret_d:
        ret_routing, ret_extension = _return_codes(
            routing=routing,
            extension=extension,
            routing_return=routing_return,
            extension_return=extension_return,
        )
        legs += (
            Leg.of(
                dests,
                origins,
                ret_d,
                route_language=ret_routing,
                extension=ret_extension,
                time_ranges=_parse_times(return_times),
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
        currency=ccy,
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
    _render_search(res, passengers=opts.pax.total)
    _emit_urls(search, matrix_url=matrix_url, google_url=google_url, result=res)


# ─────────────────────────── explore ───────────────────────────────────────

_RE_MONTH = re.compile(r"\A(\d{4})-(\d{2})\Z")


def _today() -> date:
    """Today, behind a seam: which months `--month` may name moves with it."""
    return date.today()


def _explore_origin(origin: str) -> str:
    """The one airport `explore` flies from, or exit 2.

    Refused with the search path's own reason where Google Flights cannot take
    the code, since the page would answer for somewhere else."""
    codes = _parse_iata_list(origin)
    if len(codes) != 1 or not re.fullmatch(r"[A-Z]{3}", codes[0]):
        err.print(f"[red]explore takes one origin airport code; got {_quote(origin)}[/]")
        raise typer.Exit(2)
    reasons = _gf_unserveable_reasons(BACKEND_GFLIGHT, codes[0], None)
    if reasons:
        err.print(f"[red]Google Flights' explore page can't take {_safe_text(reasons[0])}.[/]")
        raise typer.Exit(2)
    return codes[0]


def _explore_month(month: str | None) -> date | None:
    """The first day of the month `--month` names, None for the next six months,
    or exit 2.

    The page's month field carries no year, so it is limited to the months it
    can mean unambiguously; a later one would silently come back as another
    year's."""
    if month is None:
        return None
    m = _RE_MONTH.match(month.strip())
    try:
        first = None if m is None else date(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        first = None
    if first is None:
        err.print(f"[red]bad month {_quote(month)}; use YYYY-MM[/]")
        raise typer.Exit(2)
    from ._gf_explore import months_open  # noqa: PLC0415 — fli, paid only by explore

    open_months = months_open(_today())
    if first not in open_months:
        err.print(
            f"[red]--month {_quote(month)} is outside "
            f"{_safe_text(f'{open_months[0]:%Y-%m}')} to {_safe_text(f'{open_months[-1]:%Y-%m}')}: "
            "the page's month carries no year, so a month further out would "
            "return another year's.[/]"
        )
        raise typer.Exit(2)
    return first


def _explore_trip_length(days: str | None) -> TripLength:
    """The page's trip length `--days` overlaps, or exit 2. It offers three,
    and a range that overlaps none or several has no single answer."""
    from ._gf_explore import ONE_WEEK, TRIP_LENGTHS, trip_lengths_overlapping  # noqa: PLC0415

    if days is None:
        return ONE_WEEK
    lo, hi = _parse_duration(days)
    hits = trip_lengths_overlapping(lo, hi)
    if len(hits) != 1:
        choices = ", ".join(f"{t.name} ({t.nights[0]}-{t.nights[1]} nights)" for t in TRIP_LENGTHS)
        overlap = " and ".join(t.name for t in hits) or "no trip length"
        err.print(
            f"[red]--days {_quote(days)} overlaps {_safe_text(overlap)}; it must overlap "
            f"exactly one of the page's trip lengths: {_safe_text(choices)}.[/]"
        )
        raise typer.Exit(2)
    return hits[0]


def _explore_dates(d: Destination) -> str:
    dep = "—" if d.departure is None else d.departure.isoformat()
    ret = "—" if d.return_date is None else d.return_date.isoformat()
    return f"{dep} → {ret}"


def _stops_label(stops: int | None) -> str:
    if stops is None:
        return "—"
    return "nonstop" if stops == 0 else f"{stops} stop{'' if stops == 1 else 's'}"


def _render_explore(
    answer: ExploreAnswer,
    *,
    origin: str,
    month: date | None,
    trip: TripLength,
    max_price: int | None,
) -> None:
    """The priced destinations, cheapest first, one row each."""
    when = "the next six months" if month is None else f"{month:%B %Y}"
    t = Table(
        title=f"Google Flights explore · from {_safe_text(origin)} · {_safe_text(when)} · "
        f"{_safe_text(trip.name)} ({trip.nights[0]:d}-{trip.nights[1]:d} nights)"
        + ("" if max_price is None else f" · up to {_safe_text(answer.currency)}{max_price:d}"),
        show_header=True,
        header_style="bold green",
    )
    for name in ("#", "destination", "airport", "price", "dates", "nights", "carrier", "stops"):
        t.add_column(_safe_text(name), justify="right" if name in ("#", "price") else "left")
    t.add_column("duration")
    for i, d in enumerate(answer.priced, 1):
        t.add_row(
            f"{i:d}",
            _safe_text(", ".join(x for x in (d.name, d.country) if x) or "—"),
            _safe_text(d.code or "—"),
            "—" if d.price is None else f"{_safe_text(answer.currency)}{d.price:.2f}",
            _safe_text(_explore_dates(d)),
            "—" if d.nights is None else f"{d.nights:d}",
            _safe_text(d.carrier or "—"),
            _safe_text(_stops_label(d.stops)),
            "—"
            if d.duration_min is None
            else f"{d.duration_min // 60:d}h{d.duration_min % 60:02d}m",
        )
    console.print(t)


@app.command()
def explore(
    origin: Annotated[str, typer.Argument(help="Origin airport (IATA).")],
    month: Annotated[
        str | None,
        typer.Option(
            "--month",
            help="YYYY-MM: this month or one of the next five. Default: the next six months.",
        ),
    ] = None,
    days: Annotated[
        str | None,
        typer.Option(
            "--days",
            help="Trip length in nights, 'A-B'. Picks the page's weekend (1-4), one "
            "week (6-9) or two weeks (13-16); a range must overlap exactly one. "
            "Default: one week.",
        ),
    ] = None,
    max_price: Annotated[
        int | None,
        typer.Option(
            "--max-price", min=1, help="List only destinations priced at or under this, in USD."
        ),
    ] = None,
    fmt: str = _FORMAT_OPT,
    gf_headed: bool = typer.Option(
        False, "--gf-headed", help="Show the Chrome window explore opens. Default: headless."
    ),
) -> None:
    """Where ORIGIN flies, and the cheapest round trip to each: Google Flights' explore page.

    Reads the page in Chrome (the browser extra). Only priced destinations are
    listed, cheapest first; a price cap leaves the rest unpriced."""
    from ._gf_browser import interrupt_guard, session_scope  # noqa: PLC0415 — patchright
    from ._gf_explore import document  # noqa: PLC0415 — fli, paid only by explore
    from ._gf_explore import explore as read_explore  # noqa: PLC0415

    json_out = _resolve_format(fmt=fmt, json_flag=False) == "json"
    code = _explore_origin(origin)
    first = _explore_month(month)
    trip = _explore_trip_length(days)
    url = google_flights_explore_url(
        code,
        month=None if first is None else first.month,
        trip_length=trip.code,
        max_price=max_price,
    )
    try:
        with interrupt_guard(), session_scope():
            answer = read_explore(url, origin=code, month=first, headed=gf_headed)
    except GfBackendError as e:
        _report_page_refusal("No explore results", e)
        raise typer.Exit(1) from e
    priced = answer.priced
    unpriced = len(answer.destinations) - len(priced)
    if not priced and max_price is None:
        _no_explore_results(
            f"Google Flights' explore page priced none of its {unpriced:d} destinations."
        )
    if json_out:
        sys.stdout.write(json.dumps(document(answer), indent=2))
    elif priced:
        _render_explore(answer, origin=code, month=first, trip=trip, max_price=max_price)
    elif max_price is not None:
        console.print(
            f"[yellow]No destination from {_safe_text(code)} is priced at or under "
            f"{_safe_text(answer.currency)}{max_price:d}.[/]"
        )
    if unpriced:
        err.print(f"[dim]{unpriced:d} more destinations have no price under this query.[/]")


def _no_explore_results(why: str) -> NoReturn:
    err.print(f"[red]No explore results:[/] {_safe_text(why)}")
    raise typer.Exit(1)


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
    # rather than forcing Google Flights: a party the page can't take goes to
    # the backend that can price it rather than erroring on a query the alias
    # accepts. `_pick_backend` prints the reason either way.
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
        adults=adults,
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
    from ._gflight_ids import search_escalation  # noqa: PLC0415 — fli, ~95 ms

    with search_escalation():
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


@app.command()
def doctor(fmt: str = _FORMAT_OPT) -> None:
    """Pass, fail or skip for every backend, transport and credential.

    Runs one live search per backend (JFK-LAX, 30 days out) and spends one unit
    of the seats.aero daily quota when a key is stored. Exits 0 with no failure,
    75 when every failure is a throttle, brownout or outage worth retrying, and
    1 otherwise."""
    from . import _doctor  # noqa: PLC0415 — fli and every provider client, paid only here

    json_out = _resolve_format(fmt=fmt, json_flag=False) == "json"
    report = _doctor.run(on_start=lambda what: err.print(f"[dim]{_safe_text(what)}…[/]"))
    if json_out:
        sys.stdout.write(json.dumps(report.document(), indent=2))
        raise typer.Exit(report.exit_code)
    t = Table(title="flight doctor")
    t.add_column("check", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("detail", overflow="fold")
    t.add_column("time", justify="right", no_wrap=True)
    for c in report.checks:
        t.add_row(
            _safe_text(c.id),
            "[green]pass[/]"
            if c.status == "pass"
            else "[bold red]FAIL[/]"
            if c.status == "fail"
            else "[yellow]skip[/]",
            _safe_text(c.detail)
            if c.cause is None
            else f"{_safe_text(c.cause)}: {_safe_text(c.detail)}",
            f"{c.seconds:.1f}s" if c.seconds is not None else "",
        )
    console.print(t)
    statuses = [c.status for c in report.checks]
    console.print(
        f"{statuses.count('pass'):d} passed, {statuses.count('fail'):d} failed, "
        f"{statuses.count('skip'):d} skipped"
        + (
            " — every failure is retryable; run again later"
            if report.exit_code == _doctor.EX_TEMPFAIL
            else ""
        )
    )
    raise typer.Exit(report.exit_code)


@app.command()
def explain(
    routing: Annotated[
        str,
        typer.Argument(
            help="A routing string as --routing takes it, e.g. 'O:LH+' or 'F* X:LHR F*'"
        ),
    ],
) -> None:
    """Say what a routing string means, one line per token.

    Exits 1 when a token is not in the documented grammar; it is never guessed."""
    unread = False
    for token, meaning in decode_routing(routing):
        if meaning is None:
            unread = True
            console.print(f"{_quote(token)}  ->  not recognized", soft_wrap=True, highlight=False)
        else:
            console.print(
                f"{_safe_text(token)}  ->  {_safe_text(meaning)}", soft_wrap=True, highlight=False
            )
    if unread:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
