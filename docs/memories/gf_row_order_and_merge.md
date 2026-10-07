# GF `-n` trim, price order and the merged table: `--pick`, pinned Google links on stated dates, the multi-cabin join key, ranking across currencies

How a Google answer is trimmed, ordered and merged with Matrix's: `-n` applied
on the way out, a pick against the printed table, when the Google link may pin
a Matrix row, every row once in the merged table, price-ascending order, the
multi-cabin itinerary key, and ranking two currencies. Read before touching
`cli._run_gflight_path`, `cli._price_ordered`, `cli._pick_in_range`,
`cli._emit_urls`, `links.extract_pin_segments_from_slice`,
`_enrich.merge_results` / `_date_lender`, or `_multi_cabin.itinerary_key` /
`price_rank`.

**`-n` is one number, applied on the way out.** The page serves Google's whole
board — around a hundred rows (the page always sends `tfu=`) —
whatever count is asked of it, so the count is a trim rather than a query
parameter, and it keeps the cheapest rows (the order is set out below). It
bounds everything the user can act on, and all of it from one place
in `cli._run_gflight_path`: the table, the `--format json` document, the range
`--pick` accepts and the itinerary the `--matrix-url` / `--google-url` lines pin,
and the itineraries the award providers are fanned out over (the first `-n`
one-ticket rows, when a row on separate tickets is among them). All five hold on
`--fast` and on `--format json`, which are the same function — and under
`--format json` the count still bounds the document, while no link line is
printed at all. Multi-cabin keeps three of them — the table, the document and
the award fan-out — and has neither of the other two:
`cli._run_gflight_path_multi` has no `_emit_urls` call site at all, and neither
multi path is passed `pick`, which the `search` command accepts and drops there.
The dated measurement is under "What the page costs us" in [gf_search_transport.md](gf_search_transport.md).

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
merged table afterwards in `_render_merged`. The multi-cabin `--format json` and
envelope arms trim per cabin for the same reason, to the user's count and not the
bumped one, then add each other row of the cabin's board whose fare the table
prints (`cli._cabin_document_rows`), so every fare the table shows is a row of
its cabin in the document.

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
asked in one currency, every page of it when a leg takes several, so
`cli._price_ordered` keeps bare amounts.
