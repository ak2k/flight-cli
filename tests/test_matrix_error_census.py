"""Every arm in `cli.py` that fails a command on a `MatrixApiError` reports it
through `_print_matrix_error`, so one backend error reads the same whichever
command asked for it.

Parsed from the module. An arm is an `except` whose type names
`MatrixApiError`, or an `if` whose test is `isinstance(..., MatrixApiError)`
alone or under `and`/`or`. A new arm that prints the error some other way fails
here until it reports through the reporter or is added to `_SOFT` with its
reason. Not seen: a negated test, an `isinstance` inside another call, a
`match` case and a conditional expression. A list of callers in the reporter's
docstring rots on the next edit and says nothing when it does; this does."""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from collections import Counter

import pytest

from flight_cli import cli as cli_mod

_REPORTER = "_print_matrix_error"

# Arms that name a `MatrixApiError` and deliberately do not report it through the
# reporter, keyed by (enclosing function, "except" or "isinstance"). None of them
# fails the command on it: each is one part of several that still answers.
_SOFT: dict[tuple[str, str], str] = {
    ("_matrix_into", "except"): "stashes the error; the search weave reports it after",
    ("query_cabin", "except"): "one cabin's soft line, yellow and naming the cabin",
    ("_report_calendar_fanout", "isinstance"): "a lost group's soft line, beside a grid",
    ("_low_check_failure", "isinstance"): "the low check's no-answer text, exit code unchanged",
}


def _cli_tree() -> ast.Module:
    return ast.parse(pathlib.Path(cli_mod.__file__).read_text())


def _names_error(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "MatrixApiError" for n in ast.walk(node))


def _calls_reporter(body: list[ast.stmt]) -> bool:
    """Whether the arm runs the reporter whenever it runs: a statement of its
    own body calls it before one that leaves. A call inside a def, a lambda, a
    generator, a branch, a loop or a nested arm may not run when the arm does,
    and a call that exits, such as `sys.exit`, is not seen as leaving."""
    for stmt in body:
        match stmt:
            case ast.Expr(value=ast.Call(func=ast.Name(id=name))) if name == _REPORTER:
                return True
            case ast.Raise() | ast.Return() | ast.Break() | ast.Continue():
                return False
            case _:
                pass
    return False


def _tests_error(test: ast.expr) -> bool:
    """Whether the `if` body is a branch a `MatrixApiError` takes. A negated
    test sends it to the other branch, so `not` is not looked through."""
    match test:
        case ast.Call(func=ast.Name(id="isinstance")):
            return _names_error(test)
        case ast.BoolOp(values=values):
            return any(_tests_error(v) for v in values)
        case _:
            return False


def _arm_kind(node: ast.ExceptHandler | ast.If) -> str | None:
    if isinstance(node, ast.ExceptHandler):
        return "except" if node.type and _names_error(node.type) else None
    return "isinstance" if _tests_error(node.test) else None


def _arms(node: ast.AST, fn: str, found: list[tuple[str, str, bool]]) -> None:
    """Each arm the module docstring counts, as (enclosing function, kind,
    whether its body calls the reporter)."""
    for child in ast.iter_child_nodes(node):
        inside = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
        if isinstance(child, ast.ExceptHandler | ast.If) and (kind := _arm_kind(child)):
            found.append((fn, kind, _calls_reporter(child.body)))
        _arms(child, inside, found)


def test_every_arm_that_names_a_matrix_error_reports_it_through_the_reporter() -> None:
    found: list[tuple[str, str, bool]] = []
    _arms(_cli_tree(), "<module>", found)
    reporting = [(fn, kind) for fn, kind, said in found if said]
    # A key names a function and a kind, not one arm, so a listed key is counted
    # whole, reporting arms too: else a second silent arm, or one that takes the
    # place of a listed arm now reporting, would pass as the listed one.
    counted = Counter(
        (fn, kind, said) for fn, kind, said in found if not said or (fn, kind) in _SOFT
    )
    listed = Counter((fn, kind, False) for fn, kind in _SOFT)
    assert len(reporting) >= 1, found
    assert counted == listed, (
        f"arms naming MatrixApiError, as (function, kind, calls {_REPORTER}): "
        f"unlisted {sorted((counted - listed).elements())}, "
        f"stale {sorted((listed - counted).elements())}"
    )


def _census_with(
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    into: str | None = None,
    *,
    report_listed: bool = False,
) -> None:
    """Points the census at cli.py with `arm` put first in the def named `into`
    that already names the error, or in the module when `into` is None. With
    `report_listed`, the arms already in `into` call the reporter first."""
    tree = _cli_tree()
    target: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef = tree
    if into is not None:
        (target,) = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            and n.name == into
            and _names_error(n)
        ]
    for n in ast.walk(target) if report_listed else ():
        if isinstance(n, ast.ExceptHandler | ast.If) and _arm_kind(n):
            n.body[:0] = ast.parse(f"{_REPORTER}(e)").body
    target.body[:0] = ast.parse(arm).body
    monkeypatch.setattr(sys.modules[__name__], "_cli_tree", lambda: tree)


@pytest.mark.parametrize(
    ("into", "arm"),
    [
        ("query_cabin", "try:\n    pass\nexcept MatrixApiError as e2:\n    raise typer.Exit(1)"),
        ("_low_check_failure", "if isinstance(e, MatrixApiError):\n    raise typer.Exit(1)"),
    ],
    ids=["except", "isinstance"],
)
@pytest.mark.parametrize("report_listed", [False, True], ids=["listed-silent", "listed-reports"])
def test_the_census_fails_on_a_silent_arm_joining_a_soft_function(
    monkeypatch: pytest.MonkeyPatch, into: str, arm: str, report_listed: bool
) -> None:
    """A new silent arm beside the listed one, which stays silent or now reports."""
    _census_with(monkeypatch, arm, into, report_listed=report_listed)
    with pytest.raises(AssertionError) as failed:
        test_every_arm_that_names_a_matrix_error_reports_it_through_the_reporter()
    assert f"unlisted [({into!r}" in str(failed.value)


def test_the_reporter_docstring_names_no_private_function_of_the_module() -> None:
    """A census of callers in the docstring is what rotted; the test above is
    the census. Private names only: a command such as `search` is also a word."""
    names = {
        n.name
        for n in ast.walk(_cli_tree())
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    doc: str = cli_mod._print_matrix_error.__doc__ or ""  # pyright: ignore[reportPrivateUsage] — the docstring under test is the private reporter's
    named = sorted(set(re.findall(r"\b_\w+\b", doc)) & names)
    assert not named, f"{_REPORTER} docstring names {named}"


def test_the_console_sanitizing_note_points_at_this_census() -> None:
    note = pathlib.Path(__file__).parent.parent / "docs" / "memories" / "console_sanitizing.md"
    text = " ".join(note.read_text().split())
    assert "tests/test_matrix_error_census.py" in text
    assert "is the one deliberate exception" not in text


@pytest.mark.parametrize(
    "cond",
    ["isinstance(f, MatrixApiError) and f.message", "f is None or isinstance(f, MatrixApiError)"],
    ids=["and", "or"],
)
def test_the_census_fails_on_an_isinstance_arm_under_and_or(
    monkeypatch: pytest.MonkeyPatch, cond: str
) -> None:
    _census_with(monkeypatch, f"def _new_arm(f):\n    if {cond}:\n        raise typer.Exit(1)")
    with pytest.raises(AssertionError) as failed:
        test_every_arm_that_names_a_matrix_error_reports_it_through_the_reporter()
    assert "unlisted [('_new_arm', 'isinstance', False)]" in str(failed.value)


@pytest.mark.parametrize(
    "unrun",
    [
        "def later():\n            _print_matrix_error(e)",
        "later = lambda: _print_matrix_error(e)",
        "later = (_print_matrix_error(item) for item in (e,))",
        "raise typer.Exit(1)\n        _print_matrix_error(e)",
        "try:\n            pass\n        except MatrixApiError as inner:\n"
        "            _print_matrix_error(inner)",
        "if isinstance(f, MatrixApiError):\n            _print_matrix_error(f)",
    ],
    ids=["def", "lambda", "generator", "after-exit", "nested-except", "nested-isinstance"],
)
def test_the_census_fails_on_a_reporter_call_the_arm_may_not_run(
    monkeypatch: pytest.MonkeyPatch, unrun: str
) -> None:
    _census_with(
        monkeypatch,
        "def _new_arm(f):\n    try:\n        pass\n    except MatrixApiError as e:\n"
        f"        {unrun}\n        raise typer.Exit(1)",
    )
    with pytest.raises(AssertionError) as failed:
        test_every_arm_that_names_a_matrix_error_reports_it_through_the_reporter()
    assert "unlisted [('_new_arm', 'except'" in str(failed.value)
