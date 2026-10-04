# `--format envelope`: the one document `search` and `calendar` write on every path

`--format json` writes the answering path's own document: Google rows, Matrix's
raw body, `{cabin: …}` over several cabins, the award document when awards run,
or nothing at exit 0 when the award query fails. A caller has to know the path
before it can parse the answer, and what the path lost (a cabin, a calendar
sub-query, an award provider) is said on stderr only. The envelope has the same
keys whatever answered. It is built in `src/flight_cli/_envelope.py`, and
its schema is `docs/envelope.schema.json`, generated from the models
(`schema_text()`; a test fails when the two differ).

## The contract

| key | type | what it holds |
|---|---|---|
| `version` | `1` | bumped on any change a reader must handle |
| `command` | `"search"` / `"calendar"` | the discriminator |
| `backend` | `"gflight"` / `"matrix"` / null | who answered; null when nothing did |
| `currency` | string / null | the one currency every priced row shares, else null |
| `complete` | bool | false when the run exits 1 or the answer is narrower than asked |
| `notes` | list of strings | every non-blank stderr line of the run (ANSI removed, in order), then one line per null or empty key, `key: why` |
| `results` | search: `[{cabin, rows}]`, one per `--cabin` in order; calendar: `rows` | `{price, currency, row}`, where `row` is the object `--format json` prints, unchanged |
| `awards` | list / null | the award document's per-leg entries; each match also carries `flights`, every flight of its slice. Null when no award search ran or it failed |
| `insight` | `[{cabin, currency, cheapest, typical_low, typical_high, level}]` | one per Google page that carried one |
| `price_history` | `[{cabin, currency, points: [{date, price}]}]` | one per Google page that carried one |
| `verify` | object / null | `--verify`'s check of row `--pick`, the `verify` object `--format json` prints; null when not asked, on a calendar, or when the check failed (exit 1) |
| `cross_check` | object / null | `--enrich`'s Google-vs-Matrix comparison, the `cross_check` object `--format json --enrich` prints; null when not asked, skipped, on a calendar, or when Matrix's half failed |

`price` is the trip's: a Google round trip's is its return member's, the fare
every surface prints for the pair; Matrix's is the solution's price string read
as a number.

Exit codes are those of `--format json` in the same state, and an envelope is
written at exit 0 and 1. Exit 2 is a usage error and writes none.

## What makes `complete` false

Each narrowing calls `_envelope.narrow()` where it happens; outside an envelope
run the call does nothing. A site that writes no stderr line passes a note,
which joins `notes` after the stderr lines, so table and JSON output gain no
line. The sites: a cabin asked and never recorded (judged
in the recorder, from `ask_cabins` against what the leaves recorded); Matrix
finding nothing where Google had rows (`_note_google_rows_unshown`); the
round-trip pin cap note; return boards refused or pinning stopped with a board
served (`_gflight_ids._report_pin_outcome`); PointsPath skipped when it was
asked for (named in `--providers`, or tokens present that then failed;
with no tokens it is a note), both in `_pp_preflight` and at the award gate
(`cli._explain_no_awards`), where failed tokens read as no provider at all;
any other provider `--providers` names that has no credentials, at the award
gate (`cli._should_run_awards`), whether or not another provider runs;
the award query failing; the Matrix half of an `--enrich` cross-check
failing; every award failure the search names in its `Awards incomplete:`
line, which is then the note: each `record_failure` call has a `narrow()`
beside it (`providers/registry.py`, seats.aero, and a PointsPath airline search
that is not "unsupported" in `pp/client.py`, an error status with an empty
body and a request the award deadline cut included); a leg with
`pairs_not_asked`; calendar sub-queries lost;
`--max-per-query > 1` over a split, and over the one unsplit query when a
group holds every destination; a round trip over a split set, whose returns
into another airport of the set come only from the combined query; Google rows on a
search page the parser could not read (not the calendar graph's wall check,
which answers nothing). A hand-off
to Matrix, rows in another currency and a filter that empties a board are
notes, not narrowings: each is a complete answer to what was asked.

## How the run is held

`cli._envelope_command` decorates `search` and `calendar`. Under `--format
envelope` it runs the command through `_envelope.run`, which swaps the process
streams for the run. Stdout goes to a buffer, so a path that writes past the
recorder cannot put a second document beside the envelope: its text is dropped
and a `stdout:` note says so, which the tests treat as a failure. Stderr goes
through a tee that keeps a copy for `notes`. `cli.err` and `pp.cli.err` run
with `soft_wrap` on, so one message is one note. The recorder is module state,
not a context variable, because the cabin fan-outs record from worker threads.
A `typer.Abort` is said inside the run (`cli._said_abort`) and ends as exit 1:
click would print "Aborted." only after the run stopped hearing stderr.

Every path runs as under `--format json`, and each JSON leaf calls the recorder
in place of its `sys.stdout.write`. With awards on, the leaf records its cash
rows and `run_pp_for_search` records the awards. `--verify` records its check
under `verify` (`_envelope.record_verify`), and `--enrich` records Google's rows
under `results` and its comparison under `cross_check`
(`cli._answer_cross_check_document`), each with the same refusals as under
`--format json`. Where Google's half failed, `results` and `backend` stay empty,
since the rows are Google's, and their notes say `cross_check` holds Matrix's
rows alone. `--sellers` and `--fare-rules` write a document of their own
and are refused with the envelope (exit 2, before any request); a new
document-writing flag has to go inside the envelope under its own key, never
beside it.

## Price history

`ds:1[5][10][0]` is `[[epoch_ms, price], ...]`, one point a day, oldest first,
read by `_gflight_ids._price_history` beside `_price_insight` (`ds:1[5]` holds
both). The stamps are 04:00 UTC on both captures, the requesting client's local
midnight, so the day is the UTC date twelve hours after the stamp. The
JFK-LAX full-board capture holds 61 points, 2026-07-29 at 169 to 2026-09-27 at
204; JFK-LHR 62, 2026-07-28 at 289 to 2026-09-27 at 293. The currency is read
off a priced row of the page, as the insight's is. The series is the route's,
so a routing filter that restates or drops the insight leaves it as served,
and it rides on `Board.history` through every board the page builds. It costs
no request: it is on the page the search already fetched.

## Out of scope

Envelopes for `detail`, `explore`, `fare`, `gflight` and `doctor` (each
refuses `--format envelope` naming the two commands); `--sellers` and
`--fare-rules` inside it; ISO dates for Matrix calendar days (Matrix's raw
months carry no `month` key and pad weeks with disabled days, so a day's date
is not derived here); Google's facets (`ds:1[7]`); an exit code of its own for
a partial answer (`complete` says it).
