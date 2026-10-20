"""The visible-rows module docstring says which rows a multi-cabin document carries."""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _module_doc() -> str:
    tree = ast.parse((_TESTS / "test_gflight_visible_rows.py").read_text())
    return " ".join((ast.get_docstring(tree) or "").split())


def test_the_module_docstring_says_a_cabins_document_carries_its_cheapest_rows() -> None:
    doc = _module_doc()
    assert "holds the `-n` cheapest of that cabin's own board, then," in doc
    assert "first `-n` of that cabin's own board" not in doc
