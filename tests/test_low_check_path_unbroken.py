# pyright: reportPrivateUsage=false
"""The path a shape-change failure asks the user to attach is printed whole on
the low-check line, so a copied path exists.

`console` wraps a line at its width, and a path has no space to wrap at, so a
wrapped line cuts the path in the middle. The failure lines on `err` keep their
text whole already (tests/test_matrix_shape_path_unbroken.py); this is the one
that prints through `console`."""

from __future__ import annotations

import io
import pathlib
from typing import TYPE_CHECKING

from rich.console import Console

from flight_cli import _verify, cli
from flight_cli.client import MatrixShapeError
from test_verify import _as_row

if TYPE_CHECKING:
    import pytest

# Longer than the console below, and with nothing to wrap at.
_KEPT = pathlib.Path("/kept") / ("a" * 120) / "matrix-x.json"


def test_the_capture_path_on_the_low_check_line_is_unbroken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=80, emoji=False))
    n, fli_row = _as_row()
    row = _verify.google_row(fli_row)
    reason = cli._low_check_failure(MatrixShapeError("bad", captured=_KEPT))

    cli._print_low_check(cli._LowCheck(n, row, "USD100.00", None, "no-answer", reason=reason))

    assert str(_KEPT) in buf.getvalue()
