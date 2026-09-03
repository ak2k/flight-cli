# pyright: reportPrivateUsage=false
"""Refusal text is rendered as rich markup, and its content is not ours.

Two sources feed square brackets into these strings. Google's payload reaches
the `GfPageShapeError` message through fli's decoders, which raise with a code
taken verbatim from the row (`fli/search/_decoders.py`). And the user's own
`--routing` / `--extension` string is quoted back at them in the reason the
backend picker prints. Either one containing `[/x]` ended the command in a
`MarkupError` traceback instead of the refusal it was trying to explain.

THE RULE: reason strings and exception text are PLAIN TEXT wherever they are
built, and EVERY render site escapes what it interpolates exactly once. The
same reason goes out two ways from `_pick_backend` — a rich Console on the auto
path, a `typer.BadParameter` on the explicit path, and typer renders that as
plain `Text` — so a reason escaped where it is built satisfies the first and
shows the user a literal backslash in the second.

Text that came from somewhere else entirely — a Matrix error, an exception's
`str()` — goes through `_safe_text` instead of a bare `escape`: markup is not
the only thing a console reacts to, and neither `kind` nor `request_id` is
guaranteed to be a string.

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
    GfBackendError,
    GfPageShapeError,
    GfTfsUnsupportedError,
    GfThrottledError,
    GfTransportError,
)
from flight_cli.cli import (
    _GF_DECLINED,
    BACKEND_AUTO,
    BACKEND_GFLIGHT,
    BACKEND_MATRIX,
    _gf_refusal,
    _pick_backend,
    _safe_text,
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


# Bytes that a terminal ACTS on. `escape` neutralises `[` and nothing else, so
# these survive it: the first clears the screen and homes the cursor, the second
# is the 8-bit CSI doing the same thing in one byte, and the rest reverse the
# reading order of what is printed after them. A redirected stderr keeps them
# for whatever reads the file next.
#
# The last three are bidi MARKS rather than overrides or isolates: they carry no
# terminator and reorder only the neutral characters beside them, which is what
# makes them the ones a reader is least likely to notice — a price or a route in
# a refusal line, read back as something it does not say.
_ESCAPES = "\x1b[2J\x1b[1;1H\x9b31m\u202e\u2066\u061c\u200e\u200f"

# Rich markup with a control character wedged inside the tag. `escape` only sees
# a tag where `[` is followed by `[a-z#/@]`, so this is invisible to it — and
# stripping the control character AFTERWARDS uncovers live markup that a real
# terminal then styles. That is the one outcome `_safe_text`'s ordering exists
# to prevent, and the reason `_render` below runs in terminal mode.
_LIVE_MARKUP = "[\x00red]DELAYED[\x00/red] and [\x00blink]PAY NOW[\x00/blink]"


def _hostile_matrix_error() -> Any:
    """Remote text carrying every shape a console reacts to."""
    from flight_cli.client import MatrixApiError

    return MatrixApiError(
        f"QPX Warning. Bad route [/spec]{_ESCAPES}", kind="in[put]", request_id="Or[bold]wFzk"
    )


def _unstringly_matrix_error() -> Any:
    """Hostile in every field at once, and typed as the remote JSON actually
    arrives: `kind` and `request_id` are lifted out of it with no coercion, so
    a `type` of `{"code": 5}` reaches the renderer as a dict — where `escape()`
    alone raises TypeError and the refusal becomes a traceback."""
    from flight_cli.client import MatrixApiError

    return MatrixApiError(
        f"Illegal COMMAND-LINE prefix: BA[/weird]AA{_ESCAPES}",
        kind=cast("Any", {"code": 5}),
        request_id=cast("Any", 7),
    )


def _assert_drives_no_terminal(printed: str) -> None:
    """Nothing in `printed` can move a cursor, clear a screen, or flip the
    reading order."""
    for ctrl in ("\x1b", "\x9b", "\u202e", "\u2066", "\u061c", "\u200e", "\u200f"):
        assert ctrl not in printed, f"{ctrl!r} reached the terminal: {printed!r}"


def _render(markup: str) -> str:
    """Render through a real Console, which is where MarkupError comes from —
    a plain string comparison would pass on text that cannot be printed.

    `force_terminal`, because a Console not writing to one emits no escape
    sequence at all: live markup and escaped markup then render to different
    text with the same absence of styling, and an assertion about what a
    terminal is driven to do cannot see the difference between them. `no_color`
    stays — the colours are noise in every other assertion here, and the
    attributes that survive it are enough to make styling visible."""
    buf = io.StringIO()
    Console(file=buf, width=400, no_color=True, highlight=False, force_terminal=True).print(markup)
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
    else — typer renders its errors as plain text, so the user is shown the
    backslash."""
    _assert_plain_at_source(classify(routing, None).matrix_reasons, routing)


@pytest.mark.parametrize("extension", _HOSTILE_EXTENSION)
def test_an_extension_reason_is_plain_text_at_its_source(extension: str) -> None:
    _assert_plain_at_source(classify(None, extension).matrix_reasons, extension)


_REFUSALS = GfBackendError.__module__  # where the published hierarchy lives


def _every_subclass(root: type[GfBackendError]) -> list[type[GfBackendError]]:
    """Every descendant of `root`, not just its children.

    `__subclasses__()` is one level deep, so a refusal added under an existing
    one instead of beside it would never be walked."""
    found: list[type[GfBackendError]] = []
    for child in root.__subclasses__():
        found.append(child)
        found.extend(_every_subclass(child))
    return found


def _hostile_of(cls: type[GfBackendError]) -> GfBackendError:
    """An instance of `cls` carrying every shape a console reacts to."""
    payload = f"{_HOSTILE}{_ESCAPES}"
    if cls is GfTfsUnsupportedError:
        return GfTfsUnsupportedError("stops", f"a ceiling {payload}")
    return cls(f"refused {payload}")


@pytest.mark.parametrize(
    "error",
    # Driven off the hierarchy rather than listed, so a new arm cannot be added
    # without a hostile case to go with it.
    [
        pytest.param(_hostile_of(cls), id=cls.__name__)
        for cls in [
            GfBackendError,
            *(c for c in _every_subclass(GfBackendError) if c.__module__ == _REFUSALS),
        ]
    ],
)
def test_a_refusal_carrying_remote_markup_is_printable(error: GfBackendError) -> None:
    """Every arm of the dispatch, including the ones whose wording is fixed —
    a later edit that starts interpolating the exception there is the same bug
    again, and this is what catches it.

    The last case is the BASE type, which is what actually reaches the base
    arm: a 5xx from the search page is raised as one. An unrelated exception
    cannot arrive here at all — the dispatch takes a refusal, and every caller
    has one in hand."""
    refusal = _gf_refusal(error)
    for markup in (refusal.message, refusal.note):
        _render(markup)  # the assertion is that this does not raise


def _one_of(cls: type[GfBackendError]) -> GfBackendError:
    """An instance of `cls`, whatever its constructor wants."""
    if cls is GfTfsUnsupportedError:
        return GfTfsUnsupportedError("stops", "a ceiling this page cannot carry")
    return cls("refused")


def test_every_refusal_type_is_named_rather_than_merely_declined() -> None:
    """A refusal type added to the hierarchy alone must not render as the
    generic wording.

    The dispatch has a base case, so a type with no arm still prints — as
    "declined the request", the same sentence an unreachable network and a
    page-shape change would produce. Nothing raises, nothing is missing, and
    the user is told the one thing that is not true. `assert_never` catches a
    type the checker can SEE; this catches the one that lands in
    `_gf_errors.py` with no arm written for it, which is the way it happened.

    Asserted on `message`, the text a user actually reads when the refusal is
    the whole outcome. `note` is the one-line label beside a Matrix table, and
    one type keeps the generic label there on purpose — its queries never reach
    this renderer — so a rule over `note` would fail on a type that IS handled.

    The walk is recursive. `__subclasses__()` gives direct children only, so a
    type added under an existing refusal rather than beside it is invisible to
    it — and renders as its PARENT's wall, which is worse than the generic
    wording because it names a wall confidently and names the wrong one.

    Only the published hierarchy: a marker class defined elsewhere is converted
    to a public type before any renderer sees it."""
    published = [
        cls
        for cls in _every_subclass(GfBackendError)
        if cls.__module__ == GfBackendError.__module__
    ]
    assert GfTransportError in published, "the walk must see the whole hierarchy"
    seen: dict[str, str] = {}
    for cls in published:
        message = _gf_refusal(_one_of(cls)).message
        assert _GF_DECLINED not in message, (
            f"{cls.__name__} renders as the generic refusal: {message!r}"
        )
        # Two refusals reading the same is the failure mode a nested type has:
        # it inherits its parent's arm and names that wall confidently, which
        # is worse than the generic wording because it is specific and wrong.
        note = _gf_refusal(_one_of(cls)).note
        assert note not in seen, f"{cls.__name__} renders as {seen[note]} does: {note!r}"
        seen[note] = cls.__name__


def test_the_transport_refusal_agrees_with_the_pin_loop_about_the_cause() -> None:
    """Two sites report an unreachable network and they must not disagree about
    which fact it is. The pin loop already says "was unreachable"; a render that
    says Google declined the request describes a different failure, and the user
    cannot tell which one happened."""
    # Built the way `retry_throttled` builds it: the type carries the sentence,
    # so a renderer that adds its own says it twice.
    refusal = _gf_refusal(
        GfTransportError("Google Flights could not be reached: connection reset by peer")
    )
    assert "unreachable" in refusal.note
    rendered = _render(refusal.message)
    assert rendered.count("could not be reached") == 1, rendered
    assert "connection reset by peer" in rendered, rendered
    assert "--backend matrix" in rendered, rendered


def test_remote_text_cannot_style_the_terminal_it_is_reported_on() -> None:
    """Strip the control characters, THEN escape — the order, and what it buys.

    A tag with a control character inside it is invisible to `escape`, which
    only sees one where `[` is followed by `[a-z#/@]`. Stripping afterwards
    uncovers it, and the console is then handed live markup assembled out of
    remote text: a Matrix message or an exception's `str()` chooses the colour,
    the blink and the reverse video its own failure is reported in, and can
    repaint what was printed above it.

    Rendered in terminal mode on purpose. A Console with no terminal to write
    to emits no escape sequence at all, so the two orders come out as different
    text with the same absence of styling — which is a difference no assertion
    about a terminal can see."""
    printed = _render(_safe_text(f"Matrix said: {_LIVE_MARKUP}"))
    _assert_drives_no_terminal(printed)
    # Visible as itself: the brackets belong to the payload, so they are what a
    # reader should see, and the text between them must not go missing either.
    assert "[blink]PAY NOW[/blink]" in printed, printed


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


@pytest.mark.parametrize(
    ("build", "fragments"),
    [
        pytest.param(
            _hostile_matrix_error,
            ("QPX Warning. Bad route [/spec]", "in[put]", "Or[bold]wFzk"),
            id="markup-and-control-bytes",
        ),
        pytest.param(
            _unstringly_matrix_error,
            ("code", "5", "request_id: 7", "BA[/weird]AA"),
            id="fields-that-are-not-strings",
        ),
    ],
)
def test_a_matrix_error_carrying_markup_is_printable(
    monkeypatch: pytest.MonkeyPatch, build: Any, fragments: tuple[str, ...]
) -> None:
    """Matrix's rejections are quoted back verbatim too — the QPX layer answers
    a bad route with its own prose, and nothing sanitises it on the way here.
    `_run` is the shared search and detail entry point that prints it, and the
    only one that prints all three fields."""
    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))

    def _boom(*_a: object, **_kw: object) -> object:
        raise cast("Exception", build())

    monkeypatch.setattr(cli.anyio, "run", _boom)
    with pytest.raises(typer.Exit):
        cli._run(cast("Any", None), rps=1.0, impersonate="chrome", no_cache=True)

    printed = buf.getvalue()
    for fragment in fragments:
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


@pytest.mark.parametrize(
    ("build", "fragments"),
    [
        pytest.param(
            _hostile_matrix_error,
            ("QPX Warning. Bad route [/spec]", "in[put]"),
            id="markup-and-control-bytes",
        ),
        pytest.param(
            _unstringly_matrix_error,
            ("code", "5", "request_id: 7", "BA[/weird]AA"),
            id="fields-that-are-not-strings",
        ),
    ],
)
def test_the_enriched_path_escapes_every_field_of_a_matrix_error(
    monkeypatch: pytest.MonkeyPatch, build: Any, fragments: tuple[str, ...]
) -> None:
    """The enriched path prints the same Matrix error as `_run`, through the same
    helper.

    A line-level `grep` for `escape` cannot tell a sanitised field from an
    unsanitised one beside it, so every field is asserted here, the request id
    included: without it a user cannot quote the failure back to anyone who
    could look it up."""
    from datetime import date, timedelta

    from flight_cli import cli

    error = cast("Exception", build())

    class _FailingMatrix:
        """Matrix's client, refusing. Nothing here reaches the network — the
        real dispatch still runs, on a stubbed client and a stubbed GF call."""

        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _FailingMatrix:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, _search: object, **_kw: object) -> object:
            raise error

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
    for fragment in fragments:
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


def test_an_unwrapped_matrix_failure_is_reported_without_taking_the_google_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Matrix task must not carry an exception out of the weave.

    `execute()` wraps what it knows about; a connect timeout or a TLS failure is
    not on that list, and one leaving this task cancels the still-pending Google
    Flights paint and surfaces as a bare ExceptionGroup — a traceback in place
    of the rows Google had already returned. The two backends are meant to fail
    independently, which is exactly what the calendar weave beside this one
    already does.

    The message is remote text like any other, so it goes through the same
    sanitiser: driven with control bytes here, which a bare `escape` would let
    through."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(
        cli, "MatrixClient", _refusing_matrix(RuntimeError(f"connect timeout{_ESCAPES}"))
    )

    rows = [cast("Any", object())]
    painted: list[Any] = []

    def _gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return rows

    def _paint(gf: list[Any], **_kw: object) -> None:
        painted.extend(gf)

    monkeypatch.setattr(cli, "_gflight_results", _gf_rows)
    monkeypatch.setattr(cli, "_render_gflight_table", _paint)

    legs, opts = _gf_legs_and_opts()
    # No `pytest.raises`: Google answered, so the command has an answer to give.
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
    assert painted == rows, "the Google rows were lost with the Matrix failure"
    assert "Matrix search failed" in printed, printed
    assert "connect timeout" in printed, printed
    assert printed.count("Matrix search failed") == 1, printed
    _assert_drives_no_terminal(printed)


def test_awards_only_with_matrix_down_exits_nonzero_rather_than_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows FETCHED is not rows SHOWN, and the exit code has to track the
    second.

    `--awards-only` suppresses the Google table on purpose, and the award
    lookups run behind Matrix — so when Matrix fails, a non-empty Google result
    means the user got a byte-empty stdout. Exiting 0 there tells a script the
    search succeeded and returned nothing, which is the one answer a search must
    never give."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    buf = _capture(monkeypatch)
    monkeypatch.setattr(
        cli, "MatrixClient", _refusing_matrix(MatrixApiError("matrix is down", kind="X"))
    )

    def _gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return [cast("Any", object())]

    monkeypatch.setattr(cli, "_gflight_results", _gf_rows)

    legs, opts = _gf_legs_and_opts()
    sel = cli.ProviderSelection(
        awards_only=True, cash_only=False, provider_filter=None, provider_opts={}
    )
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_enriched_path(
            legs=legs,
            opts=opts,
            top_n=3,
            run_pp=False,
            sel=sel,
            matrix_url=False,
            google_url=False,
            pick=None,
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )
    assert excinfo.value.exit_code == 1, "nothing was rendered, so the exit must say so"
    assert "Matrix returned an error" in buf.getvalue(), buf.getvalue()


def test_a_renderer_that_raises_does_not_cancel_the_matrix_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first paint runs inside the weave's task group, so an exception in
    it cancels the Matrix task and the command ends as a bare ExceptionGroup —
    the very failure the task beside it is guarded against. A drifted row shape
    reaches the renderer, so this is not hypothetical.

    Google's rows are lost either way; Matrix's answer need not be."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    buf = _capture(monkeypatch)
    monkeypatch.setattr(
        cli, "MatrixClient", _refusing_matrix(MatrixApiError("no fares", kind="input"))
    )

    def _gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return [cast("Any", object())]

    def _drifted(*_a: object, **_kw: object) -> None:
        raise AttributeError("drifted row shape")

    monkeypatch.setattr(cli, "_gflight_results", _gf_rows)
    monkeypatch.setattr(cli, "_render_gflight_table", _drifted)

    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
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
    assert excinfo.value.exit_code == 1
    printed = buf.getvalue()
    assert "drifted row shape" in printed, printed
    assert "Matrix returned an error" in printed, "the Matrix half was cancelled with the paint"
    _assert_drives_no_terminal(printed)


def test_the_shared_search_path_types_an_unreachable_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`execute()` wraps what Matrix ANSWERED. A refused connection or a failed
    DNS lookup is not an answer, so nothing wrapped it and it left this path as
    a rich traceback with the cause hundreds of lines down — on the most
    ordinary command the CLI has."""
    from flight_cli import cli

    buf = _capture(monkeypatch)

    def _boom(*_a: object, **_kw: object) -> object:
        raise OSError(f"Could not resolve host{_ESCAPES}")

    monkeypatch.setattr(cli.anyio, "run", _boom)
    with pytest.raises(typer.Exit) as excinfo:
        cli._run(cast("Any", None), rps=1.0, impersonate="chrome", no_cache=True)

    assert excinfo.value.exit_code == 1
    printed = buf.getvalue()
    assert "Matrix search failed" in printed, printed
    assert "Could not resolve host" in printed, printed
    _assert_drives_no_terminal(printed)


def test_one_cabins_unreachable_matrix_does_not_cancel_the_cabins_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception leaving a cabin's task cancels its siblings and comes out
    as an ExceptionGroup, so one unreachable cabin took the whole fan-out and
    printed two tracebacks for it.

    Soft per cabin, like the Google arm beside it: the column goes missing, the
    line says why, and the cabins that answered are still an answer."""
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = _capture(monkeypatch)

    class _OneBadCabin:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _OneBadCabin:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, search: Any, **_kw: object) -> object:
            if search.options.cabin is Cabin.BUSINESS:
                raise OSError(f"Could not resolve host{_ESCAPES}")
            return cast("Any", object())

    monkeypatch.setattr(cli, "MatrixClient", _OneBadCabin)
    legs, opts = _gf_legs_and_opts()
    out = cli._run_matrix_multi(
        legs=legs,
        opts=opts,
        cabins=(Cabin.COACH, Cabin.BUSINESS),
        rps=1.0,
        impersonate="chrome",
        no_cache=True,
    )

    assert set(out) == {Cabin.COACH}, f"a cabin that answered was cancelled: {sorted(out)}"
    printed = buf.getvalue()
    assert "BUSINESS" in printed and "Could not resolve host" in printed, printed
    _assert_drives_no_terminal(printed)


# The dangerous markup shape for a fragment that must SURVIVE: rich raises on an
# unmatched closing tag but silently DROPS a valid opening one, so a probe built
# from `[/x]` proves only that nothing crashed.
_DROPPED = "[bold]x"


def _gf_legs_and_opts() -> tuple[Any, Any]:
    from datetime import date, timedelta

    from flight_cli import cli

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
    return legs, opts


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            GfPageShapeError(f"none of 2 rows parsed (AttributeError: {_DROPPED}){_ESCAPES}"),
            "Google Flights' page shape changed",
            id="a-typed-refusal-reads-as-its-own-note",
        ),
        pytest.param(
            RuntimeError(f"boom {_DROPPED}{_ESCAPES}"),
            f"boom {_DROPPED}",
            id="an-untyped-failure-is-quoted-back",
        ),
    ],
)
def test_the_cabin_fan_out_reports_why_a_column_is_missing(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    """Both arms of the fan-out's handler, which nothing reached before: a
    stubbed `search_with_ids` that returns rows never enters either. A typed
    refusal is answered with its own note, and anything else is quoted back —
    the cabin's column goes missing either way, and an unexplained gap is the
    outcome this handler exists to prevent."""
    from flight_cli import _gflight_ids as gfid
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))

    def _boom(*_a: object, **_kw: object) -> object:
        raise error

    monkeypatch.setattr(gfid, "search_with_ids", _boom)
    legs, opts = _gf_legs_and_opts()
    assert cli._run_gflight_multi(legs=legs, opts=opts, cabins=(Cabin.COACH,), top_n=5) == {}

    printed = buf.getvalue()
    assert "COACH" in printed, f"the cabin went unnamed: {printed!r}"
    assert expected in printed, f"{expected!r} was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            GfThrottledError(f"rate-limited {_DROPPED}{_ESCAPES}"),
            "Google Flights rate-limited",
            id="a-typed-refusal-reads-as-its-own-note",
        ),
        pytest.param(
            RuntimeError(f"boom {_DROPPED}{_ESCAPES}"),
            f"boom {_DROPPED}",
            id="an-untyped-failure-is-quoted-back",
        ),
    ],
)
def test_the_enriched_path_reports_a_google_flights_refusal_as_a_footnote(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    """Matrix answered, so it is authoritative and a Google Flights failure is
    a note beside its table rather than the outcome — but it stays named, or
    the merged table just looks like Google had nothing cheaper."""
    from flight_cli import cli
    from flight_cli.models import SearchResult

    buf = io.StringIO()
    captured = Console(file=buf, width=400, no_color=True, highlight=False)
    monkeypatch.setattr(cli, "err", captured)
    monkeypatch.setattr(cli, "console", captured)

    class _AnsweringMatrix:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _AnsweringMatrix:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, _search: object, **_kw: object) -> object:
            return SearchResult.model_validate({"solutions": []})

    def _boom(*_a: object, **_kw: object) -> object:
        raise error

    def _no_repaint(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "MatrixClient", _AnsweringMatrix)
    monkeypatch.setattr(cli, "_gflight_results", _boom)
    monkeypatch.setattr(cli, "_render_merged", _no_repaint)
    legs, opts = _gf_legs_and_opts()
    # No `pytest.raises`: the other half answered, so this run has an outcome.
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
    assert expected in printed, f"{expected!r} was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


def test_a_run_with_no_matrix_does_not_promise_a_table_it_never_got(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves failed, so there is no Matrix table for a Google Flights
    refusal to be a footnote to — and "showing Matrix only" is a promise the
    run cannot keep.

    This is the likeliest shape of a total outage there is: a key that will not
    resolve and no route to Google are the same lost network. What the user
    gets is the two reasons on stderr, nothing on stdout, and an exit code that
    agrees with both."""
    from flight_cli import cli
    from flight_cli._gf_errors import GfTransportError

    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=out_buf, width=400, no_color=True, highlight=False)
    )
    monkeypatch.setattr(
        cli, "err", Console(file=err_buf, width=400, no_color=True, highlight=False)
    )

    def _unreachable(*_a: object, **_kw: object) -> object:
        raise GfTransportError("Google Flights could not be reached: connection reset by peer")

    async def _matrix_fails(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        from flight_cli.client import MatrixApiError

        state["matrix_err"] = MatrixApiError("no fares", kind="input")

    monkeypatch.setattr(cli, "_gflight_results", _unreachable)
    monkeypatch.setattr(cli, "_matrix_into", _matrix_fails)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
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

    assert excinfo.value.exit_code == 1
    assert out_buf.getvalue() == "", out_buf.getvalue()
    printed = err_buf.getvalue()
    assert "showing Matrix only" not in printed, printed
    assert "could not be reached" in printed, printed
    assert "no fares" in printed, printed
    _assert_drives_no_terminal(printed)


def test_the_per_cabin_matrix_failure_is_readable_and_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cabin whose Matrix query fails is a soft failure: the column goes
    missing and this line is the only account of why. It carries every remote
    field of the error onto a markup console, on a path that never reaches the
    shared reporter because it names the cabin as well."""
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))

    class _FailingCabin:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _FailingCabin:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, _search: object, **_kw: object) -> object:
            raise cast("Exception", _unstringly_matrix_error())

    monkeypatch.setattr(cli, "MatrixClient", _FailingCabin)
    legs, opts = _gf_legs_and_opts()
    assert (
        cli._run_matrix_multi(
            legs=legs,
            opts=opts,
            cabins=(Cabin.COACH,),
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )
        == {}
    )

    printed = buf.getvalue()
    for fragment in ("COACH", "code", "5", "BA[/weird]AA"):
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


def test_the_gflight_only_path_quotes_an_untyped_failure_it_cannot_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--backend gflight` has no Matrix to fall back to, so an untyped failure
    IS the whole output. Quoting it is the only account the user gets of why
    the command ended."""
    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=400, no_color=True, highlight=False))

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError(f"boom {_DROPPED}{_ESCAPES}")

    monkeypatch.setattr(cli, "_gflight_results", _boom)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit):
        cli._run_gflight_path(legs=legs, opts=opts, top_n=3, json_out=False)

    printed = buf.getvalue()
    assert f"boom {_DROPPED}" in printed, f"the failure was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


def test_a_link_builder_that_fails_does_not_take_the_results_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL footer is a convenience printed after a table the user already
    has. Its third-party builder has no documented exception surface, so the
    failure is caught and quoted — and that quote is remote text on a markup
    console like any other."""
    from flight_cli import cli

    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf, width=400, no_color=True, highlight=False)
    )

    def _boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError(f"no url {_DROPPED}{_ESCAPES}")

    monkeypatch.setattr(cli, "google_flights_url", _boom)
    legs, opts = _gf_legs_and_opts()
    cli._emit_urls(
        cli.SpecificDateSearch(legs=legs, options=opts),
        matrix_url=False,
        google_url=True,
        result=cast("Any", None),
        pick=None,
    )

    printed = buf.getvalue()
    assert f"no url {_DROPPED}" in printed, f"the failure was mangled: {printed!r}"
    _assert_drives_no_terminal(printed)


# A URL is remote text too: the pinned builders encode airline and airport
# strings taken off a payload, and the deep link carries the user's own routing.
_HOSTILE_URL = f"https://example.test/x?q={_DROPPED}{_ESCAPES}"


def _emit(
    monkeypatch: pytest.MonkeyPatch, *, pinned: bool, pick: int | None, solutions: int = 3
) -> str:
    """Drive `_emit_urls` with both link kinds and return what it printed."""
    from flight_cli import cli
    from flight_cli.models import SearchResult

    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf, width=400, no_color=True, highlight=False)
    )

    def _hostile(*_a: object, **_kw: object) -> str:
        return _HOSTILE_URL

    def _cannot_pin(*_a: object, **_kw: object) -> str | None:
        return None

    monkeypatch.setattr(cli, "_try_pinned_matrix_url", _hostile if pinned else _cannot_pin)
    monkeypatch.setattr(cli, "_try_pinned_gflight_url", _hostile if pinned else _cannot_pin)
    if not pinned:
        monkeypatch.setattr(cli, "matrix_deep_link", _hostile)
        monkeypatch.setattr(cli, "google_flights_url", _hostile)

    legs, opts = _gf_legs_and_opts()
    result = SearchResult.model_validate({"solutions": [{} for _ in range(solutions)]})
    cli._emit_urls(
        cli.SpecificDateSearch(legs=legs, options=opts),
        matrix_url=True,
        google_url=True,
        result=result,
        pick=pick,
    )
    return buf.getvalue()


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned-links", "plain-links"])
def test_both_url_lines_sanitise_what_they_print(
    monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """Both links are built from text nobody here wrote: the pinned encoders
    take airline and airport strings off a payload, and the deep link carries
    the user's own routing string.

    The escape is per line, and the two lines have separate calls — losing it on
    either is a `MarkupError` traceback in place of a footer, or a control
    sequence handed to the terminal. Driven through a real Console, which is
    where both of those come from."""
    printed = _emit(monkeypatch, pinned=pinned, pick=1)

    assert printed.count(_DROPPED) == 2, f"a URL line lost its escape: {printed!r}"
    _assert_drives_no_terminal(printed)
    for line in printed.splitlines():
        if "example.test" in line:
            assert line.startswith("  "), f"a URL line lost its indent: {line!r}"


def test_a_pinned_link_is_labelled_with_the_row_the_user_picked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label is the only thing that says WHICH itinerary the link opens.
    Off by one it is worse than missing: the link goes to the cheapest row and
    the label says it is the row the user asked for."""
    printed = _emit(monkeypatch, pinned=True, pick=2)

    assert printed.count("itinerary #2 pinned") == 2, printed
    assert "cheapest itinerary" not in printed, printed


def test_an_out_of_range_pick_labels_the_link_it_actually_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range pick falls back to the cheapest row rather than emitting
    a broken link, so the label has to fall back with it — a "#9" over the
    cheapest itinerary is a wrong answer dressed as the requested one."""
    printed = _emit(monkeypatch, pinned=True, pick=9, solutions=3)

    assert "out of range (1-3)" in printed, printed
    assert printed.count("cheapest itinerary pinned") == 2, printed
    assert "#9" not in printed, printed


def test_an_unpinnable_result_says_so_in_both_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither builder could pin, so both lines are the plain search links —
    labelled as what they are, because a user who asked for row 2 and got the
    search page needs to be able to tell."""
    printed = _emit(monkeypatch, pinned=False, pick=2)

    assert "Matrix deep-link:" in printed, printed
    assert "Google Flights (tfs= structured):" in printed, printed
    assert "pinned" not in printed, printed


def test_the_seatmap_lookup_failure_quotes_the_remote_url_and_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves of the failure branch carry text from elsewhere: the error
    from a third-party client, and the API URL built from a remote path."""
    from typer.testing import CliRunner

    from flight_cli import cli

    bracketed = f"https://seatmaps.example/x?opts={_DROPPED}seats{_ESCAPES}"

    def _url(**_kw: object) -> str:
        return bracketed

    def _boom(**_kw: object) -> str:
        raise RuntimeError(f"upstream said no {_DROPPED}{_ESCAPES}")

    monkeypatch.setattr(cli, "console", Console(width=400, no_color=True, highlight=False))
    monkeypatch.setattr(cli, "err", Console(width=400, no_color=True, highlight=False))
    monkeypatch.setattr("flight_cli.seatmap.seatmap_api_url", _url)
    monkeypatch.setattr("flight_cli.seatmap.fetch_seatmap_url", _boom)

    result = CliRunner().invoke(cli.app, ["seatmap", "JFK", "LAX", "AA100", "--date", "2026-10-14"])

    assert result.exit_code == 1, result.output
    for fragment in (f"upstream said no {_DROPPED}", f"opts={_DROPPED}seats"):
        assert fragment in result.output, f"{fragment!r} was mangled: {result.output!r}"
    _assert_drives_no_terminal(result.output)


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
    the user it matched nothing configured.

    Both markup shapes, because they fail differently: rich RAISES on the
    unmatched closing tag and silently DROPS the valid opening one, so the
    dropping shape is the one that proves the filter survived rather than that
    nothing crashed."""
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
        provider_filter=("[/x]", "[bold]"),
        provider_opts={},
    )
    with pytest.raises(typer.Exit):
        cli._should_run_awards(sel)
    printed = buf.getvalue()
    for fragment in ("[/x]", "[bold]"):
        assert fragment in printed, f"{fragment!r} was mangled: {printed!r}"


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


# Code points a terminal does not ACT on, which is why the strip above misses
# them and `escape` never sees them. The first two are invisible rather than
# active: they survive `strip()` and sit unseen inside a carrier code or a price,
# so two values that read as identical compare unequal and nothing on screen says
# why. The third cannot be encoded at all — a lone surrogate reaches a real
# stdout as UnicodeEncodeError, so the render of a query that already SUCCEEDED
# dies on the way out. A StringIO takes all three silently, so the encode below
# is what stands in for the real console.
_UNWRITABLE = "\u200b\U000e0041\ud800"

# The other half of `_safe_text`: an exception that stringifies to nothing is
# reported by its class NAME, and a class built from a remote payload can be
# named anything. Built by `type()` rather than a `class` statement because that
# statement takes an identifier, and an identifier cannot contain an ESC.
_BlankFailure: type[RuntimeError] = type("Blank\x1b[2JTimeout", (RuntimeError,), {})
_BLANK_NAME = _BlankFailure.__name__.replace("\x1b", "")


def _unwritable_matrix_error() -> Any:
    """Matrix's own prose, carrying the invisible and the unencodable in every
    field that is quoted back."""
    from flight_cli.client import MatrixApiError

    return MatrixApiError(
        f"QPX Warning. Bad route{_UNWRITABLE}",
        kind=f"input{_UNWRITABLE}",
        request_id=f"Or{_UNWRITABLE}wFzk",
    )


def _blank_failure() -> Any:
    """`httpx.ConnectTimeout("")`'s shape: nothing to print but the class name."""
    return _BlankFailure("")


def _refusing_matrix(error: Exception) -> Any:
    """Matrix's client, refusing with `error`. Nothing here reaches the network —
    the real dispatch still runs, on a stubbed client."""

    class _Refusing:
        def __init__(self, **_kw: object) -> None: ...

        async def __aenter__(self) -> _Refusing:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def execute(self, _search: object, **_kw: object) -> object:
            raise error

    return _Refusing


def _no_gf() -> Any:
    def _none(*_a: object, **_kw: object) -> list[Any]:
        return []

    return _none


def _one_gf_row() -> Any:
    def _row(*_a: object, **_kw: object) -> list[Any]:
        return [cast("Any", object())]

    return _row


def _no_matrix() -> Any:
    async def _nothing(*_a: object, **_kw: object) -> None:
        return None

    return _nothing


def _matrix_task_raising(e: BaseException) -> Any:
    """A Matrix task that lets something escape, rather than stashing it —
    which is what puts it in the group the weave's own arm receives."""

    async def _raise(*_a: object, **_kw: object) -> None:
        raise e

    return _raise


def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Both streams into one buffer: these reporters are split across `err` and
    `console`, and which one a given line took is not what is under test."""
    from flight_cli import cli

    buf = io.StringIO()
    captured = Console(file=buf, width=400, no_color=True, highlight=False)
    monkeypatch.setattr(cli, "err", captured)
    monkeypatch.setattr(cli, "console", captured)
    return buf


def _run_enriched() -> None:
    from flight_cli import cli

    legs, opts = _gf_legs_and_opts()
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


def _via_enriched_matrix(monkeypatch: pytest.MonkeyPatch, error: Exception) -> str:
    """The shared Matrix reporter, reached from the enriched path."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "MatrixClient", _refusing_matrix(error))

    def _no_gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_gflight_results", _no_gf_rows)
    _run_enriched()
    return buf.getvalue()


def _via_cabin_matrix(monkeypatch: pytest.MonkeyPatch, error: Exception) -> str:
    """The per-cabin line, which names the cabin and so never reaches the shared
    reporter."""
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "MatrixClient", _refusing_matrix(error))
    legs, opts = _gf_legs_and_opts()
    assert (
        cli._run_matrix_multi(
            legs=legs,
            opts=opts,
            cabins=(Cabin.COACH,),
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )
        == {}
    )
    return buf.getvalue()


def _via_enriched_gf(monkeypatch: pytest.MonkeyPatch, error: Exception) -> str:
    """The enriched path's footnote for an untyped Google failure, which passes
    the exception itself rather than a message lifted off it."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    buf = _capture(monkeypatch)
    # Matrix has to end too, or the path renders a table instead of exiting.
    monkeypatch.setattr(
        cli, "MatrixClient", _refusing_matrix(MatrixApiError("no fares", kind="input"))
    )

    def _boom(*_a: object, **_kw: object) -> object:
        raise error

    monkeypatch.setattr(cli, "_gflight_results", _boom)
    _run_enriched()
    return buf.getvalue()


def _via_cabin_gf(monkeypatch: pytest.MonkeyPatch, error: Exception) -> str:
    from flight_cli import _gflight_ids as gfid
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = _capture(monkeypatch)

    def _boom(*_a: object, **_kw: object) -> object:
        raise error

    monkeypatch.setattr(gfid, "search_with_ids", _boom)
    legs, opts = _gf_legs_and_opts()
    assert cli._run_gflight_multi(legs=legs, opts=opts, cabins=(Cabin.COACH,), top_n=5) == {}
    return buf.getvalue()


@pytest.mark.parametrize(
    ("report", "build", "expected"),
    [
        pytest.param(
            _via_enriched_matrix,
            _unwritable_matrix_error,
            "QPX Warning. Bad route",
            id="matrix-prose-through-the-shared-reporter",
        ),
        pytest.param(
            _via_cabin_matrix,
            _unwritable_matrix_error,
            "QPX Warning. Bad route",
            id="matrix-prose-through-the-per-cabin-line",
        ),
        pytest.param(
            _via_enriched_gf,
            _blank_failure,
            _BLANK_NAME,
            id="a-blank-exception-through-the-enriched-footnote",
        ),
        pytest.param(
            _via_cabin_gf,
            _blank_failure,
            _BLANK_NAME,
            id="a-blank-exception-through-the-per-cabin-line",
        ),
    ],
)
def test_remote_text_arrives_visible_and_writable(
    monkeypatch: pytest.MonkeyPatch, report: Any, build: Any, expected: str
) -> None:
    """Whatever the reporter, what lands on the stream can be SEEN and can be
    WRITTEN.

    Two failures that `_assert_drives_no_terminal` cannot catch, because neither
    code point drives anything. An invisible one is read back as a value it is
    not; an unencodable one kills the write. And an exception with nothing to
    say is reported by its class name, which comes from the same remote payload
    as the message and takes the same two steps."""
    printed = report(monkeypatch, cast("Exception", build()))

    assert expected in printed, f"{expected!r} was mangled: {printed!r}"
    for ch in _UNWRITABLE:
        assert ch not in printed, f"{ch!r} reached the terminal: {printed!r}"
    _assert_drives_no_terminal(printed)
    # The assertion is that this does not raise: a StringIO accepted the lone
    # surrogate that a real stdout would have died on.
    printed.encode("utf-8")


def test_key_resolution_failing_is_a_typed_line_not_an_empty_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Matrix client resolves its API key when it is CONSTRUCTED — the disk
    cache, then the network — so a stale cache with an unreachable bootstrap
    fails on the most ordinary command there is.

    Untyped it is a traceback with both streams empty: no table, no reason. Here
    Google has nothing either, so the typed line is the whole answer and the
    exit says so."""
    from flight_cli import cli
    from flight_cli._api_key import ApiKeyResolutionError

    buf = _capture(monkeypatch)

    class _NoKey:
        def __init__(self, **_kw: object) -> None:
            raise ApiKeyResolutionError(f"could not resolve the Matrix API key{_ESCAPES}")

    def _no_gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "MatrixClient", _NoKey)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf_rows)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
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

    assert excinfo.value.exit_code == 1
    printed = buf.getvalue()
    assert "Matrix search failed" in printed, printed
    assert "could not resolve the Matrix API key" in printed, printed
    _assert_drives_no_terminal(printed)


def test_a_key_that_will_not_resolve_still_leaves_the_google_rows_on_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same contract, and the one a user notices.

    Resolving the key is the first thing the Matrix client does, so a client
    built beside the task group takes the Google half down with it: the same
    query answers with a table under `--fast` and with nothing at all by
    default. Built inside the task, a key that will not resolve is one half of
    a weave failing — the rows Google returned are painted, the reason is on
    stderr, and the exit reports what reached the user."""
    from flight_cli import cli
    from flight_cli._api_key import ApiKeyResolutionError

    buf = _capture(monkeypatch)
    rows = [cast("Any", object())]
    painted: list[Any] = []

    class _NoKey:
        def __init__(self, **_kw: object) -> None:
            raise ApiKeyResolutionError("could not resolve the Matrix API key")

    def _gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return rows

    def _paint(gf: list[Any], **_kw: object) -> None:
        painted.extend(gf)

    monkeypatch.setattr(cli, "MatrixClient", _NoKey)
    monkeypatch.setattr(cli, "_gflight_results", _gf_rows)
    monkeypatch.setattr(cli, "_render_gflight_table", _paint)

    legs, opts = _gf_legs_and_opts()
    # No `pytest.raises`: something reached the user, so the command has an
    # answer to stand behind.
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

    assert painted == rows, "the Google rows went down with the Matrix key"
    printed = buf.getvalue()
    assert "Matrix search failed" in printed, printed
    assert "could not resolve the Matrix API key" in printed, printed


def test_a_failure_after_matrix_answered_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weave stashes what it catches, and every stash has to be read.

    The transport not closing is the ordinary shape: `execute()` has already
    returned, so the answer is real and the command is right to print it — but
    the stash sat under a reader that only runs when Matrix produced NOTHING,
    which made the failure exit 0 with stderr byte-empty. It is a note beside a
    real answer, not the outcome, and not silence."""
    from flight_cli import cli
    from flight_cli.models import SearchResult

    buf = _capture(monkeypatch)

    class _AnswersThenBreaks:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _AnswersThenBreaks:
            return self

        async def __aexit__(self, *_a: object) -> None:
            raise RuntimeError(f"the transport would not close{_ESCAPES}")

        async def execute(self, _search: object, **_kw: object) -> object:
            return SearchResult.model_validate({"solutions": []})

    def _no_gf_rows(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "MatrixClient", _AnswersThenBreaks)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf_rows)

    def _no_repaint(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "_render_merged", _no_repaint)
    legs, opts = _gf_legs_and_opts()
    # Matrix answered, so this is not an exit — but it is not silence either.
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
    assert "the transport would not close" in printed, printed
    _assert_drives_no_terminal(printed)


def test_an_orderly_exit_from_inside_a_guard_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`typer.Exit` subclasses `RuntimeError` on the installed click, so every
    bare `except Exception` in this file is wide enough to catch one.

    Caught, an orderly exit becomes a message quoting its exit CODE — "could not
    be rendered: 0" — and the command carries on as though a third-party library
    had failed. Nothing raises one inside these bodies today; this is what makes
    the day one does a loud failure rather than a wrong sentence."""
    import click

    from flight_cli import cli

    assert issubclass(typer.Exit, Exception), "the guard below is only needed while this holds"

    buf = _capture(monkeypatch)
    state: dict[str, Any] = {}

    def _exits(*_a: object, **_kw: object) -> None:
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_render_gflight_table", _exits)
    with pytest.raises(click.exceptions.Exit) as excinfo:
        cli._paint_first_gf_table(
            state, [cast("Any", object())], legs=(), top_n=3, awards_only=False
        )

    assert excinfo.value.exit_code == 0
    assert "paint_err" not in state, state
    assert buf.getvalue() == "", buf.getvalue()


# Every guarded search block, and where an orderly exit can be raised inside it.
# The three shapes matter separately: a task group wraps what its HOST body
# raises as well as what its tasks do, and a worker thread's exception reaches
# the awaiting task unwrapped before the group wraps it in turn.
def _exits_with(code: int) -> Any:
    def _raise(*_a: object, **_kw: object) -> None:
        raise typer.Exit(code)

    return _raise


def _fails_with(message: str) -> Any:
    def _raise(*_a: object, **_kw: object) -> None:
        raise RuntimeError(message)

    return _raise


def _enriched(monkeypatch: pytest.MonkeyPatch) -> None:
    from flight_cli import cli

    legs, opts = _gf_legs_and_opts()
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


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
@pytest.mark.parametrize(
    "raised_in",
    ["the-google-thread", "the-matrix-task", "the-weave-body"],
)
def test_an_orderly_exit_keeps_its_code_from_anywhere_inside_the_weave(
    monkeypatch: pytest.MonkeyPatch, code: int, raised_in: str
) -> None:
    """A deliberate stop is the outcome somebody asked for, wherever it is
    raised.

    A task group hands its caller one object holding whatever left it — the host
    body's own exception included — and that object is an `Exception`, so the
    broad arm outside the group catches a stop and answers it with a backend's
    name and the wrong exit code. Three places raise it here because the three
    arrive by different routes."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    if raised_in == "the-google-thread":
        monkeypatch.setattr(cli, "_gflight_results", _exits_with(code))
        monkeypatch.setattr(cli, "MatrixClient", _refusing_matrix(RuntimeError("unused")))
        monkeypatch.setattr(cli, "_matrix_into", _no_matrix())
    elif raised_in == "the-matrix-task":
        monkeypatch.setattr(cli, "_gflight_results", _no_gf())
        monkeypatch.setattr(cli, "MatrixClient", _exits_with(code))
    else:
        monkeypatch.setattr(cli, "_gflight_results", _no_gf())
        monkeypatch.setattr(cli, "_matrix_into", _no_matrix())
        monkeypatch.setattr(cli, "_paint_first_gf_table", _exits_with(code))

    with pytest.raises(typer.Exit) as excinfo:
        _enriched(monkeypatch)

    assert excinfo.value.exit_code == code
    # Progress notes are fine; a failure line is not. The stop was asked for.
    assert "failed" not in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
def test_an_orderly_exit_never_hides_what_failed_beside_it(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """One task stops the command while another one breaks.

    Re-raising the exit alone reports the code somebody asked for with both
    streams empty — a failed half of the work wearing the face of a clean stop,
    and worst at exit 0, where a caller reading the code is told everything
    worked. The exit still decides the code; the failure is still named."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _exits_with(code))
    monkeypatch.setattr(
        cli, "_matrix_into", _matrix_task_raising(RuntimeError("the transport broke"))
    )

    with pytest.raises(typer.Exit) as excinfo:
        _enriched(monkeypatch)

    assert excinfo.value.exit_code == code
    assert "the transport broke" in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
def test_the_banner_over_an_orderly_exits_neighbours_names_no_backend(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The line that names what broke beside a deliberate stop carries a banner,
    and this group holds both backends.

    Here the stop comes from the Matrix half and the failure from the Google
    half, which is the pairing the banner gets wrong: filed under Matrix, the
    user goes and checks the backend that did as it was told."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_paint_first_gf_table", _fails_with("the table broke"))
    monkeypatch.setattr(cli, "_matrix_into", _matrix_task_raising(typer.Exit(code)))

    with pytest.raises(typer.Exit) as excinfo:
        _enriched(monkeypatch)

    printed = buf.getvalue()
    assert excinfo.value.exit_code == code
    assert "the table broke" in printed, printed
    assert "Matrix" not in printed, printed


def test_two_failures_at_once_are_both_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group can carry several, and naming the first reports half an outage as
    the whole of it: the user fixes one thing and runs the same command again.
    Naming the group instead says "unhandled errors in a TaskGroup", which is
    plumbing rather than news."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_paint_first_gf_table", _fails_with("the table broke"))
    monkeypatch.setattr(cli, "_matrix_into", _matrix_task_raising(RuntimeError("matrix broke")))

    with pytest.raises(typer.Exit) as excinfo:
        _enriched(monkeypatch)

    printed = buf.getvalue()
    assert excinfo.value.exit_code == 1
    assert "2 concurrent failures" in printed, printed
    assert "the table broke" in printed and "matrix broke" in printed, printed
    assert "TaskGroup" not in printed, printed
    # The banner names the group, and this group spans both backends: one of
    # these two leaves is the Google half, so filing them under Matrix sends
    # the user to the backend that did not break.
    assert "Matrix search failed" not in printed, printed


def test_a_google_side_failure_is_not_filed_under_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The weave's group holds both backends, so its banner cannot name one.

    The reachable shape is a broken pipe: `flight search … | head -3` closes
    stdout under the first paint while Matrix is still in flight. Reported as
    a Matrix failure, the user goes and checks a backend that was working."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_paint_first_gf_table", _fails_with("[Errno 32] Broken pipe"))
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix())

    with pytest.raises(typer.Exit):
        _enriched(monkeypatch)

    printed = buf.getvalue()
    assert "[Errno 32] Broken pipe" in printed, printed
    assert "Matrix" not in printed, printed


def test_a_matrix_error_after_matrix_answered_is_reported_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`state["matrix"]` is written by the LAST statement inside the client's
    `async with`, so a `MatrixApiError` out of `__aexit__` leaves a result and
    a failure behind at once.

    Read on one path and not the other, that is exit 0 with an answer on stdout
    and stderr byte-empty — which is the outcome `_report_weave_aftermath`'s own
    docstring forbids. Sibling of the `matrix_unexpected` case beside it, and
    the same rule: every stash held is a stash reported."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError
    from flight_cli.models import SearchResult

    buf = _capture(monkeypatch)

    class _AnswersThenRefuses:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _AnswersThenRefuses:
            return self

        async def __aexit__(self, *_a: object) -> None:
            raise MatrixApiError(f"the session had already expired{_ESCAPES}", kind="input")

        async def execute(self, _search: object, **_kw: object) -> object:
            return SearchResult.model_validate({"solutions": []})

    def _no_repaint(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(cli, "MatrixClient", _AnswersThenRefuses)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_render_merged", _no_repaint)

    # Matrix answered, so this is not an exit — but it is not silence either.
    _enriched(monkeypatch)

    printed = buf.getvalue()
    assert "the session had already expired" in printed, printed
    _assert_drives_no_terminal(printed)


def test_a_weave_that_fails_after_a_stash_reports_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two stashes, two writers, one report each.

    The Matrix task stashes what MATRIX could not do; the weave stashes what
    the GROUP could not do. Sharing a key loses whichever lands second, and
    reporting the first of the two loses one outright — the same defect as
    naming one leaf of a group and calling it the outage. The user then fixes
    one thing and runs the same command again."""
    from flight_cli import cli
    from flight_cli.client import MatrixApiError

    buf = _capture(monkeypatch)

    async def _stashes_a_matrix_error(state: dict[str, Any], *_a: object, **_kw: object) -> None:
        state["matrix_err"] = MatrixApiError("Illegal COMMAND-LINE prefix", kind="input")

    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_matrix_into", _stashes_a_matrix_error)
    monkeypatch.setattr(
        cli, "_paint_first_gf_table", _fails_with("stdout was closed while painting")
    )

    with pytest.raises(typer.Exit):
        _enriched(monkeypatch)

    printed = buf.getvalue()
    assert "Illegal COMMAND-LINE prefix" in printed, printed
    assert "stdout was closed while painting" in printed, printed


def test_a_lone_failure_inside_a_group_is_named_rather_than_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case, and the one the wrapper makes worst: one thing broke,
    and what reaches the arm outside the group is the wrapper around it."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "_gflight_results", _no_gf())
    monkeypatch.setattr(cli, "_paint_first_gf_table", _fails_with("the table broke"))
    monkeypatch.setattr(cli, "_matrix_into", _no_matrix())

    with pytest.raises(typer.Exit):
        _enriched(monkeypatch)

    printed = buf.getvalue()
    assert "the table broke" in printed, printed
    assert "TaskGroup" not in printed, printed


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
def test_an_orderly_exit_survives_the_multi_cabin_group(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The other weave with a task group in it: a cabin task that stops the
    command must not be reported as the shared client failing."""
    from flight_cli import cli
    from flight_cli.domain import Cabin

    buf = _capture(monkeypatch)
    monkeypatch.setattr(cli, "MatrixClient", _refusing_matrix(RuntimeError("unused")))

    class _ExitingClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _ExitingClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, _search: object, **_kw: object) -> object:
            raise typer.Exit(code)

    monkeypatch.setattr(cli, "MatrixClient", _ExitingClient)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_matrix_multi(
            legs=legs,
            opts=opts,
            cabins=(Cabin.COACH, Cabin.BUSINESS),
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )

    assert excinfo.value.exit_code == code
    assert "failed" not in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
def test_an_orderly_exit_survives_the_single_matrix_run(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """`_run` is the plainest of them and takes the same insurance: the arm
    below its exit arm is wide enough to catch one."""
    from flight_cli import cli

    buf = _capture(monkeypatch)

    class _ExitingClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _ExitingClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, _search: object, **_kw: object) -> object:
            raise typer.Exit(code)

    monkeypatch.setattr(cli, "MatrixClient", _ExitingClient)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
        cli._run(cli.SpecificDateSearch(legs=legs, options=opts), 1.0, "chrome", True)

    assert excinfo.value.exit_code == code
    assert "failed" not in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0", "exit-3"])
def test_an_orderly_exit_survives_the_google_only_path(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Both of its guards: the query and the renderer. Neither runs under a task
    group, so what has to hold here is only that the arms do not swallow it."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    legs, opts = _gf_legs_and_opts()

    monkeypatch.setattr(cli, "_gflight_results", _exits_with(code))
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_gflight_path(legs=legs, opts=opts, top_n=3, json_out=False)
    assert excinfo.value.exit_code == code

    monkeypatch.setattr(cli, "_gflight_results", _one_gf_row())
    monkeypatch.setattr(cli, "_render_gflight_table", _exits_with(code))
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_gflight_path(legs=legs, opts=opts, top_n=3, json_out=False)
    assert excinfo.value.exit_code == code
    assert "failed" not in buf.getvalue(), buf.getvalue()


def test_a_table_the_google_only_path_cannot_draw_is_typed_and_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--fast` has no second backend, so the renderer meeting a drifted row
    shape IS the outcome. Untyped it is a traceback with nothing on either
    stream that a user could act on; the weave beside it says the same thing in
    a sentence."""
    from flight_cli import cli

    buf = _capture(monkeypatch)

    def _rows(*_a: object, **_kw: object) -> list[Any]:
        return [cast("Any", object())]

    def _cannot_draw(*_a: object, **_kw: object) -> None:
        raise AttributeError(f"row shape drifted{_ESCAPES}")

    monkeypatch.setattr(cli, "_gflight_results", _rows)
    monkeypatch.setattr(cli, "_render_gflight_table", _cannot_draw)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_gflight_path(legs=legs, opts=opts, top_n=3, json_out=False)

    assert excinfo.value.exit_code == 1
    printed = buf.getvalue()
    assert "could not be rendered" in printed, printed
    assert "row shape drifted" in printed, printed
    _assert_drives_no_terminal(printed)


def test_a_key_failure_leaves_a_real_google_board_on_screen(
    monkeypatch: pytest.MonkeyPatch,
    gf_session: Any,
    gf_capture: Any,
) -> None:
    """The same contract, driven through the page transport and the real
    renderer rather than a stub of each: a captured board, parsed the way a
    live one would be, reaching the table while Matrix has no key at all."""
    from flight_cli import cli
    from flight_cli._api_key import ApiKeyResolutionError

    buf = _capture(monkeypatch)

    class _NoKey:
        def __init__(self, **_kw: object) -> None:
            raise ApiKeyResolutionError("could not resolve the Matrix API key")

    monkeypatch.setattr(cli, "MatrixClient", _NoKey)
    gf_session(gf_capture("ds1_jfk_lax_3rows.json"))

    legs, opts = _gf_legs_and_opts()
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
    assert "JFK" in printed and "LAX" in printed, printed
    assert "could not resolve the Matrix API key" in printed, printed


def test_the_group_level_matrix_arm_types_a_client_that_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing from a cabin reaches the group-level arm — those are caught per
    cabin — so what it catches is the shared client failing to open at all,
    which is the same key resolution the other weaves meet. Without it that is a
    raw traceback with byte-empty stderr."""
    from flight_cli import cli
    from flight_cli._api_key import ApiKeyResolutionError
    from flight_cli.domain import Cabin

    buf = _capture(monkeypatch)

    class _NoKey:
        def __init__(self, **_kw: object) -> None:
            raise ApiKeyResolutionError(f"could not resolve the Matrix API key{_ESCAPES}")

    monkeypatch.setattr(cli, "MatrixClient", _NoKey)
    legs, opts = _gf_legs_and_opts()
    with pytest.raises(typer.Exit) as excinfo:
        cli._run_matrix_multi(
            legs=legs,
            opts=opts,
            cabins=(Cabin.COACH, Cabin.BUSINESS),
            rps=1.0,
            impersonate="chrome",
            no_cache=True,
        )

    assert excinfo.value.exit_code == 1
    printed = buf.getvalue()
    assert "Matrix search failed" in printed, printed
    assert "could not resolve the Matrix API key" in printed, printed
    _assert_drives_no_terminal(printed)


def test_a_matrix_task_that_never_finishes_still_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation is a `BaseException`, so nothing stashes it and neither of
    the two reporting arms fires.

    Without a fall-through the command exits non-zero having said nothing at
    all — the outcome every reporter on this path exists to prevent, and the one
    the calendar's own reporter has had a third arm for all along."""
    from flight_cli import cli

    buf = _capture(monkeypatch)
    cli._report_search_matrix_failure({})
    printed = buf.getvalue()
    assert printed.strip(), "an empty state reported nothing at all"
    assert "did not complete" in printed, printed
