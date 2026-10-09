# pyright: reportPrivateUsage=false
"""`ALLIANCE` takes its names the way Matrix documents them: separated by spaces.
The `|` spelling stays accepted."""

from __future__ import annotations

from pathlib import Path

import pytest

from flight_cli.cli import BACKEND_AUTO, BACKEND_GFLIGHT, _pick_backend
from flight_cli.routing_predicates import AlliancePred, Tier, UnsupportedPred, parse_extension

_ROOT = Path(__file__).resolve().parent.parent
_ALLIANCE_ROW_DOCS = [
    _ROOT / ".claude" / "skills" / "flight-search" / "SKILL.md",
    _ROOT / "docs" / "memories" / "extension_codes.md",
]


@pytest.mark.parametrize(
    ("directive", "names"),
    [
        ("ALLIANCE star-alliance oneworld", {"star-alliance", "oneworld"}),
        ("alliance Oneworld  SkyTeam", {"oneworld", "skyteam"}),
        ("ALLIANCE oneworld | skyteam", {"oneworld", "skyteam"}),
        ("ALLIANCE oneworld|skyteam star-alliance", {"oneworld", "skyteam", "star-alliance"}),
    ],
)
def test_alliance_names_separated_by_spaces_form_one_native_filter(
    directive: str, names: set[str]
) -> None:
    (pred,) = parse_extension(directive)
    assert pred == AlliancePred(codes=frozenset(names))
    assert pred.tier is Tier.GF_NATIVE


def test_a_space_separated_list_naming_an_unknown_alliance_still_escalates() -> None:
    directive = "ALLIANCE oneworld galactic"
    (pred,) = parse_extension(directive)
    assert pred == UnsupportedPred(token=directive, reason=f"unknown alliance in {directive!r}")


def test_auto_serves_a_space_separated_alliance_list_on_google(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _pick_backend(
        backend=BACKEND_AUTO,
        routing=None,
        extension="ALLIANCE oneworld skyteam",
        slice_specs=None,
        depart_times=None,
        return_times=None,
        stops=None,
        children=0,
        seniors=0,
        youth=0,
        inf_seat=0,
        inf_lap=0,
        origin="JFK",
        destination="LHR",
        allow_airport_changes=True,
        show_only_available=True,
    )
    assert backend == BACKEND_GFLIGHT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("doc", _ALLIANCE_ROW_DOCS, ids=lambda p: p.name)
def test_the_alliance_code_row_says_names_are_separated_by_spaces(doc: Path) -> None:
    (row,) = (ln for ln in doc.read_text().splitlines() if ln.startswith("| `ALLIANCE"))
    assert "separated by spaces" in row
