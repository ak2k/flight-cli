# GF airport sets over several pages: legs over 11 airports, merged boards, paged round-trip pins, `calendar --fast -d` ranges, `search --split`

One Google answer built from several page loads: a leg over 11 airports split
into pages, how the pages' rows merge, what a paged round trip costs, a refused
page, one price graph per trip length, and `search --split`'s two one-way
tickets. Read before touching `_metro.gf_leg_pages` / `gf_pages_refusal`,
`cli._merged_boards`, `cli._union_pins`, `cli._PageAsk`,
`_gf_calgraph.page_budget_blocker`, or `cli._graph_range_document`.

## One answer from several Google pages

**A search leg over 11 airports is asked as several pages.** One Google page
takes at most `MAX_GF_LEG_AIRPORTS` = 11 airports a leg, origins plus
destinations, a metro code counted as its members: on 2026-10-01 it refused one
leg of 12 (no readable `ds:1`). A single-cabin search over that bound is divided
into pages by `_metro.gf_leg_pages`. Origins go into `a` contiguous groups and
destinations into `b`, and page (i, j) asks origin group i against destination
group j, so every (origin, destination) pair is on exactly one page. `a * b` is
minimized with each page at most 11 airports, a tie goes to fewer groups on the
side with more airports, group sizes are as even as possible (the first groups
one longer) and the typed order is kept. East coast to Europe,
`JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL` to
`LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB` (8 + 13 = 21 airports), is
4 pages, 2 x 2 (4 + 7 and 4 + 6 airports); a 12-airport origin list to one
airport is 2 pages of 6 origins. The bound is `MAX_GF_PAGES` = 8: past it, or
with an airport at both ends, `gf_pages_refusal` names why (`30 airports on one
leg (more than 8 pages of at most 11)`), `auto` goes to Matrix with that reason
and `--backend gflight` refuses. Three surfaces keep the one-page bound
(`gf_leg_refusal`): a multi-cabin search, whose cabins all pin the sort cabin's
outbounds from one page; the calendar's price graph, which is one page; and the
pinned Google link, which falls back to the itinerary's own airports. Measured
2026-10-01, that east-coast-to-Europe one-way as four pages: 1.5-2.6 s a page,
300, 300, 300 and 189 rows, no row on two pages or outside its own page's
airports. A page tops out near 300 rows, so more pages also return more rows.

**The pages' rows are merged by the whole itinerary.** `cli._merged_boards`
keys a row by `_gflight_ids.row_key`: every leg's carrier, flight number and
departure time, and for a round-trip combination its members' keys in slice
order. A row on two pages is kept once, at the cheaper listing, in the place
the first listing took. The merged board then takes the usual price order and
`-n` trim. It carries no price insight, since each page's insight describes
its own airports. The key also holds how Google sells each member
(`ticketing`), so a row on separate tickets stays beside the one-ticket row on
its flights, as on one page.

**What a paged leg costs.** A one-way is one GET a page. A round trip fetches
every page's outbound page first, then pins the min(n, 10) cheapest outbounds
kept across ALL pages (`cli._union_pins`), each on its own page: an outbound
two pages list is pinned once, on the page that priced it lower, and a page
that holds no pin asks no return. Under a `+CABIN` an outbound is ranked by
its cheapest listing booked in that cabin (`others`), the one a page pins. So a
paged round trip is pages + min(n, kept outbounds, 10) GETs, at most 14 for the
four east-coast-to-Europe pages at the default `-n 10`. A search that reads
the Cheapest tab adds one GET a page: each page reads its own, after its pins,
one that holds no pin included (`_page_board` with `top_n` 0, whose insight is
then its kept outbounds'). Each return is priced within its own page's airports: out
of JFK and back into IAD, an origin of another page, is not asked. A paged
round trip that answers prints one dim stderr line saying so.

**A refused page is named and the other pages still answer.** `cli._PageAsk`
asks the pages in order. A page Google refuses (a 503, a page-shape change,
consent) is named on stderr with its number and airports, `Google Flights page
2 of 4 (JFK,LGA,EWR,BOS→FCO,MUC,ZRH,VIE,CPH,DUB) is missing: <reason>.`, and
the next page is asked. A throttle, a spent transport ladder or a dead browser
is not a fact about one page, as in the pin loop above, so it stops the asking,
and each page after it is named `not asked after page N stopped the search`.
One met by a page's Cheapest tab stops it too: that page's board answered, so
no page line names it, and the separate-ticket line says why. A
round trip asks every page's outbounds before any page's returns, so a page
whose outbounds answered before the stop is named `its returns were not asked
after page N stopped the search`, and no GET follows a throttle.
When nothing merged and a page failed, the first failed page's error is raised
and takes the route a one-page refusal takes ("A Google query that FAILS",
above). Otherwise the answer is the pages that answered and the JSON list keeps
its shape, as "A partial round trip is a success, deliberately" sets out: the
account of the missing pages is stderr. `dropped`, the rows the filter removed,
is summed over every page, its outbounds pinned or not and its returns, so a
board that every page's filter emptied is still handed to Matrix under `auto`.
The board is `partial` when a page is missing or the trip is round, and the
cross-check (`--enrich`) then names no carrier absent from Google: a missing
page's flights are not on the board, and a return into another page's airports
was never asked. `unread`, the rows a page served that the parser could not
read, is summed as `dropped` is, an outbound page that holds no pin included.
So are the rows of a page missing because none of them parsed: Google served
them. Where a board is partial and counts unread rows, a Matrix-only row says
`google_unread`, not the `not_on_google` the partial board alone leaves, as it
would on one page: its trip may be one of the unread rows.
The rows over the stop ceiling are summed as `dropped` is, each counted once,
for the one stderr line the merged board prints. So is `separate_hidden`, and a
page's marked rows are filtered and counted into `dropped` as on one page.
`separate_failed` is the first page's, in page order, for the one line that
says the Cheapest tab went unread; a page holding no pin that the search did
not reach after a stop takes the stop's error, since no page line names it.
Under `--gf-transport browser` one Chrome serves every page
(`cli._browser_scope`).
The pin loop and "A partial round trip is a success, deliberately" are in
[gf_request_budget.md](gf_request_budget.md); "A Google query that FAILS" is in
[gf_search_transport.md](gf_search_transport.md).

**`calendar --fast -d 5-7` asks one price graph per trip length.** On the
browser graph only, a range is admitted when its graphs fit the 8-load budget,
counted before any load at ⌈window days / 31⌉ loads a length
(`_gf_calgraph.page_budget_blocker`); over it `--fast` refuses with the count,
`a window and trip-length range needing 12 price-graph loads (at most 8)`.
`price_graphs` asks each length with the loads the lengths before it left, as
the calendar beside Matrix does. The table has one column per length.
`--format json` writes `{"origin", "destination", "currency", "trip_lengths":
[5, 6, 7], "graphs": [...], "lost": [{"trip_length": n, "reason": text}]}`
(`cli._graph_range_document`): `graphs` holds the single-length document of
each length that priced, and `lost` each length that did not, with its reason.
A lost length is also a stderr line, `Google Flights price graph not shown:
6-night trips: <reason>`. No length priced names every length that way, then the
no-grid line, and exits 1.
`--gf-transport http` still refuses a range, and `-d 7` and a one-way are
unchanged.

**`search --split` prices two one-way tickets beside a round trip.** On a Google
Flights round trip, after the answer, it asks the one-ways each way: 2 more page
loads, 2 per page on a leg asked as several pages, with `--max-price` not
applied to them. The pair is the cheapest whose return leaves the airport the
outbound lands at, after it lands; each board is asked alone, so on a same-day
or overnight trip, or a leg of several destinations, the cheapest each way can
be a pair no one can fly. Both one-ways are one ticket each: the one-way
searches read no Cheapest tab, and a one-way Google marks as separate tickets,
already more than one booking, is passed over; with no other priced one-way
that way the reason is `Google Flights priced no outbound one-way on one
ticket`. The table gets one line after the round-trip table,
starting `Two one-way tickets:`, with each one-way's price and flights and the
total, labeled as two separate tickets; the round-trip rows are unchanged.
`--format json` writes `{"search": <the usual document>, "split_ticket":
{"outbound": row, "return": row, "total": n, "currency": c}}`, or
`"split_ticket": {"error": text}` when the one-ways could not be priced or no
return leaves where an outbound lands, after it lands. A one-way, `--slice`,
several cabins, `--backend matrix`, `--sellers`, `--verify`, `--awards-only`,
an award JSON document and the `--enrich --format json` cross-check are usage
errors; under `auto`, when Matrix answers, one stderr line says the split was
not priced. Measured 2026-10-01, JFK-LAX 10-20/10-27: round trip from USD412,
one-ways 229 + 184 = 413.
