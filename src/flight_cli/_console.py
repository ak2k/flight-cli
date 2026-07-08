"""Shared Rich consoles for the CLI: `console` (stdout) and `err` (stderr).

A single stdout console and a single stderr console are shared across every CLI
module so human-facing output and diagnostics stay on the right streams. They are
imported back into `cli` as module globals so the commands and weaves reference
the same instances.
"""

from __future__ import annotations

from rich.console import Console

console = Console()
err = Console(stderr=True)
