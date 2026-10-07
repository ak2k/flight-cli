"""`--format envelope`: one versioned JSON document for `search` and `calendar`.

`--format json` writes whatever its path answers with (a Matrix body, Google
rows, `{cabin: ...}`, the award document, or nothing when the award query
fails), and what the path lost goes to stderr alone. The envelope has the same
keys on every path: `notes` carries the stderr lines, and `complete` is false
whenever the answer is narrower than what was asked.

`run` swaps the process streams for the command. Stdout goes to a buffer, so a
path that writes past the recorder cannot put a second document beside the
envelope; stderr goes through a tee that keeps a copy for `notes`. The recorder
is module state rather than a context variable, because the fan-outs record
from worker threads and a thread does not inherit the caller's context.

Stdlib and pydantic only: every module that can narrow an answer imports this
one, and none of them can be imported from here.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — pydantic reads field annotations at runtime
import io
import json
import re
import sys
import threading
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TextIO, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

type Command = Literal["search", "calendar"]
type Backend = Literal["gflight", "matrix"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResultRow(_Frozen):
    """One itinerary, priced day or graph cell. `row` is the object `--format
    json` prints for it, unchanged; `price` is the trip's price, a round trip's
    being its last member's."""

    price: float | None
    currency: str | None
    row: Any


class CabinRows(_Frozen):
    cabin: str
    rows: list[ResultRow]


class Insight(_Frozen):
    """Google's price insight for one cabin's page."""

    cabin: str
    currency: str
    cheapest: float
    typical_low: float
    typical_high: float
    level: Literal["low", "typical", "high"]


class PricePoint(_Frozen):
    date: dt.date
    price: float


class PriceHistory(_Frozen):
    """Google's daily price history for one cabin's page."""

    cabin: str
    currency: str | None
    points: list[PricePoint]


class PriceRange(_Frozen):
    low: float
    high: float


class MinuteRange(_Frozen):
    low: int
    high: int


class CodeName(_Frozen):
    code: str
    name: str


class ConnectingAirport(_Frozen):
    code: str
    city: str


class RouteFacets(_Frozen):
    """The filter choices Google's page states for one cabin's search, labeled
    with its airports: its fare range in the rows' basis and currency, its
    trip-length and layover ranges, and the alliances, airlines and connecting
    airports its filters offer, in the page's order. An alliance's code is
    spelled as `--extension 'ALLIANCE …'` takes it."""

    cabin: str
    origins: list[str]
    destinations: list[str]
    currency: str | None
    price: PriceRange
    duration_minutes: MinuteRange
    layover_minutes: MinuteRange
    airlines: list[CodeName]
    alliances: list[CodeName]
    connecting_airports: list[ConnectingAirport]


class PriceGraphCell(_Frozen):
    """One date pair of Google's price graph; `return` is null on a one-way."""

    departure: dt.date
    return_date: dt.date | None = Field(alias="return")
    price: float


class PriceGraph(_Frozen):
    """Google's price graph for one trip length (null on a one-way): its
    estimate for each date pair, with no itinerary behind it."""

    trip_length: int | None
    currency: str
    cells: list[PriceGraphCell]


class SearchEnvelope(_Frozen):
    version: Literal[1]
    command: Literal["search"]
    backend: Backend | None
    currency: str | None
    complete: bool
    notes: list[str]
    results: list[CabinRows]
    awards: list[dict[str, Any]] | None
    insight: list[Insight]
    price_history: list[PriceHistory]
    facets: list[RouteFacets]
    price_graph: list[PriceGraph]
    verify: dict[str, Any] | None
    cross_check: dict[str, Any] | None
    split_ticket: dict[str, Any] | None


class CalendarEnvelope(_Frozen):
    version: Literal[1]
    command: Literal["calendar"]
    backend: Backend | None
    currency: str | None
    complete: bool
    notes: list[str]
    results: list[ResultRow]
    awards: list[dict[str, Any]] | None
    insight: list[Insight]
    price_history: list[PriceHistory]
    facets: list[RouteFacets]
    price_graph: list[PriceGraph]
    verify: dict[str, Any] | None
    cross_check: dict[str, Any] | None
    split_ticket: dict[str, Any] | None


ENVELOPE: TypeAdapter[SearchEnvelope | CalendarEnvelope] = TypeAdapter(
    Annotated[SearchEnvelope | CalendarEnvelope, Field(discriminator="command")]
)


def schema_text() -> str:
    """The JSON Schema `docs/envelope.schema.json` holds."""
    return json.dumps(ENVELOPE.json_schema(), indent=2) + "\n"


class _Recorder:
    def __init__(self, command: Command) -> None:
        self.command: Command = command
        self.lock = threading.Lock()
        self.backend: Backend | None = None
        # The backend whose answer each narrowing narrowed, None for the search's.
        self.narrowed: list[Backend | None] = []
        self.narrowings: list[str] = []
        self.asked: list[str] = []
        self.by_cabin: dict[str, list[ResultRow]] = {}
        self.days: list[ResultRow] = []
        self.awards: list[dict[str, Any]] | None = None
        self.insight: list[Insight] = []
        self.history: list[PriceHistory] = []
        self.facets: list[RouteFacets] = []
        self.graphs: list[PriceGraph] = []
        self.verify: dict[str, Any] | None = None
        self.cross_check: dict[str, Any] | None = None
        self.split_ticket: dict[str, Any] | None = None
        self.reasons: dict[str, str] = {}


class _Slot:
    recorder: _Recorder | None = None


_slot = _Slot()


def active() -> bool:
    """Whether an envelope run is recording; every call below is a no-op when not."""
    return _slot.recorder is not None


def narrow(note: str | None = None, *, of: Backend | None = None) -> None:
    """The answer is narrower than what was asked: said where the narrowing is.

    `note` joins the envelope's notes, for a site with no stderr line of its
    own: the table and JSON outputs print nothing there. `of` names the backend
    whose answer is narrower. Once the other backend answers the search in its
    place, what that backend could not read narrows nothing that is shown, and
    stays only as its stderr line or its note."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.narrowed.append(of)
            if note is not None:
                rec.narrowings.append(note)


def explain(key: str, reason: str) -> None:
    """Why `key` will be null or empty, for its note. The first reason given stands."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.reasons.setdefault(key, reason)


def ask_cabins(cabins: Iterable[str]) -> None:
    """The cabins a search asked for, in `--cabin` order: one `results` entry each."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.asked = list(cabins)


def record_search(
    *,
    backend: Backend,
    cabin: str,
    rows: Sequence[ResultRow],
    insights: Sequence[Insight] = (),
    histories: Sequence[PriceHistory] = (),
    facets: Sequence[RouteFacets] = (),
) -> None:
    """One cabin's rows, with the insight, history and facets of each page
    that answered it: one page, or several where a leg was asked as several."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.backend = backend
            rec.by_cabin[cabin] = list(rows)
            rec.insight.extend(insights)
            rec.history.extend(histories)
            rec.facets.extend(facets)


def record_calendar(*, backend: Backend, rows: Sequence[ResultRow]) -> None:
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.backend = backend
            rec.days = list(rows)


def record_price_graph(graphs: Sequence[PriceGraph]) -> None:
    """Google's price graph read for a calendar, one entry per trip length."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.graphs = list(graphs)


def record_awards(entries: list[dict[str, Any]]) -> None:
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.awards = entries


def record_verify(check: dict[str, Any]) -> None:
    """Row `--pick`'s check on Matrix: the `verify` object `--format json` prints."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.verify = check


def record_cross_check(check: dict[str, Any]) -> None:
    """The `--enrich` cross-check: the `cross_check` object `--format json` prints."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.cross_check = check


def record_split_ticket(ticket: dict[str, Any]) -> None:
    """`--split`'s pair: the `split_ticket` object `--format json --split` prints."""
    if (rec := _slot.recorder) is not None:
        with rec.lock:
            rec.split_ticket = ticket


class _SoftWrapping(Protocol):
    soft_wrap: bool


class _Tee:
    """A stream that writes through to `stream` and keeps a copy.

    Everything but `write` is the stream's own, so a console asking whether it
    is a terminal gets the same answer it would without the tee."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._seen = io.StringIO()
        # Reentrant: a SIGINT handler that logs runs on the thread already
        # inside `write`.
        self._lock = threading.RLock()

    def write(self, s: str) -> int:
        # One lock round both writes, so the notes keep the order stderr took.
        with self._lock:
            n = self._stream.write(s)
            self._seen.write(s)
        return n

    def flush(self) -> None:
        self._stream.flush()

    def seen(self) -> str:
        with self._lock:
            return self._seen.getvalue()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _exit_code(e: BaseException) -> int:
    """The status `e` ends the process with: click's exits and usage errors carry
    theirs, and anything else, an abort among them, ends it with 1."""
    if isinstance(e, SystemExit):
        return e.code if isinstance(e.code, int) else int(e.code is not None)
    code = getattr(e, "exit_code", None)
    return code if isinstance(code, int) else 1


def run(command: Command, call: Callable[[], None], *, consoles: Sequence[_SoftWrapping]) -> None:
    """Run `call` as an envelope run, then write its one envelope to stdout.

    No envelope at exit 2: that is a usage error, and the command never ran.
    `consoles` print one stderr line per message for the run, so each message
    is one note."""
    rec = _Recorder(command)
    out, err = sys.stdout, sys.stderr
    tee = _Tee(err)
    stray = io.StringIO()
    wrapped = [c.soft_wrap for c in consoles]
    for c in consoles:
        c.soft_wrap = True
    sys.stdout, sys.stderr = stray, cast("TextIO", tee)
    _slot.recorder = rec
    code = 0
    try:
        call()
    except BaseException as e:
        code = _exit_code(e)
        raise
    finally:
        _slot.recorder = None
        sys.stdout, sys.stderr = out, err
        for c, was in zip(consoles, wrapped, strict=True):
            c.soft_wrap = was
        if code in (0, 1):
            out.write(_document(rec, code=code, stderr=tee.seen(), stray=stray.getvalue()) + "\n")
            out.flush()


# CSI and OSC sequences: colour, cursor movement and hyperlinks.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _note_lines(text: str) -> list[str]:
    # Split on newlines alone: `str.splitlines` also breaks on separators a
    # message may carry inside one line.
    return [ln.rstrip() for ln in _ANSI.sub("", text).split("\n") if ln.strip()]


def _document(rec: _Recorder, *, code: int, stderr: str, stray: str) -> str:
    unanswered: list[str] = []
    groups: list[CabinRows] = []
    if rec.command == "search":
        asked = rec.asked or list(rec.by_cabin)
        unanswered = [c for c in asked if c not in rec.by_cabin]
        extra = [c for c in rec.by_cabin if c not in asked]
        groups = [CabinRows(cabin=c, rows=rec.by_cabin.get(c, [])) for c in [*asked, *extra]]
        rows = [r for g in groups for r in g.rows]
    else:
        rows = rec.days
    priced = {r.currency for r in rows if r.price is not None}
    currency = next(iter(priced)) if len(priced) == 1 else None
    narrowed = any(of is None or rec.backend in (None, of) for of in rec.narrowed)
    complete = code == 0 and not narrowed and not unanswered
    notes = [
        *_note_lines(stderr),
        *rec.narrowings,
        *_key_notes(rec, code, rows=rows, priced=priced),
    ]
    if stray.strip():
        notes.append(f"stdout: {len(stray)} characters written outside the envelope were dropped")
    common: dict[str, Any] = {
        "version": 1,
        "backend": rec.backend,
        "currency": currency,
        "complete": complete,
        "notes": notes,
        "awards": rec.awards,
        "insight": rec.insight,
        "price_history": rec.history,
        "facets": rec.facets,
        "price_graph": rec.graphs,
        "verify": rec.verify,
        "cross_check": rec.cross_check,
        "split_ticket": rec.split_ticket,
    }
    doc = (
        SearchEnvelope(command="search", results=groups, **common)
        if rec.command == "search"
        else CalendarEnvelope(command="calendar", results=rows, **common)
    )
    # By alias: a graph cell's `return` is a keyword in Python.
    return json.dumps(doc.model_dump(mode="json", by_alias=True), indent=2)


def _key_notes(
    rec: _Recorder, code: int, *, rows: list[ResultRow], priced: set[str | None]
) -> list[str]:
    """One line per null or empty key, naming the key and why."""
    calendar = rec.command == "calendar"
    why = rec.reasons
    failed = "the run failed before an answer" if code else "nothing answered"
    notes: list[str] = []
    if rec.backend is None:
        notes.append(f"backend: {why.get('backend', failed)}")
    if not priced:
        notes.append("currency: no row is priced")
    elif len(priced) > 1:
        notes.append("currency: the priced rows carry more than one currency")
    elif None in priced:
        notes.append("currency: the priced rows carry no currency")
    if not rows:
        none = "no priced day" if calendar else "no itinerary in any cabin asked"
        notes.append(f"results: {why.get('results', failed if rec.backend is None else none)}")
    if rec.awards is None:
        default = "the run ended before the award search" if code else "no award search ran"
        reason = "calendar runs no award search" if calendar else why.get("awards", default)
        notes.append(f"awards: {reason}")
    for key, items in (
        ("insight", rec.insight),
        ("price_history", rec.history),
        ("facets", rec.facets),
    ):
        if items:
            continue
        if calendar:
            reason = "a calendar carries none"
        elif rec.backend == "matrix":
            reason = "Matrix answered, and only a Google Flights page carries one"
        elif rec.backend == "gflight":
            reason = "no Google Flights page answered with one"
        else:
            reason = failed
        notes.append(f"{key}: {reason}")
    return [
        *notes,
        *_graph_note(rec, calendar=calendar, failed=failed),
        *_check_notes(rec, calendar=calendar),
        *_split_note(rec, calendar=calendar),
    ]


def _graph_note(rec: _Recorder, *, calendar: bool, failed: str) -> list[str]:
    if rec.graphs:
        return []
    reason = rec.reasons.get("price_graph", failed) if calendar else "a search carries none"
    return [f"price_graph: {reason}"]


def _split_note(rec: _Recorder, *, calendar: bool) -> list[str]:
    if rec.split_ticket is not None:
        return []
    unpriced = "the run ended before the split ticket was priced"
    reason = (
        "a calendar prices no split ticket"
        if calendar
        else rec.reasons.get("split_ticket", unpriced)
    )
    return [f"split_ticket: {reason}"]


def _check_notes(rec: _Recorder, *, calendar: bool) -> list[str]:
    """The note for each check of a search's rows on Matrix that the run did not record."""
    notes: list[str] = []
    if rec.verify is None:
        # A search that asked for no check says so where it starts, so the
        # fallback is a check that was asked for and never finished.
        unchecked = "the run ended before the row was checked"
        reason = (
            "a calendar checks no row on Matrix"
            if calendar
            else rec.reasons.get("verify", unchecked)
        )
        notes.append(f"verify: {reason}")
    if rec.cross_check is None:
        reason = (
            "a calendar runs no cross-check"
            if calendar
            else rec.reasons.get("cross_check", "no cross-check ran")
        )
        notes.append(f"cross_check: {reason}")
    return notes
