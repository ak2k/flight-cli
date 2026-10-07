"""Values the Google Flights transports share, and none of them owns.

A leaf, deliberately: it imports nothing from this package. `_gflight_ids` is
rung 1 and the parser; `_gf_browser` is rung 2. Rung 2 needs the record type the
parser reads and the cache dir its profile lives under, and rung 1 reaches rung
2 to run it — so with these defined in `_gflight_ids` the two modules import
each other, and only a deferred import inside a function hid it. Breaking that
cycle here is what lets `_gflight_ids` import `_gf_browser` at the top of the
file like any other module.

The transport vocabulary lives here for a second reason. `cli` validates
`--gf-transport` on EVERY search, Matrix-only ones included, while `_gflight_ids`
costs fli's import — measured at ~95 ms on top of an already-loaded `cli`. A leaf
that imports only the standard library is free, so the CLI can name the modes it
accepts, and derive them from the `Literal` itself, without dragging rung 1 onto
the Matrix path.

`_gf_errors` solves the same shape of problem for the refusal types and says so
in its own docstring. These are values rather than exceptions, and importing a
page-fetch record from a module named "errors" would mislead every later reader,
so they get a leaf named for what they are.
"""

from __future__ import annotations

import os
import pathlib
from typing import Literal, NamedTuple, get_args

# The Google Flights search page has two transports. `http` is the curl_cffi GET
# rung 1 has always used. `browser` drives a real Chrome to the same URL, which
# earns a far larger rate budget when Google throttles the thin client. `auto`
# is `http` until a throttle outlasts its ladder, then `browser` for the rest of
# the search.
type GfTransportMode = Literal["auto", "http", "browser"]

# The two modes anything compares against by name. `auto` is spelled in the
# `Literal` and in `VALID_TRANSPORT_MODES` only: what tells it apart is "not
# `http`" or "not `browser`", and the ladder dispatches on bare literals anyway,
# because a name in a `case` captures rather than compares. Annotated rather
# than bare, so a typo is a basedpyright error here instead of a mode the CLI
# offers and no rung answers.
TRANSPORT_HTTP: GfTransportMode = "http"
TRANSPORT_BROWSER: GfTransportMode = "browser"

# Derived, so the set the CLI accepts IS the type and cannot drift from it: a
# fourth mode is offered to users the moment it is added to `GfTransportMode`,
# and `_one_call_laddered`'s `assert_never` is what stops it shipping without a
# rung. A PEP 695 alias is evaluated lazily, so `get_args` has to be handed the
# alias's value — on the alias object itself it returns `()`.
VALID_TRANSPORT_MODES: tuple[GfTransportMode, ...] = get_args(GfTransportMode.__value__)


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
