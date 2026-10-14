"""A version tag, and only a version tag, publishes the declared version to PyPI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "release.yml"
_CHANGELOG = _ROOT / "CHANGELOG.md"
_CHECK_STEP = "      - name: Check the tag matches the declared version\n        run: |\n"


def _workflow_text() -> str:
    assert _WORKFLOW.is_file(), ".github/workflows/release.yml does not exist"
    return _WORKFLOW.read_text()


def _declared_version() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    return str(project["version"])


def _sections() -> tuple[str, str, str]:
    """The workflow split into header (before jobs), build job and publish job."""
    text = _workflow_text()
    assert "\njobs:\n" in text, "release.yml has no top-level jobs: block"
    header, jobs = text.split("\njobs:\n", 1)
    assert "\n  publish:\n" in jobs, "release.yml has no publish job"
    build, publish = jobs.split("\n  publish:\n", 1)
    return header, build, publish


def _check_step_script() -> str:
    text = _workflow_text()
    assert _CHECK_STEP in text, "release.yml has no tag-equals-version check step"
    body: list[str] = []
    for line in text.split(_CHECK_STEP, 1)[1].splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    script = textwrap.dedent("\n".join(body)).strip()
    assert script, "the tag-equals-version check step runs nothing"
    return script


def _run_check(tmp_path: Path, ref_name: str) -> int:
    bash = shutil.which("bash")
    assert bash, "bash is not on PATH"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "flight-cli"\nversion = "{_declared_version()}"\n'
    )
    # The interpreter running the tests has tomllib; a system python3 may not.
    path = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))
    result = subprocess.run(  # noqa: S603 — the workflow's own step, run against a temp pyproject
        [bash, "-e", "-c", _check_step_script()],
        cwd=tmp_path,
        env={"PATH": path, "GITHUB_REF_NAME": ref_name},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


def test_release_runs_on_a_version_tag_only() -> None:
    header, _, _ = _sections()
    assert 'on:\n  push:\n    tags: ["v*.*.*"]\n' in header
    for trigger in ("branches:", "pull_request", "workflow_dispatch", "schedule:"):
        assert trigger not in header, f"release.yml also triggers on {trigger}"


def test_release_publishes_through_trusted_publishing() -> None:
    header, build, publish = _sections()
    for name, block in (("workflow header", header), ("build job", build)):
        assert "id-token" not in block, f"the {name} grants id-token"
        assert "continue-on-error" not in block, f"the {name} tolerates a failed step"
    assert "continue-on-error" not in publish
    assert re.search(r"^    needs: build$", publish, re.MULTILINE)
    assert re.search(r"^    environment: pypi$", publish, re.MULTILINE)
    assert re.search(r"^      id-token: write$", publish, re.MULTILINE)
    assert re.search(r"^        uses: pypa/gh-action-pypi-publish@", publish, re.MULTILINE)
    # A password or token input would bypass the OIDC trusted publisher.
    assert not re.search(r"^\s*(password|user|token|api-token):", publish, re.MULTILINE)


def test_release_refuses_a_tag_that_differs_from_the_declared_version(tmp_path: Path) -> None:
    major, minor, patch = _declared_version().split(".")
    assert _run_check(tmp_path, f"v{major}.{minor}.{patch}") == 0
    assert _run_check(tmp_path, f"v{major}.{minor}.{int(patch) + 1}") != 0


def test_changelog_has_a_section_for_the_declared_version() -> None:
    assert _CHANGELOG.is_file(), "CHANGELOG.md does not exist"
    assert f"## [{_declared_version()}]" in _CHANGELOG.read_text()
