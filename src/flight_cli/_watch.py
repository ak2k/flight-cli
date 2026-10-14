"""`flight watch add|list|rm|run`: saved watch specs, and a one-shot price check of each;
nothing is sent or saved by `run`, and nothing notifies yet.

The store is `watches.json` in the config directory: `FLIGHT_CLI_CONFIG_DIR` (the
override `_config.py` honors), else `$XDG_CONFIG_HOME/flight-cli`, else
`~/.config/flight-cli`. The path is resolved per call so a moved variable moves it.
`add` and `rm` hold an exclusive `flock` on that directory while they read, change and
rewrite the store, so two overlapping commands each keep their change (not on Windows).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self, get_args

import typer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from ._console_text import CTRL

if TYPE_CHECKING:
    from collections.abc import Generator

Cabin = Literal["economy", "premium", "business", "first"]
CABINS: Final = get_args(Cabin)
_IATA = r"^[A-Z]{3}$"
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Watch(BaseModel):
    # Strict: lax parsing would read a stored "yes" as true and "400" as a number.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: int
    origin: str = Field(pattern=_IATA)
    destination: str = Field(pattern=_IATA)
    dep_from: date | None
    dep_to: date | None
    # JSON has no infinity, so an infinite ceiling would be saved as null.
    below: float | None = Field(gt=0, allow_inf_nan=False)
    cabin: Cabin
    award: bool

    @field_validator("dep_from", "dep_to", mode="before")
    @classmethod
    def _day(cls, value: object) -> object:
        """pydantic reads a string of digits as Unix time even in strict JSON
        (`--dep 0` would be 1970-01-01), and strict Python takes no string."""
        if not isinstance(value, str):
            return value
        if not _DAY.fullmatch(value):
            raise ValueError("a date is YYYY-MM-DD")
        return date.fromisoformat(value)

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
    moment has the store readable by others. `mkstemp` picks a name no other
    writer holds, so the cleanup only ever removes this call's own file."""
    path = watches_path()
    # 0700 applies only to the leaf this call creates: an existing directory keeps its mode.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            _ = fh.write(_WATCHES.dump_json(watches, indent=2))
        _ = tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _locked() -> Generator[None]:
    """Serialize read-modify-write cycles: a command that loaded the store before
    another saved would otherwise write back a list without that change. The lock
    sits on the config directory's own descriptor, so no file joins `watches.json`
    there; closing the descriptor releases it."""
    directory = watches_path().parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        raise _fail(f"{directory!s} cannot be created: {e.strerror}", 1) from e
    if sys.platform == "win32":
        yield
        return
    import fcntl  # noqa: PLC0415 - the module does not exist on Windows

    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as e:
        raise _fail(f"{directory!s} cannot be locked: {e.strerror}", 1) from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
    except (OSError, ValidationError) as e:
        why = _problems(e) if isinstance(e, ValidationError) else e.strerror
        raise _fail(f"{path} is unusable and was left untouched: {why}", 1) from e


watch_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Saved route watches; `run` prices each once (nothing is sent or saved).",
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
    if dep is not None and (dep_from is not None or dep_to is not None):
        raise _fail("--dep excludes --from and --to", 2)
    try:
        draft = Watch.model_validate(
            {
                "id": 0,
                "origin": origin.upper(),
                "destination": destination.upper(),
                "dep_from": dep_from if dep is None else dep,
                "dep_to": dep_to if dep is None else dep,
                "below": below,
                "cabin": cabin.lower(),
                "award": award,
            }
        )
    except ValidationError as e:
        raise _fail(_problems(e), 2) from e
    with _locked():
        stored = _load()
        new = draft.model_copy(update={"id": max((w.id for w in stored), default=0) + 1})
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


def _poll(w: Watch) -> str:
    """One line for `w`: its lowest priced date now, against its ceiling."""
    from .cli import lowest_fare_rows  # noqa: PLC0415 - cli imports this module

    head = f"{w.id}  {w.describe()}"
    if w.award:
        return f"{head}: skipped, award watches are not polled yet"
    if w.dep_from is None or w.dep_to is None:
        return f"{head}: skipped, a watch with no dates is not polled yet"
    try:
        rows = lowest_fare_rows(
            w.origin, w.destination, start=w.dep_from, end=w.dep_to, cabin=w.cabin
        )
    except Exception as e:  # noqa: BLE001 - one watch's failure must not hide the others
        return f"{head}: lookup failed, {type(e).__name__}"
    priced = [
        (r.price, r.departure, r.currency)
        for r in rows
        if r.price is not None and r.departure is not None
    ]
    if not priced:
        return f"{head}: no priced date"
    price, day, currency = min(priced, key=lambda p: (p[0], p[1]))
    verdict = ""
    if w.below is not None:
        verdict = ", under the ceiling" if price < w.below else ", over the ceiling"
    # Fixed point keeps every digit of a fare of a million or more, which `:g` rounds.
    shown = f"{price:.2f}".removesuffix(".00")
    amount = f"{currency or ''} {shown}".strip()
    return f"{head}: lowest {amount} on {day}{verdict}"


# The currency and the exception name are remote text, and a reader of stdout takes
# each line as one watch, so the newline CTRL keeps would split a record in two.
_ONE_LINE: Final = {**CTRL, ord("\n"): " "}


@watch_app.command("run")
def watch_run() -> None:
    """Price each saved watch once and print a line each; nothing is sent or saved."""
    stored = _load()
    for w in stored:
        typer.echo(_poll(w).translate(_ONE_LINE))
    if not stored:
        typer.echo("No watches saved.")


@watch_app.command("rm")
def watch_rm(watch_id: Annotated[int, typer.Argument(metavar="ID", help="Id from `list`")]) -> None:
    """Delete one saved watch."""
    if not watches_path().parent.is_dir():
        raise _fail(f"no watch with id {watch_id}", 1)
    with _locked():
        stored = _load()
        gone = next((w for w in stored if w.id == watch_id), None)
        if gone is None:
            raise _fail(f"no watch with id {watch_id}", 1)
        _save([w for w in stored if w is not gone])
    typer.echo(f"Removed {watch_id}: {gone.describe()}")
