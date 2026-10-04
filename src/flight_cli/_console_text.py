"""Text from elsewhere, made ready for a markup console.

`cli.py` and `pp/cli.py` both print through markup-enabled Rich consoles, and
`cli.py` imports `pp.cli`, so the wrappers live in this leaf rather than in
either of them. Each imports them under the underscore names its `escape_scan`
knows (`tests/test_calendar_split.py`); see `docs/memories/console_sanitizing.md`.
"""

from __future__ import annotations

from rich.markup import escape

MAX_ECHOED_VALUE = 60  # characters of a rejected value worth showing back


# Characters that drive a terminal rather than appear in it, hide inside what does
# appear, or cannot be written out at all. `escape` neutralizes `[` and nothing
# else, so an ESC or CSI inside remote text still clears the screen, repositions
# the cursor, or repaints what came before it — and a redirected stderr keeps
# every byte for whatever reads the file next.
CTRL = {
    **{c: None for c in range(0x20) if c not in (0x09, 0x0A)},  # C0, keeping tab and newline
    0x7F: None,  # DEL
    **{c: None for c in range(0x80, 0xA0)},  # C1, including the 8-bit CSI
    # `str.splitlines` breaks on these two as it does on `\n`, so one message
    # carrying one arrives at a log reader or a `readlines` caller as two records.
    0x2028: None,  # LINE SEPARATOR
    0x2029: None,  # PARAGRAPH SEPARATOR
    # Bidi. The marks reorder the run they sit in and the embeddings, overrides
    # and isolates reorder everything up to their terminator, so any of them can
    # make a sentence read back as something it does not say.
    0x061C: None,  # ARABIC LETTER MARK
    0x200E: None,  # LEFT-TO-RIGHT MARK
    0x200F: None,  # RIGHT-TO-LEFT MARK
    **{c: None for c in range(0x202A, 0x202F)},  # embeddings and overrides
    **{c: None for c in range(0x2066, 0x206A)},  # isolates
    # Invisible and not whitespace, so they survive `strip()` and `split()` and
    # sit unseen inside a carrier code or a price: two values that read as equal
    # compare unequal, and nothing on the screen says why.
    0x00AD: None,  # SOFT HYPHEN
    **{c: None for c in range(0x200B, 0x200E)},  # zero-width space, non-joiner, joiner
    0x2060: None,  # WORD JOINER
    0xFEFF: None,  # ZERO WIDTH NO-BREAK SPACE
    **{c: None for c in range(0xE0000, 0xE0080)},  # tag block
    # A lone surrogate has no utf-8 encoding at all, so one in a Matrix price
    # reaches a real stdout as UnicodeEncodeError: the render of a query that
    # succeeded dies on the way out, where a console file object hides it.
    **{c: None for c in range(0xD800, 0xE000)},
}


def safe_text(value: object) -> str:
    """Remote sentence-shaped text, ready for a console: control characters
    dropped, then markup escaped.

    For text we did not write and the user did not type — a Matrix error message,
    an exception's `str()`. Neither quoted nor truncated, unlike `quote`: this is
    a sentence someone needs to read whole, and the part that explains the failure
    is as often at the end as the start.

    Strip before escape, never after. `escape` only sees a tag where `[` is
    followed by `[a-z#/@]`, so a control character between the brackets hides the
    tag from it, and stripping afterwards uncovers a live one: `"[\x00red]x"`
    comes out of the other order as `"[red]x"`, styled."""
    text = escape(str(value).translate(CTRL))
    if not text.strip() and isinstance(value, BaseException):
        # `httpx.ConnectTimeout("")` stringifies to nothing, which would leave a
        # reporter saying "Matrix calendar failed:" and stopping. The class name is
        # the only thing such an exception carries, and it takes the same two steps
        # as the message would: a class built from a remote payload can be named
        # anything. A blank from anywhere else is a value someone chose, and stays
        # blank.
        return escape(type(value).__name__.translate(CTRL))
    return text


def elide(value: str) -> str:
    """A value cut to `MAX_ECHOED_VALUE` code points, with an ellipsis if cut.

    Separate from `quote` because the cap governs the value the user typed, not
    the message around it: `repr` can double the length of a backslash-heavy
    string, so a bound on the finished message would say nothing about the input
    it is supposed to limit."""
    if len(value) <= MAX_ECHOED_VALUE:
        return value
    return value[:MAX_ECHOED_VALUE] + "…"


def quote(value: str) -> str:
    """A rejected user value, ready to interpolate into a markup console message.

    The message exists to show WHICH value was rejected, so an oversized one is
    cut: a 4301-digit `--duration` echoed whole buries its own point, and the
    parsers accept any string a shell can pass.

    Two orderings matter. `elide` before `repr`, so the cap counts characters the
    user typed rather than the quotes and escapes `repr` adds. `repr` before
    `escape`, because `repr` doubles the backslash `escape` prepends and hands the
    tag straight back to the markup parser."""
    return escape(repr(elide(value)))
