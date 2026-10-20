"""The `_gf_dategrid` docstring gives the reason its <=61-day chunking
still exists.

fli fixed the >61-day filter drop in 0.9.0, and the package requires
`flights>=0.9`, so a docstring that still calls the chunking a dodge around that
bug tells the next reader the loop is load-bearing when it is redundant.
"""

from __future__ import annotations

from importlib.metadata import version

from flight_cli import _gf_dategrid


def _doc() -> str:
    return " ".join((_gf_dategrid.__doc__ or "").split())


def test_the_installed_fli_has_the_chunking_fix() -> None:
    """Green at the base: the premise of the docstring change."""
    major, minor = (int(part) for part in version("flights").split(".")[:2])
    assert (major, minor) >= (0, 9)


def test_the_docstring_does_not_sell_the_chunking_as_dodging_a_fli_bug() -> None:
    doc = _doc()
    assert "dodging the fli" not in doc
    assert "fixed" in doc
    assert "0.9.0" in doc
