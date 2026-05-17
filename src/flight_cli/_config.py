"""User-level config loader for ~/.config/flight-cli/config.toml.

Consumers today:
  - [providers.<name>] tables: per-provider airline lists, cabin lists, etc.
  - [http] table: rps, impersonate. Env vars (FLIGHT_RPS, FLIGHT_IMPERSONATE)
    override config; config overrides defaults.
  - [cache] table: enabled (default true). FLIGHT_NO_CACHE env disables.

The loader is forgiving: missing file → empty config, partial sections →
fill in with what's there, unknown keys → kept verbatim (callers decide
whether to honor them).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, cast

CONFIG_DIR_ENV = "FLIGHT_CLI_CONFIG_DIR"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "flight-cli" / "config.toml"


def _config_path() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override) / "config.toml"
    return DEFAULT_CONFIG_PATH


def load() -> dict[str, Any]:
    """Read config.toml, return parsed dict. Missing file → {}.

    Parse errors raise — silent fallback would hide typos that defeat the
    user's intent."""
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def provider_options(name: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pull the `[providers.<name>]` table out of config, or {} if absent.

    Caller is responsible for type-coercing the values (lists stay as lists,
    strings stay as strings — TOML's native typing is preserved).

    `name` is normalized through the alias map, AND all section keys in the
    config are normalized before lookup — so `[providers.sa]` and
    `[providers.seats-aero]` (and any of the aliased spellings) both work."""
    cfg = config if config is not None else load()
    providers_any: Any = cfg.get("providers", {})
    if not isinstance(providers_any, dict):
        return {}
    providers_dict = cast("dict[str, Any]", providers_any)
    target = canonical_provider(name)
    # Walk all sections and pick whichever matches the canonical of `name`.
    # Reverse lookup makes the user's config-file spelling forgiving without
    # forcing them to know about the canonical names.
    for section_name, section_any in providers_dict.items():
        if canonical_provider(section_name) != target:
            continue
        if isinstance(section_any, dict):
            return cast("dict[str, Any]", section_any)
    return {}


def parse_provider_opt_overrides(raw: list[str]) -> dict[str, dict[str, Any]]:
    """Parse `--provider-opt pp.airlines=United,Delta` items into nested dict.

    `pp.airlines=United,Delta` → `{"pp": {"airlines": ["United", "Delta"]}}`.
    A bare scalar (no comma) stays a string; comma-list becomes list[str].

    Provider names are normalized through the alias map — `sa.cabins=Business`
    and `seats-aero.cabins=Business` both land at
    `{"seats-aero": {"cabins": "Business"}}` so downstream consumers don't
    have to know about user-visible spellings.

    Note: seats.aero's brand contains a dot, but `seats.aero.cabins=X` parses
    as provider="seats" / key="aero.cabins" (split on first dot). Users who
    want the seats.aero brand spelling should write `'seats.aero.cabins=X'`
    AND know that we'll split incorrectly; the documented forms are
    `seats-aero` or `sa`.

    Repeated keys for the same provider merge at the outer dict; same inner
    key wins last (caller is responsible for order if it matters).
    """
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if "=" not in item:
            msg = f"--provider-opt {item!r} missing '='; expected 'provider.key=value'"
            raise ValueError(msg)
        path, value = item.split("=", 1)
        if "." not in path:
            msg = f"--provider-opt {item!r} missing '.'; expected 'provider.key=value'"
            raise ValueError(msg)
        provider_raw, key = path.split(".", 1)
        provider = canonical_provider(provider_raw)
        key = key.strip()
        if not provider or not key:
            msg = f"--provider-opt {item!r}: provider and key must be non-empty"
            raise ValueError(msg)
        parsed: str | list[str] = (
            [v.strip() for v in value.split(",")] if "," in value else value.strip()
        )
        out.setdefault(provider, {})[key] = parsed
    return out


# ─────────────────────────── runtime knob resolution ─────────────────────

DEFAULT_RPS = 1.0
DEFAULT_IMPERSONATE = "chrome"

RPS_ENV = "FLIGHT_RPS"
IMPERSONATE_ENV = "FLIGHT_IMPERSONATE"
NO_CACHE_ENV = "FLIGHT_NO_CACHE"


def _truthy_env(name: str) -> bool:
    """`FLIGHT_NO_CACHE=1` / `=true` / `=yes` → True. Unset / `=0` / `=false` → False."""
    v = os.environ.get(name, "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def http_rps(*, config: dict[str, Any] | None = None) -> float:
    """Resolve requests-per-second: env > config > default. Bad inputs surface
    a clear error rather than silent fallback to default."""
    env = os.environ.get(RPS_ENV)
    if env is not None:
        try:
            return float(env)
        except ValueError as e:
            msg = f"{RPS_ENV}={env!r} is not a number"
            raise ValueError(msg) from e
    cfg = config if config is not None else load()
    http_any: Any = cfg.get("http", {})
    if isinstance(http_any, dict) and "rps" in http_any:
        http_dict = cast("dict[str, Any]", http_any)
        v: Any = http_dict["rps"]
        try:
            return float(v)
        except (TypeError, ValueError) as e:
            msg = f"[http].rps={v!r} is not a number"
            raise ValueError(msg) from e
    return DEFAULT_RPS


def http_impersonate(*, config: dict[str, Any] | None = None) -> str:
    """Resolve curl_cffi impersonation profile: env > config > default."""
    env = os.environ.get(IMPERSONATE_ENV)
    if env:
        return env
    cfg = config if config is not None else load()
    http_any: Any = cfg.get("http", {})
    if isinstance(http_any, dict):
        http_dict = cast("dict[str, Any]", http_any)
        v: Any = http_dict.get("impersonate")
        if isinstance(v, str) and v:
            return v
    return DEFAULT_IMPERSONATE


def cache_disabled(*, config: dict[str, Any] | None = None) -> bool:
    """True when caching should be off. Env wins over config; default = cache on."""
    if _truthy_env(NO_CACHE_ENV):
        return True
    cfg = config if config is not None else load()
    cache_any: Any = cfg.get("cache", {})
    if isinstance(cache_any, dict):
        cache_dict = cast("dict[str, Any]", cache_any)
        v: Any = cache_dict.get("enabled")
        if isinstance(v, bool):
            return not v
    return False


# ─────────────────────────── provider names & aliases ────────────────────

# Canonical provider names used by the registry, config sections, and the
# --providers CLI filter. The aliases below normalize a user-friendly spelling
# (case-insensitive) onto the canonical name. Adding a new provider: add the
# canonical name + any aliases here, register the provider in registry.py,
# done. The naming is intentionally asymmetric — `pp` is the company's own
# short identifier (everyone in points-world calls it PP), so the canonical
# is `pp` and `pointspath` is the alias. Seats.aero has no widely-used
# short form, so the canonical is `seats-aero` and `sa` is the short alias.
_PROVIDER_ALIASES: dict[str, str] = {
    "pp": "pp",
    "pointspath": "pp",
    "points-path": "pp",
    "seats-aero": "seats-aero",
    "seatsaero": "seats-aero",
    "seats.aero": "seats-aero",
    "sa": "seats-aero",
}


def canonical_provider(name: str) -> str:
    """Normalize a user-supplied provider name to its canonical form.

    Case-insensitive. Unknown names pass through unchanged (lowercased) so
    upstream code can still emit clean error messages naming the typo."""
    return _PROVIDER_ALIASES.get(name.strip().lower(), name.strip().lower())


def known_canonical_providers() -> set[str]:
    """Set of canonical provider names. Useful for `--providers X` validation
    where X must match at least one known provider before we can sensibly
    construct an instance."""
    return set(_PROVIDER_ALIASES.values())


# ─────────────────────────── provider options ─────────────────────────────


def merge_provider_options(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge: override's keys win, both at provider level and key level.

    Used to layer CLI --provider-opt on top of config.toml."""
    out: dict[str, Any] = {**base}
    for k, v in override.items():
        existing = out.get(k)
        if isinstance(existing, dict) and isinstance(v, dict):
            out[k] = {**existing, **v}
        else:
            out[k] = v
    return out
