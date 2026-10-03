"""The search-filter test modules take today's date when a test runs, never
when pytest imports them, so a run that crosses midnight or a test that sets
the clock sees the date the code under test sees."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_MODULES = (
    "test_exclude_basic.py",
    "test_gf_infants.py",
    "test_gf_night_checks.py",
    "test_search_windows.py",
)


def _run_at_import(tree: ast.Module) -> list[ast.AST]:
    """What a module evaluates when it is imported: every statement but a
    function's body, which includes decorators and default values."""
    out: list[ast.AST] = []
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out += [*node.decorator_list, *node.args.defaults]
            out += [d for d in node.args.kw_defaults if d is not None]
        elif isinstance(node, ast.ClassDef):
            out += [*node.decorator_list, *node.bases]
            pending += node.body
        else:
            out.append(node)
    return out


@pytest.mark.parametrize("module", _MODULES)
def test_no_date_is_taken_at_import(module: str) -> None:
    tree = ast.parse((_TESTS / module).read_text())
    at_import = [
        call.lineno
        for node in _run_at_import(tree)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in {"today", "now"}
    ]
    assert at_import == [], f"{module} takes the date at import, line {at_import}"
