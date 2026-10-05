# GF calendar: the gated `GetCalendarGraph` date grid, `--fast` refusals and their tier phrases, the page's own price graph through Chrome

How `flight calendar` reaches Google Flights: the date-grid RPC gated since
2026-08 and why `--fast` never exits 0 without a grid, the phrases its refusals
use, the Chrome price graph (shape, airport sets, admission, paging, output,
two lows), and what re-enabling the RPC takes. Read before touching
`_gf_dategrid.py`, `_gf_calgraph.py`, `cli._run_calendar_enriched`,
`cli._run_calendar_beside_graph`, or `cli._two_lows_note`.

## GF date-grid (calendar) — `fli.search.dates.SearchDates`

**Gated off since 2026-08 (upstream report fli#223), verified here 2026-09-02:
degrades to Matrix (work-h70kv.5).**
`GetCalendarGraph` answers HTTP 200 with an empty payload unless the request
carries a signed `x-goog-batchexecute-bgr` (BotGuard) header — the same gate that
took out `GetShoppingResults`. An empty payload is not a throttle
(`_is_throttle_block` needs an RPC error marker), so `retry_throttled` read it as
a cold session and spent 4 POSTs + ~6s of backoff per ≤61-day chunk to return
`{}`. `_gf_dategrid.date_grid` therefore raises `GfGridUnavailableError` before the
chunk loop, and `_one_grid_call` keeps the same raise as its first statement,
ahead of `get_client()`: zero POSTs, zero sleeps, and `retry_throttled` (which
catches only `GfThrottledError`) propagates it. The raise has to sit at the TOP
of `date_grid`, not just in `_one_grid_call`: the loop builds `_grid_filters`
first, and that resolves airports through fli's `Airport` enum and dates through
`FlightSegment`. A city code (NYC/LON/PAR/CHI) is not an `Airport` member and a
window opening in the past fails travel-date validation, so either one raises
into the callers' broad `except` and prints `date grid failed: type object
'Airport' has no attribute 'NYC'` — a transport fault named for a request no
transport was going to carry.

The weave `cli._run_calendar_enriched`, which only `--gf-transport http` reaches,
prints one note — the observation plus the bd id, not a cause — and then waits
for Matrix. The note can only promise to wait, not to deliver: it is printed
while the Matrix request is still in flight, and Matrix can still fail after it.
**`--fast` never exits 0 without a grid.**
Every no-grid outcome — gate, throttle, an empty grid, or anything reaching the
broad except — prints "No Google Flights grid; drop --fast for Matrix." once, on
**stderr**, and exits 1. Every `--fast` refusal goes that way, the up-front ones
and this one alike, so stdout under `--fast` carries a grid or nothing and a
caller never has to parse the stream to learn which it got. The weave's note is
on stderr too, above a Matrix calendar that follows on stdout. While the gate stands, a bad airport or date is one of the gate's own
exits rather than the broad except's, so what the user reads is the standing
reason; the broad except keeps the same exit code for whatever a live transport
throws once the gate flips. When the grid branch does not apply at all (a code
that is neither an airport nor a metro code in `_metro.py`, a leg of more than
11 airports (a grid is one page) or with one airport at both ends, routing above
Tier-1, a Tier-1 code or zero bound the request would leave out, a trip-length
range over `--gf-transport http` or past the browser graph's 8 loads, or a
constraint the search page's URL cannot carry) `--fast` refuses up front on
**stderr**, naming the shape, before any Matrix call or JSON write — stdout under
a JSON request carries a document or nothing, never prose (work-h70kv.9). So a wrapper doing `--fast || fallback` can trust the exit
code unconditionally: `--fast` means "the GF grid alone, ~1s", and answering it
with the ~45s Matrix calendar — silently or otherwise — would change what the
flag means.

That refusal names the tier, the flag and the number, because the phrase is what
tells the reader where to go. `grid_can_serve` is False for Tier-2 and Tier-3
alike, so `grid_routing_blocker` re-reads the predicates for the tier; and
`classify` flattens `--routing` and `--extension` into one predicate set that no
longer remembers which carried what, so it classifies the two SEPARATELY for the
source. `--extension` takes a `;`-separated list, so its half is counted. Five
spellings per tier, ten in all, covering the ten cases (routing declines or not,
crossed with none / one / several declining extension directives, less the case
where nothing declined):

| declining | phrase (`<T>` is `Tier-2` or `Matrix-only`) |
|---|---|
| routing only | `<T> routing` |
| one extension directive | `a <T> extension code` |
| several extension directives | `<T> extension codes` |
| routing + one directive | `both <T> routing and a <T> extension code` |
| routing + several | `both <T> routing and <T> extension codes` |

Calling a booking class "Tier-2" points the reader at a post-filter that was
never the problem; calling it "routing" points them at a flag they did not set;
and "a … extension code" for three of them makes them look for one directive.
The Matrix-only phrases carry every reason in parentheses, from both flags. The
phrase counts only the EXTENSION directives, so the two counts agree when the
extension is the whole story and differ by one when routing declined as well:
"both Matrix-only routing and a Matrix-only extension code" carries two reasons,
one per flag.

A Tier-1 predicate is refused too when `apply_gf_native_filters` would not
write it in full, because neither grid has rows to check afterwards: a carrier,
alliance or connect-at code fli has no member for (the function then leaves the
whole list out), a zero MAXDUR (fli's encoder omits a falsy bound) and a zero
MAXCONNECT (fli's `LayoverRestrictions` raises). `unwritten_constraint` asks the
bridge one code at a time, so it follows fli's own tables, and the phrase names
the code or the bound. Without `--fast` such a calendar takes the Matrix
fan-out rather than the weave.

Those reason strings quote the user's `--routing` / `--extension` text verbatim
onto a markup console, and so does every response field a renderer shows. The
wrapping rule, the two helpers and the AST guard over `cli.py` are in
[console_sanitizing.md](console_sanitizing.md).

The grid paint in the weave is runtime-dead until the gate flips;
`_run_calendar_enriched` itself still runs under `--gf-transport http` (it is
what paints Matrix there).

# `--fast`: the page's own price graph, through Chrome

The search page signs its own `GetCalendarGraph`, so `_gf_calgraph` lets the page
ask: Chrome opens the filtered search page on the window's first date, clicks
"Price graph", and `GfBrowserSession.capture` returns the response the page
received — no script runs in the page and no request is written or altered.
Measured 2026-09-27: status 200, `x-goog-batchexecute-bgr` set, no error row, on
a cold headless profile. An unset `--gf-transport` is `auto`, which is the
browser, with `--fast` or without it. Under `--fast` `http` has to be asked for
and refuses with a note naming the browser, and a missing patchright or Chrome
exits 1 with the rung's install remedy, never a fallback. Without `--fast` the
same graph prints after Matrix's calendar (below); `http` keeps Matrix's
calendar alone, and `--gf-headed` with `http` is a usage error.

- **Shape.** One-way, or a round trip: one graph prices the trip length its own
  dates imply, so a range (`-d 5-7`) is one graph per length within the
  8-load budget (see "One answer from several Google pages"), and `--fast
  --gf-transport http` refuses it. Every round-trip cell's return date is
  checked against its graph's length.
  That section is in [gf_multi_page_legs.md](gf_multi_page_legs.md).
- **Airport sets.** A comma-list or metro code on either side is one page: the
  bridge writes every member airport into the URL, and the graph prices each
  date at the cheapest of them. Measured 2026-09-28, one-way, 14 dates each:
  NYC→LAX equaled the per-date minimum of JFK, LGA and EWR on all 14 (each of
  the three was the cheapest on some date), and JFK,EWR→LHR on all 14. A round
  trip is not compared that way on purpose: the set page may return to another
  airport of the origin set, as a Matrix metro code does, so its price can sit
  below the minimum of mirrored pairs. The airports are checked against ONE
  page's bound (`gf_leg_refusal`; a single-cabin search over it is asked as
  several pages, a graph is not), then `_gf_unserveable_reasons` on the
  expanded codes.
  The JSON names the user's tokens (`"NYC"`, `"JFK,EWR"`), as the table title
  does. Over `--gf-transport http` a set refuses with the browser note, since
  `date_grid` writes one airport per side; without `--fast` Matrix answers it
  through its fan-out and the graph over the whole set prints after it. That
  fan-out splits a metro code into its member airports, asks one query per
  airport pair, and merges the grids in one currency (USD with more than one
  origin, unless `--currency`); each merged day and trip length names the pair
  that priced it (`origin`, `destination` in the JSON; in the table a `route`
  column for the day's minimum, and the pair beside any trip length another
  pair priced), the two arguments `flight detail` takes. A round trip also runs the
  user's own combined query beside the pairs, the only source of a trip that
  returns to a different origin airport or comes back from a different
  destination airport: it takes a day only when strictly cheaper than
  every pair, its cells name the user's tokens, and a stderr note says so.
  Measured 2026-10-01 over 2026-10-20..11-02: `LHR,DUB JFK --one-way` merged
  DUB's EUR cells with LHR's GBP cells as bare numbers and showed GBP1137 and
  GBP952 on two days DUB was cheaper; asked in USD, all 14 days came back USD,
  each cheaper from DUB. `NYC LON -d 7` as one combined query priced 11 of 14
  days (20 solutions, cheapest USD817); as 18 pairs plus that query it priced
  14 of 14: the 3 days the combined query alone left unpriced (10-25 to 10-27,
  the last at USD766 EWR→LGW) and 6 of the 11 it did price, cheaper on 10-28
  to 11-02; the combined query was below every pair on none; 8 of the 18 pairs
  priced nothing (JFK, LGA and EWR into LTN and SEN, JFK and EWR into STN) and
  LGA→STN priced 1 day; and the 19 queries took about 110 s.
- **Admission** (`_gf_calgraph.graph_blocker`). The graph has no itineraries,
  so it is asked only when the page URL writes every constraint exactly AND
  Google was measured applying it there. Admitted, per leg: a stop ceiling of
  two or fewer, ONE marketing-carrier include (`AA+`, `AIRLINES AA DL`) or ONE
  `ALLIANCE`, a positive `MAXDUR`, a `MINCONNECT`, a positive `MAXCONNECT`, and
  on a one-way a `--depart-times` window whose buckets adjoin and end at 23:59
  (`night`, `evening,night`, `afternoon,evening,night`). Measured 2026-09-30,
  LGA→LAX one-way over 2026-10-20..11-02, one load each, dates priced
  higher/equal/lower than an unfiltered baseline (a repeat baseline equaled it
  on 14 of 14): `ALLIANCE skyteam` 13/1/0, `AA+` 14/0/0, `MAXDUR 9:00` 11/3/0,
  `MINCONNECT 3:00` 9/5/0, `MAXCONNECT 1:00` 9/5/0, `evening,night` 14/0/0.
  Google's layover bounds keep nonstops, as Matrix's do. Round trip `-d 7`,
  same route and window: `AA+` 14/0/0, so the carrier, alliance, duration and
  layover bounds are admitted on round trips too. Only 3.6 was probed on a
  round-trip page; the others are admitted on the assumption that 3.12, 3.17
  and 3.18 behave there as 3.6 does. A `--return-times night` round trip priced
  no date at all, so a time window on either leg of a round trip refuses. Only
  single codes were measured: a multi-code include is admitted as one 3.6 list,
  which Google reads as any of its codes, as Matrix reads one include.
  Refused as WIDER than asked: any other time window (Google reads a latest
  hour to its 59th minute, so `morning` asks 8:00-11:59, and `midday,night`
  the hull 11-23), two includes on a leg (one 3.6 list, read as either; named
  `an alliance filter combined with another carrier or alliance filter` as the
  search gate names it, or `a carrier filter combined with another carrier
  filter`), a `MINCONNECT` above the `MAXCONNECT` (the bridge drops the
  minimum), and two different `MAXDUR` or `MAXCONNECT` on a leg (the bridge
  writes the last). Everything else refuses in the words and order of the
  narrower gate `--fast --gf-transport http` keeps (`grid_can_serve`, then
  `page_blocker`): Tier-2/3 routing and extension codes, a code or bound the URL
  would leave out, a time window, a non-adult passenger, `--no-airport-changes`,
  `--include-unavailable`, a predicate the page cannot carry (a connecting
  airport), then round-trip legs with different predicates. The URL takes the
  LOWEST stop limit from `--stops` and every leg's `StopsPred`, because the
  bridge reads `--stops` alone and `apply_gf_native_filters` overwrites it with
  the last predicate it meets, and it writes the outbound leg's predicates
  alone: the gate has made them the return's, and both legs' together would
  list each carrier twice.
- **Span and paging.** One load covers about five weeks (seven days before the
  opening date to thirty after, on the page measured). The span is read from
  the response; a longer window re-navigates at the first uncovered date, and
  stops on a graph that covers nothing new or at eight loads. Only a window
  that needed all eight with no page loaded again is told to "narrow
  --start/--end"; loads spent on a reload, or by the trip lengths before it,
  end it with `GfGraphBudgetError`, "no price-graph load of the 8 was left for
  the rest of the window", naming the reload when there was one.
- **Envelope.** `rt=c` chunks, one `wrb.fr` row; cells at `inner[1]` as
  `[dep, ret, [[null, price], token], 1]`. An error row has an empty payload and
  its code at `row[5][0]`. Error 13 there is a refusal of the browser session,
  not a throttle, so it never goes through `_is_throttle_block`.
- **Output.** The table is `_render_date_grid` with the trip length in the
  summary line; `--format json` writes
  `{origin, destination, currency, trip_length, grid: [{departure, return?, price}]}`
  alone on stdout, with no URL lines. A range prints one column per length and
  writes the range document, one such document per length that priced
  (`cli._graph_range_document`).
- **Without `--fast`.** A table calendar reads the graph beside Matrix
  (`cli._run_calendar_beside_graph`). The graph runs on one worker thread,
  started before Matrix's first request, and Matrix runs and delivers on the
  main thread exactly as it would alone; the command waits for the graph only
  after Matrix's output is out, then prints Google's table under it. Admission
  is the `--fast` gate over a copy of the search with one trip length (every
  length shares the legs and filters), plus a budget: trip lengths × ⌈window
  days / 31⌉ ≤ 8, a one-way counting as one graph. A refused calendar prints one
  dim stderr line, `Google Flights price graph not asked: this is <reason>.`,
  and runs Matrix alone with no Chrome launch; JSON is Matrix's document alone
  and says nothing unless `--gf-transport` or `--gf-headed` asked for Chrome. A
  range asks one graph per length (`_gf_calgraph.price_graphs`), each with the
  loads the lengths before it left, and prints one column per length (`5n`
  `6n` `7n`) beside the row minimum, "—" where a length priced nothing that
  date. For a set or metro code the title says each cell is the cheapest
  across every airport pair: the graph gives no per-pair answer. Any Google
  failure is ONE stderr line starting `Google Flights price graph not shown:`,
  keeping a launch or install remedy. A range that loses some lengths prints
  the ones that priced, with no column for a lost length, and that one line
  names each lost length with its cause (`7-night trips: <cause>`). Only a
  page that drew no graph, or a length that ran out of loads, lets the next
  length be asked, and the loads either spent are counted, so the lengths past
  a spent budget read `no price-graph load of the 8 was left`; any other
  failure loses the lengths after it too (`not asked after 6-night trips
  failed`). A range that priced no length prints its first failure alone, as
  a single graph does. Stdout up to Google's table and the exit code are the `http`
  run's, and a Matrix failure keeps its lines and exit 1 with Google's table
  still printed. The SIGINT guard is armed around both halves, so a Ctrl-C
  stops the driver and exits 130. Measured 2026-09-29, NYC→LON round trip over
  2026-10-20..11-02: each of 5, 6 and 7 nights priced 14 of 14 dates in one
  load of about 12 s.
- **Two lows.** Where Matrix's grid and the graph show lows a dollar or more apart
  (Google's read as the whole dollars its table prints), one yellow stderr line
  follows Google's table and any lost-length line (`cli._two_lows_note`). It names
  each low as its table's first row shows it: date pair, nights (or one-way) and
  airports, Matrix's pair from the merged cell, Google's "cheapest across" a set.
  It says what both asked (cabin, adults, the trip lengths, USD, the same airports;
  after a lost length, Matrix's range and the lengths Google priced), the stop rule
  only when no stop limit reached Google's page (Matrix one stop more than the
  fewest on a route in each direction, Google any number), and two searches that
  show what is bookable: `flight detail` on Matrix's pair and `flight search ...
  --backend gflight` on the set, each repeating the calendar's own flags
  (`--cabin`, `--adults`, `--stops`, routing and extension codes as typed,
  `--depart-times`, `--currency`, and `-d` for `detail` when it is not 5-7).
  `detail` also carries `--currency USD` when several origins and no `--currency`
  asked the grid in USD: one pair asked alone answers in its origin's currency (LHR
  CDG in GBP, measured 2026-10-02). A Matrix grid in another currency is named and
  not compared. JSON, `--fast`, `--gf-transport http`, a failed or empty side and
  agreeing lows print no line, and stdout and the exit code are unchanged. Two lows
  that differ need not mean either table is wrong. Measured 2026-10-01, `calendar
  NYC PAR --start 2026-10-20 --end 2026-11-19 -d 5-7` (9 pairs plus the combined
  query, 295 solutions) had Matrix's low at USD698.00 (10-26, EWR→ORY, 5 and 6
  nights) and the graph's at 429 (11-10, 7 nights). Google's board for NYC PAR
  11-10/11-17 had that 429 as TAP TP214+TP454 EWR-OPO-ORY and TP453+TP211 back, one
  stop each way. Matrix's NYC PAR search for the same dates returned 60 solutions,
  all nonstop, low USD715.89 (AA/DL/UA/AF); asked EWR ORY with `--routing TP+` it
  priced the same TAP flights at USD428.19, and without the routing it returned 12
  solutions, low USD526.59 (TAP, another return). Both sides had asked the same
  airports, trip lengths, cabin and currency, so the fix is a note, not a change to
  either request. Matrix's limit holds each direction, not the trip: EWR ORY
  11-10/11-17 `--routing TP+` (the fewest is one stop each way) under the default
  limit, measured 2026-10-02, returned 100 solutions, 2 of them with two stops each
  way.
- **A page that draws no graph.** About one load in fourteen (2 of 27-29 live
  loads, 2026-09-28/29) passes the wall check and then times out on the
  "Price graph" click. One of the two was the first load of a fresh Chrome, so
  a reused session is not what causes it; the cause is not identified (12
  probe loads set up to snapshot the page on a failure all priced).
  `price_graph` loads such a page once more, from the same eight-load budget,
  under `--fast` too; a second miss raises `GfGraphStalledError`. A wall, a
  failed navigation and any answer the graph gave are never loaded again.

**Re-enabling is not just `_GRID_RPC_GATED = False`.** Nothing executes the
transport below the gate — there is no captured GetCalendarGraph envelope to test
it against, and inventing the shape is forbidden — so type-checking is its only
guard, which is why the gate is a flag and not an unconditional raise (a raise, and
`Final[bool]`, both make basedpyright treat the body as unreachable; measured).
The procedure: capture a real envelope into `tests/fixtures/`, add an ungated
contract test over it (request URL, encoded body, and the success / empty /
throttle branches of `_one_grid_call`), keep `_grid_filters` behind the two
refusals of an airport set or metro code (the gate without `--fast`,
`cli._http_date_grid` with it; it writes one airport per side, and a metro code
is not in fli's `Airport` enum), run a live smoke, then flip. Do that when the RPC answers a plain client again, or when an attested
transport lands
(work-udpp1).
A per-date page fan-out (the transport upstream fli#230 uses for search) is the
other candidate; it is tracked, not built — 61 page GETs of ~3.6 MB per chunk is
a different throttle budget entirely.

The rest of this section describes the primitive as it behaves when the RPC
answers. Google's `GetCalendarGraph` returns a whole date window's
cheapest-per-date prices in ONE call, and `DateSearchFilters` carries the full
Tier-1 filter set (airlines, stops, layover, max_duration, cabin, times, price).
Verified 2026-06-14: `airlines=LH` / `stops=NON_STOP` change the grid prices, so
Tier-1 filters ARE honored. It returns `{date, price}` only — **no itineraries**
— so Tier-2 (`O:`/`-CODESHARE`/`~UA`/flight#) can't be post-filtered on a grid;
those calendars go to Matrix. It is the throttle-friendly calendar primitive
(1 call vs a per-date fan-out), which is why we prefer it when it works, and
Matrix's `_calendar_split` already fans out for Tier-2 / multi-airport.

**fli `SearchDates` >61-day chunk filter-drop (fixed upstream in 0.9.0):** in fli
≤0.8.5, `SearchDates.search()` split windows >61 days into chunks but rebuilt
`DateSearchFilters` per chunk copying only
trip_type/passenger_info/segments/stops/seat_type/airlines/dates/duration —
**dropping `layover_restrictions`/`max_duration`/`price_limit`/`emissions`/`bags`
on chunks 2+.** fli 0.9.0 fixed this upstream (the per-chunk rebuild now copies all
14 `DateSearchFilters` fields); we require `flights>=0.9` as of PR #30. bd
work-bcdex and work-aua4v (the upstream-it follow-up) are both closed. `_gf_dategrid`
still caps each call to ≤61 days and chunks ourselves with the full filter set —
now redundant but harmless; bd work-orp1i tracks simplifying it to lean on fli's
chunking. That simplification is blocked while the RPC is gated: verifying fli's
chunker keeps the filters needs a live >61-day grid to compare against.
