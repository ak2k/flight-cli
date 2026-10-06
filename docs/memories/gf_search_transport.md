# GF search transport: the public page's `tfs=` field layout, aliased airport and airline codes, price cap, bags, CO2, row checks, the full board

The search path GETs the public Google Flights page and reads its `ds:1` rows,
because the `GetShoppingResults` RPC is gated. The `tfs=` fields, fli's aliased
airport and airline codes, `tfu=` and the full board, `--max-price`, `--bags`
and `--exclude-basic`, the CO2 slots, the table's width, and the row checks that
re-verify what the page encodes. Read before touching `links.build_search_tfs`,
`links.google_flights_search_page_url`, `fli_bridge.fli_airport` /
`fli_airline`, `_gf_postfilter.routing_keep` / `search_page_reasons`, or
`cli._render_gflight_table`.

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
25 = 1: economy without basic fares (`--exclude-basic`), after 19
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

A minute window (`--depart-times 9:30-13:45`, `--arrive-times 18:00-21:30`) asks
for its whole hours, first // 60 to last // 60, and the row filter holds the
first leg's departure, or the last leg's landing, to the minute, both ends
included (on `ds1_jfk_lax_tfu.json`, 18:00-21:30 keeps 16 rows and drops the
21:34 and 21:59 landings). Matrix takes a departure window to the minute in
`timeRanges` and has no arrival input, so an arrival window is Google-only, as
`--bags` is: refused where the search needs Matrix, and never handed to it.

Field 25 = 1 (`--exclude-basic`) on JFK-LAX 2026-10-20: 93 rows against 95
without it, 78 repriced up (AA +45 and +110, B6/DL/AS +55), 15 unchanged, 2
gone, the cheapest USD229 -> USD284. On JFK-LHR it repriced none and still
served a basic fare (2026-09-27). No row field marks a basic fare, so nothing
can be checked: every run says so, and the flag is Google-only as `--bags` is.

Two traps in that layout. **`3.5` is zero-based** while fli's `MaxStops` is
one-based (ANY=0, NON_STOP=1, …), so it's `enum.value - 1` and **omitted** for
ANY — writing a literal 0 pins every search to nonstop. **Carrier codes come
from the enum NAME, not its value**: fli maps codes to display names
(`Airline._0B.value == "Blue Air"`) and underscore-prefixes digit-leading ones,
so `airline.name.removeprefix("_")` is the code. That holds for a member
`fli_bridge.fli_airline` built: the enum's own member for six codes is named
for another carrier (below).

**Airport codes come from the member's name as well, and fli's enum aliases 48
of them to another airport.** `Airport` is an enum over a code -> display-name
table, so a code whose display name repeats an earlier one is an alias of that
member: `Airport.OKA` is `Airport.NAH` (Naha in Indonesia, not Okinawa), as NTL
is NCL, TRI is PSC, SVC is PGC and ZFA is FAO. A lookup through the enum asks
Google for the other airport, and fli's row decoder has no entry for an alias,
so every row Google serves at one fails. Build airport members only through
`fli_bridge.fli_airport`, which gives each aliased code a member of its own,
named that code with the same display name (so the JSON dump shows "Naha
Airport" for OKA and NAH alike). `tests/test_airport_alias_requests.py` fails on
an `Airport[...]` subscript, a member read off the enum, `__members__` outside
an `.items()` loop, or `getattr`/`hasattr` on it, under any import name or
dotted path, and on a `_parse_airport` call outside `_leg_airport`, all under
`src/`; `tests/test_airport_enum_gate.py` holds each form it is proven on. MLH
is the one alias kept: it is EuroAirport's second code, the same airport as BSL,
and Google serves it only as BSL (JFK-MLH asked for MLH gave an empty board,
asked for BSL 8 rows). Measured 2026-10-01: `flight search LAX OKA --dep
2026-10-20 --backend gflight --fast --format json` through the enum printed `[]`
with exit 0; through `fli_airport` it printed 27 rows (CI, BR, CX via TPE or
HKG, from USD577), each landing at OKA by its clock span: departure to arrival
less elapsed time is +960 minutes from LAX, where NAH gives +900.

**Airline codes alias the same way: fli's `Airline` enum files six codes
under another carrier's member, and every pair is two carriers.** W9 (Wizz Air
UK) is W6 (Wizz Air Hungary); Z0 is N0 (Norse Atlantic UK and Norway); MT is DK
(Thomas Cook UK, ceased 2019, and Sunclass); S0 is P4 (Aerolineas Sosa and Air
Peace); 5C is X7 (Challenge Airlines IL and BE); 1W is 1S (two
reservation-system codes). A lookup through the enum asks Google for the other
carrier, and fli's row decoder (`_parse_airline`) has no entry for an alias, so
a row sold or flown under one fails to decode, and an include naming one reads
as "a carrier Google Flights has no code for" and goes to Matrix. No airline
alias is kept the way MLH is. Build airline members only through
`fli_bridge.fli_airline`, which gives each aliased code a member of its own by
the same helper as the airport table (`_own_member`) and keys a digit-leading
code as fli does (`5C` is `_5C`). The JSON dump shows fli's display name,
"Wizz Air" for W9 and W6 alike; each leg's `amenities` carry Google's own
operating and marketing codes. `tests/test_airline_alias_requests.py` fails on
`_parse_airline` anywhere under `src/`, an `Airline[...]` subscript, a member
read off the enum, or `getattr`/`hasattr` on it. Measured 2026-10-01: one
LTN-TIA page for 2026-10-20 held 11 rows, 5 of them W9 nonstops from USD44;
through fli's decoder only the 6 others were printed (El Al connections from
USD1087), and `--ext 'AIRLINES W9' --backend gflight` exited 2.

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
positional), an infant on a multi-cabin compare, seniors and youth (no Google
kind), time buckets that do not form one window, an alliance beside another carrier or
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
which carrier is in which alliance, and a membership table kept here would go
stale (Asiana leaves Star Alliance by 2026-12-17). Google's own filter held it
when measured on 2026-10-01: a live NYC-MUC business board asked for
`ALLIANCE star-alliance; MAXDUR 14:00; MAXSTOPS 1` served 20 rows, every one
sold by a Star carrier, within one stop and 840 minutes, and skill Example 2
without `+CABIN 2` answered on auto with 5 Google round trips, all Star,
business and within 14 hours. A `+CABIN` naming the one cabin `--cabin` asked
is checked per leg: every leg's cabin (`fl[16]`) must be that cabin, and a leg
Google states none for fails. Two of those 20 business rows carried a
first-class leg (UA2301 and UA3585, then UA108), which `+CABIN 2` drops. Any
other `+CABIN` goes to Matrix, naming the code and the `--cabin`. Children are
priced, not checked. Rows over the stop ceiling are counted on stderr from a
board that is shown, once each, in every format: `Google Flights returned 3
rows over the stop ceiling it was asked for (1); they are not shown.` (JFK-LHR's
101 rows under `--stops 1` keep 98). A board they help empty keeps its own line
alone, and a multi-cabin search handed to Matrix prints none. When these checks
empty a board, the empty-answer line names every active check.

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

**The table at the output's width.** Rich takes the width from the first of
stdin, stdout and stderr that is a terminal; `COLUMNS` overrides it; with no
terminal and no `COLUMNS` it is 80. So `flight … | less` from a terminal prints
at the terminal's width, and an agent's captured stdout at 80. Rich wraps a
cell at its spaces, so the one-line legs cell "EI 104 → EI 152" printed as
"EI 104 → EI" / "152": at 80 columns that split 3 designators on the first 12
JFK-LHR rows, 84 on all 101, 100 on JFK,EWR-LHR, 66 on JFK-LAX's 95 and 18 on
the HNL-MIA round trip's 3x3. `_render_gflight_table` prints the first layout
whose natural width is at most the console's (a table exactly as wide fits):
legs on one line with CO2, legs one per line with CO2, legs one per line
without CO2. The last prints even when it is still wider; a board with no CO2
grams has only the first two. A dropped column prints a dim note that
`--format json` carries it, and the JSON is the same at every width. The width
is measured unbounded, because `console.measure` caps it at the console's. The
price column is never wrapped: at 80 columns with `--bags` over JFK,EWR-LHR,
Rich wrapped "USD293.00 †" at its space and left the separate-ticket mark on a
line of its own.

**How the full board is served.**
- Rows are deduped per itinerary (every leg's carrier, flight number and
  departure datetime), keeping the priced and cheaper listing at the first
  listing's place. No true duplicate has been measured; the key keeps dates, so
  the same flight numbers a day apart stay two trips. The dearer listing in
  another cabin mix stays on the row (`others`), and a `+CABIN` search filters
  the cheapest listing booked in its cabin, so a cheaper listing with a leg
  outside it does not hide that fare. Each itinerary still meets the row
  filter once, so the stop-ceiling count is unchanged.
- The routing filter runs inside `search_with_ids` as each board is served: on
  the outbound BEFORE the pins are taken (pins are the cheapest rows of the
  board they are taken from), on each return board after `_unpinned_board`. A pin whose return
  board the filter empties is counted in a warning, and so is one Google
  served no return for (`k of M pinned outbounds have no return flight on
  Google`). After the counts, and before any raise, each pin lost to either or
  to a refused board is named on a line of its own: `pinned outbound
  LH405/LH914 (USD943.00) lost: Google served 2 returns for it, none matching
  the routing`, `... lost: Google served no return for it`, or the refusal's
  own words. A stop (a throttle, the network, a dead browser) names no skipped
  pin. On 2026-10-01 skill Example 7 on Google (JFK-LHR, `O:LH+`,
  `MINCONNECT 1:30`) lost 2 of 5 pins: LH405/LH914 (2 returns served, none
  LH-operated on every leg) and LH411/UA9440 (1); the six boards are the
  `ds1_*_oplh_*` fixtures. The outbound board gets no check like
  `_unpinned_board`: none of 27 saved live boards carried a row off the asked
  route.
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
  against Google's range, and no line prints when no priced row is kept. A
  board merged from several pages prints none (see "One answer from several
  Google pages").
  That section is in [gf_multi_page_legs.md](gf_multi_page_legs.md).

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
`auto` is rung 1 until a throttle outlasts its ladder, then rung 2 for the rest
of the search ([gf_throttle_ladder.md](gf_throttle_ladder.md)). Details, measurements and traps: [gf_browser_rung.md](gf_browser_rung.md).
