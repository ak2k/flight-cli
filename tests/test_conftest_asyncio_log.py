"""The suite resets the `asyncio` logger level that `stop_driver` raises.

`stop_driver` sets that logger to CRITICAL for the rest of the process. The
conftest resets the module's other process-wide latches before each test but, if
it left this one, a test that ran after any stop would miss an asyncio warning
that `filterwarnings = error` would otherwise turn into a failure. The check runs
a nested pytest over a copy of this suite's conftest, so the stop and the test
that follows it share a process without touching this one."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_STOPS_A_DRIVER = """
from flight_cli import _gf_browser as gfb


def test_a_stop_raises_the_asyncio_logger() -> None:
    gfb.GfBrowserSession(headed=False).stop_driver()
"""

_READS_THE_LEVEL = """
import logging


def test_b_the_next_test_sees_the_default_level() -> None:
    assert logging.getLogger("asyncio").level == logging.NOTSET
"""


def _pytest(
    tmp_path: pathlib.Path, target: str, cwd: pathlib.Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # Without --basetemp the child's tmp dirs land under $TMPDIR, outside tmp_path.
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pytest over this suite's files
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=short",
            f"--basetemp={tmp_path / 'child'}",
            target,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_asyncio_logger_level_is_back_for_the_test_after_a_stop(
    tmp_path: pathlib.Path,
) -> None:
    conftest = pathlib.Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    (tmp_path / "test_1.py").write_text(_STOPS_A_DRIVER, encoding="utf-8")
    (tmp_path / "test_2.py").write_text(_READS_THE_LEVEL, encoding="utf-8")
    done = _pytest(tmp_path, str(tmp_path), tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 passed" in done.stdout


def test_the_nested_run_writes_no_temp_dir_outside_tmp_path(tmp_path: pathlib.Path) -> None:
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    # pytest takes its temp root from PYTEST_DEBUG_TEMPROOT when set, else from TMPDIR.
    env = {**os.environ, "TMPDIR": str(temp_root), "PYTEST_DEBUG_TEMPROOT": str(temp_root)}
    done = _pytest(
        tmp_path,
        f"{__file__}::test_the_asyncio_logger_level_is_back_for_the_test_after_a_stop",
        pathlib.Path(__file__).parent.parent,
        env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert list(temp_root.iterdir()) == []
