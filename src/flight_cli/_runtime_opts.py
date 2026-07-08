"""Shared runtime-knob resolvers and the `typer.Option` singletons they back.

The hidden HTTP knobs (`--rps`, `--impersonate`, `--no-cache`), the `--format` /
deprecated `--json` output selectors, and the `--provider-opt` override share one
definition each so every command exposes identical flags. The `resolve_*`
functions collapse a CLI flag against env/config defaults. Imported back into
`cli` (under private-name aliases) so command signatures and paths reference them
as module globals.
"""

from __future__ import annotations

import typer

from . import _config
from ._console import err

# Common-args helpers — these reduce repetition across commands.
# These flags are hidden because almost nobody touches them in normal use;
# defaults live in config.toml ([http] section) and can be overridden via
# FLIGHT_RPS / FLIGHT_IMPERSONATE env vars. The CLI flag still works for
# one-off overrides — it's just no longer in --help. None sentinel means
# "fall back to config/env"; explicit value overrides everything.
RPS_OPT = typer.Option(
    None,
    "--rps",
    hidden=True,
    help="Requests per second (default: 1.0; FLIGHT_RPS / config.toml).",
)
IMPERSONATE_OPT = typer.Option(
    None,
    "--impersonate",
    hidden=True,
    help="curl_cffi profile (default: chrome; FLIGHT_IMPERSONATE / config.toml).",
)
NO_CACHE_OPT = typer.Option(
    False,
    "--no-cache",
    hidden=True,
    help="Bypass the on-disk response cache (or set FLIGHT_NO_CACHE=1).",
)
PROVIDER_OPT = typer.Option(
    None,
    "--provider-opt",
    help=(
        "Per-provider override, repeatable: 'pp.airlines=United,Delta'. "
        "Overrides ~/.config/flight-cli/config.toml [providers.<name>]."
    ),
    rich_help_panel="Backend & providers",
)


def resolve_rps(flag: float | None) -> float:
    """CLI flag wins; otherwise fall back to env / config / default."""
    if flag is not None:
        return flag
    try:
        return _config.http_rps()
    except ValueError as e:
        err.print(f"[red]Bad rps configuration: {e}[/]")
        raise typer.Exit(2) from e


def resolve_impersonate(flag: str | None) -> str:
    if flag is not None:
        return flag
    return _config.http_impersonate()


def resolve_no_cache(flag: bool) -> bool:
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
VALID_FORMATS = ("table", "json")

FORMAT_OPT = typer.Option(
    "table",
    "--format",
    help=f"Output format: one of {'/'.join(VALID_FORMATS)}.",
    rich_help_panel="Output",
)
JSON_OPT = typer.Option(
    False,
    "--json",
    hidden=True,
    help="[deprecated] Use --format json.",
)


def resolve_format(*, fmt: str, json_flag: bool) -> str:
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
    if fmt not in VALID_FORMATS:
        err.print(f"[red]--format must be one of {'/'.join(VALID_FORMATS)}; got {fmt!r}[/]")
        raise typer.Exit(2)
    return fmt
