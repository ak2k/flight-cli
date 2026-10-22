"""The disagreement probe's replay: what it counts, and when it refuses to pass."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_PROBE = Path(__file__).resolve().parent / "live_probe" / "probe.py"
_LINE = "q1 rule 1 COACH cheapest: http 679 vs browser 457"


def _envelope(
    prices: list[float] | None = None,
    *,
    complete: bool = True,
    notes: tuple[str, ...] = ("a note",),
    rows: list[dict[str, Any]] | None = None,
    split: dict[str, Any] | None = None,
) -> str:
    if rows is None:
        rows = [
            {"price": p, "currency": "USD", "row": {"separate_tickets": False}}
            for p in prices or []
        ]
    return json.dumps(
        {
            "version": 1,
            "command": "search",
            "backend": "gflight",
            "complete": complete,
            "notes": list(notes),
            "results": [{"cabin": "COACH", "rows": rows}],
            "split_ticket": split,
        }
    )


def _step(
    runs: Path,
    name: str,
    stdout: str,
    *,
    code: str = "0",
    stderr: str = "",
    prefix: tuple[str, ...] = (),
) -> None:
    step = runs / name
    step.mkdir(parents=True)
    flight = ["search"]
    (step / "argv.json").write_text(
        json.dumps({"flight": flight, "full": [*prefix, "flight", *flight]})
    )
    (step / "stdout").write_text(stdout)
    (step / "stderr").write_text(stderr)
    (step / "seconds").write_text("1.5")
    (step / "exit").write_text(code)


def _pairs(runs: Path, http: float, browser: float, suffix: str = "") -> None:
    _step(runs, "q1-http" + suffix, _envelope([http]))
    _step(runs, "q1-browser" + suffix, _envelope([browser]))


def _run(runs: Path, out: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(_PROBE), "--replay", str(runs), "--out", str(out)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603 # argv is this file's own paths


def _replay(tmp_path: Path, runs: Path) -> tuple[int, str, dict[str, Any] | None]:
    out = tmp_path / "out"
    done = _run(runs, out)
    report = out / "report.json"
    parsed = json.loads(report.read_text()) if report.exists() else None
    return done.returncode, done.stdout + done.stderr, parsed


def test_a_disagreement_the_second_pair_does_not_repeat_is_not_counted(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 679, 457)
    _pairs(runs, 457, 457, ".2")
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, out.split()[0], _LINE in out) == (0, "0", False)
    assert report["unconfirmed"] == ["COACH cheapest"]


def test_a_repeated_disagreement_is_counted(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 679, 457)
    _pairs(runs, 679, 457, ".2")
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, out.split()[0], _LINE in out) == (1, "1", True)
    assert report["unconfirmed"] == []
    assert [f["check"] for f in report["findings"]] == ["COACH cheapest"]


def test_a_disagreement_with_no_second_pair_is_not_counted(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 679, 457)
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, _LINE in out, report["unconfirmed"]) == (0, False, ["COACH cheapest"])


def test_prices_within_a_cent_agree(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 457.005, 457)
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, report["unconfirmed"]) == (0, [])


@pytest.mark.parametrize(("http", "browser"), [(400.04, 400.03), (0.04, 0.03)])
def test_prices_a_cent_apart_agree(tmp_path: Path, http: float, browser: float) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, http, browser)
    _pairs(runs, http, browser, ".2")
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, report["findings"], report["unconfirmed"]) == (0, [], [])


def test_rule_3_counts_an_incomplete_envelope_with_no_notes(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, _envelope([457], complete=False, notes=()))
        _step(runs, "q1-browser" + suffix, _envelope([457]))
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert code == 1
    assert "q1 rule 3 incomplete: http incomplete, no notes vs browser fine" in out
    assert [(f["rule"], f["check"]) for f in report["findings"]] == [
        (1, "complete"),
        (3, "incomplete"),
    ]


@pytest.mark.parametrize("case", ["tenth", "eleventh"])
def test_only_the_top_10_rows_count_separate_tickets(tmp_path: Path, case: str) -> None:
    def rows(sep_at: int | None) -> list[dict[str, Any]]:
        return [
            {"price": 500.0, "currency": "USD", "row": {"separate_tickets": i == sep_at}}
            for i in range(11)
        ]

    sep = 9 if case == "tenth" else 10
    runs = tmp_path / "runs"
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, _envelope(rows=rows(sep)))
        _step(runs, "q1-browser" + suffix, _envelope(rows=rows(None)))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    counted = [f["check"] for f in report["findings"]]
    assert (code, counted) == ((1, ["COACH separate-ticket rows"]) if case == "tenth" else (0, []))


def test_a_list_row_counts_when_any_member_is_a_separate_ticket(tmp_path: Path) -> None:
    listed = [
        {
            "price": 500.0,
            "currency": "USD",
            "row": [{"separate_tickets": False}, {"separate_tickets": True}],
        }
    ]
    runs = tmp_path / "runs"
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, _envelope(rows=listed))
        _step(runs, "q1-browser" + suffix, _envelope([500]))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, [f["check"] for f in report["findings"]]) == (1, ["COACH separate-ticket rows"])


def test_split_ticket_count_and_cheapest_total_are_compared(tmp_path: Path) -> None:
    split = {"combinations": [{"total": 900}, {"total": 950}]}
    other = {"combinations": [{"total": 800}]}
    runs = tmp_path / "runs"
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, _envelope([457], split=split))
        _step(runs, "q1-browser" + suffix, _envelope([457], split=other))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, [f["check"] for f in report["findings"]]) == (
        1,
        ["split_ticket combinations", "split_ticket cheapest"],
    )


def test_a_throttled_step_is_recorded_not_a_finding(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _step(runs, "q1-http", "", code="1", stderr="rate-limited by Google")
    _step(runs, "q1-browser", _envelope([457]))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, report["steps"], report["findings"]) == (0, {"throttled": 1, "ok": 1}, [])


@pytest.mark.parametrize("case", ["lock-failed", "nothing-measured"])
def test_a_run_whose_lock_failed_or_that_measured_nothing_exits_2(
    tmp_path: Path, case: str
) -> None:
    runs = tmp_path / "runs"
    if case == "lock-failed":
        _step(runs, "q1-http", "", code="1", prefix=("flock",))
        _step(runs, "q1-browser", _envelope([457]))
        want = "run not valid: lock failed on 1 step(s)"
    else:
        _step(runs, "q1-http", "", code="2")
        _step(runs, "q1-browser", "not json")
        want = "run not valid: no step succeeded"
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, want in out, report["invalid"]) == (2, True, [want])


@pytest.mark.parametrize("prefix", ["no-prefix", "one-word-prefix"])
def test_a_one_word_lock_prefix_marks_a_step_locked(tmp_path: Path, prefix: str) -> None:
    runs = tmp_path / "runs"
    lock = () if prefix == "no-prefix" else ("flock",)
    _step(runs, "q1-http", "", code="1", prefix=lock)
    _step(runs, "q1-browser", _envelope([457]))
    _code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert report["steps"] == (
        {"no-document": 1, "ok": 1} if prefix == "no-prefix" else {"lock-failed": 1, "ok": 1}
    )


def test_a_locked_step_that_timed_out_is_a_lock_timeout(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _step(runs, "q1-http", "", code="124", prefix=("flock",))
    _step(runs, "q1-browser", _envelope([457]), prefix=())
    _code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert report["steps"] == {"lock-timeout": 1, "ok": 1}


@pytest.mark.parametrize("text", ["[" * 100000, "abc", "", "12345"])
def test_an_exit_file_that_is_not_a_number_makes_the_run_not_valid(
    tmp_path: Path, text: str
) -> None:
    runs = tmp_path / "runs"
    _step(runs, "q1-http", _envelope([457]), code=text)
    _step(runs, "q1-browser", _envelope([457]))
    code, out, report = _replay(tmp_path, runs)
    assert (code, report, "run not valid: q1-http: " in out) == (2, None, True)


def test_a_missing_exit_file_makes_the_run_not_valid(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _step(runs, "q1-http", _envelope([457]))
    (runs / "q1-http" / "exit").unlink()
    _step(runs, "q1-browser", _envelope([457]))
    code, out, _report = _replay(tmp_path, runs)
    assert (code, "run not valid: q1-http: " in out) == (2, True)


def test_an_out_dir_that_cannot_be_written_exits_2(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 457, 457)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    out = blocker / "out"
    done = _run(runs, out)
    assert done.returncode == 2
    assert done.stderr.startswith(f"cannot write {out}: ")
    assert done.stdout == ""
    assert not (out / "summary.txt").exists()


def test_a_usage_error_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    done = _run(missing, tmp_path / "o")
    assert done.returncode == 2
    assert "not a directory" in done.stderr


def test_a_second_pair_with_a_step_that_is_not_ok_confirms_nothing(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _step(runs, "q1-http", _envelope([457]))
    _step(runs, "q1-browser", _envelope([457], complete=False, notes=()))
    _step(runs, "q1-http.2", "", code="1", stderr="rate-limited by Google")
    _step(runs, "q1-browser.2", _envelope([457], complete=False, notes=()))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, report["findings"]) == (0, [])
    assert report["unconfirmed"] == ["complete", "incomplete"]


def test_a_second_pair_missing_a_step_confirms_nothing(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 679, 457)
    _step(runs, "q1-http.2", _envelope([679]))
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, "Traceback" in out, report["findings"]) == (0, False, [])
    assert report["unconfirmed"] == ["COACH cheapest"]


def test_a_value_no_encoding_takes_is_escaped_in_the_summary(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    odd = _envelope([457]).replace('"gflight"', '"\\ud800"')
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, odd)
        _step(runs, "q1-browser" + suffix, _envelope([457]))
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, "q1 rule 1 backend: http \\ud800 vs browser gflight" in out) == (1, True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory it cannot read")
def test_a_runs_dir_that_cannot_be_listed_makes_the_run_not_valid(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 457, 457)
    runs.chmod(0o300)
    try:
        code, out, report = _replay(tmp_path, runs)
    finally:
        runs.chmod(0o755)
    assert (code, report, f"run not valid: {runs}: " in out) == (2, None, True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a directory it cannot search")
def test_a_runs_dir_that_cannot_be_searched_makes_the_run_not_valid(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 457, 457)
    runs.chmod(0o600)
    try:
        code, out, report = _replay(tmp_path, runs)
    finally:
        runs.chmod(0o755)
    assert (code, report, "Traceback" in out, f"run not valid: {runs}: " in out) == (
        2,
        None,
        False,
        True,
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a directory it cannot search")
def test_a_runs_path_whose_parent_cannot_be_searched_is_a_usage_error(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    runs = parent / "runs"
    _pairs(runs, 457, 457)
    parent.chmod(0o600)
    try:
        code, out, report = _replay(tmp_path, runs)
    finally:
        parent.chmod(0o755)
    assert (code, report, "Traceback" in out, f"RUNS cannot be read: {runs}: " in out) == (
        2,
        None,
        False,
        True,
    )


def test_a_price_past_float_range_is_apart_from_a_float_price(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _pairs(runs, 10**400, 457.5)
    _pairs(runs, 10**400, 457.5, ".2")
    code, out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, "Traceback" in out, [f["check"] for f in report["findings"]]) == (
        1,
        False,
        ["COACH cheapest"],
    )


@pytest.mark.parametrize("depth", [64, 3000, 100000])
@pytest.mark.parametrize("file", ["stdout", "argv.json"])
def test_a_step_file_nested_past_64_levels_makes_the_run_not_valid(
    tmp_path: Path, file: str, depth: int
) -> None:
    # Python 3.12, 3.13 and 3.14 each read and write JSON to a different depth.
    runs = tmp_path / "runs"
    _pairs(runs, 457, 457)
    _pairs(runs, 457, 457, ".2")
    deep = runs / "q1-http" / file
    nested = "[" * depth + "]" * depth
    if file == "stdout":
        deep.write_text(_envelope([457]).replace('"gflight"', nested))
    else:
        deep.write_text(deep.read_text().removesuffix("}") + f', "x": {nested}}}')
    code, out, report = _replay(tmp_path, runs)
    want = f"run not valid: q1-http: {file} nests more than 64 levels deep"
    assert (code, report, "Traceback" in out, want in out) == (2, None, False, True)


def test_a_step_nested_64_levels_deep_is_reported(tmp_path: Path) -> None:
    nested = "[" * 63 + "]" * 63
    runs = tmp_path / "runs"
    for suffix in ("", ".2"):
        _step(runs, "q1-http" + suffix, _envelope([457]).replace('"gflight"', nested))
        _step(runs, "q1-browser" + suffix, _envelope([457]))
    code, _out, report = _replay(tmp_path, runs)
    assert report is not None
    assert (code, report["findings"][0]["http"]) == (1, json.loads(nested))


@pytest.mark.parametrize("link", ["q1-http.2", "stdout"])
def test_a_symbolic_link_in_runs_makes_the_run_not_valid(tmp_path: Path, link: str) -> None:
    runs, outside = tmp_path / "runs", tmp_path / "outside"
    _pairs(runs, 679, 457)
    _step(runs, "q1-browser.2", _envelope([457]))
    _step(outside, "q1-http", _envelope([679]))
    if link == "q1-http.2":
        (runs / link).symlink_to(outside / "q1-http")
    else:
        _step(runs, "q1-http.2", "")
        (runs / "q1-http.2" / link).unlink()
        (runs / "q1-http.2" / link).symlink_to(outside / "q1-http" / link)
    code, out, report = _replay(tmp_path, runs)
    want = f"run not valid: q1-http.2: {link} is a symbolic link"
    assert (code, report, "Traceback" in out, want in out) == (2, None, False, True)


@pytest.mark.parametrize("name", ["report.json", "summary.txt"])
def test_a_symbolic_link_in_out_is_not_written_through(tmp_path: Path, name: str) -> None:
    runs, out, outside = tmp_path / "runs", tmp_path / "out", tmp_path / "outside"
    _pairs(runs, 457, 457)
    out.mkdir()
    outside.write_text("keep")
    (out / name).symlink_to(outside)
    done = _run(runs, out)
    assert (done.returncode, done.stdout, outside.read_text()) == (2, "", "keep")
    assert done.stderr.startswith(f"cannot write {out}: ")


def test_the_docstring_names_every_cause_of_exit_2() -> None:
    doc = " ".join((ast.get_docstring(ast.parse(_PROBE.read_text())) or "").split())
    causes = (
        "a usage error",
        "a RUNS directory or step file that cannot be read",
        "a step directory or file that is a symbolic link",
        "an argv.json or exit file that does not parse",
        "a step file whose JSON nests more than 64 levels deep",
        "a lock that failed on any step",
        "no step that succeeded",
        "a DIR that cannot be written or whose report.json or summary.txt is a symbolic link",
    )
    assert [c for c in causes if c not in doc] == []
    assert "Otherwise exit 1 when a finding is counted and 0 when none is." in doc
