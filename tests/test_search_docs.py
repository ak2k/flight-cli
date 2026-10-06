"""The skill, the README and the routing memory say what `flight search`
serves on Google Flights.

An agent picks flags and a backend from the skill: a flag it never names goes
unused, and a constraint it lists among Matrix's is one it expects only Matrix
to answer."""

from __future__ import annotations

import re
import shlex
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from flight_cli import cli

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _sentence(memory: str, anchor: str) -> str:
    """The sentence of `docs/memories/<memory>` that names `anchor`."""
    text = " ".join((_ROUTING_MEMORY.parent / memory).read_text().split())
    at = text.index(anchor)
    return text[text.rfind(". ", 0, at) + 2 : text.find(". ", at)]


@pytest.mark.parametrize(
    ("memory", "anchor", "says"),
    [
        pytest.param(
            "gf_throttle_ladder.md",
            "`cli._open_jaw_tickets`",
            ("one one-way per slice", "--backend gflight"),
            id="escalation",
        ),
        pytest.param(
            "gf_browser_rung.md", "`cli._one_way_boards`", ("multi-city",), id="row-cap-line"
        ),
    ],
)
def test_the_memories_read_the_one_ways_of_every_multi_city_search(
    memory: str, anchor: str, says: tuple[str, ...]
) -> None:
    """Every multi-city search, an open jaw among them, reads one one-way per
    slice, and under --backend gflight Matrix is asked nothing beside them."""
    sentence = _sentence(memory, anchor)
    for words in says:
        assert words in sentence, sentence


def _skill_example(n: int) -> list[str]:
    """Example `n`'s command in the skill, as the arguments after `flight`."""
    m = re.search(rf"### Example {n}:.*?```bash\n(.*?)```", _text("skill"), re.DOTALL)
    assert m, f"the skill has no Example {n} command"
    argv = shlex.split(m.group(1).replace("\\\n", " "))
    assert argv[0] == "flight"
    return argv[1:]


def test_skill_example_2_runs_on_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red at the base, which sent Example 2's `+CABIN 2` to Matrix although it
    names the cabin `--cabin` asks for."""
    argv = _skill_example(2)
    for flag, days in (("--dep", 45), ("--return", 52)):
        argv[argv.index(flag) + 1] = (date.today() + timedelta(days=days)).isoformat()
    called: list[str] = []

    def _stub(name: str) -> Callable[..., None]:
        def _path(**_kw: object) -> None:
            called.append(name)

        return _path

    for name in ("_run_gflight_path", "_run_enriched_path", "_run_matrix_path"):
        monkeypatch.setattr(cli, name, _stub(name))
    result = CliRunner().invoke(cli.app, [*argv, "--cash-only"])
    assert result.exit_code == 0, result.output
    assert called == ["_run_enriched_path"], result.stderr
    assert "Using Matrix" not in result.stderr
