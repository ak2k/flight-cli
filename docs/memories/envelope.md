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
| `version` | `1` | bumped on any change a reader must handle; a new key is not one, but the schema forbids keys it does not name, so a reader validating against an older `docs/envelope.schema.json` regenerates it |
| `command` | `"search"` / `"calendar"` | the discriminator |
| `backend` | `"gflight"` / `"matrix"` / null | who answered; null when nothing did |
| `currency` | string / null | the one currency every priced row shares, else null |
| `complete` | bool | false when the run exits 1 or the answer is narrower than asked |
| `notes` | list of strings | every non-blank stderr line of the run (ANSI removed, in order), then one line per null or empty key, `key: why` |
| `results` | search: `[{cabin, rows}]`, one per `--cabin` in order; calendar: `rows`, one per priced day, none when the table prints the grid as empty | `{price, currency, row}`, where `row` is the object `--format json` prints, unchanged |
| `awards` | list / null | the award document's per-leg entries; each match also carries `flights`, every flight of its slice. Null when no award search ran or it failed |
| `insight` | `[{cabin, currency, cheapest, typical_low, typical_high, level}]` | one per Google page that carried one; a leg asked as several pages gives one per page, in page order |
| `price_history` | `[{cabin, currency, points: [{date, price}]}]` | one per Google page that carried one, as `insight` |
| `facets` | `[{cabin, origins, destinations, currency, price: {low, high}, duration_minutes: {low, high}, layover_minutes: {low, high}, airlines: [{code, name}], alliances: [{code, name}], connecting_airports: [{code, city}]}]` | the filter choices Google's page states for the search (see "Route facets"): one per page of the search's own board that carried them, labeled with its cabin and airports; a round trip's from its outbound page alone |
| `price_graph` | `[{trip_length, currency, cells: [{departure, return, price}]}]` | a calendar's Google price graph, one entry per trip length that priced (`trip_length` and `return` null on a one-way); each cell is Google's estimate for one date pair, with no itinerary behind it. Empty, with its note, on a search, a calendar that did not ask it (no `--gf-transport browser` or `auto`, `--gf-transport http`, or a refusal by the graph's gate, named) and one whose graph failed |
| `verify` | object / null | `--verify`'s check of row `--pick`, the `verify` object `--format json` prints; null when not asked, on a calendar, or when the check failed (exit 1) |
| `cross_check` | object / null | `--enrich`'s Google-vs-Matrix comparison, the `cross_check` object `--format json --enrich` prints, `low_check` included; null when not asked, skipped, on a calendar, or when Matrix's half failed |
| `split_ticket` | object / null | `--split`'s answer, the `split_ticket` object `--format json --split` prints: on a round trip the pair (`{outbound, return, total, currency}`), or `{error}` naming why there is none. A multi-city search's combinations (two or more `--slice` that are not a round trip; `{currency, combinations}`) or `{error}` on every envelope run, `--split` or not, since its table lists them without the flag. Under `--backend gflight` they are the whole answer: `backend` is `gflight`, `results` holds one empty entry whose note says the tickets are in `split_ticket`, and `currency` is null. Null on a round trip not asked, on a calendar, or when Matrix answered a round trip |

`price` is the trip's: a Google round trip's is its return member's, the fare
every surface prints for the pair; Matrix's is the solution's price string read
as a number, for a party the total Matrix states (`party_price`), the number its
itinerary table prints and `--max-price` reads, as Google's row prices the whole
party. A party's solution Matrix states no total for has a null `price`: one
passenger's price is not the trip's, and its `row` still carries it.
A multi-cabin Google search lists, under each cabin, its `-n` cheapest rows,
then, in price order, each other row of its board whose fare the table prints
in that cabin, and the cabin's cheapest in the requested currency (in the
currency Google priced it in, for a cabin priced in none of the requested one),
which the line under the table names when no row shows it. The table prices
every cabin on the sort cabin's itineraries, so a fare it shows can sit far
down another cabin's board, and the `-n` cheapest are cheapest by amount, so
rows Google priced in another currency can come before it; the rows added are
read off the boards already fetched. A multi-cabin Matrix search lists each
cabin's whole answer.
A `calendar --fast` trip-length range writes every priced length's cells to
`results` as one list, each the object the range document's `graphs[].grid`
prints; its `return` date names its length. `price_graph` carries the same
cells, one entry per length.

Exit codes are those of `--format json` in the same state, and an envelope is
written at exit 0 and 1. Exit 2 is a usage error and writes none.

## What makes `complete` false

Each narrowing calls `_envelope.narrow()` where it happens; outside an envelope
run the call does nothing. A site that writes no stderr line passes a note,
which joins `notes` after the stderr lines, so table and JSON output gain no
line.

A narrowing of Google's answer passes `of="gflight"`, and it counts toward
`complete` unless Matrix answered the search (`_envelope._document`, against
the backend that recorded rows). So after a whole search goes to Matrix,
`complete` is Matrix's answer's: a return board refused, a pin loop stopped, a
Cheapest tab unread or Google rows the parser could not read stay their stderr
line or note. The Google sites are every `narrow` in `_gflight_ids` (a test
fails on one that names no backend) and, in `cli`, the pin cap note, the
infant's empty board, a `partial` board, unread rows, the unread Cheapest tab
and a round trip's failed `--split` one-way. A hand-off that holds Google's board (emptied
by the filter, an infant's empty board, separate-ticket rows alone on an award
search, a multi-cabin search) notes its unread rows as `_record_google_cabin`
does. Matrix, provider, calendar and `--split`-on-Matrix narrowings name no
backend and count whoever answers, and so do a multi-city search's one-way
boards (`cli._one_way_boards` with `narrow`): their tickets are shown beside
Matrix's answer, or alone under `--backend gflight`, never as a one-ticket
row. The sites: a cabin asked and never recorded (judged
in the recorder, from `ask_cabins` against what the leaves recorded); Matrix
finding nothing where Google had rows (`_note_google_rows_unshown`); the
round-trip pin cap note; return boards refused, a pin Google served no return
board for, or pinning stopped, each with a board served
(`_gflight_ids._report_pin_outcome`): the outbound board priced round trips
through that pin, so they are missing; PointsPath skipped when it was
asked for (named in `--providers`; with no tokens it is a note), both in
`_pp_preflight` and at the award gate (`cli._explain_no_awards`); tokens
that fail to refresh are not a skip: the refresh runs in the provider build
and its failure is an award failure (the `Awards incomplete:` line, below);
any other provider `--providers` names that has no credentials, at the award
gate (`cli._should_run_awards`), whether or not another provider runs;
the award query failing; the Matrix half of an `--enrich` cross-check
failing; every award failure the search names in its `Awards incomplete:`
line, which is then the note: each `record_failure` call has a `narrow()`
beside it (`providers/registry.py`, seats.aero, and a PointsPath airline search
that is not "unsupported" in `pp/client.py`, an error status with an empty
body and a request the award deadline cut included); a leg with
`pairs_not_asked`; calendar sub-queries lost; a length of a `calendar --fast`
range whose graph was lost;
a one-way search that failed, on a `--split` round trip or on any multi-city
search (a multi-city search's table lists its tickets with no flag), where no
priced one-way, no pair one traveler can fly, or two currencies are the boards'
answer, a note; a `--split` round trip Matrix answered, or a `--split`
multi-city search Google was not asked about, since the tickets were asked for
and are not priced, while without `--split` that search's `{error}` narrows
nothing, as its table lists no tickets either; a multi-city search's one-way
board missing a page or holding rows the parser could not read
(`cli._one_way_boards`, the unread rows a note naming the slice), since its
cheapest tickets may be among them, or holding no row at all for a party with
an infant, as on a Google search;
`--max-per-query > 1` over a split when a query asks several destinations, and
over the one unsplit query when a group holds every destination; a round trip
over a split set, whose returns
into another airport of the set come only from the combined query; a
departure date of the calendar's window that Matrix's grid holds no fare for
at some trip length asked (`cli._say_unpriced`, one `Matrix priced no fare on
…` line per length), which may be a day with no service, since the grid
cannot tell the two apart; Google rows the
parser could not read, counted once from the board's `unread` where its rows
are recorded (`cli._record_google_cabin`), so the note gives the number
`cross_check.google.unread` does (the calendar graph's wall check records no
board and narrows nothing); a Google board served with no rows for a party
with an infant and not handed to Matrix (`cli._run_gflight_path`) and, under
`--enrich --format envelope`, a board with no rows for such a party
(`cli._answer_cross_check_document`), whose `results` note names the infant
and, when Matrix answered, says `cross_check` holds Matrix's rows, since Google
has served such a board on a route with flights; a Google board with rows
asked as several pages that is `partial` (`cli._gflight_pages`): a page did not
answer, or the trip is round and each return flies back between its own
page's airports; Google's Cheapest tab unread (`cli._note_separate_tickets`,
whose `Itineraries on separate tickets not read: …` line is the note), since
it may list itineraries on separate tickets the user did not opt out of. A
hand-off
to Matrix, rows in another currency, a filter that empties a board, rows
Google served over the stop ceiling asked for (`cli._note_stop_drops` counts
them), a board shown at Google's 300-row cap (`cli._note_row_cap`, never said
for a board handed to Matrix), a pin whose return board the row filter emptied,
the count of
itineraries `--no-separate-tickets` hid and a Cheapest tab left unread for a
return check its rows cannot be held to are notes, not narrowings: each is a
complete answer to what was asked. A row Google sells as separate tickets is a
result like any other, `separate_tickets: true` in its `row`; no top-level key
carries the hidden count or the unread tab, only their stderr lines in
`notes`. So is
`cross_check.low_check` in any outcome, `no-answer` included: no flag asks for
that check of Google's low row, every key asked for is whole without it, and
its `outcome` and `reason` say whether and how Matrix answered. Its stderr
line ("Asking Matrix for row N's exact flights") is a note; the line it prints
under the table is never printed in an envelope run, which takes the document
path. Each pin a round trip loses is named on a stderr line of its own
(`pinned outbound … lost: …`), a note like the count line before it; the
answer is narrower where that count is of refused or empty return boards, and
whole where it is of boards the row filter emptied. Google's price graph not
asked or not shown beside a Matrix calendar is a note too: Matrix's grid is
the answer, and the graph is Google's estimate beside it.

Like `separate_tickets`, `top_flight` is a field of each Google member of a
`row` and narrows nothing. It is true when the page that listed the member put
that itinerary on Google's Top flights board (`ds:1[2]`): an outbound's
outbound page, a return's pinned return page, a `--split`, open-jaw or
multi-city ticket's one-way page. It is false on every row the Cheapest tab
adds and on every member of a page with no such board, which writes no note.
Matrix rows carry neither key. `row` is untyped in the schema, so
`docs/envelope.schema.json` does not change.

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
`--format json`. A calendar that reads Google's graph records Matrix's days and
the graph once both are in (`cli._run_calendar_beside_graph`), the graph even
when Matrix failed. `--split` records its `split_ticket` object where `--format
json` writes `{search, split_ticket}`, and is refused where JSON refuses it
(`--enrich`, an award search), the refusal naming the format asked. Where Google's half failed, `results` and `backend` stay empty,
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

## Route facets

`ds:1[7]` holds what Google's filters offer for the search, read by
`_gflight_ids._route_facets` into `Board.facets`. Decoded on 20 of 27 captures
and 9 live pages; the other 7 captures carry null there.

- `[0]` `[[null, low], [null, high]]`: the fare range, in whole units of the
  page's currency.
- `[1]` `[alliances, airlines]`, each `[[code, name], ...]`: the Airlines
  filter's choices. The alliances are `ONEWORLD`, `SKYTEAM` and
  `STAR_ALLIANCE` on every capture; the envelope spells each as `--extension
  'ALLIANCE …'` takes it (`oneworld`, `skyteam`, `star-alliance`), since
  `ALLIANCE STAR_ALLIANCE` is refused and would send the search to Matrix.
  The airlines are the filter's list, not the rows' carriers: JFK-LAX lists
  14, and 4 of them fly a row.
- `[2]` `[[[code, city], ...], layover_low, layover_high]`: the airports a trip
  may connect at and the layover range, in minutes.
- `[3]` `[duration_low, duration_high]`: the trip-length range, in minutes.

The reader is all or nothing. A block shorter than four parts, any of these
parts of another shape, a fare that is not a finite number, or a range whose
low is above its high gives no entry, never part of one. It leaves `complete`, the exit status and stderr as
they were, and the empty key carries its note as `insight`'s does.

The block describes the search, not the rows served. It is the same on the
curl_cffi and Chrome reads of one search, on reads of 72 and 300 rows, and on
every page of a round trip (live JFK-LHR: the outbound page, five return pages
and the Cheapest tab). So an entry comes from the search's own page alone: a
round trip's outbound page, never a return page, and never the Cheapest tab,
whose block can differ (FLL-LGA: USD220 there, USD234 on the default board).
A leg asked as several pages gives one entry per answered page, in page order,
each labeled with its page's airports, and never a union, which would put one
page's low beside another's high and lose which airports a carrier serves. A
round-trip page whose outbounds answered and whose returns were refused gives
none, as it gives no insight. Several cabins give one entry each. `origins`
and `destinations` are the codes of the search's first segment, named by
`outbound_page`, since the page states none.

The price is in the rows' basis: the party's total in the page's currency, a
round trip's total on a round trip (live, 2 adults in EUR: low EUR1234, the
cheapest pair). Its currency is read off a priced row, as the insight's is,
else null. A routing filter leaves the block as served: it states what Google
offers for the search, not what the filter kept.

Not read: `[7][4]` to `[7][7]` (`[[1,2,3]]` or `[[2,3]]`, `[1,1,0,1]` or
`[1,0,0,0]`, null, `[0]`), whose meanings are not established; and `ds:1[11]`,
`[[code, name, baggage-policy url]]`, which follows each page's served rows
(3 to 22 entries across one round trip's 7 pages) and helps choose no filter.
The facets are in the envelope alone: a one-way's or round trip's `--format
json` is a bare list, which a key would turn into an object. No table line
prints them, since an entry holds 14 to 46 airlines and 20 to 53 airports. They
cost no request.

## Out of scope

Envelopes for `detail`, `explore`, `fare`, `gflight` and `doctor` (each
refuses `--format envelope` naming the two commands); `--sellers` and
`--fare-rules` inside it; ISO dates inside Matrix calendar days (a raw month
carries `month` and `year` and pads its weeks with the neighboring months'
days marked `disabled`; a `row` stays the day object Matrix sent, its day of
the month alone, though the unpriced-dates line dates each day it names);
an exit code of its own for a partial answer (`complete` says it).
