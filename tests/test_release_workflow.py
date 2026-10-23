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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "release.yml"
_CHANGELOG = _ROOT / "CHANGELOG.md"
_CHECK_STEP = "      - name: Check the tag matches the declared version"


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


def _build_job() -> tuple[str, str]:
    """The build job split into its own keys (before steps:) and its steps."""
    _, build, _ = _sections()
    assert "\n    steps:\n" in build, "the build job has no steps"
    job_keys, steps = build.split("\n    steps:\n", 1)
    return job_keys, steps


def _check_step() -> list[str]:
    """The lines of the check step, from its name: to the next step or the end of the job."""
    lines = _build_job()[1].splitlines()
    assert _CHECK_STEP in lines, "release.yml has no tag-equals-version check step"
    start = lines.index(_CHECK_STEP)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("      - ")),
        len(lines),
    )
    return lines[start:end]


def _check_step_script() -> str:
    # An allowlist, since any other step key in any spelling (if, shell, env,
    # working-directory, continue-on-error) can skip or neuter the script.
    step = _check_step()
    assert step[1:2] == ["        run: |"], f"the check step's second line is {step[1:2]}"
    body = step[2:]
    for line in body:
        assert not line.strip() or line.startswith(" " * 10), f"check step key: {line!r}"
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
    # Allowlists: a workflow default, a job key or a step key in any spelling
    # can skip or neuter the check and let a mismatched tag publish.
    header, _, _ = _sections()
    for line in header.splitlines():
        top = line.strip() and not line.startswith(" ")
        assert not top or re.match(r"(name|on|permissions):", line), f"workflow key: {line!r}"
    job_keys, _ = _build_job()
    job_lines = job_keys.splitlines()
    assert job_lines[:1] == ["  build:"], f"the build job opens with {job_lines[:1]}"
    for line in job_lines[1:]:
        assert re.fullmatch(r"    (runs-on|timeout-minutes): [\w.-]+", line), f"job key: {line!r}"
    major, minor, patch = _declared_version().split(".")
    assert _run_check(tmp_path, f"v{major}.{minor}.{patch}") == 0
    assert _run_check(tmp_path, f"v{major}.{minor}.{int(patch) + 1}") != 0


def test_changelog_has_a_section_for_the_declared_version() -> None:
    assert _CHANGELOG.is_file(), "CHANGELOG.md does not exist"
    assert f"## [{_declared_version()}]" in _CHANGELOG.read_text()


_EXTRA_JOB = """
  extra:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
"""
_PLAIN_JOB = """
  extra:
    runs-on: ubuntu-latest
    steps:
      - run: echo
"""
_PYPA_STEP = "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
_PUBLISH = "\n  publish:\n"


def test_release_tests_refuse_a_publishing_path_outside_the_pypi_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _workflow_text()
    mutations = {
        "job after publish": original.rstrip("\n") + "\n" + _EXTRA_JOB,
        "job between build and publish": original.replace(_PUBLISH, _EXTRA_JOB + _PUBLISH),
        "step in build": original.replace("\n" + _PUBLISH, "\n" + _PYPA_STEP + _PUBLISH),
        "id-token in another job": original.rstrip("\n")
        + "\n"
        + _PLAIN_JOB.replace("    steps:", "    permissions:\n      id-token: write\n    steps:"),
        "another job in environment pypi": original.rstrip("\n")
        + "\n"
        + _PLAIN_JOB.replace("    steps:", "    environment: pypi\n    steps:"),
        "inline password": original.replace(
            "uses: pypa/gh-action-pypi-publish@release/v1",
            "uses: pypa/gh-action-pypi-publish@release/v1\n        with: {password: x}",
        ),
    }
    unrefused: list[str] = []
    for label, text in mutations.items():
        assert text != original, f"mutation {label!r} changed nothing"
        mutated = tmp_path / (label.replace(" ", "-") + ".yml")
        mutated.write_text(text)
        monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW", mutated)
        try:
            test_release_publishes_through_trusted_publishing()
        except AssertionError:
            continue
        unrefused.append(label)
    monkeypatch.undo()
    test_release_publishes_through_trusted_publishing()
    assert not unrefused, f"the publish test accepts: {unrefused}"
