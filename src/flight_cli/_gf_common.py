"""Values both Google Flights transport rungs need, and neither owns.

A leaf, deliberately: it imports nothing from this package. `_gflight_ids` is
rung 1 and the parser; `_gf_browser` is rung 2. Rung 2 needs the record type the
parser reads and the cache dir its profile lives under, and rung 1 reaches rung
2 to run it — so with these defined in `_gflight_ids` the two modules import
each other, and only a deferred import inside a function hid it.

That deferred import is still there, but it now means one thing instead of two.
It exists because patchright is an optional dependency that must stay off the
http path. It is no longer also load-bearing for import order, where breaking it
by "tidying" the import to the top of the file would have raised ImportError on
a module that had not finished initializing — the failure would have looked like
a broken optional dependency and sent the reader to the wrong place entirely.

`_gf_errors` solves the same shape of problem for the refusal types and says so
in its own docstring. These are values rather than exceptions, and importing a
page-fetch record from a module named "errors" would mislead every later reader,
so they get a leaf named for what they are.
"""

from __future__ import annotations

import os
import pathlib
from typing import NamedTuple


class PageFetch(NamedTuple):
    """One fetch of the search page, whichever rung made it — and the whole of
    the evidence `_rows_from_page_html` rules on, so the parser cannot tell the
    rungs apart."""

    html: str
    final_url: str
    status_code: int


def cache_dir() -> pathlib.Path:
    """The shared CLI cache dir, honoring the same `MATRIX_CACHE_DIR` override
    the response cache does. Rung 2's browser profile resolves from here too, so
    a test that redirects the cache redirects both."""
    return pathlib.Path(
        os.environ.get("MATRIX_CACHE_DIR") or pathlib.Path.home() / ".cache" / "flight-cli"
    )
