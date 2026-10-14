"""A version tag, and only a version tag, publishes the declared version to PyPI."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "release.yml"
_CHANGELOG = _ROOT / "CHANGELOG.md"


def _workflow_text() -> str:
    assert _WORKFLOW.is_file(), ".github/workflows/release.yml does not exist"
    return _WORKFLOW.read_text()


def _declared_version() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    return str(project["version"])


def test_release_runs_on_a_version_tag_only() -> None:
    text = _workflow_text()
    assert 'on:\n  push:\n    tags: ["v*.*.*"]\n' in text
    for trigger in ("branches:", "pull_request", "workflow_dispatch", "schedule:"):
        assert trigger not in text, f"release.yml also triggers on {trigger}"


def test_release_publishes_through_trusted_publishing() -> None:
    text = _workflow_text()
    assert "pypa/gh-action-pypi-publish@" in text
    assert "id-token: write" in text
    assert "environment: pypi" in text
    # A password or token input would bypass the OIDC trusted publisher.
    assert not re.search(r"^\s*(password|user|token|api-token):", text, re.MULTILINE)


def test_release_refuses_a_tag_that_differs_from_the_declared_version() -> None:
    text = _workflow_text()
    assert "GITHUB_REF_NAME" in text
    assert "pyproject.toml" in text


def test_changelog_has_a_section_for_the_declared_version() -> None:
    assert _CHANGELOG.is_file(), "CHANGELOG.md does not exist"
    assert f"## [{_declared_version()}]" in _CHANGELOG.read_text()
