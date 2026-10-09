"""The envelope memory's out-of-scope list leaves out what the envelope now carries."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _section(heading: str) -> str:
    text = (_ROOT / "docs/memories/envelope.md").read_text()
    body = text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


def test_out_of_scope_does_not_list_google_facets() -> None:
    out_of_scope = _section("Out of scope")
    assert "facets" not in out_of_scope
    assert "ds:1[7]" not in out_of_scope
    assert "an exit code of its own for a partial answer (`complete` says it)." in out_of_scope
    assert "`ds:1[7]` holds what Google's filters offer" in _section("Route facets")
