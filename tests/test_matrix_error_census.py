"""Every arm in `cli.py` that fails a command on a `MatrixApiError` reports it
through `_print_matrix_error`, so one backend error reads the same whichever
command asked for it.

Parsed from the module, so a new arm that names `MatrixApiError` and prints it
some other way fails here until it reports through the reporter or is added to
`_SOFT` with its reason. A list of callers in the reporter's docstring rots on
the next edit and says nothing when it does; this does."""

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
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == _REPORTER
        for stmt in body
        for n in ast.walk(stmt)
    )


def _arms(node: ast.AST, fn: str, found: list[tuple[str, str, bool]]) -> None:
    """Each handler or `isinstance` branch that names the error, as
    (enclosing function, kind, whether its body calls the reporter)."""
    for child in ast.iter_child_nodes(node):
        inside = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
        if isinstance(child, ast.ExceptHandler) and child.type and _names_error(child.type):
            found.append((fn, "except", _calls_reporter(child.body)))
        elif (
            isinstance(child, ast.If)
            and isinstance(child.test, ast.Call)
            and isinstance(child.test.func, ast.Name)
            and child.test.func.id == "isinstance"
            and _names_error(child.test)
        ):
            found.append((fn, "isinstance", _calls_reporter(child.body)))
        _arms(child, inside, found)


def test_every_arm_that_names_a_matrix_error_reports_it_through_the_reporter() -> None:
    found: list[tuple[str, str, bool]] = []
    _arms(_cli_tree(), "<module>", found)
    reporting = [(fn, kind) for fn, kind, said in found if said]
    # Counted, not collected into a set: a second silent arm in a function
    # `_SOFT` already lists is a new arm, not the listed one again.
    silent = Counter((fn, kind) for fn, kind, said in found if not said)
    listed = Counter(_SOFT.keys())
    assert len(reporting) >= 1, found
    assert silent == listed, (
        f"arms naming MatrixApiError that do not call {_REPORTER}: "
        f"unlisted {sorted((silent - listed).elements())}, "
        f"stale {sorted((listed - silent).elements())}"
    )


def _census_with(monkeypatch: pytest.MonkeyPatch, arm: str, into: str | None = None) -> None:
    """Points the census at cli.py with `arm` put first in the def named `into`
    that already names the error, or in the module when `into` is None."""
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
def test_the_census_fails_on_a_second_silent_arm_in_a_soft_function(
    monkeypatch: pytest.MonkeyPatch, into: str, arm: str
) -> None:
    _census_with(monkeypatch, arm, into)
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
