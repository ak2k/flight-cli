"""CLI surface for PointsPath: `auth pp ...` subcommands + `--pp` augmentation.

The augmentation entry point (`run_pp_for_search`) is what the existing `fare`
command calls when `--pp` is on. It runs PP airline-search in parallel with the
finished Matrix result, joins via match.py, and renders.
"""
from __future__ import annotations
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..models import SearchResult
from .auth import (
    PPAuthError, TOKENS_PATH, clear_tokens, get_valid_tokens,
    import_from_tokens_file, load_tokens,
)
from .client import (
    DEFAULT_AIRLINES, DEFAULT_CABINS, PPClient, SearchSpec,
)
from .match import MatchedFare, join

console = Console()
err = Console(stderr=True)


# ─────────────────────────── auth pp subcommands ────────────────────────────

auth_app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="Auth helpers per provider. Today: PointsPath only.",
)
pp_auth_app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="PointsPath token management.",
)
auth_app.add_typer(pp_auth_app, name="pp")


@pp_auth_app.command("login")
def pp_login(
    tokens_file: Annotated[Optional[Path], typer.Option(
        "--tokens-file", "-f",
        help="Import tokens from a JSON file (e.g. one captured via CDP cookie sniff).",
    )] = None,
) -> None:
    """Save tokens to ~/.config/flight-cli/pp.json.

    Today only `--tokens-file` is wired. Browser-based login is planned but
    not yet implemented; for now, capture tokens from your authenticated Chrome
    session and pass the JSON file in.
    """
    if tokens_file is None:
        err.print("[yellow]Browser-based login isn't implemented yet.[/]")
        err.print(
            "Workaround: capture your Supabase tokens from your authenticated "
            "Chrome session and pass them in:\n"
            "  flight-cli auth pp login --tokens-file /path/to/pp_tokens.json\n"
            "Expected file shape: "
            '{"access_token": "...", "refresh_token": "...", "user": {"email": "..."}}'
        )
        raise typer.Exit(2)
    try:
        t = import_from_tokens_file(tokens_file)
    except (PPAuthError, OSError, json.JSONDecodeError, KeyError) as e:
        err.print(f"[red]Login failed: {e}[/]")
        raise typer.Exit(1)
    when = datetime.fromtimestamp(t.expires_at).isoformat() if t.expires_at else "?"
    console.print(
        f"[green]Saved[/] tokens for [bold]{t.user_email or '?'}[/] "
        f"to {TOKENS_PATH}\n  access_token expires: {when}"
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

def _parse_csv(s: Optional[str], default: tuple[str, ...]) -> tuple[str, ...]:
    if not s:
        return default
    return tuple(part.strip() for part in s.split(",") if part.strip())


_CABIN_ALIASES = {
    "y": "Economy", "economy": "Economy", "coach": "Economy", "main": "Economy",
    "w": "Premium economy", "premium": "Premium economy", "premiumeconomy": "Premium economy",
    "j": "Business", "business": "Business",
    "f": "First", "first": "First",
}


def _normalize_cabin(c: str) -> str:
    k = c.strip().lower().replace(" ", "").replace("-", "")
    return _CABIN_ALIASES.get(k, c)


def run_pp_for_search(
    res: SearchResult,
    *,
    origin: str,
    destination: str,
    dep_date: str,
    return_date: str = "",
    num_passengers: int = 1,
    airlines: Optional[str] = None,
    cabins: Optional[str] = None,
    pp_only: bool = False,
    json_out: bool = False,
) -> None:
    """Called by the `fare` command when --pp is on. Runs PP queries, joins,
    and renders. Errors here are non-fatal — print and continue so the user
    still sees their Matrix results."""
    try:
        get_valid_tokens()  # validate + refresh up-front, surface a clear error
    except PPAuthError as e:
        err.print(f"[red]--pp: {e}[/]")
        return

    airline_list = _parse_csv(airlines, DEFAULT_AIRLINES)
    cabin_list = tuple(_normalize_cabin(c) for c in _parse_csv(cabins, DEFAULT_CABINS))

    try:
        merged_by_airline, pricing = asyncio.run(
            _gather_pp(
                origin=origin, destination=destination,
                dep_date=dep_date, return_date=return_date,
                num_passengers=num_passengers,
                airlines=airline_list, cabins=cabin_list,
            )
        )
    except Exception as e:  # noqa: BLE001 — surface anything to user, don't crash CLI
        err.print(f"[red]--pp: PointsPath query failed: {e}[/]")
        return

    matches = join(res, merged_by_airline, pricing)
    if pp_only:
        # Skip cash-only rows; just dump every award flight we received.
        if json_out:
            sys.stdout.write(_serialize_pp_only(merged_by_airline)); return
        _render_pp_only(merged_by_airline, pricing)
        return

    if json_out:
        sys.stdout.write(_serialize_matches(matches)); return
    _render_matches(matches, cabin_list)


async def _gather_pp(*, origin: str, destination: str, dep_date: str,
                      return_date: str, num_passengers: int,
                      airlines: tuple[str, ...], cabins: tuple[str, ...]):
    async with await PPClient.create() as c:
        pricing = await c.pricing_info()
        merged: dict[str, "AirlineSearchResponse"] = {}  # type: ignore[name-defined]
        for cabin in cabins:
            spec = SearchSpec(
                origin=origin, destination=destination,
                date=dep_date, return_date=return_date,
                is_round_trip_return=bool(return_date),
                num_passengers=num_passengers,
                cabin_class=cabin,
                enable_matching=False,
            )
            per_airline = await c.airline_search_many(spec, airlines)
            # Merge per-cabin results into one airline → response map.
            for airline, resp in per_airline.items():
                if airline not in merged:
                    merged[airline] = resp
                    continue
                # Append outboundFlights; same flight may appear multiple
                # times across cabin queries — match.py de-dupes by key.
                existing = merged[airline]
                # Keep the union of flights; later identical-key rows
                # are tolerated and not double-counted by the matcher.
                existing.outboundFlights.extend(resp.outboundFlights)
        return merged, pricing


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


def _fmt_miles(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def _fmt_award_cell(award_options, want_cabin: str) -> str:
    """Render the best (lowest miles) offer across airlines for one cabin."""
    best: tuple[int, float, str, list[str]] | None = None
    for ao in award_options:
        for ca in ao.cabins:
            if ca.cabin != want_cabin:
                continue
            key = (ca.miles, ca.tax_usd, ao.airline, ao.funding_banks)
            if best is None or key[0] < best[0]:
                best = key
    if best is None:
        return "—"
    miles, tax, airline, _banks = best
    return f"{_fmt_miles(miles)} {airline} + ${tax:.0f}"


def _fmt_funding(award_options) -> str:
    banks: list[str] = []
    seen: set[str] = set()
    for ao in award_options:
        for b in ao.funding_banks:
            if b not in seen:
                seen.add(b); banks.append(b)
    return ", ".join(banks) if banks else ""


def _render_matches(matches: list[MatchedFare], cabin_list: tuple[str, ...]) -> None:
    if not matches:
        console.print("[yellow]No matched fares.[/]")
        return
    t = Table(title="Cash + award (matched on flight # × date)",
               show_header=True, header_style="bold cyan")
    t.add_column("flight")
    t.add_column("price", justify="right")
    for cab in cabin_list:
        t.add_column(cab, justify="right")
    t.add_column("¢/mi (Y)", justify="right")
    t.add_column("funded by")

    for m in matches:
        itn = m.itinerary.itinerary
        if not itn or not itn.slices:
            continue
        s = itn.slices[0]
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
                # CPM uses cheapest economy across airlines.
                best_y: tuple[int, float] | None = None
                for ao in m.awards:
                    for ca in ao.cabins:
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

def _render_pp_only(merged: dict, pricing) -> None:
    rows: list[tuple] = []
    pricing_idx = {p.airline: p for p in pricing.pricingInfos}
    for airline, resp in merged.items():
        pi = pricing_idx.get(airline)
        banks = ", ".join(b.bank for b in (pi.bankPointsInfos if pi else []))
        for of in resp.outboundFlights:
            for c in of.perCabinMilesPricing:
                pp = c.perPassengerPricing
                if not pp or pp.perPassengerMilesAmount <= 0:
                    continue
                rows.append((
                    airline, of.firstFlightNumber,
                    f"{of.origin}→{of.destination}",
                    of.localDepartureDateTime[:16],
                    c.cabinClass,
                    pp.perPassengerMilesAmount,
                    pp.perPassengerTaxAmountUsd,
                    banks,
                ))
    rows.sort(key=lambda r: (r[3], r[5]))  # by departure, then miles
    t = Table(title="PointsPath award availability",
               show_header=True, header_style="bold cyan")
    for col in ("airline", "flight", "route", "departs", "cabin",
                 "miles", "tax", "funded by"):
        t.add_column(col)
    for r in rows:
        t.add_row(r[0], r[1], r[2], r[3], r[4],
                  _fmt_miles(r[5]), f"${r[6]:.0f}", r[7])
    console.print(t)


# ─────────────────────────────── json shapes ───────────────────────────────

def _serialize_matches(matches: list[MatchedFare]) -> str:
    out = []
    for m in matches:
        itn = m.itinerary.itinerary
        s = (itn.slices[0] if itn and itn.slices else None)
        out.append({
            "flight": (s.flights[0] if s and s.flights else None),
            "departure": (s.departure if s else None),
            "origin": (s.origin.code if s and s.origin else None),
            "destination": (s.destination.code if s and s.destination else None),
            "cash_price": m.itinerary.price,
            "awards": [
                {
                    "airline": ao.airline,
                    "miles_to_cash_ratio": ao.miles_to_cash_ratio,
                    "funding_banks": ao.funding_banks,
                    "matched_origin": ao.flight.origin,
                    "matched_destination": ao.flight.destination,
                    "matched_departure": ao.flight.localDepartureDateTime,
                    "cabins": [
                        {"cabin": c.cabin, "miles": c.miles,
                         "tax_usd": c.tax_usd, "tax_currency": c.tax_currency}
                        for c in ao.cabins
                    ],
                }
                for ao in m.awards
            ],
        })
    return json.dumps(out, indent=2)


def _serialize_pp_only(merged: dict) -> str:
    return json.dumps(
        {a: r.model_dump() for a, r in merged.items()},
        indent=2,
    )
