"""User-level config loader for ~/.config/flight-cli/config.toml.

Today's only consumer is the provider section — per-provider airline lists,
cabin lists, etc. The shape is intentionally flat per provider so adding a
new provider (seats.aero, etc.) is a new `[providers.<name>]` table, not a
schema migration.

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
    strings stay as strings — TOML's native typing is preserved)."""
    cfg = config if config is not None else load()
    providers_any: Any = cfg.get("providers", {})
    if not isinstance(providers_any, dict):
        return {}
    providers_dict = cast("dict[str, Any]", providers_any)
    section_any: Any = providers_dict.get(name, {})
    if not isinstance(section_any, dict):
        return {}
    return cast("dict[str, Any]", section_any)


def parse_provider_opt_overrides(raw: list[str]) -> dict[str, dict[str, Any]]:
    """Parse `--provider-opt pp.airlines=United,Delta` items into nested dict.

    `pp.airlines=United,Delta` → `{"pp": {"airlines": ["United", "Delta"]}}`.
    A bare scalar (no comma) stays a string; comma-list becomes list[str].

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
        provider, key = path.split(".", 1)
        provider = provider.strip()
        key = key.strip()
        if not provider or not key:
            msg = f"--provider-opt {item!r}: provider and key must be non-empty"
            raise ValueError(msg)
        parsed: str | list[str] = (
            [v.strip() for v in value.split(",")] if "," in value else value.strip()
        )
        out.setdefault(provider, {})[key] = parsed
    return out


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
