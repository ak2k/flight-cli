"""The `_metro` docstring names every module that expands metro codes.

A reader of `_metro` learns from its docstring where a metro code turns into
member airports and what Matrix is sent. Parsed from the package so a new
importer of `_metro` fails here until the docstring names it."""

from __future__ import annotations

import ast
from pathlib import Path

import flight_cli
from flight_cli import _metro

_METRO_MODULES: frozenset[str] = frozenset({"_metro", "flight_cli._metro"})


def _imports_metro(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name in _METRO_MODULES for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        module: str = node.module or ""
        return (
            module in _METRO_MODULES
            or module.endswith("._metro")
            or any(alias.name == "_metro" for alias in node.names)
        )
    return False


def _expanding_modules() -> set[str]:
    stems: set[str] = set()
    for path in Path(flight_cli.__file__).parent.rglob("*.py"):
        if path.stem == "_metro":
            continue
        tree: ast.Module = ast.parse(path.read_text())
        if any(_imports_metro(node) for node in ast.walk(tree)):
            stems.add(path.stem)
    return stems


def test_the_docstring_names_every_module_that_expands_metro_codes() -> None:
    stems = _expanding_modules()
    assert "_calendar_split" in stems, sorted(stems)
    doc: str = _metro.__doc__ or ""
    missing = sorted(stem for stem in stems if f"`{stem}`" not in doc)
    assert not missing, f"_metro docstring does not name {missing}"
    assert "calendar" in doc
