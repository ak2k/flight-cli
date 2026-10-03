"""The skill, the README and the routing memory say what `flight search`
serves on Google Flights.

An agent picks flags and a backend from the skill: a flag it never names goes
unused, and a constraint it lists among Matrix's is one it expects only Matrix
to answer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = {
    "skill": _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md",
    "readme": _ROOT / "README.md",
}
_ROUTING_MEMORY = _ROOT / "docs" / "memories" / "gf_routing_and_carriers.md"


def _text(doc: str) -> str:
    return _DOCS[doc].read_text()


@pytest.mark.parametrize("doc", sorted(_DOCS))
@pytest.mark.parametrize("flag", ["--arrive-times", "--return-arrive-times", "--exclude-basic"])
def test_each_google_search_flag_is_named(doc: str, flag: str) -> None:
    assert re.search(re.escape(flag) + r"(?![\w-])", _text(doc)), f"{doc} never names {flag}"


@pytest.mark.parametrize("doc", sorted(_DOCS))
def test_a_departure_window_to_the_minute_is_shown(doc: str) -> None:
    assert re.search(r"--depart-times '?\d{1,2}:\d{2}-\d{1,2}:\d{2}", _text(doc))


def _skill_matrix_constraints() -> str:
    row = next(ln for ln in _text("skill").splitlines() if ln.startswith("| `flight search "))
    m = re.search(r"ITA Matrix when a constraint Google can't serve is set \(([^)]*)\)", row)
    assert m, "the search row names what goes to Matrix"
    return m.group(1)


def _readme_matrix_constraints() -> str:
    lines = _text("readme").splitlines()
    start = next(i for i, ln in enumerate(lines) if "What Google can't serve" in ln)
    block: list[str] = []
    for ln in lines[start:]:
        if not ln.startswith("#"):
            break
        block.append(ln)
    return " ".join(block)


# Any infant, not one on a multi-cabin compare, which does stay on Matrix.
@pytest.mark.parametrize("served", ["-REDEYES", "-OVERNIGHTS", r"\binfants\b"])
def test_no_doc_lists_what_google_serves_among_matrix_s(served: str) -> None:
    assert not re.search(served, _skill_matrix_constraints())
    assert not re.search(served, _readme_matrix_constraints())


def test_the_routing_memory_lists_the_night_checks_as_row_checks() -> None:
    text = _ROUTING_MEMORY.read_text()
    tier_2 = text[text.index("- **Tier 2") : text.index("- **Tier 3")]
    assert "-REDEYES" in tier_2
    assert "-OVERNIGHTS" in tier_2
