# Wire format quirks

Field-level rules for `wire.py → to_wire()` that aren't obvious from the
code. Each item is something that already burned us or would burn a future
edit.

## Two slice fields for "routing", not one

Matrix's slice has **two distinct fields** that the SPA URL state names
differently than the API:

| User UI field | SPA URL state | API request field | What goes in |
|---|---|---|---|
| Routing language box | `routing` | `routeLanguage` | `"LH+"`, `"BA AA"`, `"[F* X F*]"` |
| Extension codes box | `ext` | `commandLine` | `"MAXCONNECT 5:00"`, `"MAXSTOPS 1"` |

**Failure mode**: putting `"LH+"` in `commandLine` returns:
```
{"error":{"message":"QPX Warning.  Illegal COMMAND-LINE prefix: LH+", "type":"input"}}
```
The error is HTTP 200 with the failure in the body — `MatrixApiError`
catches it.

## Per-mode field rules

| Field | specific | calendar | followup |
|---|---|---|---|
| `slices[].date` | always | omitted | always |
| `slices[].dateModifier` | always (even `{0,0}`) | omitted | omitted |
| `slices[].isArrivalDate` | always (even `false`) | omitted | omitted |
| `inputs.filter` | `{}` | `{}` | OMITTED |
| `inputs.page` | `{current:1, size}` | `{size}` | `{current:1, size}` |
| `inputs.startDate / endDate` | omitted | required | required |
| `inputs.layover` | omitted | round-trip only | round-trip only |

`inputs.layover` is the trip-LENGTH range (nights between the outbound and the
return), so a one-way body has nothing to measure — and Matrix does not ignore
it: a one-way `calendar` or `calendarFollowup` carrying it comes back HTTP 200
`"Internal server error"` (work-h70kv.7).

Golden-file tests at `tests/fixtures/` will fail loudly if you over-emit
or under-emit these — except the `layover` row. Every captured fixture is
round-trip or `wholeTrip`; none is `calendarOneWay`, so the fixtures cannot see
that row. Its guard is `tests/test_wire_round_trip.py`:
`test_one_way_calendar_omits_trip_length`, `test_one_way_followup_omits_trip_length`
and `test_round_trip_calendar_keeps_trip_length`.

## Summarizer ORDER matters for tests (not for the server)

The server doesn't care about the order of the `summarizers` array, but
the captured SPA bodies use a specific order:

```python
["carrierStopMatrix", "currencyNotice", "solutionList", "itineraryPriceSlider",
 "itineraryCarrierList", "itineraryDepartureTimeRanges",
 "itineraryArrivalTimeRanges", "durationSliderItinerary", "itineraryOrigins",
 "itineraryDestinations", "itineraryStopCountList", "warningsItinerary"]
```

If you reorder, golden-file tests break. Either match the order or rebase
the fixtures.

## `maxLegsRelativeToMin` default = 1

The SPA's "No limit" stops setting maps to `maxLegsRelativeToMin: 1` on
the wire — i.e., up to 1 connection. Higher values DO work but exceed
what the consumer UI permits. Don't pass anything > 2 in production
without checking that Matrix's backend accepts it.

## `timeRanges` accepts arbitrary minute-granular ranges

The UI offers 6 fixed buckets, but the API takes any well-formed
`[{min: "HH:MM", max: "HH:MM"}, …]`:

- Both zero-padded (`"08:00"`) and single-digit (`"8:00"`) hours work
- Off-bucket ranges work (`{min:"9:30", max:"13:45"}` → 38 sols vs 64
  with no filter)
- Multi-range arrays work (`[{morning}, {evening}]`)
- `min < max` is required; `{min:"14:00",max:"10:00"}` errors with
  `QPX Warning. Unexpected format for DEP-TOFD-RANGES-LOCAL`
- Field name is **`timeRanges`** on the slice, NOT the SPA URL state's
  `departureDatePreferredTimes` (those are different things)

The `TimeOfDay` enum exposes the 6 named buckets. If we want
power-user arbitrary-range support, expose a parser that accepts
`"09:30-13:45"` in addition to the named values.

## `commandLine` order quirk in the SPA-state URL

The deep-link URL state for calendar mode folds round-trip into a SINGLE
slice with `routing/ext` for outbound and `routingRet/extRet` for return.
The API request, however, uses TWO slices (one per direction) and each
has its own `routeLanguage/commandLine`.

`links.matrix_deep_link()` collapses; `wire.to_wire()` expands. Don't
confuse the two shapes.

## SPA-side `slices[1].commandLine` is always None

Even when the SPA's URL state has a non-empty `routingRet`/`extRet`, the
API request only sets `routeLanguage`/`commandLine` on `slice[0]` (the
outbound). The return slice's routing is **inferred** by the server from
slice[0]. We mirror that: `wire._leg_to_wire()` sets fields per leg
faithfully, but when reconstructing from captured fixtures, expect
slice[1] to lack routing.

## Field-validation comes back as HTTP 200 (not 4xx)

All input validation errors arrive as `{"error": {"message": ..., "type": "input"}}`
inside a 200 response. The HTTP layer doesn't see them; `_raise_if_api_error`
in client.py catches and re-raises as `MatrixApiError` with the message
intact. **Don't skip this check** — quietly-empty responses are a real
failure mode otherwise.

## Calendar `solutionCount: 0` can mean three different things

1. **Real timeout**: query is too complex; server gives up. UI shows
   "Query Timeout" modal. We see an empty grid + 0 sols.
2. **Brownout**: temporary backend degradation. Same query works
   minutes later. Not specific to us — affects the real SPA too.
3. **No flights matched**: the search is valid but no fares exist for
   that combination.

These are indistinguishable from the response. Surface a helpful message
("Calendar empty. Matrix's calendar mode brownouts regularly; retry") and
include the deep-link URL so users can verify in the UI.

A fourth case is NOT one of these and must not wear that message: a fanned-out
multi-airport calendar whose sub-queries never answered. There is no response to
be ambiguous about — the merge is empty because nothing reached it. So
`_run_calendar` merges first and judges what SURVIVED, not what fraction failed:
rows still in the merged grid are worth reading even short a destination, and get
a note beside them saying how many are missing; no rows at all is a refusal: exit
1, and a stderr report that names the count and then every cause that stood
behind it, a backend error keeping its kind and request id. Judging the
merge rather than the fraction is what keeps "Matrix priced this window and found
nothing" a claim only the destinations that answered can support.

## What a black-box caller can read off `flight calendar`

Exit 0 means a document on stdout: the rendered grid, or the object `--format
json` writes. Exit 1 means the run failed: stderr carries one typed failure
report, whether the cause was a Matrix error, a backend that could not be
reached, or the fan-out refusal above. Stdout is empty for every cause that
happened before the answer was written, with two exceptions. A raise inside the
write itself, in the renderer or in the URL emitter, exits 1 over whatever had
already reached stdout, because the two are guarded as one act; and a weave
whose first paint drew the whole grid and then raised on the status line under
it exits 1 over a document that is complete. That report is one line or several:
a Matrix error adds the backend's kind and message, and its request id when
there is one, on the lines below the first; a fan-out names every sub-query that
dropped; and `--fast` says why there is no grid before it says there is none.
Its first line names the backend that failed rather than the command, so a
`--fast` run reports "Google Flights date grid failed" where every Matrix path
reports "Matrix calendar failed" — there is no Matrix behind `--fast` to blame.
Read the whole stream, not the first line. Everything a weave prints while it
still has nothing to show — the gate note, a throttle, the wait for Matrix — is
on stderr for the same reason: stdout carries the answer or nothing. Exit 2 is
an input refusal, raised before any Matrix call.

Partial sub-query coverage is the one middle state, and it is a stderr NOTE
beside a complete-looking stdout document: exit 0, a grid, and a line saying how
many origin/destination groups are missing from it.
The JSON carries no field for that, so a caller that must know whether the grid
is whole reads stderr, or asks for one destination at a time.

Four readings cover every calendar that ran. Exit 1 with stdout empty is no
answer at all: the window was never priced, and stderr says why. Exit 0 with
"Calendar empty." on stdout IS an answer — Matrix searched the window and priced
nothing in it — which is why a fan-out that lost a destination refuses rather
than printing that sentence. Exit 0 with a grid is the answer,
possibly a partial one, and stderr is where everything that qualifies it goes:
the coverage note above, and a failure the weave stashed AFTER the answer was
written — a client teardown, a first paint that died once Matrix had already
answered — which stays a line rather than an exit code because what the reader
was given still stands.

Exit 1 with something on stdout is the fourth, and the narrowest: the answer was
not delivered, and what is there is either a fragment — the write itself raised
part-way through — or a whole grid the weave painted before the status line
under it failed. The exit code says the run failed, not that the bytes are
partial, and the line on stderr says which half broke. Distinguish it from the
reading above by the exit code, not by the stream — a failure that follows a
finished write is a line and exit 0; a failure inside the write is a line and
exit 1.

A reader that hangs up is that fourth shape with nobody left to read it: exit 1,
stdout partial or empty, and no failure report at all on stderr — only whatever
the run had already written there before the reader left. Rich answers a broken
output pipe with `SystemExit`, which is not the `Exception` every guard on these
paths catches, so no report is ever composed — there is nowhere to put the
answer and nothing is said about why. What was already said still stands, and it
is not the same on every arm: a plain or `--fast` calendar leaves stderr empty,
while a fan-out has printed its provenance note and a weave its gate or status
line before the delivery the reader killed. A caller that pipes into `head` sees
this, and it is indistinguishable from the reading above on stdout alone.
