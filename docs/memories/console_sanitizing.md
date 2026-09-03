# Console sanitizing: what reaches a markup console, and how it is wrapped

Read before adding any print to `src/flight_cli/cli.py`. `console` and `err` are
markup-enabled Rich consoles, and `rich.table.Table` parses markup in its title,
in every column header and in every cell, so any string that is not this module's
own is markup until it is wrapped. An unbalanced `[/x]` raises `MarkupError` and
loses the render of a query that SUCCEEDED; a well-formed `[bold]` silently eats
the token the reader needed; and an ESC or an 8-bit CSI repaints the terminal.

## Where the values come from

The routing refusal quotes the user's `--routing` / `--extension` text verbatim:
`--routing 'BA[/weird]AA'` raised `MarkupError` where it should have refused, and
a `[bold]` form ate the token the reader needed to see. `routing_predicates` has
no console to escape for, so the sanitizing belongs at the render sites. The same
holds for Matrix's `kind` / `message` / `request_id`, which echo the routing
string back verbatim ("Illegal COMMAND-LINE prefix: BA[/weird]AA") on the path
with no refusal to catch it first, for every response field a renderer shows, and
for whatever an undocumented third-party library raised.

## Three steps, in three places

**A source that quotes a value into a sentence keeps its `repr`.**
`routing_predicates.py:284,331,343` build their reasons with `{...!r}`, and
`_config.py:136,146` build the rps `ValueError` the same way. `repr` is there for
the reader — it shows the exact string that was rejected, quotes and all — and it
happens to neutralise ESC, C1 and DEL on the way. It is not the guard, because it
does not cover the value that reaches a console any other way.

**A formatter that owns a whole field sanitizes inside itself.** `_amount` is the
model: Matrix chooses the entire price string, every caller drops the result into
a table cell or a summary line, and wrapping at each of those call sites is a
rule someone will forget. Wrapping leaves inside `_fmt_slice_route`,
`_fmt_slice_times`, `_fmt_legroom_one`, `_leg_display` and
`_fmt_gflight_legroom` is the same move one level down — and it has to be the
LEAVES, not the composed cell: `_fmt_legroom_one` writes a real `[red]` around a
below-average pitch, and one wrap around the finished cell would print that tag
instead of colouring the number.

**The render site is the guard, and it wraps exactly once.** The rule, for
everything `tests/test_calendar_split.py::escape_scan` covers: anything reaching
a markup sink from user input, a response field or an exception message goes
through `_quote` (a value the user typed: elide, `repr`, escape) or `_safe_text`
(anything remote: strip the control characters, then escape). Bare
`rich.markup.escape` is neither and is never sufficient — it neutralises `[` and
leaves every ESC, 8-bit CSI, bidi control and lone surrogate in place. Every
Matrix error goes through `_print_matrix_error`, so one backend error reads the
same whichever command asked for it; the per-cabin fan-out is the one deliberate
exception, because its failure is soft and its line names the cabin.

## `_safe_text` and `_quote`

`escape` is not the whole job for text from somewhere else. It neutralises `[`
and nothing more, so an ESC or an 8-bit CSI inside a Matrix error message still
clears the screen or repaints the line above it, a DEL rubs out what precedes it,
and a bidi override reorders the rest — and a redirected stderr keeps every byte
for whatever reads the file next. `_safe_text` drops those code points (C0 bar
tab and newline, DEL, C1, the two separators `str.splitlines` breaks on, the bidi
marks, overrides and isolates, the invisibles that survive `strip()`, the tag
block, and the lone surrogates, which have no utf-8 encoding at all and reach a
real stdout as `UnicodeEncodeError`) and then escapes — in that order, because
`escape` only sees a tag where `[` is followed by `[a-z#/@]`, so a control
character between the brackets would hide a live `[red]` from it. It neither
quotes nor truncates, unlike `_quote`: a remote error is a sentence someone has
to read whole, and the half that explains the failure is as often at the end as
at the start.

The argument parsers go through one `_quote` helper: `_elide` cuts a value past
60 characters, then `repr`, then `escape`. The message exists to show WHICH value
was rejected, and a 4301-digit `--duration` echoed whole buries that under its
own evidence. Both orderings are load-bearing. `_elide` before `repr`, so the cap
counts what the user typed — a backslash costs one code point going in and two
coming out of `repr`, so a bound on the finished message would measure the fill
rather than the cap. And `repr` before `escape`, because `repr` doubles the
backslash `escape` prepends and hands the tag straight back to the parser.

## The guard: `tests/test_calendar_split.py::escape_scan`

It parses ONE file — `src/flight_cli/cli.py` — and walks its AST, so a new print
in a covered function fails the suite. It says nothing about any other module:
`src/flight_cli/pp/cli.py` builds a second markup console and is not scanned
(work-h70kv.19).

Its polarity is inverted — everything is scanned unless excluded by name —
because an opt-in list goes stale the moment a print moves into a new helper. A
sink is `console.print` / `.log` / `.rule` / `.status` / bare `print`, AND the
calls that fill a renderable: `Table(...)` / `Panel(...)` / `Text(...)`
arguments, `add_column` and `add_row`. Reading the fill is what makes a renderer
scannable at all — the cells are where the text is chosen, and the
`console.print(t)` a hundred lines later adds none of its own. (An earlier shape
exempted that print instead and never read a cell; six reproduced `MarkupError`s
came through it while the guard stayed green.)

It judges each argument by AST shape, never by source text: a string comparison
reads `not_escape(x)` and `shell.escape(x)` as safe. A concatenation, a
conditional and an `or` are judged piece by piece, since each piece is printed on
its own. A format spec that is a single literal ending in a numeric presentation
type (`{price:.2f}`, `{i:d}`) proves the field is a number, because a string
reaching it raises — which is a proof about the value, unlike allowlisting a name
off a duck-typed object.

Two allowlists, and each has a delete-one test proving no entry is inert.
`_PRINTABLE_IDENTIFIERS` (this module's own values) is keyed per FUNCTION, since
`n` is a fan-out counter in one place and could be anything in another;
`_ESCAPE_OUT_OF_SCOPE` is keyed on the TOP-LEVEL function, so a nested helper
cannot pick one up by reusing a name — while a decorator and a default argument
belong to the scope around the `def`, because that is where they run. Each
exclusion's reason must name every identifier its function prints, matched on
word boundaries; a corpus of deliberately vacuous reasons proves that test can
fail. A regression corpus of one synthetic source per known bypass keeps the scan
itself honest.

What it does not model is scope: an allowlisted identifier is a claim about a
NAME in a function, so a closure inside inherits the pass and a second binding of
the name is invisible. `_run_calendar` is the only live shape with both. The
hostile-field tests — one payload per response field, driven one field at a time
through each renderer — are what pin the values themselves.
