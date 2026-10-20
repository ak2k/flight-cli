"""The first-paint guard's docstring says why the guard exists."""

from __future__ import annotations

import ast
from pathlib import Path

_CLI = Path(__file__).resolve().parent.parent / "src" / "flight_cli" / "cli.py"


def _docstring(name: str) -> str:
    tree = ast.parse(_CLI.read_text(), filename=str(_CLI))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return " ".join((ast.get_docstring(node) or "").split())
    raise AssertionError("no function named " + name)


def test_paint_guard_docstring_names_the_task_group_as_the_reason() -> None:
    doc = _docstring("_paint_first_gf_table")
    assert (
        "The guard exists because this paint runs inside the task group, "
        "not because renderers are untrusted." in doc
    )
