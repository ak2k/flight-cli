# GF request budget: page GETs per search, the round-trip pin cap and which outbounds are pinned, multi-cabin pin sharing, partial round trips

What a Google Flights search costs in page GETs and which outbounds a round
trip pins: the GETs table, `_PINNED_FANOUT_CAP`, the sort cabin leading every
cabin's pins, the pin loop's one stop rule, what a round-trip row's price
means, why a partial round trip exits 0, the three Matrix failures typed
rather than raised as a traceback, and `log.py`'s per-write stream. Read
before touching `_gflight_ids.search_with_ids` / `_pins` / `pin_keys`,
`cli._CabinSearches`, `cli._multi_cabin_join_note`, or `cli._pin_cap_note`.

## Request budget

Every one of these is a multi-megabyte page GET, so the count is the cost:

| query | GETs |
|---|---|
| one-way | 1 |
| round trip | 1 + min(top_n, rows on the board, `_PINNED_FANOUT_CAP` = 10) |
| a one-way leg over 11 airports | one per page, at most `MAX_GF_PAGES` = 8 |
| a round trip over 11 airports a leg | pages + min(top_n, outbounds kept across every page, 10) |
| `search --split` | 2 more (one each way), 2 per page on a leg asked as several |
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
board's cheapest round trip unless a return filter removed it, Google served its
return board empty, or its return board was refused (refused outright, or
answered for a different segment than the pin; `_report_pin_outcome` counts both
refusals in its stderr warning and no line counts an empty board). Then row 1 is
the cheapest trip through the pins whose return boards kept a row.

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
