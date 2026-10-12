"""No test sees an inherited `MATRIX_CACHE_DIR`.

`cache_dir()` reads `MATRIX_CACHE_DIR` before HOME, so a variable inherited from
the operator's shell would send a test that does not set it to the operator's
cache. The conftest autouse fixture removes it. The probe test is the inner
half: it runs only when the outer test launches it, in a fresh pytest process
whose environment carries the variable."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

_PROBE_ENV = "FLIGHT_CLI_ISOLATION_PROBE"


def _pytest(
    tmp_path: pathlib.Path, test: str, env: dict[str, str], *options: str
) -> subprocess.CompletedProcess[str]:
    # Without --basetemp the child's tmp dirs land under $TMPDIR, outside tmp_path.
    return subprocess.run(  # noqa: S603 — this interpreter running this file's own tests
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'child'}",
            *options,
            f"{__file__}::{test}",
        ],
        env=env,
        cwd=pathlib.Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_probe_sees_no_inherited_cache_dir() -> None:
    if not os.environ.get(_PROBE_ENV):
        pytest.skip(f"inner half; {_PROBE_ENV} is set by the outer test")
    # Asserting on os.environ itself would print every variable when this fails.
    inherited = os.environ.get("MATRIX_CACHE_DIR")
    assert inherited is None


def test_an_inherited_cache_dir_is_not_seen_by_a_test(tmp_path: pathlib.Path) -> None:
    env = {
        **os.environ,
        _PROBE_ENV: "1",
        "MATRIX_CACHE_DIR": str(tmp_path / "operator-cache"),
    }
    result = _pytest(tmp_path, "test_the_probe_sees_no_inherited_cache_dir", env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_the_inner_run_writes_no_temp_dir_outside_tmp_path(tmp_path: pathlib.Path) -> None:
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    # pytest takes its temp root from PYTEST_DEBUG_TEMPROOT when set, else from TMPDIR.
    env = {**os.environ, "TMPDIR": str(temp_root), "PYTEST_DEBUG_TEMPROOT": str(temp_root)}
    result = _pytest(tmp_path, "test_an_inherited_cache_dir_is_not_seen_by_a_test", env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert list(temp_root.iterdir()) == []


def test_a_failing_probe_prints_no_other_environment_variable(tmp_path: pathlib.Path) -> None:
    cache_dir = str(tmp_path / "operator-cache")
    sentinel = "flight-cli-isolation-sentinel"
    env = {
        **os.environ,
        _PROBE_ENV: "1",
        "MATRIX_CACHE_DIR": cache_dir,
        "FLIGHT_CLI_SENTINEL": sentinel,
    }
    # With no conftest nothing removes the variable, so the probe fails the way a
    # regressed fixture would make it fail; assertion reprs print untruncated.
    result = _pytest(
        tmp_path,
        "test_the_probe_sees_no_inherited_cache_dir",
        env,
        "--noconftest",
        "-o",
        "verbosity_assertions=2",
    )
    output = result.stdout + result.stderr
    # Booleans only: printing the output on failure would print what it leaked.
    failed = result.returncode == pytest.ExitCode.TESTS_FAILED
    assert (failed, cache_dir in output, sentinel in output) == (True, True, False)
