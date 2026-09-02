# pyright: reportPrivateUsage=false
"""Refusal text is rendered as rich markup, and its content is not ours.

Two sources feed square brackets into these strings. Google's payload reaches
the `GfPageShapeError` message through fli's decoders, which raise with a code
taken verbatim from the row (`fli/search/_decoders.py`). And the user's own
`--routing` / `--extension` string is quoted back at them in the reason the
backend picker prints. Either one containing `[/x]` ended the command in a
`MarkupError` traceback instead of the refusal it was trying to explain.

Escaping happens at the RENDER site, never where the text is built, and that is
load-bearing rather than a style choice: the same reason string goes out two
ways. `_pick_backend` prints it through a rich Console on the auto path, and
raises it inside a `typer.BadParameter` on the explicit path — which typer
renders as plain `Text`, no markup. A reason escaped at its source satisfies the
first and shows the user a literal backslash in the second. Both arms are
asserted below, on the same input, for exactly that reason.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
import typer
from rich.console import Console

from flight_cli._gf_errors import (
    GfConsentError,
    GfPageShapeError,
    GfTfsUnsupportedError,
    GfThrottledError,
)
from flight_cli.cli import (
    BACKEND_AUTO,
    BACKEND_GFLIGHT,
    BACKEND_MATRIX,
    _gf_refusal,
    _pick_backend,
)

if TYPE_CHECKING:
    from flight_cli import cli as cli_mod

# A closing tag with no opener: rich's parser raises on it rather than ignoring
# it, which is what turns a payload string into a crash.
_HOSTILE = "[/x]"

# Routing and extension strings a user can type that rich would read as markup.
# Nothing validates these before they are quoted back — an unparseable routing
# IS the message, so the hostile value always reaches the renderer.
_HOSTILE_ROUTING = [
    pytest.param("BA[/weird]AA", id="unopened-closing-tag"),
    pytest.param("[/]", id="bare-close"),
    pytest.param("[bold]LH", id="unclosed-opening-tag"),
    pytest.param("AA [link=file:///etc/passwd]BA", id="tag-with-a-parameter"),
    pytest.param(r"BA\[/x]AA", id="a-backslash-the-user-typed"),
    pytest.param("LH [#00ff00]GREEN", id="colour-tag"),
]
_HOSTILE_EXTENSION = [
    pytest.param("MAXDUR [/x]", id="unopened-closing-tag"),
    pytest.param("ALLIANCE [bold]", id="unclosed-opening-tag"),
    pytest.param("NOTAKEYWORD [/]", id="bare-close"),
]


def _as_quoted(value: str) -> str:
    """How `value` appears in a reason, which is built with `!r`.

    repr doubles a backslash the user typed, and that is repr's job rather than
    a markup question. Comparing against it keeps the backslash case honest and
    still catches a rich escape, which would add one more backslash on top."""
    return repr(value)[1:-1]


def _render(markup: str) -> str:
    """Render through a real Console, which is where MarkupError comes from —
    a plain string comparison would pass on text that cannot be printed."""
    buf = io.StringIO()
    Console(file=buf, width=400, no_color=True, highlight=False).print(markup)
    return buf.getvalue()


def _pick(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: str = BACKEND_AUTO,
    routing: str | None = None,
    extension: str | None = None,
) -> tuple[str, str]:
    """Run the real picker with its stderr Console captured. No network: the
    picker only classifies, it never dispatches a backend."""
    from flight_cli import cli

    buf = io.StringIO()
    captured: cli_mod.Console = Console(file=buf, width=400, no_color=True, highlight=False)
    monkeypatch.setattr(cli, "err", captured)
    resolved = _pick_backend(
        backend=backend,
        routing=routing,
        extension=extension,
        slice_specs=None,
        depart_times=None,
        return_times=None,
        children=0,
        seniors=0,
        youth=0,
        inf_seat=0,
        inf_lap=0,
        origin="JFK",
        destination="LAX",
        stops=None,
    )
    return resolved, buf.getvalue()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(GfPageShapeError(f"none of 2 rows parsed (AttributeError: {_HOSTILE})")),
        pytest.param(GfThrottledError(f"rate-limited {_HOSTILE}")),
        pytest.param(GfConsentError(f"consent {_HOSTILE}")),
        pytest.param(GfTfsUnsupportedError("stops", f"a ceiling {_HOSTILE}")),
        pytest.param(ValueError(f"an untyped failure {_HOSTILE}")),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_refusal_carrying_remote_markup_is_printable(error: Exception) -> None:
    """Every arm of the dispatch, including the ones whose wording is fixed —
    a later edit that starts interpolating the exception there is the same bug
    again, and this is what catches it."""
    refusal = _gf_refusal(error)
    for markup in (refusal.message, refusal.note):
        _render(markup)  # the assertion is that this does not raise


def test_the_page_shape_message_keeps_the_payload_text_readable() -> None:
    """Escaping must not eat the text — a shape error whose sampled reasons are
    swallowed is exactly as useless as one that crashes."""
    e = GfPageShapeError(f"none of 2 rows parsed (AttributeError: {_HOSTILE})")
    assert f"AttributeError: {_HOSTILE}" in _render(_gf_refusal(e).message)


@pytest.mark.parametrize("routing", _HOSTILE_ROUTING)
def test_hostile_routing_reaches_the_auto_path_verbatim(
    monkeypatch: pytest.MonkeyPatch, routing: str
) -> None:
    """On auto the reason is printed as markup. It must not raise, and the user
    must be able to read back the string they typed."""
    resolved, printed = _pick(monkeypatch, routing=routing)
    assert resolved == BACKEND_MATRIX
    assert _as_quoted(routing) in printed, f"the routing string was mangled: {printed!r}"


@pytest.mark.parametrize("extension", _HOSTILE_EXTENSION)
def test_hostile_extension_reaches_the_auto_path_verbatim(
    monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    resolved, printed = _pick(monkeypatch, extension=extension)
    assert resolved == BACKEND_MATRIX
    assert _as_quoted(extension) in printed, f"the extension string was mangled: {printed!r}"


@pytest.mark.parametrize("routing", _HOSTILE_ROUTING)
def test_hostile_routing_reaches_the_explicit_path_without_backslashes(
    monkeypatch: pytest.MonkeyPatch, routing: str
) -> None:
    """`--backend gflight` raises instead of printing, and typer renders that
    message as plain text. Escaping the reason at its source would satisfy the
    markup arm above and put a visible backslash here."""
    with pytest.raises(typer.BadParameter) as excinfo:
        _pick(monkeypatch, backend=BACKEND_GFLIGHT, routing=routing)
    message = str(excinfo.value)
    assert _as_quoted(routing) in message
    # A reason escaped where it was built would arrive carrying rich's own
    # backslash, and typer would print that backslash straight at the user.
    leaked = _as_quoted(routing).replace("[", "\\[")
    assert leaked not in message, f"a rich escape leaked into plain text: {message!r}"
