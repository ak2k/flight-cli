# GF carrier semantics + routing tiers + progressive enrich

How `--routing`/`--extension` reach Google Flights, and the carrier-identity
indices that make it correct. Read before touching `routing_predicates.py`,
`_gf_postfilter.py`, `fli_bridge.apply_gf_native_filters`, or
`_gflight_ids._parse_leg_amenities` / `_flight_leg`.

## Where the rest went

- [gf_search_transport.md](gf_search_transport.md) — the search page's `tfs=`
  fields (`links.build_search_tfs`), aliased codes (`fli_bridge.fli_airport`),
  price cap, bags, CO2, row checks, the full board.
- [gf_page_refusals_and_ds1.md](gf_page_refusals_and_ds1.md) — typed refusals,
  which `ds:1` blob is served, where rows sit, the authoritative empty.
- [gf_request_budget.md](gf_request_budget.md) — page GETs per search, the pin
  cap and pin choice, what a round-trip row's price means, partial round trips,
  the three Matrix failures typed rather than raised as a traceback.
- [gf_row_order_and_merge.md](gf_row_order_and_merge.md) — `-n`, `--pick`,
  price order, the merged table, the multi-cabin join key, currencies.
- [gf_throttle_ladder.md](gf_throttle_ladder.md) — the shared throttle and
  transport ladder, curl error classes, Google's rate budget.
- [gf_separate_tickets.md](gf_separate_tickets.md) — the Cheapest tab's
  separate tickets and self transfers.
- [gf_multi_page_legs.md](gf_multi_page_legs.md) — legs over 11 airports as
  several pages, `calendar --fast -d` ranges, `search --split`.
- [gf_date_grid.md](gf_date_grid.md) — the gated calendar RPC, `--fast`
  refusals, the Chrome price graph.

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
  (`F* X:FRA F*`), `MAXCONNECT`, `MAXDUR`, nonstop/`MAXSTOPS`. With several stop
  limits (`--stops` and any `MAXSTOPS`/`N`) the strictest wins.
- **Tier 2 — post-filter on the result** (`_gf_postfilter`): operating carrier
  (`O:`/`OPAIRLINES`), marketing/airport *exclude* (`~UA`, `~DFW`, `-CITIES`,
  `-AIRLINES`), `-CODESHARE`, specific flight #/range, `MINCONNECT` (the search
  page also encodes it as 3.17; the grids have no rows to check it on),
  `-REDEYES` and `-OVERNIGHTS` (the search's rows only; the grids refuse both).
- **Tier 3 — Matrix only**: fare construction (`F bc=y`, `aa.lon.yup`), mileage,
  `PADCONNECT`, aircraft, a carrier list naming a token that is not an airline
  code (`-AIRLINES UA,DL`), and anything the parser can't confidently classify.

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

**The SEARCH gate is `_gf_postfilter.search_page_reasons`**, above: a
predicate passes when the page encodes it (`page_can_encode`'s stop ceiling, or
`_served_by_page`: carrier include, alliance, `MAXDUR`, `MINCONNECT`/
`MAXCONNECT` with a positive maximum), or when it is Tier-2, the post-filter
evaluates it, and it is neither a connection-airport exclude nor a
flight-number range with `+` or `*`. `F* ~DUB F*` is one connection not at DUB
to Matrix, which drops the nonstops the filter keeps.
That gate is set out in [gf_search_transport.md](gf_search_transport.md).

A lone flight-number token is the whole slice to Matrix, and the filter reads it
that way: every leg booked under the carrier and numbered in the range, and for
one flight (bare or `?`) every leg under one number, which is Matrix's flight.
On JFK-LAX bare `AS21` is "No solutions" (2026-11-04) and `AS21+` 0 solutions
(2026-10-20), while Google's board holds AS21+AS487 and AS21+AS696, which an
any-leg filter kept; `DL747`, `AA300` and `AA1` are each one nonstop row. So one
number with any quantifier, and a bare or `?` range, are served on Google.
`AA1-3000`, `AA1-3000?`, `AA1-3000+` and `AA1-3000*` all answered with the same
10 AA nonstops, so what `+`/`*` admits over a range (several flights in it, per
`routing_language.md`) is unmeasured; those two stay on Matrix with the reason
`a flight-number range (AA1-3000+)`. Matrix rejects a reversed range
(`AA3000-1`), `AA0` and `AA10000` as `Bad route specification`, and reads
`AA00001` as AA1 (2026-09-30), so a range that is not ascending within 1-9999
stays on Matrix too, which reports the error. So does a number whose carrier
fli has no code for (`JP627`, `XX1`): the rows' carriers are read through fli's
table, so the filter would match none, and the reason is the carrier include's,
`a carrier Google Flights has no code for (JP)`. Multi-token forms (`AS21 F+`),
`~AS21` and a carrier with a digit (`B6123`) are Tier 3. `page_can_encode` itself stays
narrow: the `--fast --gf-transport http` gate (`_gf_calgraph.page_blocker`)
reads it as it is. The Chrome price graph cannot check rows either, and has its
own gate, `_gf_calgraph.graph_blocker`, which admits the includes and bounds
Google was measured applying from the URL (Admission, below).
Admission is in [gf_date_grid.md](gf_date_grid.md).

`-REDEYES` and `-OVERNIGHTS` are row checks on the search page, read off each
leg's local clocks (`_gf_postfilter._red_eye`, `_overnight_stop`), in place of
a Matrix hand-off about 45 times slower. A red-eye leg lands on a later local
date than it took off, takes off 00:00-04:59, or has clocks and duration twelve
hours or more apart (it crosses the date line, where a night flight can land on
its takeoff date). An overnight stop is a connection whose next leg leaves on a
later local date than the landing there, or whose landing is 00:00-04:59.
Matrix's `LAX JFK --dep 2026-10-20 --ext -REDEYES` gave 10 solutions, each
landing the same day by 23:55. Over 74 saved Matrix nonstop slices, "lands on a
later local date than it took off" reproduced Matrix's overnight flag on 73;
the 74th (B61024 LAX 16:30 -> JFK 00:52) Matrix keeps and the rule drops. None
of those slices crosses the date line, so the rule was never measured on one
that does. Both arms also drop daytime long-haul legs Matrix may keep: HKG
10:05 -> JFK 12:20 the same day by the twelve-hour arm, LAX 11:00 -> NRT 15:00
the next day by the date arm; what Matrix does with them is unmeasured. A board
the checks empty goes to Matrix under auto. The date grids have no rows and
refuse both.

A party with an infant is asked of the page (field 8, 3 lap and 4 seat). For
one adult and a lap infant on 2026-10-20, JFK-LHR served 30 rows (BA/AY USD324,
the adult fare plus a tenth), not the ~100-row board, and JFK-LAX none at all.
So under auto a board served no rows for a party with an infant goes to Matrix
with the note `Using Matrix: Google Flights served no rows for a party with an
infant.`; under `--backend gflight`, or beside a Google-only flag, the empty
board prints with a note naming how to ask Matrix. A multi-cabin compare with
an infant stays on Matrix, since its hand-off counts only rows a filter dropped.

## Progressive enrich (`_run_enriched_path`)

For a GF-serveable query (default; `--fast`/`--no-enrich` opts out; `--format
json` enriches only on an explicit `--enrich`, below), GF and Matrix are
dispatched **concurrently** under one `anyio.run`: GF runs in
`anyio.to_thread.run_sync` (it's sync curl_cffi) while the Matrix request
progresses on the event loop. GF paints first (~1s); when Matrix lands (~45s)
`_enrich.merge_results` reconciles by flight #+date and `_render_merged`
repaints with both prices attributed, a delta or a reason on every row, and a
caption saying where Matrix's page ends. PP/awards see Matrix's first `-n`
fares; the pins, `--pick` and `--sellers` see the first `-n` merged rows.
Per-backend `try/except` so one failing still shows the other.

**The cross-check reads Matrix's whole answer.** The one Matrix request asks for
a page of max(-n, 500) (`cli._CROSS_CHECK_PAGE`); the links and the booking
page keep `-n`. Matrix answers in price order and `solutionCount` is its whole
answer, so a page of `-n` compared Google's whole board with Matrix's first
`-n` fares. Measured 2026-10-01, uncached: JFK-LAX one-way answered 10
solutions at page 500 (55 s, 12 KB) as at page 10; the JFK-LAX round trip
answered 10 of 40 at page 10 (52 s) and 40 of 40 at page 500 (37 s, 53 KB);
JFK-LHR one-way answered 10 at page 10 (32 s) and at page 500 (20 s, 13 KB).
Depth costs no measurable time, and on a one-way it mostly proves completeness.

**A delta compares one trip in one currency.** `MergedRow.google` is the
Google row whose price the row shows, and `same_trip` holds only where
`_date_lender` gave it. The key's first Matrix row priced by a Google row left
over shares the flights and the first day, not the trip (the FI614/FI450 rows
above land on different days), so it shows no delta and the reason
`trip_unconfirmed` names both landings; a pair in two currencies is
`other_currency`. Google prices the whole party, on its search page and its
booking page alike, while the price Matrix lists is one passenger's, rounded
up (2 adults, 2026-10-01: `ext.price` USD229.00, `displayTotal` USD456.80,
Google USD457.00; Google's AS21/AS487 is USD437.00 for two and USD219.00 for
one). So `merge_results(..., passengers=)` puts Matrix's price for the party
on the row (`_enrich.party_price`: the listed price for one, `displayTotal`
for more), and the Matrix column, the rank, the delta, the `--sellers`
comparison, the caption and the document all read that one price; where
Matrix states no total the row is `unpriced`.
The FI614/FI450 rows are in [gf_row_order_and_merge.md](gf_row_order_and_merge.md).

**A row ranks on the lowest price it prints** (`_enrich._rank_price`): the
lowest in the requested currency, by exact amount, or where it prints none in
it the lowest in the currency of Matrix's price, else Google's; rows tied on it
keep the merge's order, a matched row ahead of a Google row alone. Ranked on
Matrix's listed price, a party's Matrix rows sorted on one passenger's fare
against Google's party totals: the live JFK-LAX `--adults 2 -n 10` table
(2026-10-01) printed ten GF+MX rows from USD456.80 to USD796.80 and cut
Google's AS21/AS487 and AS21/AS696 at USD437.00 and five rows at USD457.00. And
a matched row Matrix prices above Google left the first `-n` on the deeper
page: DL1788 at Google USD204, Matrix USD999, gave way to B61023.

**A reason is stated only where the two answers decide it**
(`_cross_check.py`). Matrix's answer is complete when `solutionCount` is at
most the rows listed. A Google-only row: `carrier_absent` ("no AS flight in
Matrix's answer of 10") only on a complete answer, decided from the flights it
lists: Matrix prunes its answer, so this says nothing about its inventory (its
10-solution JFK-LAX answer on 2026-10-01 held DL and B6 nonstops at USD229 and
ran to USD389, yet left out B61523 and B6123 at USD229), and
`itineraryCarrierList` labels a trip by one carrier (UA+LH under LH), so it
cannot show a carrier absent. `stops_outside` only without a stop limit, when a
slice has more flights than one beyond the fewest Matrix listed there before
`--max-price` cut any fare (its `maxLegsRelativeToMin` is 1). `past_page` names N of M and the last price.
`capped` where `--max-price` cut Matrix's fare for the row's own trip, which
it names. Otherwise `not_in_matrix`. A Matrix-only row: `no_google_answer`;
`outbound_not_priced` on a round trip whose outbound leads no Google
combination (Google pins at most `pinned_fanout(-n)` outbounds);
`carrier_absent_google` only on a one-way or beside an outbound Google priced,
and not on a board asked as several pages that misses a page or is a round
trip (each page prices returns only between its own airports); neither of
those two on a board the row filter cut; otherwise `not_on_google`.
A Google row sold as separate tickets: `separate_tickets` alone (see the
Cheapest-tab section).
That section is [gf_separate_tickets.md](gf_separate_tickets.md).
While the board counts rows Google served that the parser could not read
(`Board.unread`, a round trip's summed over its outbound page and every return
page it read, a return page none of whose rows parsed included though it
refuses its pin, and a board asked as several pages summed over every page, a
page missing because none of its rows parsed included), a
Matrix-only row that would say `carrier_absent_google` or
`not_on_google` says `google_unread` ("2 of Google's rows could not be read")
instead: its trip may be one of them. On the JFK-LAX board cut to DL1788 and
an unreadable AS21/AS487 row, one row parses, and without the count Matrix's
AS21/AS487 row reads `carrier_absent_google`. The caption reads `Google listed
95 rows, 2 unread.` where any are.
Either side: `paired_elsewhere` where the other side lists the same flights,
first day and landing minutes on another row, because a middle flight's day is
then unstated; `unmatched` where a row leaves a flight number, day or landing
unstated, so neither absence and no unpriced outbound is decided for it (no
live or fixture row has done so). Point of sale is never a reason: Google is always `gl=US`,
Matrix is sent no sales city, and no row says where it was priced.

**Google's low row is asked of Matrix.** A Google-only row's reason says what
Matrix's answer holds, not whether Matrix prices the row, because Matrix prunes
its answer. So where the first Google-only row the table shows is under every
fare in Matrix's answer, in the requested currency and for the party
(`_cross_check.low_row`), the search asks Matrix for that row's exact flights
once the table is printed: `--verify`'s chain search, uncached, and booking
details per candidate, with no fare rules and no unrouted second search, in one
`anyio.run` under `anyio.move_on_after(cli._LOW_CHECK_SECONDS)` (60 s), so the
bound cancels the request in flight. One line under the table answers it:
`Matrix asked for row N's flights (CHAIN DATE; CHAIN DATE): Matrix P · Google P`
with the gap, both prices for the party; `…: not priced as these flights: R`,
R being "Matrix returned no fare on these exact flights" or the other-itinerary
sentence; or, in yellow, `…: no answer: R`, R the 60 s or the error's kind and
message. The table, the row's reason and the exit code stay as they were. A
row both sides price, a Matrix fare in another currency or with no party
total, and Matrix's low at or under Google's ask nothing more. A row Google
sells as separate tickets is passed over for the next Google-only row: Matrix
prices one ticket, so a gap would say nothing of that booking. So is a row whose
cheapest listing states a leg in another cabin than the search's
(`_gf_postfilter.states_other_cabin`; an unstated cabin does not count), since
Matrix is asked in the search's cabin. Measured
2026-10-02 at `-n 10`, Google's low was under Matrix's whole answer on all four
routes tried, and the chain priced Google's exact flights on three: EWR-ORY
11-10/11-17, TAP USD429 against Matrix's 12 trips from USD527, Matrix
USD429.00 (26.7 s); MIA-LAX, F9 USD165 against 8 AA and DL trips from USD454,
Matrix USD170.00 (47.3 s); NYC-CHI, F9 USD139 against 10 trips at USD173,
Matrix USD144.00 (20.5 s). On JFK-LHR, AF9656 with DL9603/KL6149 at USD810
against 88 trips from USD818, the chain was empty (19.0 s); `--verify`'s
unrouted second search then listed only AA, BA and IB though the 88 trips name
DL and VS, so a carrier read off it would be wrong, and this check never asks
it. The bound does not cover building the client, which reads the API key from
its disk cache. The check's client is built with `rebootstrap=False`: a 403
invalidates the cached key and is the line `…: no answer: ApiKeyResolutionError:
Matrix rejected the API key with HTTP 403.`, since the re-bootstrap other
clients run on a 403 is synchronous and would hold the loop past the bound. The
key can differ from the search's, whose answer may come from the response cache.

**`--format json --enrich`** writes `{"search": <the plain --format json
document, the same -n rows>, "cross_check": {"currency", "delta":
"google_minus_matrix", "matrix": {"listed", "solution_count", "complete",
"last_price"}, "google": {"listed", "answered", "unread"[, "separate"]}, "rows": [...],
"low_check"}}`, the rows being the table's, from the pure `_cross_check.document`.
`low_check` is null where no row was asked of Matrix, else `{"row",
"google_low", "matrix_low", "outcome"` (`match`, `other-itinerary`,
`no-solution` or `no-answer`), `"matrix_price", "delta", "reason", "routing"}`,
the prices for the party and `delta` Google minus Matrix on a match. Plain `--format json`
does not cross-check; on auto a failed Google query is still handed to Matrix,
as before, and `--fast` asks Matrix nothing. It needs no awards (`--cash-only`)
and no `--sellers` or `--split` (exit 2 otherwise); a Google-only flag (`--bags`, an arrival
window, `--exclude-basic`) prints the table's "No Matrix enrichment" note and the
plain document, and `--verify` prints its own such note
and writes `{"search", "verify"}` instead. Matrix failing leaves `cross_check`
null (exit 0), Google failing leaves `search` empty with every Matrix row
`no_google_answer`, both failing is exit 1 with stdout empty.

**Codeshare display**: marketing matching is loose (Matrix-consistent: a flight
sellable as LH matches `LH+` even if its primary number is UA). To keep that
honest, `_leg_display` relabels a codeshare match to the matched identity —
`LH9403 (op UA58)` under `--routing LH+` — using `marketing_flights` +
`_match_carriers` (marketing-include filters only).
