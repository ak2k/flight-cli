# pyright: reportPrivateUsage=false
"""Refusal text is rendered as rich markup, and its content is not ours.

Two sources feed square brackets into these strings. Google's payload reaches
the `GfPageShapeError` message through fli's decoders, which raise with a code
taken verbatim from the row (`fli/search/_decoders.py`). And the user's own
`--routing` / `--extension` string is quoted back at them in the reason the
backend picker prints. Either one containing `[/x]` ended the command in a
`MarkupError` traceback instead of the refusal it was trying to explain.

THE RULE: reason strings and exception text are PLAIN TEXT wherever they are
built, and EVERY render site escapes what it interpolates (repr first, then
escape). Escaping at the source cannot work, for two independent reasons. The
same reason goes out two ways from `_pick_backend` — a rich Console on the auto
path, a `typer.BadParameter` on the explicit path, and typer renders that as
plain `Text` — so a pre-escaped reason satisfies the first and shows the user a
literal backslash in the second. And the calendar paths escape at their own
render sites, so a reason escaped at the source would be escaped twice.

Three groups of tests below, one per half of the rule: `matrix_reasons` is plain
at the source; both `_pick_backend` arms are correct on the same hostile input;
and remote exception text survives the render sites that print it.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, cast

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
from flight_cli.routing_predicates import classify

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


def test_the_reported_routing_string_has_no_backslash_in_its_reason() -> None:
    """The reported case, asserted literally against the whole string."""
    (reason,) = classify("BA[/weird]AA", None).matrix_reasons
    assert reason == "routing 'BA[/weird]AA' not GF-expressible"
    assert "\\" not in reason


def _assert_plain_at_source(reasons: tuple[str, ...], typed: str) -> None:
    """A reason quotes `typed` exactly as repr writes it, and no more.

    Not a blanket "contains no backslash": repr doubles a backslash the user
    typed, which is repr's job. What must not appear is a SECOND layer — rich's
    escape, applied on top — so the check is for the escaped form of the quoted
    string rather than for the character."""
    assert reasons, "an unparseable value must produce a reason"
    joined = " ".join(reasons)
    quoted = _as_quoted(typed)
    assert quoted in joined
    leaked = quoted.replace("[", "\\[")
    assert leaked not in joined, f"a renderer's escape was baked in at the source: {joined!r}"


@pytest.mark.parametrize("routing", _HOSTILE_ROUTING)
def test_a_routing_reason_is_plain_text_at_its_source(routing: str) -> None:
    """`classify` builds the reason; it must hand back what the user typed with
    no renderer's escaping baked in.

    This is the half of the rule the render-site tests cannot see. A reason
    escaped here reads correctly through a rich Console and wrongly everywhere
    else — typer's plain-text errors, and the calendar paths, which escape at
    their own render sites and would escape it a second time."""
    _assert_plain_at_source(classify(routing, None).matrix_reasons, routing)


@pytest.mark.parametrize("extension", _HOSTILE_EXTENSION)
def test_an_extension_reason_is_plain_text_at_its_source(extension: str) -> None:
    _assert_plain_at_source(classify(None, extension).matrix_reasons, extension)


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


def test_a_matrix_error_carrying_markup_is_printable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix's rejections are quoted back verbatim too — the QPX layer answers
    a bad route with its own prose, and nothing sanitises it on the way here.
    `_run` is the shared search and detail entry point that prints it."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))

    def _boom(*_a: object, **_kw: object) -> object:
        raise MatrixApiError(
            "QPX Warning. Bad route [/spec]", kind="in[put]", request_id="Or[FG]wFzk"
        )

    monkeypatch.setattr(cli.anyio, "run", _boom)
    with pytest.raises(typer.Exit):
        cli._run(cast("Any", None), rps=1.0, impersonate="chrome", no_cache=True)

    printed = buf.getvalue()
    for fragment in ("QPX Warning. Bad route [/spec]", "in[put]", "Or[FG]wFzk"):
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"


def test_the_enriched_path_escapes_every_field_of_a_matrix_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enriched path prints the same Matrix error as `_run`, from its own
    line, and it escaped the message while leaving `kind` bare.

    That is the failure mode a line-level `grep -v escape` cannot see: the line
    already said `escape`, so it looked done. Every field is asserted here, not
    just the one that was wrong."""
    from datetime import date, timedelta

    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    class _FailingMatrix:
        """Matrix's client, refusing. Nothing here reaches the network — the
        real dispatch still runs, on a stubbed client and a stubbed GF call."""

        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _FailingMatrix:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, _search: object, **_kw: object) -> object:
            raise MatrixApiError(
                "QPX Warning. Bad route [/spec]", kind="in[put]", request_id="Or[FG]x"
            )

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))
    monkeypatch.setattr(cli, "MatrixClient", _FailingMatrix)

    def _no_gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _no_gf_rows)

    legs = (cli.Leg.of(("JFK",), ("LAX",), date.today() + timedelta(days=45)),)
    opts = cli._build_options(
        cabin="economy",
        adults=1,
        children=0,
        seniors=0,
        youth=0,
        infants_in_seat=0,
        infants_in_lap=0,
        stops=None,
        allow_airport_changes=True,
        show_only_available=True,
    )
    with pytest.raises(typer.Exit):
        cli._run_enriched_path(
            legs=legs,
            opts=opts,
            top_n=3,
            run_pp=False,
            sel=None,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )

    printed = buf.getvalue()
    for fragment in ("QPX Warning. Bad route [/spec]", "in[put]"):
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"


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


def test_the_awards_only_refusal_escapes_the_providers_the_user_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--awards-only --providers '[/x]'` quotes the filter back while telling
    the user it matched nothing configured."""
    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))
    # A provider IS configured, so the refusal is about the filter matching
    # none of them — which is the branch that quotes the filter back.
    monkeypatch.setattr(
        "flight_cli.providers.registry.has_any_configured", lambda: True, raising=True
    )
    sel = cli.ProviderSelection(
        awards_only=True,
        cash_only=False,
        provider_filter=("[/x]",),
        provider_opts={},
    )
    with pytest.raises(typer.Exit):
        cli._should_run_awards(sel)
    assert "[/x]" in buf.getvalue()


@pytest.mark.parametrize("fetch", [False, True], ids=["url-only", "resolved"])
def test_seatmap_prints_a_url_the_user_can_actually_paste(
    monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """These two prints pass the URL as a BARE argument, which rich still reads
    as markup — and a bare string is the dangerous case: rich DROPS a bracketed
    segment rather than raising, so the user copies a silently wrong URL.

    A URL can carry brackets legitimately (RFC 3986 reserves them for IPv6
    literals, and query strings in the wild use them unencoded)."""
    from typer.testing import CliRunner

    from flight_cli import cli

    # `[bold]` is the dangerous shape, not `[/x]`: rich RAISES on an unmatched
    # closing tag but silently DROPS a valid opening one, so this URL comes out
    # short and wrong rather than loudly broken. `[1A]` would prove nothing —
    # rich leaves non-tag-shaped brackets alone.
    bracketed = "https://seatmaps.example/x?f=AA100&opts=[bold]seats"

    def _url(**_kw: object) -> str:
        return bracketed

    monkeypatch.setattr(cli, "console", Console(width=400, no_color=True, highlight=False))
    monkeypatch.setattr("flight_cli.seatmap.seatmap_api_url", _url)
    monkeypatch.setattr("flight_cli.seatmap.fetch_seatmap_url", _url)

    args = ["seatmap", "JFK", "LAX", "AA100", "--date", "2026-10-14"]
    result = CliRunner().invoke(cli.app, args if fetch else [*args, "--no-fetch"])

    assert result.exit_code == 0, result.output
    assert "[bold]" in result.output, f"the bracketed segment was eaten: {result.output!r}"
