"""The `test_gf_second_ctrl_c` fixture docstring names where an ignored SIGINT comes from.

No test leaves SIGINT ignored for a later one: a pytest process that was
launched in the background starts with it ignored, and the autouse fixture
resets it for that case. Parsed from the file, so a reworded docstring that
blames an earlier test fails here."""

from __future__ import annotations

import ast
from pathlib import Path


def test_the_sigint_fixture_docstring_blames_the_launch_not_an_earlier_test() -> None:
    path: Path = Path(__file__).parent / "test_gf_second_ctrl_c.py"
    fixtures = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "_restore_sigint"
    ]
    assert len(fixtures) == 1, fixtures
    flat: str = " ".join((ast.get_docstring(fixtures[0]) or "").split())
    assert "earlier test" not in flat, flat
    assert "may start with SIGINT ignored" in flat, flat
    assert "background launch" in flat, flat
