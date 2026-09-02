"""Refusal text is rendered as rich markup, and its content is not ours.

Two sources feed square brackets into these strings. Google's payload reaches
the `GfPageShapeError` message through fli's decoders, which raise with a code
taken verbatim from the row (`fli/search/_decoders.py`). And the user's own
`--routing` / `--extension` string is quoted back at them in the reason the
backend picker prints. Either one containing `[/x]` used to end the command in a
`MarkupError` traceback instead of the refusal it was trying to explain.

The fix escapes at two different places on purpose: exception text at each
render site, because the exception is built far away and nothing else touches
it; routing reasons where they are BUILT, because several render sites share
them and escaping at each is a rule someone will miss.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from flight_cli._gf_errors import (
    GfConsentError,
    GfPageShapeError,
    GfTfsUnsupportedError,
    GfThrottledError,
)
from flight_cli.cli import _gf_refusal  # pyright: ignore[reportPrivateUsage]
from flight_cli.routing_predicates import classify, page_can_encode

# A closing tag with no opener: rich's parser raises on it rather than ignoring
# it, which is what turns a payload string into a crash.
_HOSTILE = "[/x]"


def _render(markup: str) -> str:
    """Render through a real Console, which is where MarkupError comes from —
    a plain string comparison would pass on text that cannot be printed."""
    buf = io.StringIO()
    Console(file=buf, width=200, no_color=True, highlight=False).print(markup)
    return buf.getvalue()


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


def test_a_routing_string_with_markup_survives_the_backend_reason() -> None:
    """`--routing 'BA[/weird]AA'` routes to Matrix, and the line explaining why
    quotes the routing string back. It is printed with `[dim]…[/]` around it."""
    _ok, reasons = page_can_encode(classify("BA[/weird]AA", None).predicates)
    assert reasons, "a non-expressible routing must produce a reason to print"
    rendered = _render(f"[dim]Using Matrix: Google Flights can't serve {reasons[0]}.[/]")
    assert "BA[/weird]AA" in rendered


def test_an_extension_string_with_markup_survives_too() -> None:
    _ok, reasons = page_can_encode(classify(None, "MAXDUR [/x]").predicates)
    assert reasons
    assert "[/x]" in _render(f"[dim]{reasons[0]}[/]")
