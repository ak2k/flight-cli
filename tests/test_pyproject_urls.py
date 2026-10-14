"""The package metadata links the repository and the changelog."""

from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_project_urls_name_the_repository_and_the_changelog() -> None:
    assert _PYPROJECT.is_file(), "pyproject.toml does not exist"
    with _PYPROJECT.open("rb") as f:
        project = tomllib.load(f)["project"]
    urls = project.get("urls", {})
    assert urls.get("Repository") == "https://github.com/ak2k/flight-cli"
    assert urls.get("Changelog") == "https://github.com/ak2k/flight-cli/blob/main/CHANGELOG.md"
