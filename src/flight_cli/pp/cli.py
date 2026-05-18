"""CLI surface for PointsPath: `auth pp ...` subcommands + augmentation entry.

`run_pp_for_search` is wired into `flight search` (both backends): it runs
implicitly whenever any provider's tokens are present (opt out with `--no-pp`).
Iterates the provider registry, fans out each leg's queries in parallel,
joins via match.py, and renders.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - typer evaluates annotations at runtime
from typing import TYPE_CHECKING, Annotated, Any

import anyio
import typer
from rich.console import Console
from rich.table import Table

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
    from collections.abc import Mapping

    from ..models import SearchResult
    from ..providers.base import AwardFlight, LegQuery


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
    console.print(f"[green]Saved Seats.aero key to {_seats_auth.KEY_PATH}.[/]")


@seats_auth_app.command("whoami")
def seats_whoami() -> None:
    """Show whether a key is configured and probe the current quota.

    Hits /partnerapi/search with a tiny query to grab the latest
    X-RateLimit-Remaining header. Uses ~1 of your 1000/day quota.
    """
    key = _seats_auth.load_key()
    if key is None:
        err.print("[yellow]No Seats.aero key configured.[/]")
        err.print(f"Run `flight auth seats-aero key <KEY>` or set {_seats_auth.API_KEY_ENV}.")
        raise typer.Exit(1)
    source = (
        "env" if _seats_auth.os.environ.get(_seats_auth.API_KEY_ENV) else str(_seats_auth.KEY_PATH)
    )
    console.print(f"[green]Seats.aero key configured[/] (source: {source})")

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
                err.print(f"[red]Probe failed: HTTP {e.status}[/]")
                raise typer.Exit(1) from e
            rl = c.last_rate_limit
            if rl is None:
                console.print("[yellow]No rate-limit headers returned.[/]")
            else:
                console.print(
                    f"Quota: {rl.remaining}/{rl.limit} remaining (resets in {rl.reset_seconds}s)"
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
        console.print(f"[green]Deleted {_seats_auth.KEY_PATH}.[/]")
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
        err.print(f"[red]Login failed ({type(e).__name__}): {e}[/]")
        raise typer.Exit(1) from e

    when = datetime.fromtimestamp(t.expires_at).isoformat() if t.expires_at else "?"
    console.print(
        f"[green]Saved[/] tokens for [bold]{t.user_email or '?'}[/] "
        f"to {TOKENS_PATH}\n  source: {source}\n  access_token expires: {when}"
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
    console.print(f"email:   [bold]{t.user_email or claims.get('email') or '?'}[/]")
    console.print(f"sub:     {claims.get('sub', '?')}")
    console.print(f"role:    {claims.get('role', '?')}")
    console.print(f"expires: {when}")
    console.print(f"store:   {TOKENS_PATH}")


@pp_auth_app.command("logout")
def pp_logout() -> None:
    """Delete the on-disk PointsPath token store."""
    if clear_tokens():
        console.print(f"[green]Deleted[/] {TOKENS_PATH}")
    else:
        console.print(f"Nothing to delete ({TOKENS_PATH} doesn't exist).")


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
) -> None:
    """Run award augmentation through the provider registry, join against
    `res`'s cash itineraries, render. Registry hands back any configured
    providers (PointsPath, Seats.aero, ...); the matcher is provider-blind.

    Errors are non-fatal — print and continue so the user still sees their
    cash results.

    The `pp_only` arg is named for historical reasons; today it means
    "render in awards-only mode" — applies to whatever providers were
    selected, not just PP.

    `cash_per_cabin` maps `id(itinerary) -> {pp_cabin_name: cash_usd}` so
    the renderer can compute cents-per-mile per cabin against the right
    cash basis (business miles vs business cash, not business miles vs
    economy cash). Callers should build it from the cabins they queried —
    single-cabin invocations pass a one-entry inner dict; multi-cabin
    passes one entry per queried cabin. When None, no CPM is shown.
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
                err.print(f"[red]--pp: {e}[/]")
                return
            err.print(f"[yellow]PointsPath skipped: {e}[/]")

    cabin_list = tuple(_normalize_cabin(c) for c in _parse_csv(cabins, DEFAULT_CABINS))
    explicit_airlines = _parse_csv(airlines, ()) if airlines else None

    # If `res` carries gflight-captured opaque flight IDs on its slices, build
    # PP cash hints per leg so the request goes out with enable_matching=True
    # and the matcher's matched-id key becomes available. Matrix-built
    # SearchResults won't have flight_id populated; hints stays empty.
    cash_hints_per_leg: list[tuple[CashFlightHint, ...]] = [
        tuple(cash_hints_from_search_result(res, slice_index=leg.slice_index)) for leg in legs
    ]

    async def _go() -> tuple[list[list[AwardFlight]], list[Any]]:
        return await gather_awards(
            legs=legs,
            num_passengers=num_passengers,
            cabins=cabin_list,
            pp_airlines=explicit_airlines,
            seats_sources=seats_sources,
            cash_hints_per_leg=cash_hints_per_leg,
            provider_filter=provider_filter,
        )

    try:
        per_leg, providers = anyio.run(_go)
    except Exception as e:  # noqa: BLE001 — surface anything to user, don't crash CLI
        err.print(f"[red]--pp: award query failed: {e}[/]")
        return

    try:
        if pp_only:
            if json_out:
                sys.stdout.write(_serialize_pp_only_per_leg(per_leg, legs))
                return
            for leg, awards in zip(legs, per_leg, strict=True):
                console.print(f"\n[bold]Leg: {leg.label}[/]")
                _render_pp_only(awards)
            return

        matches_per_leg: list[list[MatchedFare]] = [
            join(res, awards, slice_index=leg.slice_index)
            for leg, awards in zip(legs, per_leg, strict=True)
        ]
        if json_out:
            sys.stdout.write(_serialize_matches_per_leg(matches_per_leg, legs))
            return
        for leg, matches in zip(legs, matches_per_leg, strict=True):
            console.print(f"\n[bold]Leg: {leg.label}[/]")
            _render_matches(
                matches,
                cabin_list,
                slice_index=leg.slice_index,
                cash_per_cabin=cash_per_cabin,
            )
    finally:
        # Providers hold HTTP keepalive; close them so the event loop doesn't
        # warn about unclosed transports on exit.
        anyio.run(_aclose_all, providers)


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


def _best_award_for_cabin(
    award_flights: list[AwardFlight], want_cabin: str
) -> tuple[int, float, str, list[str]] | None:
    """Cheapest (lowest miles) offer across providers for one cabin, or None."""
    best: tuple[int, float, str, list[str]] | None = None
    for af in award_flights:
        for ca in af.cabins:
            if ca.cabin != want_cabin:
                continue
            key = (ca.miles, ca.tax_usd, af.program, af.funding_banks)
            if best is None or key[0] < best[0]:
                best = key
    return best


def _fmt_award_cell(
    award_flights: list[AwardFlight], want_cabin: str, cash_usd: float | None = None
) -> str:
    """Render the best (lowest miles) offer across providers for one cabin.

    When `cash_usd` is provided AND an award exists, append the cabin's
    ¢/mi redemption value on a second line (dim-styled). Inline ¢/mi keeps
    each cabin's miles cost + redemption value adjacent in the table
    without growing the column count — the alternative (separate ¢/mi
    columns per cabin) overflows narrow terminals in multi-cabin renders.
    """
    best = _best_award_for_cabin(award_flights, want_cabin)
    if best is None:
        return "—"
    miles, tax, program, _banks = best
    head = f"{_fmt_miles(miles)} {program} + ${tax:.0f}"
    if cash_usd is None:
        return head
    cpm = _cents_per_mile(cash_usd, miles, tax)
    if cpm is None:
        return head
    return f"{head}\n[dim]{cpm:.1f}¢/mi[/]"


def _fmt_funding(award_flights: list[AwardFlight]) -> str:
    banks: list[str] = []
    seen: set[str] = set()
    for af in award_flights:
        for b in af.funding_banks:
            if b not in seen:
                seen.add(b)
                banks.append(b)
    return ", ".join(banks) if banks else ""


def _dedupe_per_leg(matches: list[MatchedFare], slice_index: int = 0) -> list[MatchedFare]:
    """Matrix returns the cross-product of outbound x return itineraries, so
    the same leg-flight surfaces in many rows. Collapse to one row per
    (flight_number, departure_date), keeping the row with cheapest cash.
    """
    best: dict[tuple[str, str], MatchedFare] = {}
    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or len(itn.slices) <= slice_index:
            continue
        s = itn.slices[slice_index]
        if not s.flights or not s.departure:
            continue
        key = (s.flights[0].upper().replace(" ", ""), (s.departure or "")[:10])
        cash = _parse_cash(m.itinerary.price) or float("inf")
        existing = best.get(key)
        if existing is None or (_parse_cash(existing.itinerary.price) or float("inf")) > cash:
            best[key] = m
    # Preserve original order (cheapest cash first, since `solutions` is sorted).
    seen: set[tuple[str, str]] = set()
    out: list[MatchedFare] = []
    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or len(itn.slices) <= slice_index:
            continue
        s = itn.slices[slice_index]
        if not s.flights or not s.departure:
            continue
        key = (s.flights[0].upper().replace(" ", ""), (s.departure or "")[:10])
        if key in seen:
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
    t.add_column("flight")
    t.add_column("price", justify="right")
    for cab in cabin_list:
        t.add_column(cab, justify="right")
    if show_funding:
        t.add_column("funded by")

    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or not itn.slices or len(itn.slices) <= slice_index:
            continue
        s = itn.slices[slice_index]
        flight = (s.flights or ["?"])[0]
        cash_str = m.itinerary.price or "—"
        empty: Mapping[str, float] = {}
        per_cabin_cash = cash_per_cabin.get(id(m.itinerary), empty) if cash_per_cabin else empty

        cells = [flight, cash_str]
        for cab in cabin_list:
            # CPM is shown only when we have cash for THIS cabin specifically
            # — otherwise the value would mix cabins (e.g. business miles vs
            # economy cash) and mislead. Cabins without per-cabin cash render
            # the award without a ¢/mi line.
            cells.append(_fmt_award_cell(m.awards, cab, per_cabin_cash.get(cab)))
        if show_funding:
            cells.append(_fmt_funding(m.awards))
        t.add_row(*cells)
    console.print(t)


# ──────────────────────────── render: pp-only ──────────────────────────────


def _render_pp_only(awards: list[AwardFlight]) -> None:
    """One leg's provider-merged awards as a flat table. Multi-provider
    today is degenerate (PP only); the table just shows `provider | program`
    so when seats.aero lands the surface doesn't need to change."""
    rows: list[tuple[str, str, str, str, str, str, int, float, str]] = []
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
                    af.departure[:16],
                    c.cabin,
                    c.miles,
                    c.tax_usd,
                    ", ".join(af.funding_banks),
                ),
            )
    rows.sort(key=lambda r: (r[4], r[6]))  # by departure, then miles
    t = Table(title="Award availability", show_header=True, header_style="bold cyan")
    cols = ("source", "program", "flight", "route", "departs", "cabin", "miles", "tax", "funded by")
    for col in cols:
        t.add_column(col)
    for r in rows:
        t.add_row(r[0], r[1], r[2], r[3], r[4], r[5], _fmt_miles(r[6]), f"${r[7]:.0f}", r[8])
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


def _serialize_matches(matches: list[MatchedFare]) -> str:
    out: list[dict[str, Any]] = []
    for m in matches:
        itn = m.itinerary.itinerary
        s = itn.slices[0] if itn and itn.slices else None
        out.append(
            {
                "flight": (s.flights[0] if s and s.flights else None),
                "departure": (s.departure if s else None),
                "origin": (s.origin.code if s and s.origin else None),
                "destination": (s.destination.code if s and s.destination else None),
                "cash_price": m.itinerary.price,
                "awards": [_serialize_award(af) for af in m.awards],
            },
        )
    return json.dumps(out, indent=2)


def _serialize_matches_per_leg(
    matches_per_leg: list[list[MatchedFare]], legs: list[LegQuery]
) -> str:
    return json.dumps(
        [
            {
                "leg": leg.label,
                "slice_index": leg.slice_index,
                "matches": json.loads(_serialize_matches(matches)),
            }
            for leg, matches in zip(legs, matches_per_leg, strict=True)
        ],
        indent=2,
    )


def _serialize_pp_only_per_leg(
    per_leg: list[list[AwardFlight]],
    legs: list[LegQuery],
) -> str:
    return json.dumps(
        [
            {
                "leg": leg.label,
                "slice_index": leg.slice_index,
                "awards": [_serialize_award(af) for af in awards],
            }
            for leg, awards in zip(legs, per_leg, strict=True)
        ],
        indent=2,
    )
