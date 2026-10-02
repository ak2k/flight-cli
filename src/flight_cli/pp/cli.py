"""CLI surface for PointsPath: `auth pp ...` subcommands + augmentation entry.

`run_pp_for_search` is wired into `flight search` (both backends): it runs
implicitly whenever any provider's tokens are present (opt out with `--no-pp`).
Asks the provider registry about each airport pair of each leg, joins via
match.py, and renders one entry per leg.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby, islice
from pathlib import Path  # noqa: TC003 - typer evaluates annotations at runtime
from typing import TYPE_CHECKING, Annotated, Any, Final

import anyio
import typer
from rich.console import Console
from rich.table import Table

from .._console_text import safe_text as _safe_text
from ..providers.base import award_run
from ..providers.registry import gather_awards
from .auth import (
    TOKENS_PATH,
    PPAuthError,
    Tokens,
    clear_tokens,
    get_valid_tokens,
    import_from_tokens_file,
    load_tokens,
    login_from_chrome,
    login_via_browser,
)
from .client import DEFAULT_CABINS, CashFlightHint
from .gflight_adapter import cash_hints_from_search_result
from .match import MatchedFare, join

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..models import SearchResult
    from ..providers.base import AwardFlight, LegQuery, ProviderFailure


console = Console()
err = Console(stderr=True)


# ─────────────────────────── auth pp subcommands ────────────────────────────

auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Auth helpers per provider.",
)
pp_auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="PointsPath token management.",
)
auth_app.add_typer(pp_auth_app, name="pp")

# Seats.aero sub-app is registered inline here to avoid a circular import
# (providers/seats_aero/auth.py is leaner than pp/auth.py and we keep the
# Typer wiring in this file rather than fanning out per-provider auth CLIs).
from ..providers.seats_aero import auth as _seats_auth  # noqa: E402

seats_auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Seats.aero API key management.",
)
# Canonical mount point. `sa` is registered below as a convenience alias —
# same sub-app, different name. Users can type either `flight auth seats-aero`
# or `flight auth sa`; both reach the same commands.
auth_app.add_typer(seats_auth_app, name="seats-aero")
auth_app.add_typer(seats_auth_app, name="sa")


@seats_auth_app.command("key")
def seats_key(api_key: Annotated[str, typer.Argument(help="Partner API key (pro_...)")]) -> None:
    """Save a Seats.aero Pro API key to ~/.config/flight-cli/seats.json.

    Overwrites any existing value. The file is written with 0600 perms.
    Alternative: set the SEATS_AERO_API_KEY env var, which takes precedence
    over the on-disk value at runtime.
    """
    _seats_auth.save_key(api_key)
    console.print(f"[green]Saved Seats.aero key to {_safe_text(_seats_auth.KEY_PATH)}.[/]")


@seats_auth_app.command("whoami")
def seats_whoami() -> None:
    """Show whether a key is configured and probe the current quota.

    Hits /partnerapi/search with a tiny query to grab the latest
    X-RateLimit-Remaining header. Uses ~1 of your 1000/day quota.
    """
    key = _seats_auth.load_key()
    if key is None:
        err.print("[yellow]No Seats.aero key configured.[/]")
        err.print(
            f"Run `flight auth seats-aero key <KEY>` or set {_safe_text(_seats_auth.API_KEY_ENV)}."
        )
        raise typer.Exit(1)
    source = (
        "env" if _seats_auth.os.environ.get(_seats_auth.API_KEY_ENV) else str(_seats_auth.KEY_PATH)
    )
    console.print(f"[green]Seats.aero key configured[/] (source: {_safe_text(source)})")

    # Probe the quota. We import lazily to avoid pulling httpx unless asked.
    from ..providers.seats_aero.client import SeatsAeroClient, SeatsAeroError  # noqa: PLC0415

    async def _probe() -> None:
        async with SeatsAeroClient(api_key=key) as c:
            try:
                # Smallest-possible call: JFK-LHR, today, take=1, no trips.
                await c.search(
                    origin="JFK",
                    destination="LHR",
                    start_date=datetime.now(UTC).date().isoformat(),
                    end_date=datetime.now(UTC).date().isoformat(),
                    include_trips=False,
                    take=1,
                )
            except SeatsAeroError as e:
                err.print(f"[red]Probe failed: HTTP {e.status:d}[/]")
                raise typer.Exit(1) from e
            rl = c.last_rate_limit
            if rl is None:
                console.print("[yellow]No rate-limit headers returned.[/]")
            else:
                console.print(
                    f"Quota: {rl.remaining:d}/{rl.limit:d} remaining "
                    f"(resets in {rl.reset_seconds:d}s)"
                )

    anyio.run(_probe)


@seats_auth_app.command("logout")
def seats_logout() -> None:
    """Delete the on-disk Seats.aero key file.

    Does not affect SEATS_AERO_API_KEY env var (which takes precedence
    when set). After logout, the provider's `is_configured()` returns
    False and `flight search --providers seats` will error.
    """
    if _seats_auth.clear_key():
        console.print(f"[green]Deleted {_safe_text(_seats_auth.KEY_PATH)}.[/]")
    else:
        console.print("[yellow]No on-disk Seats.aero key to delete.[/]")


@pp_auth_app.command("login")
def pp_login(
    tokens_file: Annotated[
        Path | None,
        typer.Option(
            "--tokens-file",
            "-f",
            help="Import tokens from a JSON file (e.g. previously captured Supabase session).",
        ),
    ] = None,
    from_chrome: Annotated[
        bool,
        typer.Option(
            "--from-chrome",
            help=(
                "Read tokens from a local Chrome profile via rookiepy. "
                "Inherits Chrome's session (and its rotation chain — Chrome and "
                "the CLI may race on refresh). Convenience path; prefer the "
                "default headed browser login for a clean, independent session."
            ),
        ),
    ] = False,
) -> None:
    """Authenticate with PointsPath. Three modes (mutually exclusive).

    Default (no flag): open a headed Patchright Chrome so you can sign
    in normally. Captures the resulting Supabase session into
    ~/.config/flight-cli/pp.json. Independent of any user-facing Chrome
    PP session. Uses Patchright (a Playwright fork that patches the CDP
    Runtime.enable leak + navigator.webdriver) to clear Cloudflare's
    bot fingerprint check. Needs patchright at runtime (ephemeral or
    installed): README PP Setup covers `uv run --with patchright` + the
    one-time `uvx --from patchright patchright install chrome`.

    --from-chrome: import the session from your local Chrome profile via
    cookies. Quicker, but the CLI then shares Chrome's refresh-token
    chain (Supabase rotates single-use, so a refresh on one side will
    eventually invalidate the other).

    --tokens-file PATH: import from a pre-captured JSON file. Expected
    shape: {access_token, refresh_token, user.email}.
    """
    modes = [bool(tokens_file), from_chrome]
    if sum(modes) > 1:
        err.print("[red]Pick at most one of --tokens-file or --from-chrome.[/]")
        raise typer.Exit(2)

    try:
        t: Tokens
        if tokens_file is not None:
            t = import_from_tokens_file(tokens_file)
            source = f"{tokens_file}"
        elif from_chrome:
            t = login_from_chrome()
            source = "local Chrome cookies"
        else:
            console.print(
                "[dim]Opening a browser to PointsPath. Sign in normally; "
                "the CLI will capture your session and close the browser.[/]"
            )
            t = login_via_browser()
            source = "headed browser login"
    except (PPAuthError, OSError, json.JSONDecodeError, KeyError) as e:
        err.print(f"[red]Login failed ({_safe_text(type(e).__name__)}): {_safe_text(e)}[/]")
        raise typer.Exit(1) from e

    when = datetime.fromtimestamp(t.expires_at).isoformat() if t.expires_at else "?"
    console.print(
        f"[green]Saved[/] tokens for [bold]{_safe_text(t.user_email or '?')}[/] "
        f"to {_safe_text(TOKENS_PATH)}\n  source: {_safe_text(source)}\n"
        f"  access_token expires: {_safe_text(when)}"
    )


@pp_auth_app.command("whoami")
def pp_whoami() -> None:
    """Print the authenticated user, expiry, and token store path."""
    t = load_tokens()
    if t is None:
        err.print("[yellow]Not logged in.[/] Run `flight-cli auth pp login --tokens-file ...`.")
        raise typer.Exit(1)
    claims = t.jwt_claims()
    when = datetime.fromtimestamp(t.expires_at).isoformat() if t.expires_at else "?"
    email = t.user_email or claims.get("email") or "?"
    console.print(f"email:   [bold]{_safe_text(email)}[/]")
    console.print(f"sub:     {_safe_text(claims.get('sub', '?'))}")
    console.print(f"role:    {_safe_text(claims.get('role', '?'))}")
    console.print(f"expires: {_safe_text(when)}")
    console.print(f"store:   {_safe_text(TOKENS_PATH)}")


@pp_auth_app.command("logout")
def pp_logout() -> None:
    """Delete the on-disk PointsPath token store."""
    if clear_tokens():
        console.print(f"[green]Deleted[/] {_safe_text(TOKENS_PATH)}")
    else:
        console.print(f"Nothing to delete ({_safe_text(TOKENS_PATH)} doesn't exist).")


# ───────────────────── augmentation entry point for `fare` ──────────────────


def _parse_csv(s: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not s:
        return default
    return tuple(part.strip() for part in s.split(",") if part.strip())


_CABIN_ALIASES = {
    "y": "Economy",
    "economy": "Economy",
    "coach": "Economy",
    "main": "Economy",
    "w": "Premium economy",
    "premium": "Premium economy",
    "premiumeconomy": "Premium economy",
    "j": "Business",
    "business": "Business",
    "f": "First",
    "first": "First",
}


def _normalize_cabin(c: str) -> str:
    k = c.strip().lower().replace(" ", "").replace("-", "")
    return _CABIN_ALIASES.get(k, c)


# One airport-pair query costs PointsPath a request per cabin and airline, and
# seats.aero one unit of its daily quota, so a metro round trip (`NYC LON` is
# 36 pairs) is cut to this many and the output names the pairs left unasked.
MAX_AWARD_PAIR_QUERIES: Final = 8

# PointsPath rejects a very large `googleFlightDetails` array, so one pair
# query carries at most this many cash hints.
_HINTS_PER_QUERY: Final = 50


def _pair_query_cap(n_legs: int) -> int:
    """The most pair queries one search asks: never fewer than one a leg."""
    return max(MAX_AWARD_PAIR_QUERIES, n_legs)


@dataclass(frozen=True)
class _AwardLeg:
    """One leg of an award search: the pair queries asked, in the order they
    go out, and those the cap left unasked."""

    label: str
    slice_index: int
    asked: tuple[LegQuery, ...]
    not_asked: tuple[LegQuery, ...]


def _cash_flown_first(res: SearchResult, queries: list[LegQuery]) -> list[LegQuery]:
    """One leg's queries with the pairs a cash row of `res` flies at that
    slice first, in row order, then the rest in typed order: the awards a
    capped search asks for are the ones the cash rows can show."""
    if len(queries) == 1:
        return queries
    slice_index = queries[0].slice_index
    flown: dict[tuple[str, str], int] = {}
    for it in res.solutions:
        itn = it.itinerary
        if not itn or slice_index >= len(itn.slices):
            continue
        s = itn.slices[slice_index]
        o = ((s.origin.code if s.origin else None) or "").upper()
        d = ((s.destination.code if s.destination else None) or "").upper()
        flown.setdefault((o, d), len(flown))
    return sorted(
        queries,
        key=lambda q: flown.get((q.origin.upper(), q.destination.upper()), len(flown)),
    )


def _plan_pair_queries(res: SearchResult, queries: Sequence[LegQuery]) -> list[_AwardLeg]:
    """`queries` regrouped into legs (consecutive queries with one
    slice_index) and cut to the cap, which is dealt one query per leg per
    round so every leg is asked at least its first pair. A search with more
    legs than the cap is asked one pair a leg."""
    legs = [
        _cash_flown_first(res, list(group))
        for _, group in groupby(queries, key=lambda q: q.slice_index)
    ]
    budget = _pair_query_cap(len(legs))
    asked = [0] * len(legs)
    for round_ in range(max((len(leg) for leg in legs), default=0)):
        for i, leg in enumerate(legs):
            if budget and round_ < len(leg):
                asked[i] += 1
                budget -= 1
    return [
        _AwardLeg(leg[0].label, leg[0].slice_index, tuple(leg[:n]), tuple(leg[n:]))
        for leg, n in zip(legs, asked, strict=True)
    ]


def _not_asked_line(leg: _AwardLeg, cap: int) -> str:
    pairs = ", ".join(f"{q.origin}→{q.destination}" for q in leg.not_asked)
    total = len(leg.asked) + len(leg.not_asked)
    return (
        f"Awards for {leg.label}: asked {len(leg.asked)} of {total} airport pairs "
        f"(at most {cap} a search); not asked: {pairs}"
    )


def _counted(reasons: Sequence[str]) -> str:
    """Each reason once, the most frequent first, with a count when repeated:
    `ReadTimeout, 3 queries; HTTP 500`."""
    tally = sorted(Counter(reasons).items(), key=lambda kv: (-kv[1], kv[0]))
    return "; ".join(f"{reason}, {n} queries" if n > 1 else reason for reason, n in tally)


def _awards_incomplete_line(failures: Sequence[ProviderFailure]) -> str | None:
    """Every failure the providers swallowed in one search, as one line, or None
    when there was none. Each provider and each airline is named once.

    Plain text with remote parts in it: the caller wraps it before printing."""
    if not failures:
        return None
    clauses: list[str] = []
    for provider in sorted({f.provider for f in failures}):
        own = [f for f in failures if f.provider == provider]
        whole = [f.reason for f in own if f.airline is None]
        per_airline: dict[str, list[str]] = {}
        for f in own:
            if f.airline is not None:
                per_airline.setdefault(f.airline, []).append(f.reason)
        said: list[str] = []
        if whole:
            said.append(f"failed ({_counted(whole)})")
        if per_airline:
            airlines = sorted(per_airline.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            said.append(
                "did not answer for "
                + ", ".join(f"{airline} ({_counted(rs)})" for airline, rs in airlines)
            )
        clauses.append(f"{provider} {' and '.join(said)}")
    return f"Awards incomplete: {'; '.join(clauses)}."


def _print_awards_incomplete(failures: Sequence[ProviderFailure]) -> None:
    line = _awards_incomplete_line(failures)
    if line is not None:
        err.print(_safe_text(line), style="yellow", highlight=False, soft_wrap=True)


def run_pp_for_search(
    res: SearchResult,
    *,
    legs: list[LegQuery],
    num_passengers: int = 1,
    airlines: str | None = None,
    cabins: str | None = None,
    pp_only: bool = False,
    json_out: bool = False,
    provider_filter: tuple[str, ...] | None = None,
    seats_sources: tuple[str, ...] | None = None,
    cash_per_cabin: Mapping[int, Mapping[str, float]] | None = None,
    bags_included: Mapping[int, Sequence[tuple[int | None, int | None]]] | None = None,
) -> None:
    """Run award augmentation through the provider registry, join against
    `res`'s cash itineraries, render. Registry hands back any configured
    providers (PointsPath, Seats.aero, ...); the matcher is provider-blind.

    `legs` holds one query per airport pair, a leg's queries consecutive and
    sharing its slice_index. At most `MAX_AWARD_PAIR_QUERIES` are asked
    (`_plan_pair_queries`); a leg the cap cut is named on stderr, in table
    and JSON runs alike, and its JSON entry lists the pairs not asked. A
    leg's answers are joined, rendered and serialized as one entry.

    Errors are non-fatal — print and continue so the user still sees their
    cash results. Every failure a provider swallowed during the fan-out is
    named in one `Awards incomplete:` line on stderr, in table and JSON runs
    alike, and none at all when there was none.

    The `pp_only` arg is named for historical reasons; today it means
    "render in awards-only mode" — applies to whatever providers were
    selected, not just PP.

    `cash_per_cabin` maps `id(itinerary) -> {pp_cabin_name: cash_usd}` so
    the renderer can compute cents-per-mile per cabin against the right
    cash basis (business miles vs business cash, not business miles vs
    economy cash). Callers should build it from the cabins they queried —
    single-cabin invocations pass a one-entry inner dict; multi-cabin
    passes one entry per queried cabin. When None, no CPM is shown.

    `bags_included` maps `id(itinerary)` to the `(checked, carry_on)` bags
    Google says each of its slices is priced with, None where it does not say.
    When given, each cash match in the JSON document carries its slice's.
    """
    # PP tokens are required only if PP is actually going to run. Skip the
    # pre-flight check when the filter excludes PP — otherwise a seats-only
    # invocation errors here before seats even gets a chance to run.
    pp_in_filter = provider_filter is None or any(
        p.strip().lower() == "pp" for p in provider_filter
    )
    if pp_in_filter:
        try:
            get_valid_tokens()  # validate + refresh up-front, surface a clear error
        except PPAuthError as e:
            # Soft-warn if PP fails but other providers might still run.
            # When PP is the only target (filter explicitly == "pp"), it's
            # a hard error; otherwise log and continue.
            if provider_filter == ("pp",):
                err.print(f"[red]--pp: {_safe_text(e)}[/]")
                return
            err.print(f"[yellow]PointsPath skipped: {_safe_text(e)}[/]")

    cabin_list = tuple(_normalize_cabin(c) for c in _parse_csv(cabins, DEFAULT_CABINS))
    explicit_airlines = _parse_csv(airlines, ()) if airlines else None

    plan = _plan_pair_queries(res, legs)
    cap = _pair_query_cap(len(plan))
    for leg in plan:
        if leg.not_asked:
            err.print(
                _safe_text(_not_asked_line(leg, cap)),
                style="yellow",
                highlight=False,
                soft_wrap=True,
            )
    queries = [q for leg in plan for q in leg.asked]

    # If `res` carries gflight-captured opaque flight IDs on its slices, build
    # PP cash hints per query so the request goes out with enable_matching=True
    # and the matcher's matched-id key becomes available. Matrix-built
    # SearchResults won't have flight_id populated; hints stays empty. A query
    # carries only the hints of rows on its own pair, so a provider is never
    # handed an id it could echo onto an award from another airport. The cap
    # applies after that filter: another airport's rows filling it would send
    # a pair with no hint, and so with matching off.
    slice_hints = {
        leg.slice_index: cash_hints_from_search_result(
            res, slice_index=leg.slice_index, max_hints=sys.maxsize
        )
        for leg in plan
    }
    cash_hints_per_query: list[tuple[CashFlightHint, ...]] = [
        tuple(
            islice(
                (
                    h
                    for h in slice_hints[q.slice_index]
                    if (h.origin, h.dest) == (q.origin.upper(), q.destination.upper())
                ),
                _HINTS_PER_QUERY,
            )
        )
        for q in queries
    ]

    async def _go() -> tuple[list[list[AwardFlight]], list[ProviderFailure]]:
        # Construct, query, AND close providers all within this single event
        # loop. Providers hold async HTTP transports (curl_cffi/httpx) whose
        # sockets are bound to the running loop; closing them in a *second*
        # anyio.run() — after this loop has closed — makes their teardown fire
        # `loop.call_soon` on a dead loop ("RuntimeError: Event loop is
        # closed", a full traceback + exit 1 on every otherwise-successful
        # run). `per_query` is plain data, safe to return after close.
        with award_run() as run:
            per_query, providers = await gather_awards(
                legs=queries,
                num_passengers=num_passengers,
                cabins=cabin_list,
                pp_airlines=explicit_airlines,
                seats_sources=seats_sources,
                cash_hints_per_leg=cash_hints_per_query,
                provider_filter=provider_filter,
            )
            try:
                return per_query, run.failures
            finally:
                await _aclose_all(providers)

    try:
        per_query, failures = anyio.run(_go)
    except Exception as e:  # noqa: BLE001 — surface anything to user, don't crash CLI
        err.print(f"[red]--pp: award query failed: {_safe_text(e)}[/]")
        return
    _print_awards_incomplete(failures)

    per_leg: list[list[AwardFlight]] = []
    start = 0
    for leg in plan:
        end = start + len(leg.asked)
        per_leg.append([af for awards in per_query[start:end] for af in awards])
        start = end

    if pp_only:
        if json_out:
            sys.stdout.write(_serialize_pp_only_per_leg(per_leg, plan))
            return
        for leg, awards in zip(plan, per_leg, strict=True):
            console.print(f"\n[bold]Leg: {_safe_text(leg.label)}[/]")
            _render_pp_only(awards)
        return

    matches_per_leg: list[list[MatchedFare]] = [
        join(res, awards, slice_index=leg.slice_index)
        for leg, awards in zip(plan, per_leg, strict=True)
    ]
    if json_out:
        sys.stdout.write(_serialize_matches_per_leg(matches_per_leg, plan, bags_included))
        return
    for leg, matches in zip(plan, matches_per_leg, strict=True):
        console.print(f"\n[bold]Leg: {_safe_text(leg.label)}[/]")
        _render_matches(
            matches,
            cabin_list,
            slice_index=leg.slice_index,
            cash_per_cabin=cash_per_cabin,
            num_passengers=num_passengers,
        )


async def _aclose_all(providers: list[Any]) -> None:
    for p in providers:
        aclose = getattr(p, "aclose", None)
        if aclose is not None:
            await aclose()


# ──────────────────────────── render: matched ──────────────────────────────

_CASH_NUM_RE = __import__("re").compile(r"[\d,]*\d+(?:\.\d+)?")


def _parse_cash(s: str | None) -> float | None:
    """Pull the first numeric value out of strings like 'USD530.00', '$1,078',
    '1,078 USD'. Returns None if nothing parseable found."""
    if not s or s in ("—", "-"):
        return None
    m = _CASH_NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cents_per_mile(cash_usd: float, miles: int, tax_usd: float) -> float | None:
    if miles <= 0:
        return None
    net = max(cash_usd - tax_usd, 0.0)
    return (net / miles) * 100


_MILES_K_THRESHOLD = 1000  # render as "30.0k" once we cross 1000 miles


def _fmt_miles(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= _MILES_K_THRESHOLD else str(n)


def _fmt_stops(n: int) -> str:
    """Glanceable stop count for the award tables: 'nonstop' vs 'N stop(s)'.

    The single most important attribute when comparing redemptions is whether
    an award is nonstop — and it was invisible in the table before (work-72syf).
    """
    if n <= 0:
        return "nonstop"
    return f"{n} stop" + ("s" if n > 1 else "")


def _fmt_iso_compact(s: str) -> str:
    """'2026-08-15T13:53' → 'Aug15 13:53'. Compact so the column stops getting
    ellipsized to '→2026-0…'. Passes the raw (minute-trimmed) value through
    when it can't be parsed."""
    try:
        dt = datetime.fromisoformat(s[:16])
    except ValueError:
        return s[:16]
    return f"{dt:%b%d %H:%M}"


def _best_award_for_cabin(
    award_flights: list[AwardFlight], want_cabin: str
) -> tuple[int, float, str, list[str], str, int | None] | None:
    """Cheapest offer across providers for one cabin, or None.

    Ranked on (miles, tax) — miles first, since that is the scarce currency,
    but ties broken on cash out-of-pocket. Comparing miles alone let a
    30k + $500 offer beat an identical 30k + $6 one purely on provider order.

    Basic-economy fares are excluded here rather than silently rendered as
    unrestricted Economy: they carry different seat, bag and change rights, so
    presenting one under the plain "Economy" heading overstates what the
    traveller gets. `_basic_economy_award_for_cabin` surfaces them explicitly.
    """
    best: tuple[int, float, str, list[str], str, int | None] | None = None
    for af in award_flights:
        for ca in af.cabins:
            if ca.cabin != want_cabin or ca.is_basic_economy:
                continue
            key = (
                ca.miles,
                ca.tax_usd,
                af.program,
                af.funding_banks,
                ca.tax_currency,
                ca.remaining_seats,
            )
            if best is None or (key[0], key[1]) < (best[0], best[1]):
                best = key
    return best


def _basic_economy_award_for_cabin(
    award_flights: list[AwardFlight], want_cabin: str
) -> tuple[int, float, str, list[str], str, int | None] | None:
    """Cheapest BASIC-economy offer for one cabin — the fares
    `_best_award_for_cabin` deliberately skips. Rendered with an explicit
    label so the restriction is visible rather than implied."""
    best: tuple[int, float, str, list[str], str, int | None] | None = None
    for af in award_flights:
        for ca in af.cabins:
            if ca.cabin != want_cabin or not ca.is_basic_economy:
                continue
            key = (
                ca.miles,
                ca.tax_usd,
                af.program,
                af.funding_banks,
                ca.tax_currency,
                ca.remaining_seats,
            )
            if best is None or (key[0], key[1]) < (best[0], best[1]):
                best = key
    return best


def _fmt_award_cell(
    award_flights: list[AwardFlight],
    want_cabin: str,
    cash_usd: float | None = None,
    pax: int = 1,
) -> str:
    """Render the best (lowest miles) offer across providers for one cabin.

    When `cash_usd` is provided AND an award exists, append the cabin's
    ¢/mi redemption value on a second line (dim-styled). Inline ¢/mi keeps
    each cabin's miles cost + redemption value adjacent in the table
    without growing the column count — the alternative (separate ¢/mi
    columns per cabin) overflows narrow terminals in multi-cabin renders.
    """
    best = _best_award_for_cabin(award_flights, want_cabin)
    label = ""
    if best is None:
        # Nothing unrestricted — show the basic-economy fare rather than an
        # empty cell, but say so.
        best = _basic_economy_award_for_cabin(award_flights, want_cabin)
        label = " [dim](basic)[/]"
    if best is None:
        return "—"
    miles, tax, program, _banks, tax_ccy, seats = best
    # Print the tax in the currency it is actually denominated in. Formatting a
    # EUR amount as "$" both misstates it and invites the reader to add it to a
    # USD fare.
    tax_str = f"${tax:.0f}" if tax_ccy in ("", "USD") else f"{tax:.0f} {_safe_text(tax_ccy)}"
    # An award with fewer seats than the party cannot be booked for it. The
    # provider reports this; we were discarding it, so a 1-seat fare rendered
    # as available for a party of four. `None` means "not reported" (PointsPath
    # never does), so only an affirmative shortfall is flagged.
    short = (
        f" [yellow]({seats} seat{'s' if seats != 1 else ''})[/]"
        if (seats is not None and pax > 0 and seats < pax)
        else ""
    )
    head = f"{_fmt_miles(miles)} {_safe_text(program)} + {tax_str}{label}{short}"
    if cash_usd is None:
        return head
    # ¢/mi nets the tax off a USD cash fare, so a non-USD tax would silently
    # subtract the wrong magnitude. Suppress rather than convert: we have no
    # rate source, and a wrong valuation is worse than a missing one.
    if tax_ccy not in ("", "USD"):
        return head
    cpm = _cents_per_mile(cash_usd, miles, tax)
    if cpm is None:
        return head
    return f"{head}\n[dim]{cpm:.1f}¢/mi[/]"


def _fmt_funding(award_flights: list[AwardFlight], cabins: tuple[str, ...] = ()) -> str:
    """Transfer partners that fund the award(s) actually DISPLAYED.

    Unioning banks across every attached award claimed that programs funding
    hidden, more-expensive offers also funded the winning one: a 30k Amex
    winner beside a hidden 40k Chase offer rendered "Amex, Chase", implying
    Chase points could buy the 30k fare. Restricted to the offers the row
    shows; `cabins` empty keeps the old union for callers with no cabin
    context.
    """
    winners: list[tuple[int, float, str, list[str], str, int | None]] = []
    for cab in cabins:
        for pick in (
            _best_award_for_cabin(award_flights, cab),
            _basic_economy_award_for_cabin(award_flights, cab),
        ):
            if pick is not None:
                winners.append(pick)
    sources: list[list[str]] = (
        [w[3] for w in winners] if cabins else [af.funding_banks for af in award_flights]
    )
    banks: list[str] = []
    seen: set[str] = set()
    for group in sources:
        for b in group:
            if b not in seen:
                seen.add(b)
                banks.append(b)
    return ", ".join(banks) if banks else ""


def _leg_row_key(m: MatchedFare, slice_index: int) -> tuple[str, str, str, str] | None:
    """First flight, departure date and airports of the slice. The airports
    count because a set search's rows can share a first flight and end at
    different airports, each with its own award."""
    itn = m.itinerary.itinerary
    if not itn or len(itn.slices) <= slice_index:
        return None
    s = itn.slices[slice_index]
    if not s.flights or not s.departure:
        return None
    return (
        s.flights[0].upper().replace(" ", ""),
        s.departure[:10],
        ((s.origin.code if s.origin else None) or "").upper(),
        ((s.destination.code if s.destination else None) or "").upper(),
    )


def _dedupe_per_leg(matches: list[MatchedFare], slice_index: int = 0) -> list[MatchedFare]:
    """Matrix returns the cross-product of outbound x return itineraries, so
    the same leg-flight surfaces in many rows. Collapse to one row per
    `_leg_row_key`, keeping the row with cheapest cash.
    """
    best: dict[tuple[str, str, str, str], MatchedFare] = {}
    for m in matches:
        key = _leg_row_key(m, slice_index)
        if key is None:
            continue
        cash = _parse_cash(m.itinerary.price) or float("inf")
        existing = best.get(key)
        if existing is None or (_parse_cash(existing.itinerary.price) or float("inf")) > cash:
            best[key] = m
    # Preserve original order (cheapest cash first, since `solutions` is sorted).
    seen: set[tuple[str, str, str, str]] = set()
    out: list[MatchedFare] = []
    for m in matches:
        key = _leg_row_key(m, slice_index)
        if key is None or key in seen:
            continue
        if best.get(key) is m:
            seen.add(key)
            out.append(m)
    return out


def _render_matches(
    matches: list[MatchedFare],
    cabin_list: tuple[str, ...],
    *,
    slice_index: int = 0,
    cash_per_cabin: Mapping[int, Mapping[str, float]] | None = None,
    num_passengers: int = 1,
) -> None:
    matches = _dedupe_per_leg(matches, slice_index=slice_index)
    if not matches:
        console.print("[yellow]No matched fares.[/]")
        return
    # Drop the funded-by column when multi-cabin is active: with N cabin
    # columns each carrying a two-line cell (miles cost + ¢/mi), the
    # funded-by string (typically 4-6 banks long) is the column most likely
    # to wrap awkwardly and bloat row height. Single-cabin renders keep it
    # since horizontal budget is fine there.
    show_funding = len(cabin_list) <= 1

    t = Table(
        title="Cash + award (matched on flight # x date)",
        show_header=True,
        header_style="bold cyan",
    )
    t.add_column("flight", overflow="fold")
    t.add_column("stops")
    t.add_column("price", justify="right")
    for cab in cabin_list:
        t.add_column(_safe_text(cab), justify="right")
    if show_funding:
        t.add_column("funded by", overflow="fold")

    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or not itn.slices or len(itn.slices) <= slice_index:
            continue
        s = itn.slices[slice_index]
        # Show every marketing flight# in the slice (not just the first) so a
        # connection is visible; `stops` makes nonstop-vs-connection explicit.
        flight = "/".join(s.flights) if s.flights else "?"
        stops_n = len(s.stops) if s.stops else max(len(s.flights) - 1, 0)
        cash_str = m.itinerary.price or "—"
        empty: Mapping[str, float] = {}
        per_cabin_cash = cash_per_cabin.get(id(m.itinerary), empty) if cash_per_cabin else empty

        cells = [_safe_text(flight), _fmt_stops(stops_n), _safe_text(cash_str)]
        for cab in cabin_list:
            # CPM is shown only when we have cash for THIS cabin specifically
            # — otherwise the value would mix cabins (e.g. business miles vs
            # economy cash) and mislead. Cabins without per-cabin cash render
            # the award without a ¢/mi line.
            cells.append(_fmt_award_cell(m.awards, cab, per_cabin_cash.get(cab), num_passengers))
        if show_funding:
            cells.append(_safe_text(_fmt_funding(m.awards, tuple(cabin_list))))
        t.add_row(*cells)
    console.print(t)


# ──────────────────────────── render: pp-only ──────────────────────────────


def _render_pp_only(awards: list[AwardFlight]) -> None:
    """One leg's provider-merged awards as a flat table. Multi-provider
    today is degenerate (PP only); the table just shows `provider | program`
    so when seats.aero lands the surface doesn't need to change."""
    # (source, program, flight, route, num_connections, departs, cabin, miles, tax, funded)
    rows: list[tuple[str, str, str, str, int, str, str, int, float, str]] = []
    for af in awards:
        for c in af.cabins:
            if c.miles <= 0:
                continue
            rows.append(
                (
                    af.provider,
                    af.program,
                    af.flight_number,
                    f"{af.origin}→{af.destination}",
                    af.num_connections,
                    af.departure[:16],
                    c.cabin,
                    c.miles,
                    c.tax_usd,
                    ", ".join(af.funding_banks),
                ),
            )
    rows.sort(key=lambda r: (r[5], r[7]))  # by departure, then miles
    t = Table(title="Award availability", show_header=True, header_style="bold cyan")
    # `fold` (not the default ellipsis) on the wordy columns so program/route
    # stay legible instead of truncating to 'Ameri…' / 'MCO→M…'. `source` is a
    # fixed short token ("PointsPath"/"seats.aero") — leave it unfolded so it
    # doesn't wrap awkwardly under width pressure.
    t.add_column("source")
    t.add_column("program", overflow="fold")
    t.add_column("flight")
    t.add_column("route", overflow="fold")
    t.add_column("stops")
    t.add_column("departs")
    t.add_column("cabin")
    t.add_column("miles", justify="right")
    t.add_column("tax", justify="right")
    t.add_column("funded by", overflow="fold")
    for r in rows:
        t.add_row(
            _safe_text(r[0]),
            _safe_text(r[1]),
            _safe_text(r[2]),
            _safe_text(r[3]),
            _safe_text(_fmt_stops(r[4])),
            _safe_text(_fmt_iso_compact(r[5])),
            _safe_text(r[6]),
            _safe_text(_fmt_miles(r[7])),
            f"${r[8]:.0f}",
            _safe_text(r[9]),
        )
    console.print(t)


# ─────────────────────────────── json shapes ───────────────────────────────


def _serialize_award(af: AwardFlight) -> dict[str, Any]:
    return {
        "provider": af.provider,
        "program": af.program,
        "miles_to_cash_ratio": af.miles_to_cash_ratio,
        "funding_banks": af.funding_banks,
        "matched_origin": af.origin,
        "matched_destination": af.destination,
        "matched_departure": af.departure,
        "flight_number": af.flight_number,
        "cabins": [
            {
                "cabin": c.cabin,
                "miles": c.miles,
                "tax_usd": c.tax_usd,
                "tax_currency": c.tax_currency,
            }
            for c in af.cabins
        ],
    }


def _serialize_matches(
    matches: list[MatchedFare],
    slice_index: int = 0,
    bags_included: Mapping[int, Sequence[tuple[int | None, int | None]]] | None = None,
) -> str:
    """`slice_index` selects the leg to describe — it MUST match the leg whose
    awards are being serialized. Hardcoding slice 0 made the `--json` return
    leg report the OUTBOUND flight number, route and departure beside the
    return leg's awards, while the wrapper labelled it "return".

    With `bags_included`, each match also says what bags its slice is priced
    with; a slice the map does not cover says nothing, as null."""
    out: list[dict[str, Any]] = []
    for m in matches:
        itn = m.itinerary.itinerary
        s = (
            itn.slices[slice_index]
            if itn and itn.slices and slice_index < len(itn.slices)
            else None
        )
        row: dict[str, Any] = {
            "flight": (s.flights[0] if s and s.flights else None),
            "departure": (s.departure if s else None),
            "origin": (s.origin.code if s and s.origin else None),
            "destination": (s.destination.code if s and s.destination else None),
            "cash_price": m.itinerary.price,
        }
        if bags_included is not None:
            stated = bags_included.get(id(m.itinerary), ())
            checked, carry_on = stated[slice_index] if slice_index < len(stated) else (None, None)
            row["bags_included"] = {"checked": checked, "carry_on": carry_on}
        row["awards"] = [_serialize_award(af) for af in m.awards]
        out.append(row)
    return json.dumps(out, indent=2)


def _leg_entry(leg: _AwardLeg, key: str, value: object) -> dict[str, Any]:
    """One leg of the award document. `pairs_not_asked` appears only on a
    leg the cap cut, so an uncut leg keeps exactly its three keys."""
    entry: dict[str, Any] = {"leg": leg.label, "slice_index": leg.slice_index, key: value}
    if leg.not_asked:
        entry["pairs_not_asked"] = [
            {"origin": q.origin, "destination": q.destination} for q in leg.not_asked
        ]
    return entry


def _serialize_matches_per_leg(
    matches_per_leg: list[list[MatchedFare]],
    legs: list[_AwardLeg],
    bags_included: Mapping[int, Sequence[tuple[int | None, int | None]]] | None = None,
) -> str:
    return json.dumps(
        [
            _leg_entry(
                leg,
                "matches",
                json.loads(_serialize_matches(matches, leg.slice_index, bags_included)),
            )
            for leg, matches in zip(legs, matches_per_leg, strict=True)
        ],
        indent=2,
    )


def _serialize_pp_only_per_leg(
    per_leg: list[list[AwardFlight]],
    legs: list[_AwardLeg],
) -> str:
    return json.dumps(
        [
            _leg_entry(leg, "awards", [_serialize_award(af) for af in awards])
            for leg, awards in zip(legs, per_leg, strict=True)
        ],
        indent=2,
    )
