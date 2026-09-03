# Console sanitizing: what reaches a markup console, and how it is wrapped

Read before adding any print to `src/flight_cli/cli.py`. `console` and `err` are
markup-enabled Rich consoles, and `rich.table.Table` parses markup in its title
and caption, in every column header and footer, and in every cell, so any string
that is not this module's own is markup until it is wrapped. An unbalanced
`[/x]` raises `MarkupError` and loses the render of a query that SUCCEEDED; a
well-formed `[bold]` silently eats the token the reader needed; and an ESC or an
8-bit CSI repaints the terminal.

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
`_config.py:139,149` build the rps `ValueError` the same way. `repr` is there for
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
calls that fill a renderable: `Table(...)` and `Panel(...)` arguments,
`Text.from_markup`, `Console.render_str`, `add_column` and `add_row`. A bare
`Text(...)` is absent on purpose — it takes its argument literally, and naming a
constructor also exempts the name it is assigned to from the print check, so
leaving it out is what makes `console.print(Text(f"{e}"))` a fault. Reading the
fill is what makes a renderer scannable at all — the cells are where the text is
chosen, and the `console.print(t)` a hundred lines later adds none of its own.
Exempting that print instead, and never reading a cell, is the shape that hides
a MarkupError from the scan: six reproduced ones fit through that gap with the
guard green.

It judges each argument by AST shape, never by source text: a string comparison
reads `not_escape(x)` and `shell.escape(x)` as safe. A concatenation, a
conditional and an `or` are judged piece by piece, since each piece is printed on
its own. A format spec that is a single literal ending in a numeric presentation
type of `d` or `f` and holding no `%` (`{price:.2f}`, `{i:d}`) proves the field is
a number, because a string reaching it raises — a proof about the value, unlike
allowlisting a name off a duck-typed object. The `%` clause is what keeps
`{when:%Y-%m-%d}` out: `date.__format__` is `strftime`, so a date survives every
presentation type and comes back a string. An object with its own `__format__`
survives them too, so for such a value this is not a proof; none reaches a
numeric spec in `cli.py` today.

There is no per-function exemption. One would pre-approve every FUTURE print in a
function rather than one value, and every MarkupError this guard has caught
arrived behind one. What a function may print without a wrapper is said one
identifier at a time in `_PRINTABLE_IDENTIFIERS`, keyed per FUNCTION since `n` is
a fan-out counter in one place and could be anything in another. A decorator and
a default argument belong to the scope around the `def`, because that is where
they run.

Every list the scan consults is checked by a test that breaks it.
`_PRINTABLE_IDENTIFIERS` has one: no entry is inert — delete any entry and the
scan speaks, so an entry that allows nothing cannot sit there pre-approving
whatever later takes its name. What an entry does NOT get is a check on the
value behind it. The scan reads a name's binding only when it is a top-level
f-string over a bare name, which no binding in `cli.py` is, so an entry is a
claim held by the hostile-field tests: give a new one an arm that fails when the
value stops being this module's own, or wrap at the sink instead, as
`title_prefix` is, because a parameter's value belongs to callers the scan never
reads. `_SAFE_WRAPPERS`, `_NUMERIC_PRESENTATION` and `_RENDERABLE_SINKS` each
have a delete-one test — measured over the bypass corpus as well as `cli.py`,
since dropping a member makes one speak and the other go quiet — and every text
sink has a probe that goes silent without it. A regression corpus of one
synthetic source per known bypass keeps the scan itself honest. A printed table
needs no entry at all: the scan reads the assignment and asks whether this scope
built a renderable, which is a claim about the binding rather than about the
name.

The scan reads CALLS, so a markup slot filled by assignment (`t.title = x`,
`t.caption = x`, `t.columns[0].header = x`) or by an API it does not name is not
read; none is live in `cli.py` today, and `Panel` sits in `_RENDERABLE_SINKS`
unimported, so an aliased import of it would have coverage that looks present and
is not. It models scope only as far as the INNERMOST function: an allowlisted
identifier is a claim about a NAME in one body, so a closure that shadows the name
with a parameter or binds it to something else is scanned like any other function.
What it cannot tell apart is two bodies of the same name, which share their
entries — the two `query_cabin` closures printing `cab.value` are that shape on
purpose, and the entry is keyed on the closure that prints it.
The hostile-field tests — one payload per response field, driven one field at a
time through each renderer — are what pin the values themselves.

A Typer `help=` / `epilog=` string is a markup sink as surely as a table cell:
the app sets `rich_markup_mode="rich"`, so Typer renders every help string
through `Text.from_markup` — or `Text.from_ansi`, when the string holds a
control character — on the way to the terminal. The scan reads those strings, in
`typer.Option` / `typer.Argument` / `typer.Typer` calls, but NARROWER than it
reads a print: an f-string field that is a call or an attribute read, which is
where a runtime value comes from. A bare name in one is not read — these strings
are built at module scope, where there is no function to key an allowlist entry
on, and every name in one today is a constant of literal text this file wrote —
so a module constant that stops being literal is what this boundary leaves
uncovered. The wrapper is `_safe_text` for the usual reason plus one of its own:
an ESC survives `escape` and sends the whole string down the from-ANSI branch,
which eats the sequence and renders a path nobody configured. And a literal tag
that is meant to READ as text — `\[providers.<name>]` — takes the backslash,
or the parser takes it for a style and the reader sees nothing where the name
should be.
