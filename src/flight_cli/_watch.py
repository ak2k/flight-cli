"""`flight watch add|list|rm`: saved watch specs; nothing polls or notifies from them yet.

The store is `watches.json` in the config directory: `FLIGHT_CLI_CONFIG_DIR` (the
override `_config.py` honors), else `$XDG_CONFIG_HOME/flight-cli`, else
`~/.config/flight-cli`. The path is resolved per call so a moved variable moves it.
"""

from __future__ import annotations

import os
from datetime import date  # noqa: TC003 - pydantic evaluates the field annotations at runtime
from pathlib import Path
from typing import Annotated, Final, Literal, Self, get_args

import typer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from ._console_text import CTRL

Cabin = Literal["economy", "premium", "business", "first"]
CABINS: Final = get_args(Cabin)
_IATA = r"^[A-Z]{3}$"


class Watch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    origin: str = Field(pattern=_IATA)
    destination: str = Field(pattern=_IATA)
    dep_from: date | None
    dep_to: date | None
    # JSON has no infinity, so an infinite ceiling would be saved as null.
    below: float | None = Field(gt=0, allow_inf_nan=False)
    cabin: Cabin
    award: bool

    @model_validator(mode="after")
    def _window(self) -> Self:
        if (self.dep_from is None) != (self.dep_to is None):
            raise ValueError("--from and --to go together")
        if self.dep_from and self.dep_to and self.dep_to < self.dep_from:
            raise ValueError("--to is before --from")
        return self

    def describe(self) -> str:
        when = "any date"
        if self.dep_from and self.dep_to:
            when = f"{self.dep_from}" + (f"..{self.dep_to}" if self.dep_to != self.dep_from else "")
        parts = [f"{self.origin}-{self.destination}", when, self.cabin]
        if self.below is not None:
            parts.append(f"below {self.below:g}")
        return "  ".join([*parts, "award"] if self.award else parts)


_WATCHES = TypeAdapter(list[Watch])


def watches_path() -> Path:
    override = os.environ.get("FLIGHT_CLI_CONFIG_DIR")
    if override:
        return Path(override) / "watches.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "flight-cli" / "watches.json"


def _save(watches: list[Watch]) -> None:
    """A 0600 temp file renamed into place: no reader sees half a file, and no
    moment has the store readable by others."""
    path = watches_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            _ = fh.write(_WATCHES.dump_json(watches, indent=2))
        _ = tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fail(message: str, code: int) -> typer.Exit:
    """The store path comes from the environment and a key in an unusable store
    from the file, so either can carry characters a terminal acts on."""
    typer.echo(message.translate(CTRL), err=True)
    return typer.Exit(code)


def _problems(e: ValidationError) -> str:
    """Field names and rules only: pydantic's `input` is the user's raw text,
    which a terminal would act on."""
    return "; ".join(f"{'.'.join(map(str, p['loc'])) or 'watch'}: {p['msg']}" for p in e.errors())


def _load() -> list[Watch]:
    path = watches_path()
    if not path.exists():
        return []
    try:
        return _WATCHES.validate_json(path.read_bytes())
    except ValidationError as e:
        raise _fail(f"{path} is unusable and was left untouched: {_problems(e)}", 1) from e


watch_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Saved route watches (stored only; nothing polls or notifies yet).",
)


@watch_app.command("add")
def watch_add(
    origin: Annotated[str, typer.Argument(help="Origin IATA")],
    destination: Annotated[str, typer.Argument(help="Destination IATA")],
    dep: Annotated[str | None, typer.Option("--dep", help="Departure date YYYY-MM-DD")] = None,
    dep_from: Annotated[str | None, typer.Option("--from", help="Window start; needs --to")] = None,
    dep_to: Annotated[str | None, typer.Option("--to", help="Window end; needs --from")] = None,
    below: Annotated[float | None, typer.Option("--below", help="Price ceiling")] = None,
    cabin: Annotated[str, typer.Option("--cabin", help=f"One of: {', '.join(CABINS)}")] = "economy",
    award: Annotated[bool, typer.Option("--award", help="Watch award space")] = False,
) -> None:
    """Save a watch; no date means any date."""
    if dep and (dep_from or dep_to):
        raise _fail("--dep excludes --from and --to", 2)
    stored = _load()
    try:
        new = Watch.model_validate(
            {
                "id": max((w.id for w in stored), default=0) + 1,
                "origin": origin.upper(),
                "destination": destination.upper(),
                "dep_from": dep or dep_from,
                "dep_to": dep or dep_to,
                "below": below,
                "cabin": cabin.lower(),
                "award": award,
            }
        )
    except ValidationError as e:
        raise _fail(_problems(e), 2) from e
    _save([*stored, new])
    typer.echo(f"Added {new.id}: {new.describe()}")


@watch_app.command("list")
def watch_list() -> None:
    """Print the saved watches."""
    stored = _load()
    for w in stored:
        typer.echo(f"{w.id}  {w.describe()}")
    if not stored:
        typer.echo("No watches saved.")


@watch_app.command("rm")
def watch_rm(watch_id: Annotated[int, typer.Argument(metavar="ID", help="Id from `list`")]) -> None:
    """Delete one saved watch."""
    stored = _load()
    gone = next((w for w in stored if w.id == watch_id), None)
    if gone is None:
        raise _fail(f"no watch with id {watch_id}", 1)
    _save([w for w in stored if w is not gone])
    typer.echo(f"Removed {watch_id}: {gone.describe()}")
