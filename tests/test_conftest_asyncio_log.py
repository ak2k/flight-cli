"""The suite resets the `asyncio` logger level that `stop_driver` raises.

`stop_driver` sets that logger to CRITICAL for the rest of the process. The
conftest resets the module's other process-wide latches before each test but, if
it left this one, a test that ran after any stop would miss an asyncio warning
that `filterwarnings = error` would otherwise turn into a failure. The check runs
a nested pytest over a copy of this suite's conftest, so the stop and the test
that follows it share a process without touching this one."""

from __future__ import annotations

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


def test_the_asyncio_logger_level_is_back_for_the_test_after_a_stop(
    tmp_path: pathlib.Path,
) -> None:
    conftest = pathlib.Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    (tmp_path / "test_1.py").write_text(_STOPS_A_DRIVER, encoding="utf-8")
    (tmp_path / "test_2.py").write_text(_READS_THE_LEVEL, encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pytest over files written above
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=short",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 passed" in done.stdout
