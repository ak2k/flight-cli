"""CLI surface for PointsPath: `auth pp ...` subcommands + augmentation entry.

`run_pp_for_search` is wired into `flight search` (both backends): it runs
implicitly whenever any provider's tokens are present (opt out with `--no-pp`).
Iterates the provider registry, fans out each leg's queries in parallel,
joins via match.py, and renders.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
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
from .client import DEFAULT_CABINS
from .match import MatchedFare, join

if TYPE_CHECKING:
    from ..models import SearchResult
    from ..providers.base import AwardFlight, LegQuery


console = Console()
err = Console(stderr=True)


# ─────────────────────────── auth pp subcommands ────────────────────────────

auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Auth helpers per provider. Today: PointsPath only.",
)
pp_auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="PointsPath token management.",
)
auth_app.add_typer(pp_auth_app, name="pp")


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
) -> None:
    """Run award augmentation through the provider registry, join against
    `res`'s cash itineraries, render. Today the registry hands back
    PointsPath only; future providers (seats.aero) join via the same path.

    Errors are non-fatal — print and continue so the user still sees their
    cash results.
    """
    try:
        get_valid_tokens()  # validate + refresh up-front, surface a clear error
    except PPAuthError as e:
        err.print(f"[red]--pp: {e}[/]")
        return

    cabin_list = tuple(_normalize_cabin(c) for c in _parse_csv(cabins, DEFAULT_CABINS))
    explicit_airlines = _parse_csv(airlines, ()) if airlines else None

    async def _go() -> tuple[list[list[AwardFlight]], list[Any]]:
        return await gather_awards(
            legs=legs,
            num_passengers=num_passengers,
            cabins=cabin_list,
            pp_airlines=explicit_airlines,
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
            _render_matches(matches, cabin_list, slice_index=leg.slice_index)
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


def _fmt_award_cell(award_flights: list[AwardFlight], want_cabin: str) -> str:
    """Render the best (lowest miles) offer across providers for one cabin."""
    best: tuple[int, float, str, list[str]] | None = None
    for af in award_flights:
        for ca in af.cabins:
            if ca.cabin != want_cabin:
                continue
            key = (ca.miles, ca.tax_usd, af.program, af.funding_banks)
            if best is None or key[0] < best[0]:
                best = key
    if best is None:
        return "—"
    miles, tax, program, _banks = best
    return f"{_fmt_miles(miles)} {program} + ${tax:.0f}"


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
    matches: list[MatchedFare], cabin_list: tuple[str, ...], *, slice_index: int = 0
) -> None:
    matches = _dedupe_per_leg(matches, slice_index=slice_index)
    if not matches:
        console.print("[yellow]No matched fares.[/]")
        return
    t = Table(
        title="Cash + award (matched on flight # x date)",
        show_header=True,
        header_style="bold cyan",
    )
    t.add_column("flight")
    t.add_column("price", justify="right")
    for cab in cabin_list:
        t.add_column(cab, justify="right")
    t.add_column("¢/mi (Y)", justify="right")
    t.add_column("funded by")

    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or not itn.slices or len(itn.slices) <= slice_index:
            continue
        s = itn.slices[slice_index]
        flight = (s.flights or ["?"])[0]
        cash_str = m.itinerary.price or "—"
        # Parse cash price for cpm calc. Matrix returns "USD530.00", "$1,078",
        # "1,078 USD" etc. — strip currency tokens and grab the first number.
        cash_val: float | None = _parse_cash(cash_str)

        cells = [flight, cash_str]
        cpm_str = "—"
        for cab in cabin_list:
            cells.append(_fmt_award_cell(m.awards, cab))
            if cab == "Economy" and cash_val is not None:
                # CPM uses cheapest economy across providers.
                best_y: tuple[int, float] | None = None
                for af in m.awards:
                    for ca in af.cabins:
                        if ca.cabin == "Economy" and (best_y is None or ca.miles < best_y[0]):
                            best_y = (ca.miles, ca.tax_usd)
                if best_y is not None:
                    cpm = _cents_per_mile(cash_val, best_y[0], best_y[1])
                    if cpm is not None:
                        cpm_str = f"{cpm:.1f}¢"
        cells.append(cpm_str)
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
