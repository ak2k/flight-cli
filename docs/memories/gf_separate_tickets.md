# GF separate tickets and self transfers: the Cheapest tab's `row[7]`, merged into the default board, never priced against Matrix

Google sells some itineraries as more than one booking and marks them only on
its Cheapest tab. The `row[7]` decode, the `tfu=` that asks for the tab, how its
marked rows join the default board, marked round trips, where the tab is read,
and what each surface does with a marked row; then the multi-cabin key and a
multi-city trip priced as one one-way ticket per slice. Read before touching
`_gflight_ids._ticketing` / `_with_separate_tickets`,
`cli._note_separate_tickets`, `cli._return_checks_google_skips`,
`_multi_cabin.itinerary_key`, `_open_jaw`, or `--no-separate-tickets`.

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
  that is absent, not a list, or holds a code other than 1 or 2 (none seen) states
  nothing.
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

The row filter can check that outbound and the round-trip total, never the
return. So when the return carries a check Google's query does not apply, a
Tier-2 predicate (`-AIRLINES AA`) or a time window, which Google widens to whole
hours, the Cheapest tab is not read and one stderr line names the check. An
empty answer then hands off to Matrix as it does without the tab, and the line
prints first, with the other separate-ticket notes (each cabin's, on a
multi-cabin search), ahead of the `Using Matrix:` line. Every path that reads
the tab makes this check (`cli._return_checks_google_skips`).

**One-way: none seen from a US IP.** One-way Cheapest boards carried no mark on
FLL-LGA (114 rows), LAX-BKK (95), JFK-ATH (127), CMN-DXB or LAX-OKA. The decode
is the same; the one-way test marks a captured row by hand.

**Where it is read.** Every one-cabin `search` asks for the Cheapest tab:
`_run_gflight_path` (`--fast`, `--format json`, `--verify`, `--bags`) and the
default `_run_enriched_path` (the merged table and `--enrich --format json`),
which reads it in the Google worker that already runs beside Matrix. A leg
asked as several pages reads each page's tab (see "One answer from several
Google pages"). The
renderers mark a row (`†` separate tickets, `‡` self transfer, on the Google,
merged and multi-cabin tables, each with its key line; JSON `separate_tickets`, and fli's
`self_transfer` for the subset on which bags are rechecked). A multi-cabin search
reads each cabin's tab (one GET more per cabin). `--awards-only` and the
deprecated `gflight` command read no Cheapest page: the first prints no Google
row, the second passes no mode. A refused
Cheapest page leaves the base answer and one stderr line naming why, and
`--no-separate-tickets` one line counting what it hid, once per search on every
path (`cli._note_separate_tickets`).
"One answer from several Google pages" is in [gf_multi_page_legs.md](gf_multi_page_legs.md).

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
count one-ticket rows too; where Google listed any on separate tickets, the
caption adds "and N on separate tickets" and the document `google.separate: N`.

**Every surface that acts on a row skips one.** The award matcher reads the
first `-n` one-ticket rows of the whole board, so a separate-ticket row that
takes a table row takes none from the award table, and stderr says once how
many shown rows on separate tickets are not in it. The default search's award
table matches Matrix's fares, and the same line counts the merged table's
separate-ticket rows (`cli._note_award_skips`). An award search on `_run_gflight_path` whose row
filter leaves separate-ticket rows alone is handed to Matrix, as one it leaves
with no row is, and stderr counts them; `--cash-only` lists them. `--sellers` refuses the row before Chrome opens
("Google sells #N as separate tickets; --sellers reads one-ticket booking pages
only.", exit 1). `--verify` answers `separate-tickets` without asking Matrix
(exit 0). `cli._pin_segments` returns None for it, so every Google link and the
pin-clamp sentence fall back as when a pin fails, on both paths, and a note
under the unpinned Google link names the row and why.

**The supply flickers.** On FLL-LGA 2026-10-20/27, the Cheapest URL this code
fetches served 66 rows, 5 `[2]` (JetBlue nonstops at USD246) and 12 `[1]` (self
transfers, USD282-1449), at 2026-10-03 00:01:30-00:02 EDT, then 54 rows, none
marked, at 00:06:29 and 00:07 on the same client and URL. None were seen on 37
routes or in Chrome at 23:49-23:59 the evening before, and the 01:17 run above
printed no mark. So an unmarked live board says nothing about the code path; the
tests prove the behavior on the committed FLL-LGA captures.

## Multi-cabin rows, and a multi-city trip as one-way tickets

**Multi-cabin key.** `_multi_cabin.itinerary_key` ends a marked listing's key in
its ticketing, so the join never puts a separate-ticket fare and a one-ticket
fare of the same flights on one row: probe 2026-10-05, B6172 listed in economy
at USD260 on one ticket and USD246 on separate tickets, business USD900 on one
ticket, joined into ONE row (USD246 beside USD900) before the key had it. A
one-way marked listing joins the same marked listing in another cabin. A marked
round trip is its outbound alone and Google lists no return for it, so each
cabin's is a row of its own (`merge(slices=...)`). Award matching and the cash
map skip marked rows, and `_note_award_skips` counts them once.

**Multi-city (an open jaw, or three or more slices).** Two or more `--slice`
that are not a round trip (`cli._one_way_per_slice`; two slices are a round
trip by `links.is_inverse_pair`) go to Matrix, which prices one ticket. On
`--backend auto`, one cabin, a table, `--format envelope` or `--split`,
`search` also asks each slice's one-way board alone (the fetch `--split` uses,
`cli._one_way_boards`: priced one-ticket rows, no price cap, the rows over a
stop ceiling counted on stderr) and `_open_jaw.combine` combines them: every
combination flyable in order, sorted by total in cents, ties in board order
(first board first), at most `-n`, none over `--max-price`. Flyable: from the
airport a ticket lands at, the next leaves after it lands (one local clock);
from another airport, on a later local day, because rows carry no UTC offset
and the ground transfer is unknown. No minimum connection is imposed: the `†`
key names the risk. A row priced in another currency is never summed.

`combine` never builds the boards' product (three 300-row boards: 27,000,000
combinations). A backward pass gives each row the cheapest flyable run from it
to the last board, which bounds every combination through it; a depth-first
walk in price order stops a board at its first row whose fare passes the
`-n`-th cheapest found, and checks the bound before it asks `flyable`. Three
300-row boards whose cheapest middle tickets are unflyable cost 61,234
`flyable` calls (`tests/test_open_jaw.py`); a hypothesis property holds it to
a brute-force product over 2-4 boards.

The envelope asks exactly what the table asks and records the combinations
as `split_ticket`, so the two hold the same tickets; `--format json` keeps
Matrix's own body and asks Google only with `--split`, saying so on stderr. The
`Using Matrix:` line above the table names "a multi-city itinerary on one
ticket": Google sells the trip only as the tickets below it.

`--depart-times`/`--return-times` beside a `--slice` are refused with exit 2
before any request (`cli._refuse_date_option_conflicts`, called by `search` and
by the deprecated `fare`): a slice takes no time window, on Matrix or on Google.

Google is not asked when a slice's own codes, flex or arrival date are ones
the page can't serve as a one-way (`_google_reasons` on the slice alone; a
top-level `--routing`/`--extension` is the default code of every slice with no
`r=`/`e=` of its own, `cli._slice_legs`, so it is judged with each slice and
each one-way is asked with it), when `--cabin` names several cabins (each
ticket is priced in one), or under `--no-separate-tickets`
(`cli._open_jaw_blocker`). The envelope records that reason as `{error}`, which
narrows `complete` only under `--split`. A slice board Google served no row at
all for a party with an infant is `_NoInfantRows`: Google has done that on
routes with flights (see `_run_gflight_path`), so it narrows the run instead of
reading as a slice with no fare. A `--split` round trip's outbound or return
board does the same, narrowing Google's answer alone (`of="gflight"`); its
`No split tickets:` line names the board and offers no Matrix remedy, since
Google's round-trip answer is the one shown. A failed or stopped board's reason
is `_OneWaysUnpriced`, typed apart from the boards' own answers.

**`--backend gflight`** on the same trip answers with the combinations alone
and asks Matrix nothing (`cli._answer_multi_city_on_google`): `_pick_backend`
keeps it on Google when "a multi-city itinerary on one ticket" is its only
reason. The table is the separate-tickets table; `--format json` is
`{"search": [], "split_ticket": …}`, `--split` or not; the envelope has
`backend: gflight`, one empty `results` entry with a note saying the tickets
are in `split_ticket`, and no `currency`. A failed or stopped board exits 1.
Every flag that acts on a row on one ticket (`--awards-only`, `--sellers`,
`--enrich`, `--bags`, `--exclude-basic`, an arrival window, `--pick`) and every
reason Google would not be asked is refused before any request
(`cli._gflight_multi_city_blocker`). With awards on, a table or envelope says
no award search runs; `--format json`, which an award search would replace,
is refused for `--cash-only`. A `--slice` round trip stays refused.

**Measured 2026-10-05, JFK-LHR 2026-10-20 + CDG-JFK 2026-10-27, one adult.**
Matrix: 39 solutions from USD812. Google one-ways: 97 and 113 priced rows,
10,961 flyable pairs, cheapest USD915 = 295 + 620 (via BEG, +2d). So the
separate tickets cost more than Matrix's one ticket here; the table is there
for the routes where they do not, and its `†` key says what the traveler gives
up.

**Measured 2026-10-06, SFO-ORD 2026-11-05 + ORD-BOS 11-08 + BOS-SFO 11-12, one
adult.** Matrix: 378 solutions from USD461 on one ticket. Google one-ways: 163,
109 and 142 priced rows in 4.9 s, cheapest combination USD425.00 = 116 + 158 +
151 (UA2847, UA2071, WN1537/WN2104). Here the separate tickets are the cheaper
answer.
