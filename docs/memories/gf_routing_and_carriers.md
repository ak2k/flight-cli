# GF carrier semantics + routing tiers + progressive enrich

How `--routing`/`--extension` reach Google Flights, and the carrier-identity
indices that make it correct. Read before touching `routing_predicates.py`,
`_gf_postfilter.py`, `fli_bridge.apply_gf_native_filters`,
`links.build_search_tfs`, `fli_bridge.fli_airport`, or
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
1  = 28 (constant)          8  = passenger kind, one per traveler:
2  = 2 (constant)                1 adult, 2 child, 3 infant on lap, 4 infant in seat
3  = segment, repeated      9  = cabin class
3.2  = departure date       14 = 1 (constant)
3.4  = selected leg, rep.   16 = max-uint64 pin (booking deep links only)
3.5  = stop ceiling         19 = 2 one-way, 1 round-trip (3 multi-city is unusable)
3.6  = carrier include, repeated: bare IATA codes and alliance names
       (ONEWORLD, SKYTEAM, STAR_ALLIANCE) in one list
3.7  = carrier exclude (Google ignored it on JFK-LHR; not written)
3.8/3.9   = earliest/latest departure hour   3.10/3.11 = earliest/latest arrival hour
3.12 = maximum duration, minutes
3.13 = origin   3.14 = destination   3.15 = layover airports (not written)
3.17/3.18 = min/max layover minutes
12 = price cap, whole units of the page's `curr=` (sent on a USD page only, up to 2**31-1)
13 = bags {2: carry-on (0 or 1), 3: checked count}, a zero count left out
```

The hour fields are whole hours and a "latest" hour is the last hour included:
the UI's "9:00 PM" latest arrival wrote 20 and "end of day" wrote 23, and all
four are written once any one is set (the unset side of a pair as 0 and 23).
So Matrix's morning (8:00-11:00) asks for 8 to 11, Google also returns 11:01 to
11:59, and the row filter drops those. Infant codes 3 and 4 are the reverse of
fast_flights' enum names: BA112 JFK-LHR priced $295 for one adult, $324 beside
a 3 (a tenth of the fare, a lap) and $589 beside a 4 (a seat). Every writer
(`build_search_tfs`, the pinned and booking links, `google_flights_url`)
writes Google's codes.

Two traps in that layout. **`3.5` is zero-based** while fli's `MaxStops` is
one-based (ANY=0, NON_STOP=1, …), so it's `enum.value - 1` and **omitted** for
ANY — writing a literal 0 pins every search to nonstop. **Carrier codes come
from the enum NAME, not its value**: fli maps codes to display names
(`Airline._0B.value == "Blue Air"`) and underscore-prefixes digit-leading ones,
so `airline.name.removeprefix("_")` is the code.

**Airport codes come from the member's name as well, and fli's enum aliases 48
of them to another airport.** `Airport` is an enum over a code -> display-name
table, so a code whose display name repeats an earlier one is an alias of that
member: `Airport.OKA` is `Airport.NAH` (Naha in Indonesia, not Okinawa), as NTL
is NCL, TRI is PSC, SVC is PGC and ZFA is FAO. A lookup through the enum asks
Google for the other airport, and fli's row decoder has no entry for an alias,
so every row Google serves at one fails. Build airport members only through
`fli_bridge.fli_airport`, which gives each aliased code a member of its own,
named that code with the same display name (so the JSON dump shows "Naha
Airport" for OKA and NAH alike). `tests/test_airport_alias_requests.py` fails
on any `getattr`/`hasattr` call on the enum or `Airport[...]` subscript under
`src/`. MLH is the one alias kept: it is EuroAirport's second code, the same
airport as BSL, and Google serves it only as BSL (JFK-MLH asked for MLH gave an
empty board, asked for BSL 8 rows). Measured 2026-10-01: `flight search LAX OKA
--dep 2026-10-20 --backend gflight --fast --format json` through the enum
printed `[]` with exit 0; through `fli_airport` it printed 27 rows (CI, BR, CX
via TPE or HKG, from USD577), each landing at OKA by its clock span: departure
to arrival less elapsed time is +960 minutes from LAX, where NAH gives +900.

**What the page costs us.** Without `tfu=` it serves Google's top ~30 rows per
leg. `links.google_flights_search_page_url` always sends `tfu=EgQIABABIgA`
(`{2: {1: 0, 2: 1}, 4: {}}`, the "show all" bit), which serves the full board:
JFK-LAX 30 -> 95 rows, JFK-LHR 22 -> 101 (measured 2026-09-27), at roughly twice
the page size (3.6 -> 7.5 MB, +0.7 s). A round trip costs one page fetch per
pinned outbound. The page encodes a stop ceiling, a carrier or alliance
include, a maximum duration, layover minutes, a departure-hour window per leg
and children; each was sent live and the board honored it (2026-09-27). On the
full board the Tier-2 predicates the post-filter evaluates the way Matrix does
are served by Google too, so the search gate is per predicate
(`_gf_postfilter.search_page_reasons`), and anything else routes to Matrix with
its reason printed. Still on Matrix: carrier and alliance excludes as encoded
fields (they stay post-filters), connection airports (3.15; Matrix's meaning is
positional), infants (Google answered JFK-LAX with no rows for any infant, so an
empty answer would not be one), seniors and youth (no Google kind), time
buckets that do not form one window, an alliance beside another carrier or
alliance include (3.6 is one list, so Google would answer either), a zero
`MAXCONNECT` or `MAXDUR` (fli's maximums are positive), and a carrier code fli
has no member for.

**Encoded constraints are checked on the rows too.** Google has ignored a field
it was sent (the carrier exclude on JFK-LHR), so `_gf_postfilter.routing_keep`
holds every row to what the row can show: the stop count (legs less one, held
to the strictest of `--stops`, a MAXSTOPS and a routing `N`, on every board),
the carrier include (any seller, the marketing reading), Google's own total
duration (`FlightResult.duration`, never a difference of leg datetimes, which
are local to each airport and off by the zone offset), every layover's minutes,
and the first departure's clock time, to the minute. A layover is the page's
own figure for that connection (`data[0][13]`, elapsed minutes). Where the row
states none it is the clock difference at the connecting airport, which a
daylight-saving change there puts an hour out; a negative one is such a change
and is not held against the row. An alliance is not checked: nothing here says
which carrier is in which alliance. Children are priced, not checked. When
these checks empty a board, the empty-answer line names every active check.

**A price cap and bags (`search --max-price N`, `--bags CHECKED[,CARRY]`).**
Both are top-level fields, written after the cabin (9) and before 14.
- Field 12 is a varint of whole units of the page's currency. On the JFK-LAX
  2026-11-04 page a cap of 250 served 26 rows at $204-$249, prices unchanged;
  JFK-LHR at 300 served priced rows at $293-$299 and kept 4 unpriced rows.
  Google honored it on a round trip's pinned return board as well (JFK-LAX
  10-20/27 at 450: all 15 returns $398-$442). Only a USD page is asked for
  it: a EUR page asked for a cap served fewer of the fares under it than the
  uncapped EUR page. JFK-LAX 2026-10-20 at EUR 240 served 34 rows against 45
  at or under 240 uncapped, all at the same prices; the 11 missing were AA
  connections at EUR 196-232. The USD page at 250 served all 16 rows the
  uncapped page had at or under $250. Off USD the page is fetched uncapped and the row check alone
  applies the cap. Every row is still held to it:
  `routing_keep` drops a row priced over the cap, unpriced, or priced in
  another currency, on every board, and an emptied board is reported as not
  matching "a price cap of USD 250". A board Google served empty says "no fare
  at or under USD 250" only when the page was asked for the cap; one fetched
  uncapped says "no results". N is compared with the printed price, which for
  a party is the total. `--sellers` holds the booking page's offers to the
  same rule, in its table and in `booking_options`; when none is left it says
  so on one stderr line and exits 1.
- Matrix has no price input, so it is asked in the cap's currency (USD when
  `--currency` is unset). Left unset, it priced LHR-JFK in GBP (cheapest
  GBP1004) and a USD 2000 cap kept none of it (2026-09-29). `cli._price_capped`
  cuts its page to the fares under the cap before the pick, the fare rules, the
  awards, the links and the enriched merge read it; Matrix answers in price
  order, so the cut loses no cheaper fare. When the cap drops a row of the
  fetched page, the JSON's
  `solutionCount` (top level and in `solutionList`) and the table's count are
  the kept rows (the GBP JFK-LHR page at 690 keeps 14 of 25). When it drops
  none, the fares past the page went unchecked, so both keep Matrix's total, as
  uncapped (88 beside the same page's 25 rows at 736); a larger `-n` fetches
  more of them. The facets, the price slider and `carrierStopMatrix` stay as
  served. The table prints no carrier x stops grid, whose cells are minima
  over every fare.
- Field 13 is `{2: carry-on, 3: checked}`. The forms sent live left a zero
  count out (checked 1 = `agIYAQ`, carry-on 1 = `agIQAQ`); the UI writes both
  (`agQQARgB` for one of each). JFK-LAX with one checked bag repriced all 95
  rows by $45-$55; EWR-ORD with a carry-on repriced the UA rows by $50-$95 and
  F9 by $40, leaving AA, DL and B6, which include one; on JFK-LHR both were
  no-ops. Google honors it on a pinned return board too.
- `row[4][6]` is `[checked, carry-on]`: the bags the row's price covers,
  matching the page's own "1 carry-on bag included. 0 checked bags included".
  JFK-LAX states `[0, 1]` on all 95 rows and `[1, 1]` with one checked bag
  asked; JFK-LHR states `[null, 1]` on 97 rows and nothing on 4; EWR-ORD with a
  carry-on asked states `[0, 1]` on all 74. A missing, short or malformed slot
  is "not stated" and never drops the row; some rows of a round trip state
  nothing. Under `--bags` each JSON row (each member of a pair) carries
  `bags_included: {checked, carry_on}`, null where not stated, as does each
  cash match in the award document (its own slice's statement), and the table a
  `bags` column: `incl.` only when the row states at least the count asked of
  each kind asked for, `not incl.` when it states fewer, `unknown` otherwise.
- The statement and field 13 both count the whole party: 2 adults state
  `[0, 2]`, and with 13 set JFK-LAX 2 adults cost $45 more once and 102 of 102
  rows stated nothing. So `--bags` takes one seated traveler.
- Matrix prices no bags, so `--bags` never reaches it: `--backend matrix` is
  refused, and on `auto` any reason that would send the search to Matrix
  refuses it instead, naming the reason. It skips the Matrix enrichment and
  the fallback after the row check empties a board, and `--sellers` is
  refused, since the booking page is not asked for bags. No refusal under
  `--bags` points at `--backend matrix`; it says to drop `--bags`.
- Neither flag takes more than one `--cabin`. Printed Google links carry
  neither field; under `--bags` each link says its prices leave the bags out.

The date grids do not serve any of the new constraints yet: `page_can_encode`
and each predicate's `Tier` still answer for them, and they have no rows to
check against. Only the strictest-stops rule reached them.

**Google's CO2 estimate (`row[22]`).** An 18-slot list on all 208 rows of six
captures: `[7]` the row's grams (whole kilograms), `[8]` the route's typical
grams (one value per board), `[3]` the signed integer percent from `[8]`, and
`[2]` Google's label for that comparison (1 lower, 2 typical, 3 higher, 0 none).
Each leg's own grams are `fl[31]`; `[7]` is their sum rounded to 1000. fli's
decoder takes the label from `[11]`, which with `[10]` compares the row with the
board's median grams instead. Google's help compares each flight with the
route's typical, `[8]`, so the label is `[2]`: `[11]` differs from it on 35 of 95
JFK-LAX and 40 of 101 JFK-LHR rows, and labels 13 rows lower at a percent of 0
to +4. JFK-LAX states grams on 95 of 95 rows and JFK-LHR on 100 of 101 (VS46
states only the typical). Each direction is its own: the HNL-MIA outbound page
states 4264000 and its pinned return board 1539000, and no page states a pair
total. Google says the estimate is for the passengers searched; only one adult
has been measured. Each Google JSON row fills `co2_emissions_g`,
`co2_emissions_typical_g`, `co2_emissions_delta_pct` and `emissions_tag`, and
each leg `co2_emissions_g`, null where the slot is empty. The Google table adds
`CO2 kg` (kilograms and the percent; green lower, red higher) when a shown row
has a figure.

**How the full board is served.**
- Rows are deduped per itinerary (every leg's carrier, flight number and
  departure datetime), keeping the priced and cheaper listing at the first
  listing's place. No true duplicate has been measured; the key keeps dates, so
  the same flight numbers a day apart stay two trips.
- The routing filter runs inside `search_with_ids` as each board is served: on
  the outbound BEFORE the pins are taken (pins are the cheapest rows of the
  board they are taken from), on each return board after `_unpinned_board`. A pin whose return
  board the filter empties is counted in a warning.
- A pin names each leg's OPERATING flight (`fl[22]`). Pinned under the
  codeshare number it is booked as (AA142 as AY3787), the return board comes
  back empty; pinned as AA142 it serves 20 rows at the same $799 combo price
  (2026-09-27). The row keeps its booking identity for the table, the JSON and
  the post-filter.
- An answer the filter emptied goes to Matrix under `auto` with the reason on
  stderr, and says why under `--backend gflight` (stdout `[]` in JSON mode, or
  the award document when awards run: an empty board still queries the award
  providers, as Matrix's empty answer does).
- A Google query that FAILS goes to Matrix the same way under `auto` with
  `--format json`, unless `--fast`, `--bags` or `--sellers` is set: one
  `Using Matrix: <reason>.` line on stderr, in the words the merged table
  prints beside Matrix's answer for the same wall, then Matrix's document. The
  default table survives the same walls inside its weave, so JSON answers
  wherever the table does. `--fast` means Google alone, Matrix prices no bags,
  and a `--sellers` document wraps a Google row, so each keeps exit 1 with
  stdout empty, as does `--backend gflight`.
  A multi-cabin search goes to Matrix WHOLE under `auto` when the filter
  emptied any cabin (one `Using Matrix:` line names each emptied cabin and its
  count): handing on only that cabin would put Google's rows beside Matrix's
  documents in one answer and join prices from two sources. When Matrix then
  returns no itinerary for a cabin Google had rows for, failing or finding
  none, the answer stays Matrix's and stderr names that cabin and
  `--backend gflight`, which shows Google's rows.
  A cabin Google served nothing for stays Google's answer. Under
  `--backend gflight` the multi-cabin path prints the reason per cabin and
  leaves the cabin empty. Neither hand-off prints a note on Google's table (the
  pin cap, the cabin-join legend, rows in another currency): the table that
  follows is Matrix's, where '—' is a cabin with no price.
- A round trip that took pins answers with a board even when no pair survives,
  its count covering the rows removed on both legs, so it takes the same route.
  Under `--backend gflight` the line also names how many outbounds were pinned,
  the cheapest ones: the rest were never searched for returns. With nothing
  removed, Google serving no return for any pin stays "no results".
- `ds:1[5]` is Google's price insight: `[code, [None, cheapest], [None, _],
  [None, _], [None, typical_low], [None, typical_high], ...]`. The level is
  derived (below the range low, above it high); `[0]` looked like a level code
  (4, 4, 5) in three samples and is not used. One `Price insight:` line prints
  under the Google table, in the page's currency; the JSON document does not
  carry it. The multi-cabin table does not print it yet. Google's cheapest is
  the unfiltered board's, so when the routing filter removed rows the level is
  restated from the cheapest fare kept (a combination's by its return member)
  against Google's range, and no line prints when no priced row is kept.

**Carrier exclude reads the booking carrier.** `~XX+` / `-AIRLINES XX` drops a
row only when a leg is booked under XX (`flights[i]`), which is Matrix's meaning
for the fare shown: AA100, AA-sold with BA among its other sellers, stays under
`~BA+`, and BA178 booked as AA6939 stays too. A leg whose booking carrier cannot
be read (no flight number) fails the exclude, as a leg with no operating carrier
fails `-CODESHARE` and `-OPAIRLINES`. Carrier include still matches the
booking carrier or any listed seller.

**A carrier list takes space-separated airline codes.** `AIRLINES`,
`-AIRLINES`, `OPAIRLINES` and `-OPAIRLINES` naming any token that is not a
two-character code (`-AIRLINES UA,DL`, `-AIRLINES UA, DL`, `OPAIRLINES |`)
parse to one Matrix-only predicate whose reason quotes the token. Google matches
no row to such a token, so `-AIRLINES UA,DL` excluded nothing and printed DL742,
DL747 and DL771 on JFK-LAX, and `-AIRLINES UA, DL` excluded DL alone. Matrix
refuses the list itself (`SLICE-PROHIBITED-CARRIERS: "UA,DL" is not a carrier`,
exit 1) and answers `-AIRLINES UA DL`. `-CITIES` names airports and is not held
to this rule.

The encoder is an enforced allowlist, not a deny-list: every field on fli's
`FlightSearchFilters` must be named in one of three sets (encoded / refused /
deliberately ignored), and `build_search_tfs` raises on any remainder. `flights`
is pinned with an open floor (`>=0.9`), so a minor that adds a filter would
otherwise encode as if the new field were unset — dropping a constraint the user
asked for, silently.

One more trap: a stop ceiling only encodes up to **two**. fli's `MaxStops` tops
out at `TWO_OR_FEWER_STOPS`, so a ceiling of 3+ maps to `ANY` and omits field
3.5 entirely — `--stops 3` then encodes byte-identically to no `--stops` at all.
The search gate (`_gf_postfilter.search_page_reasons`) holds only the strictest
of `--stops` and every `MAXSTOPS` (or `N`) to that ceiling, since that one limit
is all the page is asked for: `--stops 3` alone goes to Matrix, and beside
`MAXSTOPS 0` it is a nonstop search the page serves.

**That page has two rungs.** Rung 1 is the curl_cffi GET above. Rung 2
(`--gf-transport browser`) drives a real Chrome to the *same* URL and hands its
`response.text()` to the *same* parser — Google's rate budget is keyed on client
context, not IP, so real Chrome survives the throttle that blocks the thin
client. `_one_call_laddered(filters, transport)` picks the rung; `_fetch_page`
and `GfBrowserSession.get_html` both feed `_rows_from_page_html`, which is the
only place a refusal is diagnosed. A rung supplies bytes, never interpretation.
`auto` is accepted today and identical to `http`; escalate-on-throttle is a
follow-up. Details, measurements and traps: [gf_browser_rung.md](gf_browser_rung.md).

**Refusals are typed** (`_gf_errors`):

- `GfThrottledError` — the captcha interstitial, which arrives three ways. A
  redirect puts `/sorry/` in the final URL; Google also serves the same page
  in place with HTTP 200, where the body marker ("Our systems have detected
  unusual traffic") is the only tell; and an outright **HTTP 429, which arrives
  as a RESPONSE**. `_get_search_page` goes through fli's session rather than
  `Client.get`, so nothing calls `raise_for_status()` on our behalf and
  `_rows_from_page_html` reads the status itself — which is what lets the ladder
  see a throttle instead of a wrapped transport error.
- `GfConsentError` — no `ds:1` *and* consent markers, checked in that order,
  because a real results page links to the consent domain in its footer.
- `GfPageShapeError` — no readable `ds:1`; or a payload too short to reach
  `[3]`; or a value at `[2]`/`[3]` that is neither absent nor row-shaped; or
  rows found outside `[2]`/`[3]` with none served; or rows present and none
  parsed (with sampled reasons).
- `GfUpstreamStatusError` — a non-2xx that is not a throttle. Typed apart from
  `GfPageShapeError` because "Google declined to serve this" and "the extract
  is broken" send a reader to different work, and either rung can raise it:
  rung 1 reads the status off the response, rung 2 off the navigation.
- `GfBrowserUnavailableError` — rung 2 could not produce bytes at all: no
  patchright, no Chrome, a profile another `flight` holds, a dead navigation.
  Never a statement about the route. Its `remedy` is a separate attribute that
  BOTH renderings must carry: the message quotes `str(e)`, and the note the
  default enrich path prints has to append it explicitly.

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
| multi-cabin | the above, times the cabin count; a round trip fetches each cabin's outbound page once, ahead of its pins, and hands it back |
| a persistently throttled leg | `_THROTTLE_RETRY_ATTEMPTS` + 1 = 5, then it aborts |
| a transport blip | up to 3 GETs per leg (`_TRANSPORT_RETRY_ATTEMPTS` + 1) |
| a leg that both throttles and blips | 1 + `_THROTTLE_RETRY_ATTEMPTS` + `_TRANSPORT_RETRY_ATTEMPTS` = 7 |
| a persistently throttled multi-cabin fan-out | one ladder for the group: at most 5 + (cabins - 1) |
| a multi-cabin fan-out under a transport outage | one ladder for the group: at most 3 + (cabins - 1) |
| a round trip whose pins meet a throttle or an outage | it stops at that pin: no further pin is fetched |
| a round trip whose every pin blips and recovers | 1 + 3 x pins = 31 at the default `-n 10` |
| a round trip whose return boards all refuse (5xx, consent, layout) | 1 + pins, the same as a successful search |
| a round trip on a wall that keeps lifting and closing | 55 for one cabin, 220 for four, against 44 healthy — `cabins x calls x (_THROTTLE_RETRY_ATTEMPTS + 1)` |
| a one-cabin `search` that is not `--awards-only` (the default merged table, `--fast`, either `--format json`) | the above + 1: the Cheapest tab, fetched last and not at all once the pins stopped on a wall, an outage or a dead browser |

The flapping row is the worst case and the one that needs its bound named. Any
sibling's success refills the wall — correctly, it is per-IP — so the shared
ladder never exhausts there and cannot bound anything. What does is the attempt
count each `retry_throttled` call carries, applied to the eleven calls a default
round trip makes — one board and ten pins — and then to the cabin count. That
product is the row above.

Two things make the rest of those numbers hold, and this paragraph is where that
arithmetic lives — the docstrings that depend on it point here rather than restating it.
`_get_search_page` goes through fli's SESSION, not `Client.get`, which is wrapped
in `@retry(stop_after_attempt(3))`: a throttled leg would otherwise cost up to 15
GETs, fli's ladder running inside each rung of ours. `retry_throttled` is the
only ladder. And the round-trip pin is capped
at 10 regardless of `top_n`: the multi-cabin path bumps `top_n` 5x (to 100) to
widen the pool it filters, which was free on the old RPC and would otherwise
mean ~2 x 31 page fetches for a two-cabin round trip. The default `-n 10` is
unchanged by the cap.

Which outbounds the budget buys is the cheapest ones the filtered board lists
(`_gflight_ids._pins`, on the same `fare_key` the `-n` trim sorts by: ties in
page order, unpriced rows last). An outbound row's price is already the cheapest
round trip through it (see "What a round-trip row's price means" below), so the
cheapest outbounds are where the cheapest combinations are. On the JFK-LHR
capture the page lists five USD295 top flights before three USD293 rows; `-n 1`
pins EI104+EI152, the sixth row, and spends the same two GETs it spent on the
first.

The bump therefore widens the leg-1 rows each cabin keeps and NOT the round-trip
pins. What makes the cabins' pins overlap is that the sort cabin leads
(`cli._CabinSearches`): every cabin pins, in the sort cabin's order, each
outbound the sort cabin pins that its own filtered board lists, matched on the
whole leg sequence `_itinerary_key` uses (`_gflight_ids.pin_keys`), then fills
the rest of the same budget with its own cheapest rows
(`search_with_ids`' `prefer`). The sort cabin's pins are exactly the ones it
takes alone, so the rows the table shows lose nothing. The cost is that a
non-sort cabin's own cheapest outbounds get only the slots the sort cabin's
leave, and its `--format json` list moves with them. Each page is fetched once
(`search_with_ids`' `first`), so the GETs are unchanged.

Rung 1 runs that in two parallel rounds: every cabin's outbound page, then
every cabin's return boards. Rung 2 serves one cabin at a time, so it runs the
sort cabin's page and pins first and then each other cabin's whole search: the
order the cabins took before they shared pins when the sort cabin is the first
`--cabin`. Every cabin's page ahead would load pages that a fallback to rung 1
loads again, and a Chrome failure on a later cabin's page would print a
missing-column note for a column that the fallback then serves.

So **Google Flights prices every cabin on up to `<pin budget>` of the `<sort>`
cabin's cheapest outbounds; '—' means that cabin's search returned no fare
for the itinerary** — the budget being `pinned_fanout` of the bumped page size,
which the cap holds at 10 however large `-n` is. `cli._multi_cabin_join_note`
builds that sentence from the pin budget rather than a literal, and
`cli._run_gflight_path_multi` prints it on a multi-cabin round trip it does not
hand to Matrix, because an empty cabin cell otherwise reads as "that fare does
not exist". "Returned no fare" and not "does not list": a filter such as
`--max-price` can remove a fare the board lists, a return board can be refused,
and a row from a follower's own outbounds was never searched in the sort cabin.
"Up to", because the cap bounds how many outbounds the join can see and a board
may hold fewer.

When the sort cabin pinned nothing — its page refused, or its filter kept no
row — every cabin pins its own cheapest outbounds, and the note says that
instead: "joins cabins on up to `<pin budget>` of each cabin's cheapest
outbounds; '—' means no shared itinerary, not no fare." The fan-out reports
which cabin led (`cli._CabinBoards.leader`) rather than leaving it to be read
off a board, because a sort cabin whose pins were handed on and whose every
return board then failed has no board and still led: its column is empty, its
refusal is printed beside the table, and the note is the led one.

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
prints "combines returns against up to `<pin budget>` cheapest outbounds" on
every round-trip path (the enriched one, `--fast`, `--format json` and
multi-cabin) whenever the pin cap is below the `-n` asked for and the search is
not handed to Matrix, and always to stderr so a JSON document stays a document.
It is passed the user's count, never the multi-cabin bump — a wider pool per
cabin that nobody asked for — and prints the pin budget that count resolves to,
which is the number the join will actually see rather than the one being
corrected. Above the cap the rows shown are the `-n` cheapest combinations of
the ten cheapest outbounds, not the `-n` cheapest round trips on the board, and
the note is what says so.

**`-n` is one number, applied on the way out.** The page serves Google's whole
board — around thirty rows; the dated measurement is at the top of this file —
whatever count is asked of it, so the count is a trim rather than a query
parameter, and it keeps the cheapest rows (the order is set out below). It
bounds everything the user can act on, and all of it from one place
in `cli._run_gflight_path`: the table, the `--format json` document, the range
`--pick` accepts and the itinerary the `--matrix-url` / `--google-url` lines pin,
and the itineraries the award providers are fanned out over. All five hold on
`--fast` and on `--format json`, which are the same function — and under
`--format json` the count still bounds the document, while no link line is
printed at all. Multi-cabin keeps three of them — the table, the document and
the award fan-out — and has neither of the other two:
`cli._run_gflight_path_multi` has no `_emit_urls` call site at all, and neither
multi path is passed `pick`, which the `search` command accepts and drops there.

**A pick names a row on the table that was printed, and the enriched path is
where that is easy to get wrong.** Its table is the MERGED one: price-sorted, and
holding Google-only rows the Matrix half never had. So it differs from the Matrix
solution list in ORDER as much as in size, and order is the half that bites — at
`-n 3` nothing is out of range and every pick still names the wrong itinerary,
under the number the user read off the screen. Both surfaces are therefore built
from one list, `merged[:top_n]`: the table numbers it, `cli._pick_in_range`
measures the pick against its length, and `_emit_urls` is handed a result whose
solutions ARE it. The label is then true by construction rather than by
agreement between two call sites.

**The Google line pins a row only on dates its source states.** A pinned link
names each flight's departure day, and Matrix gives a slice's two ends and no
flight's date. The ends do not date the flights between them: NZ104 SYD-AKL
plus NZ10 AKL-HNL lands on the day it left while NZ10 leaves the next day, and
an eastbound red-eye second flight leaves the day before the slice lands.
`links.extract_pin_segments_from_slice` therefore pins a slice only when
`pin_dates_are_stated` holds: one date per flight in `segment_dates`, or a
nonstop, dated by its departure day. A Matrix connection gets those dates from
`_enrich.merge_results` when a Google row names the same flights, leaves on the
same day and lands at the same local minute (the landing day alone does not date
the last flight: one flight number flown at different hours on two days can land
on the same day both times). Every Google row sharing the match key is tried,
not only the first; two that qualify but date a flight differently lend nothing.
A connection no Google row dates gets the unpinned search link, and `--sellers`
refuses it with the `--fast` remedy.

**Every row of either side is in the merged table once.** The match key fixes
the flights and the first day, not the trip. Icelandair's FI614 then FI450 out
of JFK connects in Keflavik the next morning or the one after: the JFK-LHR
capture lists both (USD617 and USD690), and Matrix priced FI614/FI450 twice on
2026-10-20 (USD884 and USD1180). Each Matrix row of a key first takes the Google
row that is its own trip (`_date_lender`); only then does the key's first
Matrix row, if it found none, take the first Google row left, undated. In the
other order a USD884 Matrix fare would show the USD1180 trip's Google price.
Every row left on either side is a row of its own, extra Google rows in board
order and extra Matrix rows after their key's first, so where no key is shared
the list is the one-row-per-key merge exactly
(`test_enrich.test_a_board_with_no_shared_key_merges_as_it_always_did`).

**And it keeps one order: price, ascending.** `cli._price_ordered` sorts every
Google answer immediately before each trim, on one key
(`_gflight_ids.fare_key`, which the round-trip pins use too): a priced row by
its fare, a row Google did not price after every row it did, and a stable sort,
so equal fares keep the order they arrived in. A one-way row's fare is its own.
A combination's is its terminal member's — the pinned leg is what makes the
combination that combination, so its fare is the one every surface prints.

One order, because every other list the CLI prints is already in it — the
merged and multi-cabin tables, the date grid, explore — and because neither
arrival order is a ranking a user can act on. A one-way board arrives with
Google's top flights (`ds:1[2]`) ahead of the rest, a composite of price,
duration and stops: on the JFK-LHR capture `-n 5` kept five USD295 top flights
and left the board's three USD293 rows, at 6-8, off the table, and NYC-LAX,
measured live on 2026-10-01, listed a 175 at row 6 under 169, 229, 229, 229 and
234. A round trip's combinations arrive outbound by outbound, so `-n 3` unsorted
is three trips from one outbound with cheaper trips from the next off the
table. The trade is that a top-flights row, often a nonstop a few dollars
dearer, can fall below a small `-n`; a larger `-n` brings it back. The page's
order survives as the tie-break. The `-n` help string states the rule.

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

**The multi-cabin join keys the whole itinerary.** `_multi_cabin.itinerary_key`
takes, per slice, every flight number, each flight's date where the answer gives
one (Google's `segment_dates`; Matrix gives none) and the slice's departure and
arrival strings. Nothing cabin-specific is in it, so an itinerary both cabins
list is one row with both prices. A cabin that lists one itinerary twice is
priced at the listing that ranks first under `price_rank`, and the row's
`itinerary` is that listing, so it carries the price printed. A key of the first
flight and its date alone joins the full JFK-LAX board (95 itineraries) into 62
rows, 4 of them printed at another itinerary's fare (AS41+AS2415, USD255 of its
own, at USD312), and JFK-LHR (101) into 64 rows, 9 of them mispriced. It also
joins Matrix's FI+ JFK-LHR answer for 2026-10-20 (9 trips through Keflavik) into
3 rows. The whole key gives 95, 101 and 9 rows, and it partitions both Google
boards exactly as `_gflight_ids._itinerary_key` does.

**Neither merge ranks two currencies by their numbers.** `_multi_cabin.merge`
and `_enrich.merge_results` sort through `_multi_cabin.price_rank`: rows priced
in the requested currency (`--currency`, else USD) first by amount, then each
other currency in code order by its own amounts, then prices naming no currency,
unpriced rows last. No exchange rate is applied or derived, so a fare in another
currency ranks after every requested one, and a list in one currency orders by
amount exactly as before. A merged row ranks on its price in the requested
currency, Matrix's when both sides have one. The enriched weave asks Matrix in
the currency Google is asked in, so its merge is one currency at the source:
left unset, Matrix prices in its own default (GBP from LHR, 2026-09-28) and the
merged LHR-JFK table ranked GBP1004 above USD1043 (about GBP780) before the
trim. The rank is the backstop for a row Google still prices in another
currency, which `cli._note_other_currencies` names on stderr. A Google board is
one page in one currency, so `cli._price_ordered` keeps bare amounts.

**What a round-trip row's price means.** The two boards price different things.
An outbound row carries the cheapest round-trip TOTAL reachable from that
outbound; the return board fetched with that outbound pinned prices each of its
rows at THAT combination's own total. Measured on the committed capture pair —
the numbers and the assertions are in `tests/pp/test_gflight_adapter.py` — the
pinned board's minimum is exactly the outbound row's price, while the other
combination is a dearer trip. Live 2026-09-03 (HNL-MIA business, 2 adults) says
the same from the other end: outbound 854/305 quoted 6806 and its two
combinations totalled 6806 and 7650. Pricing a combination from the outbound
therefore reports every one of them but the cheapest under its real fare. The
human table prints each member's own price on its `Na`/`Nb` rows and
`--format json` emits both, so both carry the true number; the SearchResult
the award comparison reads carries one, and it is the total. The cash baseline
that comparison is made against is therefore the cheapest of the rows SHOWN,
which with every Google list in price order is row one: a one-way board's
lowest fare, and on a round trip the cheapest trip through the pinned outbounds.
A single-cabin round trip pins its cheapest outbound first, so that is the
board's cheapest round trip unless a return filter removed it.

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

## Separate tickets and self transfers: the Cheapest tab's `row[7]`

Google lists the itineraries it sells as more than one booking on its Cheapest
tab only. Measured live 2026-10-02 on FLL-LGA, round trip 2026-10-20/27, `gl=US`
(the committed `ds1_fll_lga_rt_best.json` and `ds1_fll_lga_rt_cheapest.json`):

- The default board (`tfu=EgQIABABIgA`) served 58 rows, none labeled. The
  Cheapest tab served 86, and its page labeled 28 "Self transfer" (Frontier via
  ATL, layovers of 9 to 20 hours) and 5 "Separate tickets booked together"
  (JetBlue nonstops).
- Matched row by row to the page's own data: "Self transfer" is `row[7] == [1]`
  (28 of 28; those rows also carry `row[0][26] == [1]`), "Separate tickets booked
  together" is `row[7] == [2]` (5 of 5), and an unlabeled row is `row[7] == []`
  (53 of 53). `row[0][12]`, which fli reads as `self_transfer`, is 0 on all 86,
  so it is not this flag. `_gflight_ids._ticketing` decodes `row[7]`; a slot
  that is absent or not a list states nothing.
- The request is the same `tfs=` with `tfu=EggIABABIAIoASIA`
  (`{2: {1: 0, 2: 1, 4: 2, 5: 1}, 4: {}}`). Over rung 1 it returned the same 86
  rows with the same marks; tfs field 16 is not needed. One GET, about 2 s.

**Merged, not swapped.** By flight id the Cheapest board holds all 58 default
rows plus the 28 self transfers, but it prices 7 shared rows lower: the 5
JetBlue rows at USD247 instead of 307, marked `[2]`, and 2 unmarked Frontier
rows at 226 and 220 instead of 234. Swapping boards would change the price of
rows sold as one ticket, so the default board stays and only the marked rows of
the Cheapest board are added (`_gflight_ids._with_separate_tickets`). A marked
row is added even when its flights are on the default board, because it is a
different booking. The two cheaper unmarked fares keep the default board's
price; who sells them is not settled.

**Field 17 is not the default board either.** tfs field 17 = 1 on the Cheapest
URL returns the 58 default ids, unmarked, at the Cheapest tab's prices (JetBlue
297, Frontier 226 and 220). So `--no-separate-tickets` drops the marked rows
from the page it read rather than asking for another board, which is also how
it counts what it hid.

**No return for a marked round trip.** The pinned return page for the cheapest
self-transfer outbound (F9 3013 + F9 3454) served 0 rows with either `tfu`, and
Chrome's own click on a `[2]` outbound served 5 one-ticket returns at USD307,
not the USD247 trip. A marked round-trip itinerary is therefore its outbound
alone at Google's round-trip total: a one-member row, `[outbound]` in JSON. A
link never pins one, since the page it would open lists neither the trip's
returns nor its price.

**One-way: none seen from a US IP.** One-way Cheapest boards carried no mark on
FLL-LGA (114 rows), LAX-BKK (95), JFK-ATH (127), CMN-DXB or LAX-OKA. The decode
is the same; the one-way test marks a captured row by hand.

**Where it is read.** Every one-cabin `search` asks for the Cheapest tab:
`_run_gflight_path` (`--fast`, `--format json`, `--verify`, `--bags`) and the
default `_run_enriched_path` (the merged table and `--enrich --format json`),
which reads it in the Google worker that already runs beside Matrix. The
renderers mark a row (`†` separate tickets, `‡` self transfer, on the Google and
the merged table, each with its key line; JSON `separate_tickets`, and fli's
`self_transfer` for the subset on which bags are rechecked). `--awards-only`,
the multi-cabin search and the deprecated `gflight` command read no Cheapest
page: the first prints no Google row, the other two pass no mode. A refused
Cheapest page leaves the base answer and one stderr line naming why, and
`--no-separate-tickets` one line counting what it hid, once per search on every
path (`cli._note_separate_tickets`).

**Cost on the default path.** One GET, inside the Google worker, so it delays
the first (Google) table and not the merged one while Matrix is the long pole.
FLL-LGA 2026-10-20/27, `-n 100`, one run each at 2026-10-03 01:16-01:17 EDT:
the Google table painted at 7.0 s before this change and 9.0 s after; the merged
table landed at 46.2 s before it.

**Never priced against Matrix.** Matrix sells one ticket, so a separate-ticket
row and a Matrix row on the same flights are two bookings: `_enrich.merge_results`
never pairs a marked row, which stays a `GF` row with no Matrix price and no
delta. Its cross-check reason is `separate_tickets` alone ("Google sells this
trip as separate tickets; Matrix prices one ticket", or "as a self transfer on
separate tickets"). Every Google-side fact a Matrix row is explained by (its
carriers, trips and priced outbounds) reads one-ticket rows only, so a marked
row never makes a Matrix row read `paired_elsewhere`, never hides
`carrier_absent_google`, and an outbound Google prices only on separate tickets
keeps `outbound_not_priced`. The caption's and the document's `google.listed`
count one-ticket rows too; the caption adds "and N on separate tickets".

**Every surface that acts on a row skips one.** The award matcher reads the
one-ticket rows, and stderr says once how many shown rows on separate tickets
are not in the award table. An award search on `_run_gflight_path` whose row
filter leaves separate-ticket rows alone is handed to Matrix, as one it leaves
with no row is, and stderr counts them; `--cash-only` lists them. `--sellers` refuses the row before Chrome opens
("Google sells #N as separate tickets; --sellers reads one-ticket booking pages
only.", exit 1). `--verify` answers `separate-tickets` without asking Matrix
(exit 0). `cli._pin_segments` returns None for it, so every Google link and the
pin-clamp sentence fall back as when a pin fails, on both paths.

**The supply flickers.** On FLL-LGA 2026-10-20/27, the Cheapest URL this code
fetches served 66 rows, 5 `[2]` (JetBlue nonstops at USD246) and 12 `[1]` (self
transfers, USD282-1449), at 2026-10-03 00:01:30-00:02 EDT, then 54 rows, none
marked, at 00:06:29 and 00:07 on the same client and URL. None were seen on 37
routes or in Chrome at 23:49-23:59 the evening before, and the 01:17 run above
printed no mark. So an unmarked live board says nothing about the code path; the
tests prove the behavior on the committed FLL-LGA captures.

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
  page also encodes it as 3.17; the grids have no rows to check it on).
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

`-REDEYES` and `-OVERNIGHTS` still escalate to Matrix. The raw-row checks in
`_gf_postfilter._row_passes` read per-leg datetimes, so either could be added
there.

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
`other_currency`. Google prices the whole party, while the price Matrix lists
is one passenger's, rounded up (2 adults, 2026-10-01: `ext.price` USD229.00,
`displayTotal` USD456.80, Google USD457), so for more than one passenger the
Matrix column, the delta, the caption and the document read Matrix's
`displayTotal`; where Matrix states none the row is `unpriced`. The merge's pairing and ranking are unchanged: a matched row
still ranks on Matrix's price, so with the deeper page a Google fare that
Matrix prices higher can rank below where it ranked on a page of `-n`.

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
`carrier_absent_google` only on a one-way or beside an outbound Google priced;
neither of those two on a board the row filter cut; otherwise `not_on_google`.
A Google row sold as separate tickets: `separate_tickets` alone (see the
Cheapest-tab section). Either side: `paired_elsewhere` where the other side lists the same flights,
first day and landing minutes on another row, because a middle flight's day is
then unstated; `unmatched` where a row leaves a flight number, day or landing
unstated, so neither absence and no unpriced outbound is decided for it (no
live or fixture row has done so). Point of sale is never a reason: Google is always `gl=US`,
Matrix is sent no sales city, and no row says where it was priced.

**`--format json --enrich`** writes `{"search": <the plain --format json
document, the same -n rows>, "cross_check": {"currency", "delta":
"google_minus_matrix", "matrix": {"listed", "solution_count", "complete",
"last_price"}, "google": {"listed", "answered"}, "rows": [...]}}`, the rows
being the table's, from the pure `_cross_check.document`. Plain `--format json`
does not cross-check; on auto a failed Google query is still handed to Matrix,
as before, and `--fast` asks Matrix nothing. It needs no awards (`--cash-only`)
and no `--sellers` (exit 2 otherwise); `--bags` prints the table's "No Matrix
enrichment" note and the plain document, and `--verify` prints its own such note
and writes `{"search", "verify"}` instead. Matrix failing leaves `cross_check`
null (exit 0), Google failing leaves `search` empty with every Matrix row
`no_google_answer`, both failing is exit 1 with stdout empty.

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
11 airports or with one airport at both ends, routing above Tier-1, a Tier-1
code or zero bound the request would leave out, a trip-length range, or a
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

### `--fast`: the page's own price graph, through Chrome

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

- **Shape.** One-way, or a round trip of ONE trip length (`-d 7`): the page's
  graph prices the trip length its own dates imply, so `5-7` refuses. Every
  round-trip cell's return date is checked against that length.
- **Airport sets.** A comma-list or metro code on either side is one page: the
  bridge writes every member airport into the URL, and the graph prices each
  date at the cheapest of them. Measured 2026-09-28, one-way, 14 dates each:
  NYC→LAX equaled the per-date minimum of JFK, LGA and EWR on all 14 (each of
  the three was the cheapest on some date), and JFK,EWR→LHR on all 14. A round
  trip is not compared that way on purpose: the set page may return to another
  airport of the origin set, as a Matrix metro code does, so its price can sit
  below the minimum of mirrored pairs. The airports are checked as a search's
  are (`gf_leg_refusal`, then `_gf_unserveable_reasons` on the expanded codes).
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
  user's own combined query beside the pairs, the only source of a return into
  another airport of the set: it takes a day only when strictly cheaper than
  every pair, its cells name the user's tokens, and a stderr note says so.
  Measured 2026-10-01 over 2026-10-20..11-02: `LHR,DUB JFK --one-way` merged
  DUB's EUR cells with LHR's GBP cells as bare numbers and showed GBP1137 and
  GBP952 on two days DUB was cheaper; asked in USD, all 14 days came back USD,
  each cheaper from DUB. `NYC LON -d 7` as one combined query priced 11 of 14
  days (20 solutions, cheapest USD817); as 18 pairs plus that query it priced
  14 of 14, cheaper on 7 days (10-27 USD766 EWR→LGW), the combined query was
  below every pair on none, the 7 pairs into STN, LTN or SEN priced nothing,
  and the 19 queries took about 110 s.
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
  alone on stdout, with no URL lines.
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
