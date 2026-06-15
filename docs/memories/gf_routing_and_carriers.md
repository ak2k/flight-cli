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

## GF throttle (per-IP, dynamic) — handle reactively, not with a fixed cap

Measured 2026-06-14 (instrumented, distinguishing genuine `code-13` from
transport errors): the GF RPC throttle is **per-IP and dynamic**, with two
limits — a per-second burst cap (~3–4 at ~10/s, but it floated as high as 30 a
run earlier) and a rolling allowance (~25–30 calls per ~2–3 min ≈ 10–12/min) —
and **fast recovery** (the call right after a block often returns data). Because
the ceiling moves, a fixed rate limiter is the wrong tool. The design is a
closed loop: `_classify` detects a real block (HTTP 200 + `ErrorResponse`/code-13
body, vs a transport exception, vs cold-session empty), and `_one_call_with_retry`
backs off + retries on a genuine block (typed `GfThrottledError` on exhaustion).
One-shot `flight` processes can't share a proactive budget, but they DO share the
`code-13` signal, so per-process reactive backoff self-regulates even across
concurrent invocations. In the woven flow a persistent GF throttle degrades to
Matrix-only rather than erroring. (Datacenter VPN exits — e.g. PIA — are
pre-flagged and blocked on sight; only residential IPs work.)
