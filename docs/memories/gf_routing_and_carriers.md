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

The count is **structural**, exactly like the scan at those indices: rows are
counted, never parsed, so a board whose rows have all changed shape still wins
and reaches the 0-of-N guard as a layout change rather than losing to a husk.
With no blob carrying rows the fallback takes the first one long enough to reach
`[3]`, then the first decodable one at all — so a genuinely flight-less page
stays flight-less and a truncated placeholder above it does not become a shape
error.

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
| a persistently throttled multi-cabin fan-out | one ladder for the group: 5 + (cabins - 1) |

Two things make those numbers hold. `_fetch_page` goes through fli's SESSION,
not `Client.get` — which is wrapped in `@retry(stop_after_attempt(3))`, so a
throttled leg used to cost up to 15 GETs as fli's ladder ran inside each of
ours. `retry_throttled` is the only ladder now. And the round-trip pin is capped
at 10 regardless of `top_n`: the multi-cabin path bumps `top_n` 5x (to 100) to
widen the pool it filters, which was free on the old RPC and would otherwise
mean ~2 x 31 page fetches for a two-cabin round trip. The default `-n 10` is
unchanged by the cap.

The bump therefore widens the leg-1 rows each cabin keeps and NOT the round-trip
pins, so **Google Flights joins cabins on each cabin's 10 cheapest outbounds;
'—' means no shared itinerary, not no fare.** `cli._run_gflight_path_multi`
prints that sentence on a multi-cabin round trip, because an empty cabin cell
otherwise reads as "that fare does not exist". Widening the join means pinning
on the intersection of the cabins' outbounds rather than raising the cap; that
is a separate design and is tracked on bd work-h70kv.

The two counters are independent, so one leg can spend both budgets: four 429s,
two transport blips and a final 429 costs 7 GETs. That is the ceiling, and it is
deliberate — a wall that lifts and a network that drops are different failures,
and sharing one counter would let a blip eat the throttle budget.

A throttle aborts the whole search after ONE ladder per cabin: `search_with_ids`
does not catch `GfThrottledError` in its pinning loop, so the remaining pins are
never fetched. That is deliberate — the throttle is per-IP, so the next leg
would hit the same wall.

Per **cabin** is not per **search**, and the multi-cabin fan-out runs a cabin
per thread. Four cabins laddering separately spend 4 x 5 = 20 multi-megabyte
GETs against an IP that is already refusing us, to learn what the first ladder
learned. `_gflight_ids.shared_throttle_ladder` — armed by `cli._run_gflight_multi`
around the fan-out — gives the whole group one ladder to draw retry numbers
from: the first worker to exhaust it trips it, and every other worker's next
throttle re-raises without another GET.

Owning the ladder meant re-homing one thing fli's `Client.get` did for us: it
also retried transport errors three times. `retry_throttled` now has a third
arm for a curl-level failure — a reset connection, a read timeout — on a
deliberately smaller budget than the throttle arm. A throttle is a wall that
lifts on its own; a transport failure that survives three attempts is usually
the network being down, and a long backoff there only delays the Matrix
fallback the user is going to get anyway. When the budget is spent it becomes a
plain `GfBackendError`, so the enriched path degrades to Matrix and
`--backend gflight` prints a typed line rather than a curl traceback.

Only a failure to REACH Google is retried — `curl_cffi`'s `ConnectionError` and
`Timeout`, which is DNS, TLS, a reset socket, connect and read timeouts.
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
one. An earlier revision refused single-block pages to catch this and broke
every round-trip instead. Under-returning is the accepted cost; the alternative
measured worse.

Refusing on the misplaced-block probe alone was the other tempting fix, and it
is worse for the same reason. Live pages carry 4 to 9 blocks that are
row-shaped by structure (4, 9 and 7 across the three captures), so a Google
row-schema change that makes any ONE of them parse would refuse a board we can
already serve completely. So a partial relocation now under-returns **with a
`log.warning` naming the indices** rather than refusing: the user keeps their
results, and the next maintainer has the indices to re-derive from.

The refusal predicate is `misplaced and not rows` — rows found somewhere else
and none served from where we read. An earlier revision also required
`not blocks_seen`, on the theory that an empty block at `[2]`/`[3]` is how
Google answers a flight-less search. It is not: the MEASURED flight-less shape
is `None` at both indices, and an empty husk `[[]]` has never been seen on a
live page. A husk plus flight rows sitting elsewhere is far likelier a
relocation than a coincidence, and the two outcomes are not symmetric —
refusing degrades to Matrix, while reading it as an empty tells the user the
route has no flights. A zero-row board with nothing misplaced is still an
authoritative empty.

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
redirect or in place; or an HTTP 429 that fli's client raises), so treat the
numbers as the grid's and re-measure before quoting them for the page. The
reactive design carries over unchanged: both raise `GfThrottledError` into the
same `retry_throttled` backoff.

**Budget arithmetic on the page path.** fli's `Client.get` is wrapped in
`@retry(stop_after_attempt(3))`, so a hard 429 costs THREE requests before our
own backoff ever sees it — and `retry_throttled` then makes up to 5 attempts of
its own. Worst case a single throttled leg fetch spends ~15 requests against an
IP that is already blocking us. Budget accordingly before raising either count,
and prefer widening fli's backoff to widening ours.

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
