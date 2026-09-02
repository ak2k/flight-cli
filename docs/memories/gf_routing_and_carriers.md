# GF carrier semantics + routing tiers + progressive enrich

How `--routing`/`--extension` reach Google Flights, and the carrier-identity
indices that make it correct. Read before touching `routing_predicates.py`,
`_gf_postfilter.py`, `fli_bridge.apply_gf_native_filters`, or
`_gflight_ids._parse_leg_amenities` / `_flight_leg`.

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

**The gate** (`_pick_backend` → `_gf_postfilter.gf_can_serve`): GF serves a query
iff it has no Tier-3 predicate AND every Tier-2 predicate is post-filterable.
Native filters are a pure *optimization* — if an fli carrier/airport code doesn't
map, that query dimension is skipped (no under-return) and the post-filter (a
string-based backstop that also enforces marketing-include + connect-at) is the
correctness guarantee.

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
into the callers' broad `except` and prints `date-grid failed: type object
'Airport' has no attribute 'NYC'` — a transport fault named for a request no
transport was going to carry.

The weave `cli._run_calendar_enriched` prints one note — the observation plus the
bd id, not a cause — and then waits for Matrix. The note can only promise to
wait, not to deliver: it is printed while the Matrix request is still in flight,
and Matrix can still fail after it. **`--fast` never exits 0 without a grid.**
Every no-grid outcome — gate, throttle, an empty grid, or anything reaching the
broad except — prints "No Google Flights grid; drop --fast for Matrix." once and
exits 1. While the gate stands, a bad airport or date is one of the gate's own
exits rather than the broad except's, so what the user reads is the standing
reason; the broad except keeps the same exit code for whatever a live transport
throws once the gate flips. When the grid branch does not apply at all (JSON
output, a round-trip window, a multi-airport route, or routing above Tier-1)
`--fast` refuses up front on **stderr**, naming the shape, before any Matrix call
or JSON write — stdout under a JSON request carries a document or nothing, never
prose (work-h70kv.9). So a wrapper doing `--fast || fallback` can trust the exit
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

Those reason strings quote the user's `--routing` / `--extension` text verbatim,
and `err` is a markup-enabled console: `--routing 'BA[/weird]AA'` raised
`MarkupError` where it should have refused, and a `[bold]` form ate the token the
reader needed to see. `routing_predicates` has no console to escape for, so the
escape belongs at the render sites. The rule for the calendar and detail paths:
**anything that reaches `err.print` or `console.print` from user input or an
exception message is wrapped in `rich.markup.escape`** — the blocker, the
date-grid failure text, every argument parser's own message, and Matrix's
`kind` / `message` / `request_id`, which echo the routing string back verbatim
("Illegal COMMAND-LINE prefix: BA[/weird]AA") on the path with no refusal to
catch it first. Where the message also quotes, `repr` runs BEFORE `escape`:
reversed, `repr` doubles the backslash `escape` prepends and hands the tag
straight back to the parser. `tests/test_calendar_split.py` greps for the rule,
so a new unescaped interpolation in these functions fails the suite.

The grid paint in the weave and
`_render_date_grid` are runtime-dead until the gate flips;
`_run_calendar_enriched` itself still runs (it is what paints Matrix).

**Re-enabling is not just `_GRID_RPC_GATED = False`.** Nothing executes the
transport below the gate — there is no captured GetCalendarGraph envelope to test
it against, and inventing the shape is forbidden — so type-checking is its only
guard, which is why the gate is a flag and not an unconditional raise (a raise, and
`Final[bool]`, both make basedpyright treat the body as unreachable; measured).
The procedure: capture a real envelope into `tests/fixtures/`, add an ungated
contract test over it (request URL, encoded body, and the success / empty /
throttle branches of `_one_grid_call`), teach `_grid_filters` to map or refuse
city codes (they are not in fli's `Airport` enum, and while the gate stands it is
the only thing between them and an `AttributeError`), run a live smoke, then
flip. Do that when the RPC answers a plain client again, or when an attested
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

## GF throttle (per client-context, dynamic) — handle reactively, not with a fixed cap

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
