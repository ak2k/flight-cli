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


def test_the_probe_sees_no_inherited_cache_dir() -> None:
    if not os.environ.get(_PROBE_ENV):
        pytest.skip(f"inner half; {_PROBE_ENV} is set by the outer test")
    assert "MATRIX_CACHE_DIR" not in os.environ


def test_an_inherited_cache_dir_is_not_seen_by_a_test(tmp_path: pathlib.Path) -> None:
    env = {
        **os.environ,
        _PROBE_ENV: "1",
        "MATRIX_CACHE_DIR": str(tmp_path / "operator-cache"),
    }
    result = subprocess.run(  # noqa: S603 — this interpreter and this file's own probe test
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{__file__}::test_the_probe_sees_no_inherited_cache_dir",
        ],
        env=env,
        cwd=pathlib.Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
