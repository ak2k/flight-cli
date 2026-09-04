# GF carrier semantics + routing tiers + progressive enrich

How `--routing`/`--extension` reach Google Flights, and the carrier-identity
indices that make it correct. Read before touching `routing_predicates.py`,
`_gf_postfilter.py`, `fli_bridge.apply_gf_native_filters`,
`links.build_search_tfs`, or `_gflight_ids._parse_leg_amenities` /
`_flight_leg`.

## Booking carrier: `fl[15]` (marketing) vs `fl[22]` (operating)

Each Google Flights leg tuple (`data[0][2][i]`) carries two carrier identities:

- `fl[22]` = `[code, number, _, name]` of the **operating** carrier (the metal).
- `fl[15]` = `null`, or a list `[[code, number, _, name], …]` of the
  **marketing** (selling / codeshare) carriers.
- `fl[18]` = truthy (`[true]`) when the operating carrier markets the leg under
  its own code; falsy/`null` on operated-for (regional feeder) legs.

The carrier a passenger **books** (and what Matrix surfaces) is:

```
booking = fl[15][0]  if fl[15] present AND fl[18] falsy   # operated-for regional
          else fl[22]                                     # self-marketed / mainline
```

Ground-truthed 2026-06-13 against GF's own headline labels:

| Leg | `fl[22]` | `fl[15]` | `fl[18]` | GF headline → booking |
|---|---|---|---|---|
| OS36 JFK→VIE | OS / Austrian | `[UA…]` | `[true]` | **Austrian** (`fl[22]`) |
| EN8858 FRA→FLR | EN / Air Dolomiti | `[LH9498]` | `null` | **Lufthansa LH9498** (`fl[15]`) |
| LX39 SFO→ZRH | LX / SWISS | `null` | `[true]` | **SWISS** (`fl[22]`) |

`_gflight_ids._flight_leg` sets `FlightLeg.airline`/`flight_number` to the
*booking* carrier so gflight flight numbers match Matrix's (marketing) numbers —
which is what makes the GF↔Matrix reconcile join fire. `_parse_leg_amenities`
also keeps the operating carrier (`operating_carrier`/`_name`), the marketing
codes (`marketing_carriers`), and the full marketing flight #s
(`marketing_flights`, e.g. `LH9407`) for the `O:` filter, `-CODESHARE`, and
codeshare-aware display. All flow through `LegInfo`.

## Search transport: the public page's `tfs=`, not `GetShoppingResults`

Since 2026-08 the `FlightsFrontendService` RPCs require an
`x-goog-batchexecute-bgr` header the page's own JavaScript signs over the exact
request bytes, so a captured token can't be replayed. Every plain HTTP client
gets HTTP 200 with a payload-less `wrb.fr` row carrying error 13 — which the old
`_one_call` read as an empty leg and printed as "no results".

The **search path** therefore GETs
`https://www.google.com/travel/flights?tfs=<proto>&hl=en&gl=US&curr=USD` and
reads the rows Google inlines in the page's `AF_initDataCallback` blob keyed
`ds:1`: `[2][0]` is Google's own top-flights board, `[3][0]` the rest. Verified
live 2026-09-02 (JFK-LAX): 3 + 27 = 30 rows, `data[0][17]` flight_id present,
33-element leg tuples, `leg[13]` legroom class present — so
`_parse_flight_with_id` and `_parse_leg_amenities` are untouched, and every
index in the "Legroom + amenities" recipe still applies.

`links.build_search_tfs` writes that parameter on the SAME `_PbWriter` the
pinned booking link uses; the only difference is `pin_max_u64=False` (field 16
is a deep-link marker the page doesn't need), so the byte-exact pin fixture
guards both. Field layout, reverse-engineered and cross-checked against fli
PR #230:

```
1  = 28 (constant)          8  = passenger kind, repeated (adult = 1)
2  = 2 (constant)           9  = cabin class
3  = segment, repeated      14 = 1 (constant)
3.2  = departure date       16 = max-uint64 pin (booking deep links only)
3.4  = selected leg, rep.   19 = 2 one-way, 1 round-trip (3 multi-city is unusable)
3.5  = stop ceiling         3.13 = origin   3.14 = destination
3.6/3.7 = carrier incl/excl (NOT written yet — see below)
3.15 = layover airports     3.17/3.18 = min/max layover minutes
```

Two traps in that layout. **`3.5` is zero-based** while fli's `MaxStops` is
one-based (ANY=0, NON_STOP=1, …), so it's `enum.value - 1` and **omitted** for
ANY — writing a literal 0 pins every search to nonstop. **Carrier codes come
from the enum NAME, not its value**: fli maps codes to display names
(`Airline._0B.value == "Blue Air"`) and underscore-prefixes digit-leading ones,
so `airline.name.removeprefix("_")` is the code.

**What the page costs us.** It serves Google's default board (~30 rows/leg) with
no back-fill, so a `-n` above that returns fewer rows than the RPC did, and a
round-trip costs one page fetch per pinned outbound. More importantly the tfs
parameter carries far fewer filters than `f.req` did, so `routing_predicates.
page_can_encode` is a SECOND, narrower gate in front of the Tier model below:
today only a stop ceiling encodes, and everything else routes to Matrix with its
reason printed. Post-filtering a fixed 30-row board would answer a constrained
search with a plausible-looking "no results" — the exact failure this whole
design is built to avoid. Re-widening `3.6`/`3.7`/`3.15`/`3.17`/`3.18` is the
obvious next step and is tracked in bd work-h70kv.

The encoder is an enforced allowlist, not a deny-list: every field on fli's
`FlightSearchFilters` must be named in one of three sets (encoded / refused /
deliberately ignored), and `build_search_tfs` raises on any remainder. `flights`
is pinned with an open floor (`>=0.9`), so a minor that adds a filter would
otherwise encode as if the new field were unset — dropping a constraint the user
asked for, silently.

One more trap: a stop ceiling only encodes up to **two**. fli's `MaxStops` tops
out at `TWO_OR_FEWER_STOPS`, so a ceiling of 3+ maps to `ANY` and omits field
3.5 entirely — `--stops 3` then encodes byte-identically to no `--stops` at all.
Both spellings hit the same ceiling (`routing_predicates.MAX_ENCODABLE_STOPS`,
shared so the two sites can't drift): the routing-language `MAXSTOPS 3` through
`page_can_encode`, and the `--stops` flag through `_pick_backend` directly.

**Refusals are typed** (`_gf_errors`):

- `GfThrottledError` — the captcha interstitial, which arrives three ways. A
  redirect puts `/sorry/` in the final URL; Google also serves the same page
  in place with HTTP 200, where the body marker ("Our systems have detected
  unusual traffic") is the only tell; and an outright **HTTP 429, which arrives
  as a RESPONSE**. `_fetch_page` goes through fli's session rather than
  `Client.get`, so nothing calls `raise_for_status()` on our behalf and
  `_one_call` reads `resp.status_code` itself — which is what lets the ladder
  see a throttle instead of a wrapped transport error.
- `GfConsentError` — no `ds:1` *and* consent markers, checked in that order,
  because a real results page links to the consent domain in its footer.
- `GfPageShapeError` — no readable `ds:1`; or a payload too short to reach
  `[3]`; or a value at `[2]`/`[3]` that is neither absent nor row-shaped; or
  rows found outside `[2]`/`[3]` with none served; or rows present and none
  parsed (with sampled reasons).

A page that decodes with zero rows returns `[]` and is Google's authoritative
answer, so the search path passes `retry_empty=False` and spends exactly one GET
on it.

**A page may carry more than one `ds:1` blob**, and this one hydrates in stages,
so `_extract_ds1` decodes them ALL and serves the one carrying the most rows at
`[2]`/`[3]`, earliest blob on a tie. Choosing by position is a bet: a
placeholder emitted above the populated blob reads as an authoritative empty (or
trips the arity guard) while the real board sits further down the document,
unexamined. Choosing on "carries a row block" is the same bet one level down,
because a staged blob can carry an empty husk `[[]]`, or one row where the
settled board carries thirty.

Only a blob the row scan could actually SERVE competes on rows
(`_is_a_readable_board`, which mirrors `_rows_from_ds1`'s two refusals: the
arity floor, and a value at `[2]`/`[3]` that is neither absent nor row-shaped).
A staged blob can be truncated above `[3]` or carry rows beside a placeholder at
`[3]`, and either holds MORE rows than the finished board — counting them turned
a served page into a typed refusal.

The count is **structural**, exactly like the scan at those indices: rows are
counted, never parsed, so a board whose rows have all changed shape still wins
and reaches the 0-of-N guard as a layout change rather than losing to a husk.
With no blob carrying rows the fallback takes the first one long enough to reach
`[3]`, then the first decodable one at all — so a genuinely flight-less page
stays flight-less and a truncated placeholder above it does not become a shape
error.

The accepted cost, measured: a readable blob whose `[2]` holds many row-shaped
NON-flight entries outranks the real board in either order, and the search then
refuses with "none of N Google Flights rows parsed". That fails loud and
degrades to Matrix, which is the right side of the trade — the alternative is a
selector that parses rows to choose between blobs, and it would drop a real
board whose row layout has just changed.

Loud only while NONE of the decoy's rows parse. A decoy carrying four rows one
of which is a genuine flight row beats a real three-row board and serves that
one flight, with no warning at any level — the 0-of-N guard never fires because
one of N parsed. Measured; unchanged by the readability floor. The trade is the
same one and still worth taking, but the failure it degrades to is a short
table rather than a refusal, which is the quieter half.

### Request budget

Every one of these is a multi-megabyte page GET, so the count is the cost:

| query | GETs |
|---|---|
| one-way | 1 |
| round trip | 1 + min(top_n, rows on the board, `_PINNED_FANOUT_CAP` = 10) |
| multi-cabin | the above, times the cabin count |
| a persistently throttled leg | `_THROTTLE_RETRY_ATTEMPTS` + 1 = 5, then it aborts |
| a transport blip | up to 3 GETs per leg (`_TRANSPORT_RETRY_ATTEMPTS` + 1) |
| a leg that both throttles and blips | 1 + `_THROTTLE_RETRY_ATTEMPTS` + `_TRANSPORT_RETRY_ATTEMPTS` = 7 |
| a persistently throttled multi-cabin fan-out | one ladder for the group: at most 5 + (cabins - 1) |
| a multi-cabin fan-out under a transport outage | one ladder for the group: at most 3 + (cabins - 1) |
| a round trip whose pins meet a throttle or an outage | it stops at that pin: no further pin is fetched |
| a round trip whose every pin blips and recovers | 1 + 3 x pins = 31 at the default `-n 10` |
| a round trip whose return boards all refuse (5xx, consent, layout) | 1 + pins, the same as a successful search |
| a round trip on a wall that keeps lifting and closing | 55 for one cabin, 220 for four, against 44 healthy — `cabins x calls x (_THROTTLE_RETRY_ATTEMPTS + 1)` |

The flapping row is the worst case and the one that needs its bound named. Any
sibling's success refills the wall — correctly, it is per-IP — so the shared
ladder never exhausts there and cannot bound anything. What does is the attempt
count each `retry_throttled` call carries, applied to the eleven calls a default
round trip makes — one board and ten pins — and then to the cabin count. That
product is the row above.

Two things make the rest of those numbers hold, and this paragraph is where that
arithmetic lives — the docstrings that depend on it point here rather than restating it.
`_fetch_page` goes through fli's SESSION, not `Client.get`, which is wrapped in
`@retry(stop_after_attempt(3))`: a throttled leg would otherwise cost up to 15
GETs, fli's ladder running inside each rung of ours. `retry_throttled` is the
only ladder. And the round-trip pin is capped
at 10 regardless of `top_n`: the multi-cabin path bumps `top_n` 5x (to 100) to
widen the pool it filters, which was free on the old RPC and would otherwise
mean ~2 x 31 page fetches for a two-cabin round trip. The default `-n 10` is
unchanged by the cap.

The bump therefore widens the leg-1 rows each cabin keeps and NOT the round-trip
pins, so **Google Flights joins cabins on up to `<pin budget>` of each cabin's
first-ranked outbounds; '—' means no shared itinerary, not no fare** — the budget
being `pinned_fanout` of the bumped page size, which the cap holds at 10 however
large `-n` is. `cli._multi_cabin_join_note`
builds that sentence from the pin budget rather than a literal, and
`cli._run_gflight_path_multi` prints it on a multi-cabin round trip, because an
empty cabin cell otherwise reads as "that fare does not exist". "Up to",
because the cap bounds how many outbounds the join can see and a board may hold
fewer — stating the budget as a count is the half of this that had to go. Widening the join means pinning
on the intersection of the cabins' outbounds rather than raising the cap; that
is a separate design and is tracked on bd work-h70kv.

The two counters are independent, so one leg can spend both budgets: four 429s,
two transport blips and a final 429 costs 7 GETs. That is the ceiling, and it is
deliberate — a wall that lifts and a network that drops are different failures,
and sharing one counter would let a blip eat the throttle budget.

**The pin loop has ONE rule for stopping.** A throttle, or a transport ladder
that ran out, says nothing about the pin it happened on: the wall is per-IP and
the network is one network, so every remaining pin walks into the same one
having just spent a whole ladder measuring it. `search_with_ids` therefore stops
pinning on either, returns the combinations already fetched, and logs one
counted warning naming the cause and how many return boards it skipped. It
raises only when NOTHING was served — then the refusal is the whole outcome, and
swallowing it would report a round trip with no return legs as a route with no
return flights. A refusal of one URL (a re-shaped board, a consent wall, a 503)
is a different fact and still continues to the next pin, counted the same way.
`GfTransportError` exists so the loop can tell an exhausted transport ladder
apart from those.

**Three Matrix failures that reach the user typed rather than as a traceback.**
`execute()` wraps what Matrix answered; it does not wrap a client that could not
be built or a socket that was never opened. Each of these arms exists because
the untyped shape of that failure is worse than the failure:

| site | what it catches | why it is not a traceback |
|---|---|---|
| the shared search path and the multi-cabin group | anything `execute()` does not wrap — a refused connection, a failed DNS lookup, an API key that will not resolve | the key is resolved when the client is CONSTRUCTED, so on those paths it fails before any task exists; untyped it is a rich traceback with the cause hundreds of lines down |
| the multi-cabin per-cabin arm | the same, for one cabin | an exception leaving a cabin's task cancels its siblings and surfaces as an ExceptionGroup, so one unreachable cabin took the cabins that answered with it |
| the enriched weave's own run | the same, for the loop and the task group themselves | a failure there is not one half of the weave failing, so nothing else in the command is left to report it |

**A round trip says how many outbounds it will combine.** `cli._pin_cap_note`
prints it on every round-trip path — the enriched one, `--fast`, `--format json`
and multi-cabin — whenever the pin cap is below the `-n` asked for, and always
to stderr so a JSON document stays a document. It is passed the user's count,
never the multi-cabin bump — a wider pool per cabin that nobody asked for — and
prints the pin budget that count resolves to, which is the number the join will
actually see rather than the one being corrected.

**`-n` is one number, applied on the way out.** The page serves Google's whole
board — around thirty rows; the dated measurement is at the top of this file —
whatever count is asked of it, so the count is a trim rather than a query
parameter. It bounds everything the user can act on, and all of it from one place
in `cli._run_gflight_path`: the table, the `--format json` document, the range
`--pick` accepts and the itinerary `--emit-urls` pins, and the itineraries the
award providers are fanned out over. All five hold on `--fast` and on
`--format json`, which are the same function. Multi-cabin keeps three of them —
the table, the document and the award fan-out — and has neither of the other
two: `cli._run_gflight_path_multi` has no `_emit_urls` call site at all, and
neither multi path is passed `pick`, which the `search` command accepts and
drops there. On the default enriched path `--pick` indexes the Matrix solutions
rather than the merged rows, and those two sets differ in SIZE whenever any
Google-only row survives the merge — the merged table can be longer than the
Matrix solution list, so a pick naming a printed row is refused as out of
range.

**And it keeps two different orders, because the two sets are ordered by
different things.** A one-way board arrives ranked by Google — a composite of
price, duration and stops that nothing here reproduces — so the trim keeps the
page's order and `-n` means the rows the page put first. A round trip's
combinations are ours: the pin loop builds them outbound by outbound, so their
order is the loop's artifact and carries no ranking at all. Left alone, `-n 3`
there is three trips from one outbound with cheaper trips from the next outbound
off the table entirely. `cli._price_ordered` sorts them on their terminal member,
which is the fare every surface prints, immediately before each of the three
trims. The `-n` help string says both halves.

Three things still read the whole board, and this is why the trim cannot move
into the query: the Tier-2 post-filter, because a routing constraint is answered
out of every row Google served or answered wrong — the flight that satisfies it
can sit at row 25 of 30 — the multi-cabin join, whose per-cabin queries are
deliberately widened (`_bumped_query_top_n`) so the cabins have overlap to join
on and are trimmed back to the user's count by `_multi_cabin.merge`, and the
enriched weave, which hands the untrimmed board to `merge_results` and bounds the
merged table afterwards in `_render_merged`. The multi-cabin `--format json` arm
trims per cabin for the same reason, to the user's count and not the bumped one —
the same count as the table beside it, drawn from a different set.

**What a round-trip row's price means.** The two boards price different things.
An outbound row carries the cheapest round-trip TOTAL reachable from that
outbound; the return board fetched with that outbound pinned prices each of its
rows at THAT combination's own total. Measured on the committed capture pair —
the numbers and the assertions are in `tests/pp/test_gflight_adapter.py` — the
pinned board's minimum is exactly the outbound row's price, while the other
combination is a dearer trip. Live 2026-09-03 (HNL-MIA business, 2 adults) says
the same from the other end: outbound 854/305 quoted 6806 and its two
combinations totalled 6806 and 7650. So an itinerary is priced from its terminal
member — the pinned leg is what makes the combination that combination — and
pricing it from the outbound reports every combination but the cheapest under its
real fare. The human table prints each member's own price on its `Na`/`Nb` rows
and `--format json` emits both, so both carry the true number; the SearchResult
the award comparison reads carries one, and it is the total. The cash baseline
that comparison is made against is therefore the cheapest of the rows SHOWN —
one-way rows in Google's order, combinations in price order — and not the
cheapest on the board, which is what `-n` bounding everything means.

**Release before park.** A worker that is about to wait on another arm's round
gives up any round it still owns first. Two workers can otherwise each hold what
the other waits for, and nothing ends it: no rung is spent, so nothing exhausts.
The other half is `retry_throttled`'s `finally`, for the worker that crosses and
takes the second round instead of parking on it.

**A partial round trip is a success, deliberately.** When the loop stops early
with something served, the command exits 0 and `--format json` emits the
combinations it has, in the ordinary shape — no envelope, no marker, no
different exit code. The account of what is missing is the counted warning on
stderr, which the default log level shows. A human sees it; a machine consumer
does not, and that gap is known: an envelope would change the output contract
for every existing consumer to signal a condition that also arises from
ordinary upstream thinness, and a non-zero exit would make a normal throttle
look like a failure to a script. If a machine-readable signal is ever wanted it
belongs behind a new format, never a silent shape change.

**A diagnostic resolves its stream per write.** Both halves of `log.py` do it and
for the same reason: `_StderrHandler` looks up `sys.stderr` per record, and
structlog's logger writes through a proxy that looks it up per write, because
`cache_logger_on_first_use` otherwise pins whichever stream carried the first
record for the life of the process. Two failures follow from a pinned stream,
and only an embedding host reaches either — the CLI is one shot with a real
stderr. A host that replaces and closes it takes `ValueError: I/O operation on
closed file` out of the next log line; a host with no `sys.stderr` at all fares
worse, because `PrintLogger` reads `file or stdout` and puts the diagnostic in
the stream the JSON document is written to. A stream that cannot be written to
drops the line instead, and there is no fallback to stdout at any point.

**One ladder per fan-out, not per cabin.** The multi-cabin path runs a cabin per
thread; laddering separately, four cabins spend 4 x 5 = 20 multi-megabyte GETs
against an IP already refusing us to learn what the first ladder learned.
`_gflight_ids.shared_throttle_ladder` — armed by `cli._run_gflight_multi` around
the fan-out — hands the group one ladder, and it is a **single prober**: the
first worker throttled owns the backoff and its retry is the probe, while any
other worker throttled meanwhile waits on that outcome instead of sleeping a
schedule of its own. A probe that gets through releases every waiter to retry,
so a wall that lifts inside the ladder serves the whole fan-out rather than
whichever cabin happened to be probing. When the rungs run out the waiters raise
without spending a request on a wall just measured. The transport budget rides
the same object because the network is one network, and it probes the same way:
the classifier admits only the curl failures that DO clear, so a waiter has an
outcome worth waiting for. Each arm keeps its own round; only the lock is
shared. One worker can own both at once, so standing down releases both — a
SUCCESS does not, and the next paragraph is where that asymmetry is stated.

**A waiter's park ends on the owner's report and on nothing else.** The wait
carries no clock, because there is nothing for one to decide: `release()` sets
the very event the waiter holds, so waking is the report arriving. Any rule that
lets a waiter go earlier — a timeout read as an answer, a poll — puts three more
multi-megabyte GETs in flight beside the prober's, which is the amplification
the shared budget exists to remove; and an owner IS slow by construction, since
every attempt of its ladder can burn the full request timeout. What bounds a
waiter is its own attempt count: it meets the wall at most
`_THROTTLE_RETRY_ATTEMPTS` = 4 times and the network at most
`_TRANSPORT_RETRY_ATTEMPTS` = 2, because the next meeting is `final` and returns
without parking. One park ends no later than the owner's remaining ladder —
`4 x REQUEST_TIMEOUT (60 s) + b1..b4 (<= 22.5 s) = 262.5 s` on the wall and
`124.5 s` on the network — so one wall waiter's whole call is bounded at
`5 x 60 + 4 x 262.5 = 1350 s` and one network waiter's at 429 s. What guarantees
the report arrives at all is `retry_throttled`'s `finally`, which stands an owner
down whatever door it leaves by. A round whose owner thread DIED without doing
so is released by the next worker to meet the same wall, which costs that worker
one GET against a wall this round had already measured. The case none of them
covers is a GET that never returns: the owner is then a worker thread the task
group is waiting on, so the command is wedged whatever its waiters do — that is
a request timeout's job, not a ladder's.

A call whose own attempts are spent never parks at all. It cannot use a backoff,
so waiting for one is latency it will throw away, and it takes no round it will
not probe. It does still book the rung of a round it already owns: that booking
is how the group learns the wall has been measured to the end, and an owner that
walked away without it leaves every waiter to spend a GET proving what the call
already knew — measured at 9 GETs for a four-cabin outage, against the
`3 + (cabins - 1)` the table above bounds one at, which is 6 for four cabins.

A successful call REFILLS the WALL's rungs: the wall is per-IP, so any call
getting through is evidence it lifted whoever made it, and a wall that returns
later is a different one. It does NOT refill the network's rungs for everybody —
fli's session is a `threading.local`, so the socket that carried a sibling's
call is no evidence about this one's, and crediting it let every healthy cabin
hand a failing one another rung. Only the worker that met a transport failure
gets those back.

**That is what the "one ladder" budget is bounded by — no success getting
through, not elapsed time.** And because the wall's refill is shared and
correct, the ladder alone cannot bound a single call: each `retry_throttled`
call carries its own attempt count as well, so a flapping link costs a bounded
number of requests per call whatever the siblings are doing. There is no
time floor: a success five milliseconds old refills the budget exactly as one
from half an hour ago does. So a wall that lets the prober past and closes again
refills on each probe and costs more than one ladder. No single count is quoted
for that here, because it turns on what "only the prober gets through" means: a
wall that stays open for as long as a prober is through costs about one ladder,
while one that admits only the owner's own probe costs roughly three times that
at three cabins. Each reading is deterministic; they are different questions.
What is bounded whatever the wall does is the per-call cost in the table above.

The rejected alternative is worth recording, because it is the lever if a
request bound is ever traded away. A shared DEADLINE — every worker retries on
its own schedule until one clock expires — recovers every cabin just as well
and does not bound requests at all, since each worker keeps spending until the
deadline. Bounding AND recovering needs a shared budget plus a broadcast of the
probe's outcome, which is a counter and a condition variable; that is what this
is.

A decorator cannot express this, which is why the loop is written out: retry
decorators bound ONE call against a counter of its own, while this budget
belongs to the per-IP wall and is shared sideways across worker threads.

Owning the ladder re-homes one thing fli's `Client.get` does for us: it also
retries transport errors three times. `retry_throttled` carries a third arm
for a curl-level failure — a reset connection, a read timeout — on a
deliberately smaller budget than the throttle arm. A throttle is a wall that
lifts on its own; a transport failure that survives three attempts is usually
the network being down, and a long backoff there only delays the Matrix
fallback the user is going to get anyway. When the budget is spent it becomes
`GfTransportError`, so the enriched path degrades to Matrix, `--backend gflight`
prints a typed line rather than a curl traceback, and the pin loop can tell an
unreachable network apart from a board that refused for its own reasons.

Only a failure to REACH Google is retried — `curl_cffi`'s `ConnectionError` and
`Timeout` (DNS, a reset socket, connect and read timeouts), **plus four
result codes those classes do not cover**: `PARTIAL_FILE`, `HTTP2`,
`HTTP2_STREAM` and `HTTP3`. curl_cffi maps several codes onto classes that also
carry permanent faults, so the class alone cannot decide — a multi-megabyte body
cut short arrives as `IncompleteRead` and the HTTP/2 and HTTP/3 stream errors
all arrive as `HTTPError`, which is otherwise a status never to retry.

Classify by class OR code, **minus a deny-list read first**. `SSLError`
subclasses `ConnectionError`, so the class arm sweeps in seven codes that name
this machine's own TLS setup: a CA bundle or CRL it cannot read, a crypto engine
it does not have, a pin that does not match, a client certificate the server
would not take. Those are identical on the third attempt, and reporting them as
"Google Flights could not be reached" sends the reader to the network for a
fault that is local. So TLS is both retried and not, by code — which is why the
decision is enumerated per code in the test rather than re-derived from the
rule: a test that restates the rule agrees with it even where it is wrong.

Everything else propagates on the first try, including the rest of curl's own
`CurlError` tree (`InvalidURL`, `InvalidSchema`, `SessionClosed`,
`CookieConflict`, `ImpersonateError`, `TooManyRedirects`). Those name a request
WE built wrongly — the shape a `build_search_tfs` regression takes — and
retrying a bug three times and relabelling it "Google could not be reached" is
how a defect becomes unfindable.

The request timeout is fli's own `REQUEST_TIMEOUT`, imported rather than copied:
it is the value that reads and validates `FLI_TIMEOUT`, and a duplicate constant
here silently ignores whatever the user set.

**Where a served page puts its rows varies, so no count of blocks is a validity
test.** Six live pages, measured 2026-09-02:

| page | `ds:1[2]` | `ds:1[3]` | arity |
|---|---|---|---|
| JFK-LAX one-way | 3 rows | 27 rows | 32 |
| HNL-MIA round-trip outbound, business | 3 rows | 5 rows | 31 |
| HNL-MIA round-trip outbound, first | 2 rows | 4 rows | 31 |
| HNL-MIA pinned return (business) | **`None`** | 3 rows | 27 |
| HNL-MIA pinned return (first) | 3 rows | 2 rows | 27 |
| HNL-MIA one-way nonstop, no nonstop exists | **`None`** | **`None`** | 32 |

Two things follow, and both cost a release-blocking bug to learn. A pinned leg
**may** omit `[2]` — the business return did, the first-class return did not, so
do not build a rule on it or on a story about top-flights ranking. And a
genuinely flight-less board is an ordinary results page with no flight cards and
**no block at either index** — refusing that reports "the page shape changed"
for a route that simply has nothing matching. Two synthetic shapes are also
pinned in the fixtures (an empty block `[[]]` at both indices, and at one); they
have never been seen in the wild and are labelled as synthetic, but an empty
block at those indices with nothing misplaced must not read as a refusal if
Google starts sending one. It must not win a blob contest either — an empty husk
carries no rows, which is why `_extract_ds1` counts rows rather than blocks.

So the guard is a POSITIVE scan rather than a count. `_rows_from_ds1` collects
rows from `[2]`/`[3]` structurally, and separately probes every OTHER top-level
index for a block whose leading rows actually parse as flight rows:

- rows only at `[2]`/`[3]` → those rows (however many blocks carried them)
- nothing row-shaped anywhere → `[]`, an authoritative empty
- rows found outside `[2]`/`[3]` and **none served from them** →
  `GfPageShapeError`
- rows found outside `[2]`/`[3]` **with rows also served** → the served rows,
  and a `log.warning` naming the indices
- a payload too short to reach `[3]`, or a value at `[2]`/`[3]` that is neither
  absent nor row-shaped → `GfPageShapeError`

`None` and a bare `[]` both count as ABSENT at `[2]`/`[3]`: neither carries rows
and neither claims anything, and `None` is the shape Google actually sends.
`[[]]` is a different fact — a block that exists and holds no rows. Either way
it serves no rows, and served rows are the only thing the refusal predicate
turns on; `blocks_seen` is carried for the message and the debug line, not for
the decision.

That last case is not pedantry. Enumerating a list never visits an index that
isn't there, so a truncated or junk `ds:1` (`[]`, `[null]`) would otherwise fall
straight through the scan and be served to the user as "no flights on this
route" at exit 0.

The two probes differ on purpose. Away from `[2]`/`[3]` the test must PARSE a
row, because `ds:1` carries other list-of-list-of-list structures on every page
(indices 1, 6, 7, 11, 14, 17, 25, 26 and 30 across the three captures) and a
nesting-depth test would report a relocation on every ordinary page. It reads
EVERY row, not a leading window: unparseable rows at the head of a moved block
are exactly what a layout change looks like, so any fixed depth is a number some
payload sits just past. The decoys hold 2-7 rows and the scan is sub-millisecond,
so full depth costs nothing worth a cutoff.
At `[2]`/`[3]` the test must NOT require a parse, or a block whose rows have all
changed shape would drop to an empty board instead of reaching the 0-of-N parse
guard below, which is what catches a moved ROW layout. Both share one tuple of
"this did not decode" exception types (`_ROW_PARSE_ERRORS`), so a widening —
`OverflowError` from an absurd price, `TypeError` from a null legs field — can't
land in one and miss the other.

**What this does not detect, stated plainly:** a partial relocation — rows
leaving `[2]` while `[3]` still parses — yields a short board. There is no
signal that proves it: one block is an ordinary served shape (the business
pinned return), so a missing block cannot be told from a board that never had
one. Refusing a single-block page to catch it breaks every round trip, which is
the worse trade; under-returning is the accepted cost.

Refusing on the misplaced-block probe alone is worse for the same reason. Live
pages carry 4 to 9 blocks that are row-shaped by structure (4, 9 and 7 across
the three captures), so a Google row-schema change that makes any ONE of them
parse would refuse a board we can already serve completely. A partial relocation
therefore under-returns **with a `log.warning` naming the indices**: the user
keeps their results, and the next maintainer has the indices to re-derive
from.

The refusal predicate is `misplaced and not rows` — rows found somewhere else
and none served from where we read. It deliberately does NOT also require
`not blocks_seen`: an empty block at `[2]`/`[3]` is not how Google answers a
flight-less search. The MEASURED flight-less shape is `None` at both indices,
and an empty husk `[[]]` has never been seen on a live page, so a husk plus
flight rows sitting elsewhere is far likelier a relocation than a coincidence —
and the two outcomes are not symmetric, since refusing degrades to Matrix while
reading it as an empty tells the user the route has no flights. A zero-row board
with nothing misplaced is still an authoritative empty.

## Tier model: who honors each constraint

`routing_predicates.classify(routing, extension)` parses both DSLs into a flat
predicate set, each tagged with a tier:

- **Tier 1 — native GF filter** (`fli_bridge.apply_gf_native_filters`): marketing
  carrier *include* (`LH+`, `AIRLINES`), alliance, connect-at airport
  (`F* X:FRA F*`), `MAXCONNECT`, `MAXDUR`, nonstop/`MAXSTOPS`.
- **Tier 2 — post-filter on the result** (`_gf_postfilter`): operating carrier
  (`O:`/`OPAIRLINES`), marketing/airport *exclude* (`~UA`, `~DFW`, `-CITIES`,
  `-AIRLINES`), `-CODESHARE`, specific flight #/range.
- **Tier 3 — Matrix only**: fare construction (`F bc=y`, `aa.lon.yup`), mileage,
  `PADCONNECT`, aircraft, and anything the parser can't confidently classify.

Routing language is **positional**, so it's parsed all-or-nothing: only single
order-independent forms map (one carrier-with-quantifier, nonstop, one flight #,
the `F* X:LHR F*` via-airport idiom). Ordered chains (`BA AA`, `DFW DEN`), bare
single-segment carriers (`LH` without `+`/`*`), country filters, and count
placeholders escalate the whole routing to Tier 3 — never partially honored.

**The tiers are the DATE GRID's question** — it still POSTs
`GetCalendarGraph`, so `_gf_dategrid.grid_can_serve` reads them directly, and it
is **Tier-1-only**: the grid returns prices per date, not itineraries, so there
is nothing for a Tier-2 predicate to post-filter and any Tier-2 predicate sends
the whole calendar to Matrix. Native filters are a pure *optimization* there —
if an fli carrier/airport code doesn't map, that query dimension is skipped (no
under-return) and the post-filter (a string-based backstop that also enforces
marketing-include + connect-at) is the correctness guarantee.

`_gf_postfilter.gf_can_serve` is the looser rule — no Tier-3, but Tier-2 is
admitted as long as this module can evaluate it — and it has **no production
caller**: `_pick_backend` asks `page_can_encode` instead. Only its own tests
reach it. Re-wire it or delete it; don't cite it as the gate.

**The SEARCH gate is `page_can_encode`**, above: strictly narrower, because the
page's tfs= parameter has no field for most of Tier 1 and the ~30-row board
makes post-filtering Tier 2 unsafe. `_gf_postfilter` stays wired in as the
backstop; it just has less to do.

Time-based Tier-2 predicates (`MINCONNECT`, `-REDEYES`, `-OVERNIGHTS`) currently
escalate to Matrix — `_gf_postfilter` can't evaluate them yet (no per-segment
times threaded through `LegInfo`). Promote by threading those times, then adding
them to `_SUPPORTED` + `_slice_passes`.

## Progressive enrich (`_run_enriched_path`)

For a GF-serveable query (default; `--fast`/`--no-enrich` opts out, JSON output
stays GF-only), GF and Matrix are dispatched **concurrently** under one
`anyio.run`: GF runs in `anyio.to_thread.run_sync` (it's sync curl_cffi) while
the Matrix request progresses on the event loop. GF paints first (~1s); when
Matrix lands (~45s) `_enrich.merge_results` reconciles by flight #+date and
`_render_merged` repaints with both prices attributed (they can differ a lot —
Matrix surfaces cheaper published fares). PP/awards + URLs run on the Matrix
(authoritative) result. Per-backend `try/except` so one failing still shows the
other.

**Codeshare display**: marketing matching is loose (Matrix-consistent: a flight
sellable as LH matches `LH+` even if its primary number is UA). To keep that
honest, `_leg_display` relabels a codeshare match to the matched identity —
`LH9403 (op UA58)` under `--routing LH+` — using `marketing_flights` +
`_match_carriers` (marketing-include filters only).

## GF date-grid (calendar) — `fli.search.dates.SearchDates`

Google's `GetCalendarGraph` RPC returns a whole date window's cheapest-per-date
prices in ONE call, and `DateSearchFilters` carries the full Tier-1 filter set
(airlines, stops, layover, max_duration, cabin, times, price). Verified
2026-06-14: `airlines=LH` / `stops=NON_STOP` change the grid prices, so Tier-1
filters ARE honored. It returns `{date, price}` only — **no itineraries** — so
Tier-2 (`O:`/`-CODESHARE`/`~UA`/flight#) can't be post-filtered on a grid; those
calendars go to Matrix. This is the throttle-friendly calendar primitive (1 call
vs a per-date fan-out), so we prefer it; **no GF fan-out is needed** (Matrix's
`_calendar_split` already fans out for Tier-2 / multi-airport).

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
chunking.

## GF throttle (per client-context, dynamic) — handle reactively, not with a fixed cap

Everything measured below was measured against the **RPC** transport, which is
what the date grid still uses. The search page is a different endpoint with a
different budget and a different block signal (the captcha interstitial, by
redirect or in place; or an HTTP 429, which arrives as a response status), so treat the
numbers as the grid's and re-measure before quoting them for the page. The
reactive design carries over unchanged: both raise `GfThrottledError` into the
same `retry_throttled` backoff.

The budget is keyed on **client context, not just IP.** Verified 2026-06-15
(`research/experiment_gf_patchright.py` + `capture_gf_request.py`): a real Chrome
(patchright, `channel=chrome`) pulled 10/10 `GetShoppingResults` from an IP that
was *simultaneously* `code-13` throttling our curl_cffi client (re-probed the same
minute). curl_cffi's chrome146 TLS fingerprint passes the edge (we reach the
backend — a structured error, not a CAPTCHA), but the generous budget is gated
behind dynamic, JS-generated session proof the SPA sends and we don't: URL
`f.sid`/`bl`, a token embedded in `f.req`, and the `x-goog-batchexecute-bgr`
per-request integrity token (plus `x-same-domain`/`origin`/`referer`, `accept: */*`
vs our navigation `text/html`, high-entropy client hints, and `OTZ`/`__Secure-BUCKET`
cookies beyond `NID`). No `x-client-data` and no `at` XSRF token are involved. So a
thin curl_cffi client gets a deliberately small budget that static header mirroring
can't fully close (bd work-udpp1). Datacenter VPN exits — e.g. PIA — are also
pre-flagged and blocked on sight; only residential IPs work.

Measured 2026-06-14 for the curl_cffi path (instrumented, distinguishing genuine
`code-13` from transport errors): two limits — a per-second burst cap (~3–4 at
~10/s, but it floated as high as 30 a run earlier) and a rolling allowance (~25–30
calls per ~2–3 min ≈ 10–12/min) — and **fast recovery** (the call right after a
block often returns data). Because the ceiling moves, a fixed rate limiter is the
wrong tool. The design is a closed loop: `_classify` detects a real block (HTTP 200
+ `ErrorResponse`/code-13 body, vs a transport exception, vs cold-session empty),
and `_one_call_with_retry` backs off + retries on a genuine block (typed
`GfThrottledError` on exhaustion). One-shot `flight` processes can't share a
proactive budget, but they DO share the `code-13` signal, so per-process reactive
backoff self-regulates even across concurrent invocations. In the woven flow a
persistent GF throttle degrades to Matrix-only rather than erroring.
