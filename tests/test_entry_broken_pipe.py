"""A reader that hung up does not change the exit code a command chose.

CPython's final stdout flush fails on a pipe nobody reads and forces exit 120, a
code this CLI never uses. The test runs the entry point in a child process whose
stdout is a pipe with no reader: the child's `os.write` path is the real one, which
no in-process runner reproduces."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_CHILD = """
import sys
import typer
from flight_cli import cli

@cli.app.command("spew")
def spew() -> None:
    sys.stdout.write("x\\n")
    raise typer.Exit(2)

sys.argv = ["flight", "spew"]
cli.app()
"""


def test_a_closed_pipe_keeps_the_exit_code_the_command_chose(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "MATRIX_CACHE_DIR": str(home / "matrix"),
        "FLIGHT_CLI_CONFIG_DIR": str(home / "flight-config"),
    }
    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader is gone before the child writes
    try:
        done = subprocess.run(  # noqa: S603 — this interpreter running a literal child
            [sys.executable, "-c", _CHILD],
            env=env,
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        os.close(write_end)
    assert done.returncode == 2, done.stderr
    assert "BrokenPipeError" not in done.stderr, done.stderr


def test_the_memo_says_the_entry_point_settles_a_closed_pipe() -> None:
    root = pathlib.Path(__file__).parent.parent
    text = (root / "docs/memories/wire_format_quirks.md").read_text()
    memo = " ".join(text.split())
    assert "the `flight` entry point (`_FlightApp` in cli.py) flushes stdout itself" in memo
    assert "ends at exit 120" not in memo
