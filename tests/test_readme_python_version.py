"""The README states the Python floor pyproject.toml declares."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _declared_floor() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    spec = str(project["requires-python"])
    m = re.fullmatch(r">=\s*(\d+\.\d+)", spec)
    assert m, f"requires-python is {spec!r}, not a single >= floor"
    return m.group(1)


def test_readme_states_the_declared_python_floor() -> None:
    floor = _declared_floor()
    assert f"Requires Python {floor}+." in (_ROOT / "README.md").read_text()
