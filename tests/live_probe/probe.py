"""Replay saved runs of the Google http-versus-Chrome disagreement probe.

    python tests/live_probe/probe.py --replay RUNS --out DIR

RUNS holds one step directory per load, named <query>-<role> (role http or
browser) with a `.2` suffix on the second, back-to-back pair. A step directory
holds argv.json, stdout, stderr, seconds and exit. The script writes
DIR/report.json and DIR/summary.txt and prints the summary. It reads files
only and imports nothing outside the standard library.

Exit 2 when the run cannot be counted: a usage error, a RUNS directory or step
file that cannot be read, a step directory or file that is a symbolic link, an
argv.json or exit file that does not parse, a step file whose JSON nests more
than 64 levels deep, a lock that failed on any step, no step that succeeded, or
a DIR that cannot be written or whose report.json or summary.txt is a symbolic
link. Otherwise exit 1 when a finding is counted and 0 when none is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, assert_never, cast

_STEP = re.compile(r"(?P<query>.+)-(?P<role>http|browser)(?P<second>\.2)?")
_EXIT = re.compile(r"-?\d{1,4}")
_ROLES = {"http", "browser"}
_PRICE_TOLERANCE = 0.01
_TOP_ROWS = 10
_MAX_DEPTH = 64

type Finding = tuple[int, str]  # (rule, check name)
type Status = Literal[
    "ok", "timeout", "lock-timeout", "lock-failed", "throttled", "refused", "no-document"
]


class RunNotValid(Exception):
    """A step directory the replay cannot read."""


@dataclass(frozen=True)
class Step:
    status: Status
    doc: dict[str, Any] | None


def _is_ok(status: Status) -> bool:
    match status:
        case "ok":
            return True
        case "timeout" | "lock-timeout" | "lock-failed" | "throttled" | "refused" | "no-document":
            return False
        case _:
            assert_never(status)


def _obj(value: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _arr(value: object) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def _num(value: object) -> float | None:
    return value if isinstance(value, int | float) else None


def _read(path: Path) -> str:
    try:
        # A link could lead outside RUNS, or let one load stand in for another.
        for link in (path.parent, path):
            if link.is_symlink():
                raise RunNotValid(f"{path.parent.name}: {link.name} is a symbolic link")
        return path.read_text(errors="replace")
    except OSError as err:
        raise RunNotValid(f"{path.parent.name}: {err.strerror or err}") from err


def _write(path: Path, text: str) -> None:
    # O_NOFOLLOW: a link left in DIR would carry the write outside it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "w") as file:
        file.write(text)


def _depth(value: object) -> int:
    """How many lists and objects nest in a parsed JSON value."""
    deepest, todo = 0, [(value, 1)]
    while todo:
        item, level = todo.pop()
        members = _arr(item)
        if members is None and (obj := _obj(item)) is not None:
            members = list(obj.values())
        if members is not None:
            deepest = max(deepest, level)
            todo.extend((m, level + 1) for m in members)
    return deepest


def _parse(text: str, where: str) -> object:
    """The JSON in text; ValueError when it is not JSON."""
    # A fixed limit: how deep json reads and writes differs by Python version.
    too_deep = RunNotValid(f"{where} nests more than {_MAX_DEPTH} levels deep")
    try:
        value = json.loads(text)
    except RecursionError as err:
        raise too_deep from err
    if _depth(value) > _MAX_DEPTH:
        raise too_deep
    return value


def _load_step(path: Path) -> Step:
    try:
        argv = _obj(_parse(_read(path / "argv.json"), f"{path.name}: argv.json"))
    except ValueError as err:
        raise RunNotValid(f"{path.name}: argv.json is not JSON") from err
    flight = _arr(argv.get("flight")) if argv else None
    full = _arr(argv.get("full")) if argv else None
    if flight is None or full is None:
        raise RunNotValid(f"{path.name}: argv.json lacks flight and full lists")
    locked = len(full) > len(flight) + 1
    # Matched as text: a JSON parse of a file like [[[... raises RecursionError.
    text = _read(path / "exit").strip()
    if not _EXIT.fullmatch(text):
        raise RunNotValid(f"{path.name}: exit file is not an integer")
    code = int(text)
    stdout = _read(path / "stdout")
    if code == 124:
        return Step("lock-timeout" if locked else "timeout", None)
    if locked and code != 0 and not stdout.strip():
        return Step("lock-failed", None)
    if "rate-limited" in _read(path / "stderr"):
        return Step("throttled", None)
    if code == 2:
        return Step("refused", None)
    if code in (0, 1):
        try:
            doc = _obj(_parse(stdout, f"{path.name}: stdout"))
        except ValueError:
            doc = None
        if doc is not None and "complete" in doc:
            return Step("ok", doc)
    return Step("no-document", None)


def _cabins(doc: dict[str, Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for item in _arr(doc.get("results")) or []:
        group = _obj(item)
        if group is not None and isinstance(group.get("cabin"), str):
            out[group["cabin"]] = _arr(group.get("rows")) or []
    return out


def _cheapest(rows: list[Any]) -> float | None:
    prices = [p for r in rows if (p := _num((_obj(r) or {}).get("price"))) is not None]
    return min(prices) if prices else None


def _separate(row: object) -> bool:
    body = (_obj(row) or {}).get("row")
    members = _arr(body) or [body]
    return any((_obj(m) or {}).get("separate_tickets") is True for m in members)


def _split(doc: dict[str, Any]) -> tuple[int | None, float | None]:
    combos = _arr((_obj(doc.get("split_ticket")) or {}).get("combinations"))
    if combos is None:
        return None, None
    totals = [t for c in combos if (t := _num((_obj(c) or {}).get("total"))) is not None]
    return len(combos), (min(totals) if totals else None)


def _apart(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is not b
    try:
        # Float subtraction puts many one-cent gaps, such as 400.04 - 400.03, just above 0.01.
        return round(abs(a - b), 9) > _PRICE_TOLERANCE
    except OverflowError:  # an int past float range is far from any float
        return True


def _rule1(http: dict[str, Any], browser: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Check name -> (http value, browser value) for each field that differs."""
    found: dict[str, tuple[Any, Any]] = {}
    for key in ("complete", "backend"):
        if http.get(key) != browser.get(key):
            found[key] = (http.get(key), browser.get(key))
    a, b = _cabins(http), _cabins(browser)
    for cabin in sorted(a.keys() | b.keys()):
        if (cabin in a) != (cabin in b):
            found[cabin + " board"] = (cabin in a, cabin in b)
            continue
        ra, rb = a[cabin], b[cabin]
        if len(ra) != len(rb):
            found[cabin + " rows"] = (len(ra), len(rb))
        ca, cb = _cheapest(ra), _cheapest(rb)
        if _apart(ca, cb):
            found[cabin + " cheapest"] = (ca, cb)
        sa = sum(_separate(r) for r in ra[:_TOP_ROWS])
        sb = sum(_separate(r) for r in rb[:_TOP_ROWS])
        if sa != sb:
            found[cabin + " separate-ticket rows"] = (sa, sb)
    (na, ta), (nb, tb) = _split(http), _split(browser)
    if na != nb:
        found["split_ticket combinations"] = (na, nb)
    if _apart(ta, tb):
        found["split_ticket cheapest"] = (ta, tb)
    return found


def _rule3(doc: dict[str, Any]) -> bool:
    return doc.get("complete") is False and not doc.get("notes")


def _pair_findings(pair: dict[str, Step]) -> dict[Finding, tuple[Any, Any]]:
    found: dict[Finding, tuple[Any, Any]] = {}
    http, browser = pair["http"].doc, pair["browser"].doc
    if http is not None and browser is not None:
        for check, values in _rule1(http, browser).items():
            found[(1, check)] = values
    bad = {r: s.doc is not None and _rule3(s.doc) for r, s in pair.items()}
    if any(bad.values()):
        found[(3, "incomplete")] = (
            "incomplete, no notes" if bad["http"] else "fine",
            "incomplete, no notes" if bad["browser"] else "fine",
        )
    return found


def _confirm(
    first: dict[Finding, tuple[Any, Any]], second: dict[str, Step] | None
) -> tuple[dict[Finding, tuple[Any, Any]], list[str]]:
    """Split a query's first-pair findings into counted and unconfirmed."""
    again: set[Finding] = set()
    if (
        second is not None
        and set(second) == _ROLES
        and all(_is_ok(s.status) for s in second.values())
    ):
        again = set(_pair_findings(second))
    counted = {k: v for k, v in first.items() if k in again}
    return counted, [k[1] for k in first if k not in again]


def replay(runs: Path) -> tuple[dict[str, Any], list[str]]:
    """The report and its summary lines."""
    pairs: dict[tuple[str, bool], dict[str, Step]] = {}
    try:
        # is_dir raises when RUNS can be read but not searched.
        listed = sorted(p for p in runs.iterdir() if p.is_dir())
    except OSError as err:
        raise RunNotValid(f"{runs}: {err.strerror or err}") from err
    for path in listed:
        m = _STEP.fullmatch(path.name)
        if m is None:
            continue
        step = _load_step(path)
        pairs.setdefault((m["query"], m["second"] is not None), {})[m["role"]] = step
    queries = sorted({q for q, _ in pairs})
    steps = Counter(s.status for p in pairs.values() for s in p.values())
    findings: list[dict[str, Any]] = []
    unconfirmed: list[str] = []
    lines: list[str] = []
    for query in queries:
        first = pairs.get((query, False), {})
        if set(first) != _ROLES:
            continue
        counted, dropped = _confirm(_pair_findings(first), pairs.get((query, True)))
        unconfirmed += dropped
        for (rule, check), (a, b) in counted.items():
            findings.append({"query": query, "rule": rule, "check": check, "http": a, "browser": b})
            lines.append(f"{query} rule {rule} {check}: http {a} vs browser {b}")
    invalid: list[str] = []
    if steps["lock-failed"]:
        invalid.append(f"run not valid: lock failed on {steps['lock-failed']} step(s)")
    if not steps["ok"]:
        invalid.append("run not valid: no step succeeded")
    counts = ", ".join(f"{k}={v}" for k, v in sorted(steps.items()))
    head = f"{len(findings)} disagreement(s) over {len(queries)} queries; steps: {counts}"
    report: dict[str, Any] = {
        "queries": queries,
        "steps": dict(steps),
        "findings": findings,
        "unconfirmed": unconfirmed,
        "invalid": invalid,
    }
    return report, [head, *lines, *invalid]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay saved disagreement-probe runs.")
    parser.add_argument("--replay", required=True, type=Path, metavar="RUNS")
    parser.add_argument("--out", required=True, type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    runs: Path = args.replay
    out: Path = args.out
    try:
        # Python 3.12's is_dir raises when a parent of RUNS cannot be searched.
        is_dir = runs.is_dir()
    except OSError as err:
        parser.error(f"RUNS cannot be read: {runs}: {err.strerror or err}")
    if not is_dir:
        parser.error(f"RUNS is not a directory: {runs}")
    try:
        report, lines = replay(runs)
    except RunNotValid as err:
        print(f"run not valid: {err}", file=sys.stderr)
        return 2
    # Query names and JSON strings may hold text no output encoding takes.
    summary = ("\n".join(lines) + "\n").encode("ascii", "backslashreplace").decode("ascii")
    try:
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "report.json", json.dumps(report, indent=2) + "\n")
        _write(out / "summary.txt", summary)
    except OSError as err:
        print(f"cannot write {out}: {err}", file=sys.stderr)
        return 2
    sys.stdout.write(summary)
    if report["invalid"]:
        return 2
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
