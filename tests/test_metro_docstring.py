"""The `_metro` docstring names every module that expands metro codes.

A reader of `_metro` learns from its docstring where a metro code turns into
member airports and what Matrix is sent. Parsed from the package so a new
importer of `_metro` fails here until the docstring names it. A module in a
subpackage is named by its dotted path (`pp.links`), so it never passes for a
top-level module with the same file name."""

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
    root: Path = Path(flight_cli.__file__).parent
    names: set[str] = set()
    for path in root.rglob("*.py"):
        name: str = ".".join(path.relative_to(root).with_suffix("").parts)
        if name == "_metro":
            continue
        tree: ast.Module = ast.parse(path.read_text())
        if any(_imports_metro(node) for node in ast.walk(tree)):
            names.add(name)
    return names


def test_the_docstring_names_every_module_that_expands_metro_codes() -> None:
    names = _expanding_modules()
    assert "_calendar_split" in names, sorted(names)
    doc: str = _metro.__doc__ or ""
    missing = sorted(name for name in names if f"`{name}`" not in doc)
    assert not missing, f"_metro docstring does not name {missing}"
    assert "calendar" in doc


def _flat_metro_doc() -> str:
    return " ".join((_metro.__doc__ or "").split())


def test_the_docstring_states_what_matrix_is_sent_and_what_cli_does_with_members() -> None:
    """Two claims a reader acts on, each in its own sentence: Matrix gets the
    user's metro token except in the per-pair queries `_calendar_split` builds,
    and `cli` expands members for the award providers as well as counting them."""
    flat: str = _flat_metro_doc()
    matrix = (
        "Matrix is sent the user's metro token as typed, except in the "
        "per-pair queries `_calendar_split` builds."
    )
    award = "`cli` also asks the award providers one query per member-airport pair."
    assert matrix in flat, flat
    assert award in flat, flat
    for sentence in flat.split(". "):
        assert "always receives" not in sentence, sentence
        assert "only counts" not in sentence, sentence


def test_the_airport_sets_test_docstring_names_the_award_and_calendar_expansions() -> None:
    """The `test_gf_airport_sets` docstring names every place a metro code
    expands: the Google sites, the award providers and the calendar split."""
    path: Path = Path(__file__).parent / "test_gf_airport_sets.py"
    doc: str = ast.get_docstring(ast.parse(path.read_text())) or ""
    flat: str = " ".join(doc.split())
    assert "sites only" not in flat, flat
    assert "`_build_pp_legs`" in flat, flat
    assert "`_calendar_split`" in flat, flat
