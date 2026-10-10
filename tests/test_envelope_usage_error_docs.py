"""`--format envelope` is written at exit 0 and 1; a usage error (exit 2) writes no document."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from flight_cli import cli

_ROOT = Path(__file__).resolve().parent.parent
_SAYS = "a usage error (exit 2) writes no document"
_BULLET_DOCS = [
    _ROOT / "README.md",
    _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md",
]


def _flat(text: str) -> str:
    return " ".join(text.replace("│", " ").split())


def test_the_format_help_says_a_usage_error_writes_no_document() -> None:
    for command in ("search", "calendar"):
        result = CliRunner().invoke(cli.app, [command, "--help"], env={"COLUMNS": "200"})
        flat = _flat(result.stdout)

        assert _SAYS in flat, (command, flat)
        assert "document on every path" not in flat, (command, flat)


@pytest.mark.parametrize("doc", _BULLET_DOCS, ids=lambda p: p.name)
def test_the_envelope_bullet_says_a_usage_error_writes_no_document(doc: Path) -> None:
    (bullet,) = (
        ln for ln in doc.read_text().splitlines() if ln.startswith("- `--format envelope`")
    )

    assert _SAYS in bullet, bullet
    assert "same keys on every path" not in bullet, bullet


def test_the_envelope_note_title_names_the_exit_codes_that_write_one() -> None:
    title = (_ROOT / "docs" / "memories" / "envelope.md").read_text().splitlines()[0]

    assert "at exit 0 and 1" in title, title
    assert "on every path" not in title, title
